import discord
import asyncio
import os
import random
import threading
import time
import re
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request, session, redirect

SECRET_KEY     = os.getenv("FLASK_SECRET", os.urandom(24).hex())
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
DAILY_TARGET   = 85000
OWO_BOT_ID     = 408785106942164992   # official OWO bot user ID

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ─── Shared state ──────────────────────────────────────────────────────────────
state = {
    "status":             "waiting",
    "logged_in_as":       "",
    "channel_name":       "",
    "channel_id":         "",
    "token":              os.getenv("DISCORD_TOKEN", ""),

    # Real tracked cowony (from OWO bot responses)
    "real_cowony":        0,
    "real_events":        [],       # last N real-money events

    # Command counters
    "total_commands":     0,
    "command_stats":      {},

    # Live status
    "start_time":         None,
    "last_error":         "",
    "last_command":       "",
    "last_command_time":  None,
    "paused":             False,
    "pause_reason":       "",

    # Smart features
    "team_ready":         False,
    "lootboxes_opened":   0,
    "quests_completed":   0,
    "animals_caught":     0,
    "rate_limits":        {},       # cmd -> resume_at timestamp
    "activity_log":       [],       # last 20 bot actions
}

send_lock      = None
bot_restart_event = threading.Event()

# ─── Commands ──────────────────────────────────────────────────────────────────
# NO fake "earn" values — we track real from OWO responses
# delay = base cooldown in seconds
COMMANDS = [
    {"cmd": "owo battle",        "delay": 62,     "name": "Battle"},
    {"cmd": "owo pray",          "delay": 305,    "name": "Pray"},
    {"cmd": "owo curse",         "delay": 308,    "name": "Curse"},
    {"cmd": "owo work",          "delay": 905,    "name": "Work"},
    {"cmd": "owo crime",         "delay": 1805,   "name": "Crime"},
    {"cmd": "owo hunt",          "delay": 1808,   "name": "Hunt"},
    {"cmd": "owo fish",          "delay": 1812,   "name": "Fish"},
    {"cmd": "owo slots 500",     "delay": 605,    "name": "Slots"},
    {"cmd": "owo coinflip 500",  "delay": 310,    "name": "Coinflip"},
    {"cmd": "owo sell all",      "delay": 7205,   "name": "Sell"},
    {"cmd": "owo checklist",     "delay": 3610,   "name": "Checklist"},
    {"cmd": "owo daily",         "delay": 43205,  "name": "Daily"},
    {"cmd": "owo weekly",        "delay": 604810, "name": "Weekly"},
]

# ─── Activity log helper ───────────────────────────────────────────────────────

def log_activity(msg: str, kind: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
    state["activity_log"].insert(0, entry)
    state["activity_log"] = state["activity_log"][:25]
    print(f"[{entry['time']}] {msg}")

# ─── Real cowony parser from OWO responses ─────────────────────────────────────

def parse_owo_cowony(content: str) -> int:
    """Extract cowony amount from OWO message text."""
    patterns = [
        r'(?:daily|weekly|earned)[^\d]*(\d[\d,]+)\s*(?:cowon|:cowon)',
        r'total of\s*:cowoncy:\s*(\d[\d,]+)',
        r'won\s*:cowoncy:\s*(\d[\d,]+)',
        r'receive[d]?\s*:cowoncy:\s*(\d[\d,]+)',
        r':cowoncy:\s*(\d[\d,]+)\s*cowon',
        r'Here is your\s+\w+\s+:cowoncy:\s*(\d[\d,]+)',
    ]
    for pat in patterns:
        m = re.search(pat, content, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0

def parse_cowony_loss(content: str) -> int:
    """Extract cowony lost from OWO message text."""
    patterns = [
        r'lost\s*:cowoncy:\s*(\d[\d,]+)',
        r'lost it all.*?(\d[\d,]+)',
    ]
    for pat in patterns:
        m = re.search(pat, content, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0

# ─── OWO message handler ───────────────────────────────────────────────────────

async def handle_owo_message(message, channel, client):
    """Parse every OWO bot response and react smartly."""
    content = message.content

    # ── Real cowony tracking ──────────────────────────────────────────────────
    earned = parse_owo_cowony(content)
    if earned > 0:
        state["real_cowony"] += earned
        state["real_events"].insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                         "amount": f"+{earned:,}", "kind": "earn"})
        state["real_events"] = state["real_events"][:30]
        log_activity(f"+{earned:,} cowony earned", "earn")

    loss = parse_cowony_loss(content)
    if loss > 0:
        state["real_cowony"] = max(0, state["real_cowony"] - loss)
        state["real_events"].insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                         "amount": f"-{loss:,}", "kind": "loss"})
        state["real_events"] = state["real_events"][:30]

    # ── Battle team not set up ────────────────────────────────────────────────
    if "do not have an active battle team" in content.lower():
        log_activity("No battle team — auto-creating...", "warn")
        state["team_ready"] = False
        client.loop.create_task(auto_create_team(channel))

    # ── Lootbox found → open it ───────────────────────────────────────────────
    if "you found a lootbox" in content.lower() or "found a lootbox" in content.lower():
        client.loop.create_task(auto_open_lootbox(channel))

    # ── Animal caught → count ──────────────────────────────────────────────────
    if "caught" in content.lower() and ("common" in content.lower() or
        "uncommon" in content.lower() or "rare" in content.lower() or
        "epic" in content.lower() or "legendary" in content.lower()):
        state["animals_caught"] += 1

    # ── Rate limit detected ───────────────────────────────────────────────────
    if "slow down" in content.lower():
        m = re.search(r'in (\d+)\s*(minute|second)', content, re.I)
        if m:
            val  = int(m.group(1))
            unit = m.group(2).lower()
            wait = val * 60 if "minute" in unit else val
            log_activity(f"Rate limited — pausing {wait}s", "warn")

    # ── Quest / checklist completion ──────────────────────────────────────────
    if "quest" in content.lower() and "complet" in content.lower():
        state["quests_completed"] += 1
        log_activity("Quest completed!", "earn")

    # ── Leveled up ────────────────────────────────────────────────────────────
    if "leveled up" in content.lower():
        log_activity("Level up!", "earn")

    # ── Daily reward ──────────────────────────────────────────────────────────
    if "here is your daily" in content.lower():
        streak_m = re.search(r'(\d+)\s+daily streak', content, re.I)
        if streak_m:
            log_activity(f"Daily collected! Streak: {streak_m.group(1)}", "earn")

    # ── Weapon crate ──────────────────────────────────────────────────────────
    if "weapon crate" in content.lower() or "loot crate" in content.lower():
        client.loop.create_task(auto_open_crate(channel))

# ─── Auto battle team creation ─────────────────────────────────────────────────

async def auto_create_team(channel):
    """Fetch user's animals and create a battle team automatically."""
    global send_lock
    await asyncio.sleep(random.uniform(2, 5))
    log_activity("Fetching animals to build team...", "info")

    # Get animals list
    try:
        async with send_lock:
            await channel.send("owo animals")
            await asyncio.sleep(random.uniform(2, 4))
    except Exception as e:
        log_activity(f"Team setup error: {e}", "warn")
        return

    # Wait for OWO to respond with animal list, then parse it
    # We'll handle the response in on_message → parse_team_animal
    state["_waiting_for_animals"] = True

async def parse_and_add_to_team(content: str, channel):
    """Parse animal list response and add first strong animal to team."""
    global send_lock
    # OWO animals response looks like "1. :epic: :lion: Lion" etc.
    # Try to extract animal name
    animal_patterns = [
        r':(?:legendary|epic|rare|uncommon|common):\s*:\w+:\s*(\w+(?:\s\w+)?)',
        r'(\w+)\s*\|\s*(?:Legendary|Epic|Rare|Uncommon|Common)',
        r'(?:legendary|epic|rare|uncommon|common)[^\n]*?(\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)',
    ]
    animal = None
    for pat in animal_patterns:
        m = re.search(pat, content, re.I)
        if m:
            animal = m.group(1).strip().lower()
            break

    if not animal:
        # Fallback: try to find any capitalized word that could be an animal name
        m = re.search(r'\b([A-Z][a-z]{2,})\b', content)
        if m:
            animal = m.group(1).lower()

    if animal:
        log_activity(f"Adding '{animal}' to battle team...", "info")
        try:
            await asyncio.sleep(random.uniform(1.5, 3))
            async with send_lock:
                await channel.send(f"owo team add {animal}")
                await asyncio.sleep(2)
            state["team_ready"]     = True
            state["_waiting_for_animals"] = False
            log_activity(f"Battle team created with {animal}!", "earn")
        except Exception as e:
            log_activity(f"Team add error: {e}", "warn")
    else:
        log_activity("Could not parse animals list for team", "warn")

# ─── Auto lootbox opener ───────────────────────────────────────────────────────

async def auto_open_lootbox(channel):
    global send_lock
    await asyncio.sleep(random.uniform(3, 8))
    log_activity("Lootbox found — opening!", "earn")
    try:
        async with send_lock:
            await channel.send("owo lootbox")
            await asyncio.sleep(2)
        state["lootboxes_opened"] += 1
    except Exception as e:
        log_activity(f"Lootbox open error: {e}", "warn")

async def auto_open_crate(channel):
    global send_lock
    await asyncio.sleep(random.uniform(3, 8))
    log_activity("Weapon crate — opening!", "earn")
    try:
        async with send_lock:
            await channel.send("owo crate")
            await asyncio.sleep(2)
    except Exception as e:
        log_activity(f"Crate open error: {e}", "warn")

# ─── Send command with anti-detect ────────────────────────────────────────────

async def human_typing(channel, cmd_len=10):
    wpm      = random.uniform(48, 85)
    chars    = cmd_len + random.randint(-2, 4)
    duration = min(max((chars / (wpm * 5)) * 60, 0.4), 3.0)
    async with channel.typing():
        await asyncio.sleep(duration)

async def smart_send(channel, cmd: str) -> bool:
    """Send command with anti-detect and respect rate limits."""
    global send_lock
    async with send_lock:
        await human_typing(channel, len(cmd))
        await channel.send(cmd)
        await asyncio.sleep(random.uniform(2.2, 4.5))
    return True

# ─── Per-command farming loop ─────────────────────────────────────────────────

async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]

    # Stagger startup
    await asyncio.sleep(random.uniform(3, 60))

    # Wait for team if this is battle
    if name == "Battle":
        await asyncio.sleep(15)  # give team setup time

    break_counter = 0

    while True:
        # Respect global pause
        while state["paused"]:
            await asyncio.sleep(5)

        # Skip if rate-limited for this specific command
        resume_at = state["rate_limits"].get(name, 0)
        if resume_at > time.time():
            await asyncio.sleep(resume_at - time.time() + 2)
            continue

        # Random skip (human simulation)
        if random.random() < 0.04:
            jitter_skip = random.uniform(0.4, 0.9)
            await asyncio.sleep(delay * jitter_skip)
            continue

        try:
            await smart_send(channel, cmd)
            state["total_commands"]  += 1
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]       = cmd
            state["last_command_time"]  = datetime.now()

        except discord.Forbidden:
            state["last_error"] = "No permission in channel"
            log_activity("Permission denied in channel!", "warn")
            await asyncio.sleep(120)
        except discord.HTTPException as e:
            if e.status == 429:
                retry = float(getattr(e, "retry_after", 15))
                log_activity(f"HTTP rate limit: {retry:.0f}s", "warn")
                await asyncio.sleep(retry + random.uniform(3, 8))
            else:
                state["last_error"] = f"HTTP {e.status}"
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            await asyncio.sleep(10)

        # Cooldown with ±20% jitter
        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        await asyncio.sleep(max(10, delay + jitter))

        # Human break every ~90 min
        break_counter += 1
        if break_counter >= random.randint(85, 135):
            break_counter = 0
            dur = random.randint(90, 320)
            state["paused"]       = True
            state["pause_reason"] = f"Human break ({dur//60}m {dur%60}s)"
            log_activity(f"Taking {dur}s human break", "info")
            await asyncio.sleep(dur)
            state["paused"]       = False
            state["pause_reason"] = ""
            log_activity("Break over, resuming", "info")

# ─── Bot setup on ready ────────────────────────────────────────────────────────

async def initial_setup(channel, client):
    """Run on ready: check team, open any pending lootboxes."""
    await asyncio.sleep(5)
    log_activity("Running initial setup...", "info")

    # Check battle team status
    try:
        async with send_lock:
            await channel.send("owo team")
            await asyncio.sleep(3)
    except Exception:
        pass

# ─── Bot core ──────────────────────────────────────────────────────────────────

def run_bot():
    global send_lock

    while True:
        bot_restart_event.clear()

        token      = state["token"]
        ch_id_str  = CHANNEL_ID_STR

        if not token:
            state["status"] = "waiting"
            bot_restart_event.wait(timeout=5)
            continue

        if not ch_id_str:
            state["status"]     = "no_channel"
            state["last_error"] = "No channel ID set"
            bot_restart_event.wait(timeout=5)
            continue

        try:
            channel_id = int(ch_id_str)
        except ValueError:
            state["status"]     = "error"
            state["last_error"] = "Channel ID must be a number"
            bot_restart_event.wait(timeout=30)
            continue

        state["status"]     = "connecting"
        state["last_error"] = ""

        client = discord.Client()

        @client.event
        async def on_ready():
            global send_lock
            send_lock = asyncio.Lock()

            state["logged_in_as"]     = str(client.user)
            state["status"]           = "online"
            state["start_time"]       = datetime.now()
            state["last_error"]       = ""
            state["command_stats"]    = {}
            state["real_cowony"]      = 0
            state["real_events"]      = []
            state["total_commands"]   = 0
            state["lootboxes_opened"] = 0
            state["quests_completed"] = 0
            state["animals_caught"]   = 0
            state["activity_log"]     = []
            state["team_ready"]       = False
            state["_waiting_for_animals"] = False

            log_activity(f"Logged in as {client.user}", "info")

            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except Exception:
                    pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found — update Channel ID"
                log_activity(f"Channel {channel_id} not found!", "warn")
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            log_activity(f"Farming in #{state['channel_name']}", "info")

            # Initial setup (team check etc.)
            client.loop.create_task(initial_setup(channel, client))

            # Start all farming loops
            for cmd_info in COMMANDS:
                client.loop.create_task(farm_command(channel, cmd_info))

            # Watch for restart signal
            async def watch_restart():
                while not bot_restart_event.is_set():
                    await asyncio.sleep(2)
                await client.close()
            client.loop.create_task(watch_restart())

        @client.event
        async def on_message(message):
            # Only care about OWO bot messages in our channel
            if not state.get("channel_id"):
                return
            if message.channel.id != channel_id:
                return

            is_owo = (message.author.id == OWO_BOT_ID or
                      message.author.name.lower() in ("owo", "owospace"))
            if not is_owo:
                return

            content = message.content

            # Check if waiting for animal list to create team
            if state.get("_waiting_for_animals"):
                await parse_and_add_to_team(content, message.channel)

            # Handle all OWO responses
            await handle_owo_message(message, message.channel, client)

        @client.event
        async def on_disconnect():
            state["status"] = "reconnecting"
            log_activity("Disconnected — reconnecting...", "warn")

        try:
            client.run(token, reconnect=True)
        except discord.LoginFailure:
            state["status"]     = "error"
            state["last_error"] = "Invalid token — please login again"
            state["token"]      = ""
            log_activity("Invalid token!", "warn")
            bot_restart_event.wait(timeout=5)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            log_activity(f"Bot crashed: {e}", "warn")
            bot_restart_event.wait(timeout=8)

        time.sleep(3)

# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if not state["token"]:
        return redirect("/login")
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    data     = request.get_json()
    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    token, ticket, err = discord_login(email, password)
    if token:
        state["token"] = token
        bot_restart_event.set()
        return jsonify({"success": True})
    if err == "2fa":
        session["mfa_ticket"] = ticket
        return jsonify({"mfa": True})
    return jsonify({"error": err or "Login failed"}), 401

@app.route("/api/mfa", methods=["POST"])
def api_mfa():
    data   = request.get_json()
    code   = (data.get("code") or "").strip()
    ticket = session.get("mfa_ticket")
    if not ticket:
        return jsonify({"error": "Session expired, please login again"}), 400
    token, err = discord_mfa(ticket, code)
    if token:
        state["token"] = token
        session.pop("mfa_ticket", None)
        bot_restart_event.set()
        return jsonify({"success": True})
    return jsonify({"error": err or "2FA failed"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    state["token"]       = ""
    state["status"]      = "waiting"
    state["logged_in_as"] = ""
    bot_restart_event.set()
    return jsonify({"success": True})

@app.route("/api/set_channel", methods=["POST"])
def api_set_channel():
    global CHANNEL_ID_STR
    data = request.get_json()
    ch   = str(data.get("channel_id") or "").strip()
    if not ch.isdigit():
        return jsonify({"error": "Channel ID must be a number"}), 400
    CHANNEL_ID_STR = ch
    bot_restart_event.set()
    return jsonify({"success": True})

@app.route("/api/stats")
def stats():
    d = dict(state)
    d["daily_target"] = DAILY_TARGET
    d["channel_id_set"] = bool(CHANNEL_ID_STR)
    if state["start_time"]:
        elapsed = (datetime.now() - state["start_time"]).total_seconds()
        d["uptime_seconds"] = int(elapsed)
        hours = max(elapsed / 3600, 0.001)
        d["daily_rate"] = int(state["real_cowony"] / hours * 24)
        d["start_time"] = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        d["uptime_seconds"] = 0
        d["daily_rate"]     = 0
        d["start_time"]     = None
    if state["last_command_time"]:
        d["last_command_time"] = state["last_command_time"].strftime("%H:%M:%S")
    else:
        d["last_command_time"] = "—"
    d.pop("token", None)
    d.pop("_waiting_for_animals", None)
    return jsonify(d)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Discord login helpers ─────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v9"
LOGIN_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

def discord_login(email, password):
    try:
        r = requests.post(f"{DISCORD_API}/auth/login", headers=LOGIN_HEADERS,
                          json={"login": email, "password": password,
                                "undelete": False, "captcha_key": None}, timeout=15)
        d = r.json()
        if "token" in d:   return d["token"], None, None
        if d.get("mfa"):   return None, d.get("ticket"), "2fa"
        return None, None, d.get("message") or str(d.get("errors", "Login failed"))
    except Exception as e:
        return None, None, str(e)

def discord_mfa(ticket, code):
    try:
        r = requests.post(f"{DISCORD_API}/auth/mfa/totp", headers=LOGIN_HEADERS,
                          json={"code": code.replace(" ", ""), "ticket": ticket}, timeout=15)
        d = r.json()
        if "token" in d: return d["token"], None
        return None, d.get("message") or "Invalid 2FA code"
    except Exception as e:
        return None, str(e)

# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
