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
OWO_BOT_ID     = 408785106942164992

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ─── State ────────────────────────────────────────────────────────────────────
state = {
    "status":             "waiting",
    "logged_in_as":       "",
    "channel_name":       "",
    "channel_id":         "",
    "token":              os.getenv("DISCORD_TOKEN", ""),
    "real_cowony":        0,
    "real_events":        [],
    "battle_streak":      0,
    "battles_won":        0,
    "total_xp":           0,
    "team_animals":       [],
    "team_ready":         False,
    "total_commands":     0,
    "command_stats":      {},
    "start_time":         None,
    "last_error":         "",
    "last_command":       "",
    "last_command_time":  None,
    "paused":             False,
    "pause_reason":       "",
    "lootboxes_opened":   0,
    "quests_completed":   0,
    "animals_caught":     0,
    "activity_log":       [],
    # Internal
    "_cmd_cooldowns":     {},   # name -> next_allowed_at timestamp
    "_team_creating":     False,
    "_team_try_idx":      0,
}

send_lock         = None
last_sent_at      = 0.0
MIN_GAP           = 4.5       # minimum seconds between any two commands
bot_restart_event = threading.Event()

# ─── Animals to try for team creation ────────────────────────────────────────
# Common/easily caught animals FIRST (user screenshots show: chipmunk, cat2)
TEAM_ANIMALS_TO_TRY = [
    # Most likely to be caught via hunt/fish (common & uncommon)
    "chipmunk","cat","dog","rabbit","mouse","bee","butterfly","snail",
    "beetle","bug","rooster","sheep","chick","duck","frog","snake",
    "pig","cow","horse","hamster","parrot","otter","penguin","panda",
    # Rare/epic animals
    "fox","bear","wolf","deer","shrimp","lion","tiger","monkey","eagle",
    "owl","peacock","flamingo","toucan","camel","crocodile","shark",
    "whale","dolphin","turtle","elephant","giraffe","zebra","gorilla",
    # Golden variants (unlikely unless specifically hunted)
    "gfox","gshrimp","gdeer","gcamel","gcat","gdog","gbear",
]

# ─── Commands with cooldowns ──────────────────────────────────────────────────
# Each command runs on its own timer managed by the global scheduler
COMMANDS = [
    {"cmd": "owo hunt",          "delay": 65,     "name": "Hunt",      "priority": 1},
    {"cmd": "owo battle",        "delay": 68,     "name": "Battle",    "priority": 1},
    {"cmd": "owo fish",          "delay": 72,     "name": "Fish",      "priority": 2},
    {"cmd": "owo pray",          "delay": 305,    "name": "Pray",      "priority": 3},
    {"cmd": "owo curse",         "delay": 310,    "name": "Curse",     "priority": 3},
    {"cmd": "owo work",          "delay": 910,    "name": "Work",      "priority": 4},
    {"cmd": "owo crime",         "delay": 1810,   "name": "Crime",     "priority": 4},
    {"cmd": "owo slots 500",     "delay": 610,    "name": "Slots",     "priority": 5},
    {"cmd": "owo coinflip 500",  "delay": 315,    "name": "Coinflip",  "priority": 5},
    {"cmd": "owo sell all",      "delay": 7210,   "name": "Sell",      "priority": 6},
    {"cmd": "owo checklist",     "delay": 3615,   "name": "Checklist", "priority": 6},
    {"cmd": "owo daily",         "delay": 43210,  "name": "Daily",     "priority": 7},
    {"cmd": "owo weekly",        "delay": 604815, "name": "Weekly",    "priority": 8},
]

# ─── Logging ──────────────────────────────────────────────────────────────────
def log_activity(msg: str, kind: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
    state["activity_log"].insert(0, entry)
    state["activity_log"] = state["activity_log"][:30]
    print(f"[{entry['time']}] {msg}")

# ─── Parsers ──────────────────────────────────────────────────────────────────
def parse_cowony_earned(text: str) -> int:
    patterns = [
        r'daily\s*:cowoncy:\s*(\d[\d,]+)',
        r'weekly\s*:cowoncy:\s*(\d[\d,]+)',
        r'total of\s*:cowoncy:\s*(\d[\d,]+)',
        r'won\s*:cowoncy:\s*(\d[\d,]+)',
        r'earn[ed]*\s*:cowoncy:\s*(\d[\d,]+)',
        r'Here is your\s+\w+\s+:cowoncy:\s*(\d[\d,]+)',
        r'\+\s*:cowoncy:\s*(\d[\d,]+)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0

def parse_xp(text: str) -> int:
    m = re.search(r'\+\s*([\d,]+)\s*xp', text, re.I)
    return int(m.group(1).replace(",", "")) if m else 0

def parse_streak(text: str) -> int:
    m = re.search(r'Streak:\s*(\d+)', text, re.I)
    return int(m.group(1)) if m else -1

def parse_turns(text: str) -> int:
    m = re.search(r'won in (\d+) turn', text, re.I)
    return int(m.group(1)) if m else 0

def get_full_text(message) -> str:
    """Get all text from a message including embeds."""
    parts = [message.content or ""]
    for emb in message.embeds:
        if emb.title:       parts.append(emb.title)
        if emb.description: parts.append(emb.description)
        if emb.footer and emb.footer.text: parts.append(emb.footer.text)
        for f in emb.fields:
            parts.append(f"{f.name} {f.value}")
    return "\n".join(parts)

# ─── Anti-detect send (GLOBAL RATE-AWARE) ────────────────────────────────────
async def smart_send(channel, cmd: str, skip_gap=False):
    """Send with typing sim + guaranteed minimum gap between all commands."""
    global send_lock, last_sent_at

    async with send_lock:
        if not skip_gap:
            now = time.time()
            gap = now - last_sent_at
            if gap < MIN_GAP:
                await asyncio.sleep(MIN_GAP - gap + random.uniform(0.8, 2.0))

        # Simulate typing
        wpm      = random.uniform(50, 85)
        chars    = len(cmd) + random.randint(-2, 4)
        duration = min(max((chars / (wpm * 5)) * 60, 0.5), 2.5)
        async with channel.typing():
            await asyncio.sleep(duration)

        await channel.send(cmd)
        last_sent_at = time.time()
        await asyncio.sleep(random.uniform(1.5, 2.5))

# ─── Team creation: try animal names one by one ───────────────────────────────
async def try_create_team(channel):
    """Try animal names one by one until team has at least 1 animal (up to 3)."""
    if state["_team_creating"]:
        return
    state["_team_creating"] = True
    state["team_ready"]     = False
    log_activity("Auto-creating battle team...", "info")

    slots_added = 0
    attempts    = 0

    for animal in TEAM_ANIMALS_TO_TRY:
        if slots_added >= 3:
            break
        if attempts >= 40:
            log_activity("Tried 40 animals — will retry after catching more", "warn")
            break

        attempts += 1
        # Reset flag before each attempt so we can detect next success
        state["_last_team_add_success"] = False

        log_activity(f"Team slot {slots_added+1}: trying '{animal}'", "info")
        try:
            await smart_send(channel, f"owo team add {animal}")
            await asyncio.sleep(random.uniform(3.5, 5.5))   # wait for OWO reply
        except Exception as e:
            log_activity(f"Team add error: {e}", "warn")
            await asyncio.sleep(3)
            continue

        if state.get("_last_team_add_success"):
            slots_added += 1
            log_activity(f"'{animal}' added! Team: {slots_added}/3 slots", "earn")
            state["team_ready"] = True
            await asyncio.sleep(random.uniform(2, 4))
        # else: OWO said "don't own" or no response — silently try next animal

    state["_team_creating"] = False

    if state["team_ready"]:
        log_activity(f"Battle team ready! ({slots_added} animal(s))", "earn")
    else:
        log_activity("No animals owned yet — hunt/fish to catch some first!", "warn")

# ─── OWO response handler ─────────────────────────────────────────────────────
async def handle_owo_message(message, channel, client):
    full = get_full_text(message)
    low  = full.lower()

    # ── Rate limit — mark command as rate-limited ─────────────────────────────
    if "slow down" in low:
        m = re.search(r'in (\d+)\s*(minute|second)', full, re.I)
        wait = 0
        if m:
            val  = int(m.group(1))
            wait = val * 60 if "minute" in m.group(2).lower() else val
        # Back off a bit more than OWO says
        extra = wait + random.randint(8, 20)
        log_activity(f"Rate limited — backing off {extra}s", "warn")
        # Record global cooldown to slow everything down
        state["_cmd_cooldowns"]["__global__"] = time.time() + extra

    # ── Real cowony tracking ──────────────────────────────────────────────────
    earned = parse_cowony_earned(full)
    if earned > 0:
        state["real_cowony"] += earned
        state["real_events"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "amount": f"+{earned:,}", "kind": "earn"
        })
        state["real_events"] = state["real_events"][:30]
        log_activity(f"+{earned:,} cowony earned", "earn")

    # ── Battle won ────────────────────────────────────────────────────────────
    if "you won in" in low:
        xp     = parse_xp(full)
        streak = parse_streak(full)
        turns  = parse_turns(full)
        if xp > 0:     state["total_xp"]     += xp
        if streak >= 0: state["battle_streak"] = streak
        state["battles_won"] += 1

        # Parse team animal levels from the battle message
        team_found = []
        for m2 in re.finditer(r'L\.\s*(\d+)\s*:(\w+):', full):
            team_found.append({"level": int(m2.group(1)), "emoji": m2.group(2)})
        if team_found:
            state["team_animals"] = team_found[:3]

        log_activity(f"Battle won! +{xp:,}xp | Streak {state['battle_streak']} | {turns}t", "earn")

    # ── Team add confirmation ─────────────────────────────────────────────────
    if ("added" in low and "team" in low) or ("team" in low and "member" in low):
        state["team_ready"]              = True
        state["_last_team_add_success"]  = True
        log_activity("Animal added to battle team!", "earn")

    # ── Team already full → team is ready ────────────────────────────────────
    if "team is full" in low or "already on your team" in low:
        state["team_ready"]              = True
        state["_last_team_add_success"]  = True   # count as success too
        state["_team_creating"]          = False
        log_activity("Team already full — ready to battle!", "earn")

    # ── Animal not owned / not found → silently skip (try_create_team handles)
    if "do not own" in low or "not found" in low or "doesn't exist" in low:
        state["_last_team_add_success"] = False   # ensure not counted

    # ── No battle team error → auto-create ───────────────────────────────────
    if "do not have an active battle team" in low:
        log_activity("No battle team — creating now...", "warn")
        state["team_ready"] = False
        if not state["_team_creating"]:
            client.loop.create_task(try_create_team(channel))

    # ── Hunt result ───────────────────────────────────────────────────────────
    if "you found" in low or "hunt is empowered" in low:
        xp = parse_xp(full)
        if xp > 0: state["total_xp"] += xp
        state["animals_caught"] += 1
        team_found2 = []
        for m2 in re.finditer(r'L\.\s*(\d+)\s*:(\w+):', full):
            team_found2.append({"level": int(m2.group(1)), "emoji": m2.group(2)})
        if team_found2:
            state["team_animals"] = team_found2[:3]

    # ── Fish result ───────────────────────────────────────────────────────────
    if "caught" in low and ("cowoncy" in low or "common" in low or
        "uncommon" in low or "rare" in low):
        state["animals_caught"] += 1

    # ── Lootbox found ─────────────────────────────────────────────────────────
    if "you found a lootbox" in low:
        client.loop.create_task(auto_open_lootbox(channel))

    # ── Weapon / loot crate ──────────────────────────────────────────────────
    if "weapon crate" in low or "loot crate" in low:
        client.loop.create_task(auto_open_crate(channel))

    # ── Level up ─────────────────────────────────────────────────────────────
    if "leveled up" in low:
        log_activity("Account leveled up!", "earn")

    # ── Daily collected ───────────────────────────────────────────────────────
    if "here is your daily" in low:
        sm = re.search(r'(\d+)\s+daily streak', full, re.I)
        s_txt = f" (streak {sm.group(1)})" if sm else ""
        log_activity(f"Daily collected!{s_txt}", "earn")

    # ── Quest completed ───────────────────────────────────────────────────────
    if "quest" in low and "complet" in low:
        state["quests_completed"] += 1
        log_activity("Quest completed!", "earn")

# ─── Auto open lootbox / crate ────────────────────────────────────────────────
async def auto_open_lootbox(channel):
    await asyncio.sleep(random.uniform(4, 8))
    log_activity("Lootbox found → opening!", "earn")
    try:
        await smart_send(channel, "owo lootbox")
        state["lootboxes_opened"] += 1
    except Exception as e:
        log_activity(f"Lootbox err: {e}", "warn")

async def auto_open_crate(channel):
    await asyncio.sleep(random.uniform(4, 8))
    log_activity("Crate found → opening!", "earn")
    try:
        await smart_send(channel, "owo crate")
    except Exception as e:
        log_activity(f"Crate err: {e}", "warn")

# ─── Per-command farming loop ─────────────────────────────────────────────────
async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    name  = cmd_info["name"]
    delay = cmd_info["delay"]

    # Stagger startup — spread commands out over first 3 minutes
    idx   = COMMANDS.index(cmd_info)
    await asyncio.sleep(random.uniform(idx * 8 + 5, idx * 12 + 30))

    # Battle waits a bit more for team setup
    if name == "Battle":
        await asyncio.sleep(35)

    break_counter = 0

    while True:
        # Respect global pause
        while state["paused"]:
            await asyncio.sleep(5)

        # Respect global rate limit backoff
        global_resume = state["_cmd_cooldowns"].get("__global__", 0)
        if global_resume > time.time():
            wait = global_resume - time.time()
            log_activity(f"Global cooldown: waiting {wait:.0f}s", "warn")
            await asyncio.sleep(wait + random.uniform(3, 8))
            continue

        # Respect per-command cooldown
        cmd_resume = state["_cmd_cooldowns"].get(name, 0)
        if cmd_resume > time.time():
            await asyncio.sleep(cmd_resume - time.time() + random.uniform(2, 5))
            continue

        # 4% random human skip
        if random.random() < 0.04:
            await asyncio.sleep(delay * random.uniform(0.5, 0.9))
            continue

        # Send command
        try:
            await smart_send(channel, cmd)
            state["total_commands"] += 1
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]       = cmd
            state["last_command_time"]  = datetime.now()

        except discord.Forbidden:
            state["last_error"] = "No permission in channel"
            log_activity("No permission!", "warn")
            await asyncio.sleep(120)
        except discord.HTTPException as e:
            if e.status == 429:
                retry = float(getattr(e, "retry_after", 15))
                log_activity(f"HTTP 429: wait {retry:.0f}s", "warn")
                state["_cmd_cooldowns"]["__global__"] = time.time() + retry + 10
                await asyncio.sleep(retry + random.uniform(5, 12))
            else:
                state["last_error"] = f"HTTP {e.status}"
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            log_activity(f"Send error: {e}", "warn")
            await asyncio.sleep(10)

        # Wait full cooldown + ±20% jitter before next send
        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        await asyncio.sleep(max(15, delay + jitter))

        # Human break every ~90 min
        break_counter += 1
        if break_counter >= random.randint(85, 140):
            break_counter = 0
            dur = random.randint(90, 320)
            state["paused"]       = True
            state["pause_reason"] = f"Human break {dur//60}m {dur%60}s"
            log_activity(f"Taking {dur}s break", "info")
            await asyncio.sleep(dur)
            state["paused"]       = False
            state["pause_reason"] = ""
            log_activity("Break over, resuming", "info")

# ─── Startup: check team first ────────────────────────────────────────────────
async def startup_routine(channel, client):
    await asyncio.sleep(8)
    log_activity("Startup: checking battle team...", "info")
    try:
        await smart_send(channel, "owo team", skip_gap=True)
        # Wait a few seconds for OWO to respond
        await asyncio.sleep(5)
    except Exception:
        pass

    # If team still not confirmed, start creation
    if not state["team_ready"]:
        log_activity("No team confirmed — auto-creating...", "info")
        await try_create_team(channel)

# ─── Bot ─────────────────────────────────────────────────────────────────────
def run_bot():
    global send_lock, last_sent_at

    while True:
        bot_restart_event.clear()

        token  = state["token"]
        ch_str = CHANNEL_ID_STR

        if not token:
            state["status"] = "waiting"
            bot_restart_event.wait(timeout=5)
            continue
        if not ch_str:
            state["status"]     = "no_channel"
            state["last_error"] = "No channel ID set"
            bot_restart_event.wait(timeout=5)
            continue
        try:
            channel_id = int(ch_str)
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
            global send_lock, last_sent_at
            send_lock    = asyncio.Lock()
            last_sent_at = 0.0

            state.update({
                "logged_in_as": str(client.user), "status": "online",
                "start_time": datetime.now(), "last_error": "",
                "command_stats": {}, "real_cowony": 0, "real_events": [],
                "total_commands": 0, "lootboxes_opened": 0,
                "quests_completed": 0, "animals_caught": 0,
                "activity_log": [], "team_ready": False,
                "battle_streak": 0, "battles_won": 0, "total_xp": 0,
                "team_animals": [], "_cmd_cooldowns": {},
                "_team_creating": False, "_team_try_idx": 0,
                "_last_team_add_success": False,
            })

            log_activity(f"Online as {client.user}", "info")

            channel = client.get_channel(channel_id)
            if channel is None:
                try: channel = await client.fetch_channel(channel_id)
                except Exception: pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found"
                log_activity(f"Channel {channel_id} not found!", "warn")
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            log_activity(f"Farming in #{state['channel_name']}", "info")

            # Run startup check then start all command loops
            client.loop.create_task(startup_routine(channel, client))
            for cmd_info in COMMANDS:
                client.loop.create_task(farm_command(channel, cmd_info))

            async def watch_restart():
                while not bot_restart_event.is_set():
                    await asyncio.sleep(2)
                await client.close()
            client.loop.create_task(watch_restart())

        @client.event
        async def on_message(message):
            if not state.get("channel_id"): return
            if message.channel.id != channel_id: return
            is_owo = (message.author.id == OWO_BOT_ID or
                      message.author.name.lower() in ("owo", "owospace"))
            if not is_owo: return
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
            bot_restart_event.wait(timeout=5)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            log_activity(f"Bot error: {e}", "warn")
            bot_restart_event.wait(timeout=8)
        time.sleep(3)

# ─── Flask ────────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    if not state["token"]: return redirect("/login")
    return render_template("index.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json()
    email, pw = (d.get("email") or "").strip(), d.get("password") or ""
    if not email or not pw: return jsonify({"error": "Email and password required"}), 400
    token, ticket, err = discord_login(email, pw)
    if token:
        state["token"] = token; bot_restart_event.set()
        return jsonify({"success": True})
    if err == "2fa":
        session["mfa_ticket"] = ticket; return jsonify({"mfa": True})
    return jsonify({"error": err or "Login failed"}), 401

@app.route("/api/mfa", methods=["POST"])
def api_mfa():
    d      = request.get_json()
    code   = (d.get("code") or "").strip()
    ticket = session.get("mfa_ticket")
    if not ticket: return jsonify({"error": "Session expired"}), 400
    token, err = discord_mfa(ticket, code)
    if token:
        state["token"] = token; session.pop("mfa_ticket", None)
        bot_restart_event.set(); return jsonify({"success": True})
    return jsonify({"error": err or "2FA failed"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    state["token"] = ""; state["status"] = "waiting"
    state["logged_in_as"] = ""; bot_restart_event.set()
    return jsonify({"success": True})

@app.route("/api/set_channel", methods=["POST"])
def api_set_channel():
    global CHANNEL_ID_STR
    ch = str((request.get_json() or {}).get("channel_id") or "").strip()
    if not ch.isdigit(): return jsonify({"error": "Channel ID must be a number"}), 400
    CHANNEL_ID_STR = ch; bot_restart_event.set()
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
        d["daily_rate"]  = int(state["real_cowony"] / hours * 24)
        d["start_time"]  = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        d["uptime_seconds"] = 0; d["daily_rate"] = 0; d["start_time"] = None
    if state["last_command_time"]:
        d["last_command_time"] = state["last_command_time"].strftime("%H:%M:%S")
    else:
        d["last_command_time"] = "—"
    for k in ("token", "_cmd_cooldowns", "_team_creating", "_team_try_idx"):
        d.pop(k, None)
    return jsonify(d)

@app.route("/ping")
def ping():
    return "PONG", 200

DISCORD_API = "https://discord.com/api/v9"
HEADERS_H   = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def discord_login(email, password):
    try:
        r = requests.post(f"{DISCORD_API}/auth/login", headers=HEADERS_H,
                          json={"login": email, "password": password, "undelete": False, "captcha_key": None}, timeout=15)
        d = r.json()
        if "token" in d: return d["token"], None, None
        if d.get("mfa"):  return None, d.get("ticket"), "2fa"
        return None, None, d.get("message") or str(d.get("errors", "Login failed"))
    except Exception as e:
        return None, None, str(e)

def discord_mfa(ticket, code):
    try:
        r = requests.post(f"{DISCORD_API}/auth/mfa/totp", headers=HEADERS_H,
                          json={"code": code.replace(" ", ""), "ticket": ticket}, timeout=15)
        d = r.json()
        if "token" in d: return d["token"], None
        return None, d.get("message") or "Invalid 2FA code"
    except Exception as e:
        return None, str(e)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
