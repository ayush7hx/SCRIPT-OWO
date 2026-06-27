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
    # Cowony
    "real_cowony":        0,
    "real_events":        [],
    # Battle / XP
    "battle_streak":      0,
    "battles_won":        0,
    "total_xp":           0,
    "team_animals":       [],   # [{"name": "Fox", "level": 30}, ...]
    "team_ready":         False,
    "team_setup_tried":   False,
    # Commands
    "total_commands":     0,
    "command_stats":      {},
    # Session
    "start_time":         None,
    "last_error":         "",
    "last_command":       "",
    "last_command_time":  None,
    "paused":             False,
    "pause_reason":       "",
    # Smart features
    "lootboxes_opened":   0,
    "quests_completed":   0,
    "animals_caught":     0,
    "activity_log":       [],
    "rate_limits":        {},
    # Internals
    "_animals_list":      [],   # parsed from owo animals
    "_waiting_animals":   False,
    "_team_slots_filled": 0,
}

send_lock         = None
bot_restart_event = threading.Event()

# ─── Farming commands ─────────────────────────────────────────────────────────
# hunt + battle are the CORE XP loop (like Diva)
# hunt every ~1min, battle every ~1min for max XP+cowony
COMMANDS = [
    {"cmd": "owo hunt",          "delay": 65,     "name": "Hunt"},
    {"cmd": "owo battle",        "delay": 68,     "name": "Battle"},
    {"cmd": "owo fish",          "delay": 72,     "name": "Fish"},
    {"cmd": "owo pray",          "delay": 305,    "name": "Pray"},
    {"cmd": "owo curse",         "delay": 310,    "name": "Curse"},
    {"cmd": "owo work",          "delay": 910,    "name": "Work"},
    {"cmd": "owo crime",         "delay": 1810,   "name": "Crime"},
    {"cmd": "owo slots 500",     "delay": 610,    "name": "Slots"},
    {"cmd": "owo coinflip 500",  "delay": 315,    "name": "Coinflip"},
    {"cmd": "owo sell all",      "delay": 7210,   "name": "Sell"},
    {"cmd": "owo checklist",     "delay": 3615,   "name": "Checklist"},
    {"cmd": "owo daily",         "delay": 43210,  "name": "Daily"},
    {"cmd": "owo weekly",        "delay": 604815, "name": "Weekly"},
]

# Common OWO animal names to try when creating team (rarity order: best first)
COMMON_ANIMAL_NAMES = [
    "ffox","fcat","fdog","fbear","fwolf","fdeer","fshrimp","fduck",
    "fox","cat","dog","bear","wolf","deer","shrimp","duck","rabbit",
    "hamster","parrot","penguin","otter","panda","lion","tiger",
    "chipmunk","mouse","snail","bee","butterfly","bug","beetle","rooster",
    "chick","sheep","pig","cow","horse","elephant","giraffe","zebra",
]

# ─── Logging ──────────────────────────────────────────────────────────────────
def log_activity(msg: str, kind: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
    state["activity_log"].insert(0, entry)
    state["activity_log"] = state["activity_log"][:30]
    print(f"[{entry['time']}] {msg}")

# ─── OWO response parsers ─────────────────────────────────────────────────────
def parse_cowony_earned(text: str) -> int:
    patterns = [
        r'daily\s*:cowoncy:\s*(\d[\d,]+)',
        r'weekly\s*:cowoncy:\s*(\d[\d,]+)',
        r'total of\s*:cowoncy:\s*(\d[\d,]+)',
        r'won\s*:cowoncy:\s*(\d[\d,]+)',
        r'earn[ed]*\s*:cowoncy:\s*(\d[\d,]+)',
        r'Here is your\s+\w+\s+:cowoncy:\s*(\d[\d,]+)',
        r'\+\s*:cowoncy:\s*(\d[\d,]+)',
        r':cowoncy:\s*(\d[\d,]+)\s*cowon',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return 0

def parse_xp(text: str) -> int:
    m = re.search(r'\+\s*([\d,]+)\s*xp', text, re.I)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0

def parse_streak(text: str) -> int:
    m = re.search(r'Streak:\s*(\d+)', text, re.I)
    if m:
        return int(m.group(1))
    return -1

def parse_turns(text: str) -> int:
    m = re.search(r'won in (\d+) turn', text, re.I)
    if m:
        return int(m.group(1))
    return 0

def parse_animal_level(text: str) -> list:
    """Parse 'L. 30 :gfox: - ...' style lines from battle info."""
    animals = []
    for m in re.finditer(r'L\.\s*(\d+)\s*:(\w+):', text):
        animals.append({"level": int(m.group(1)), "emoji": m.group(2)})
    return animals

def parse_animals_from_text(text: str) -> list:
    """Parse OWO animals list (text or embed) for animal names."""
    names = []
    # Pattern: ":emoji_name: Animal Name" or just look for known capitalized names
    for m in re.finditer(r':(\w+):\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)', text):
        name = m.group(2).strip().lower()
        if name not in names and len(name) > 2:
            names.append(name)
    # Also try bare names after rarity labels
    for m in re.finditer(r'(?:Legendary|Epic|Rare|Uncommon|Common)[^\n]*?([A-Z][a-z]+)', text):
        name = m.group(1).strip().lower()
        if name not in names and len(name) > 2:
            names.append(name)
    return names

def parse_team_from_response(text: str) -> list:
    """Parse 'Diva's Team\nL.30 :gfox:...' style team info."""
    teams = []
    for m in re.finditer(r'L\.\s*(\d+)\s*:(\w+):', text):
        teams.append({"level": int(m.group(1)), "emoji": m.group(2)})
    return teams

# ─── Team management ──────────────────────────────────────────────────────────
async def create_battle_team(channel):
    """Auto-create a 3-animal battle team."""
    global send_lock
    if state["_waiting_animals"] or state["team_ready"]:
        return

    state["_waiting_animals"] = True
    log_activity("Fetching animal list to build team...", "info")

    try:
        await asyncio.sleep(random.uniform(2, 4))
        async with send_lock:
            await channel.send("owo animals")
            await asyncio.sleep(random.uniform(3, 5))
    except Exception as e:
        log_activity(f"Animals fetch error: {e}", "warn")
        state["_waiting_animals"] = False

async def try_add_animals_to_team(channel, names: list):
    """Try adding animals from list to team (up to 3 slots)."""
    global send_lock
    slots_needed = 3 - state["_team_slots_filled"]
    if slots_needed <= 0:
        state["team_ready"] = True
        return

    added = 0
    for name in names[:10]:
        if added >= slots_needed:
            break
        try:
            await asyncio.sleep(random.uniform(1.5, 3))
            async with send_lock:
                await channel.send(f"owo team add {name}")
                await asyncio.sleep(random.uniform(2, 3.5))
            log_activity(f"Added {name} to team (slot {state['_team_slots_filled']+1})", "earn")
            state["_team_slots_filled"] += 1
            added += 1
        except Exception as e:
            log_activity(f"Team add error ({name}): {e}", "warn")

    if state["_team_slots_filled"] >= 1:
        state["team_ready"] = True
        log_activity(f"Battle team ready! ({state['_team_slots_filled']} animals)", "earn")

    state["_waiting_animals"] = False
    state["team_setup_tried"] = True

# ─── OWO message handler ──────────────────────────────────────────────────────
async def handle_owo_message(message, channel, client):
    content = message.content
    # Also check embeds
    embed_text = ""
    for emb in message.embeds:
        if emb.description:
            embed_text += emb.description + "\n"
        for f in emb.fields:
            embed_text += f.name + " " + f.value + "\n"
    full_text = content + "\n" + embed_text

    # ── Real cowony ───────────────────────────────────────────────────────────
    earned = parse_cowony_earned(full_text)
    if earned > 0:
        state["real_cowony"] += earned
        state["real_events"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "amount": f"+{earned:,}", "kind": "earn"
        })
        state["real_events"] = state["real_events"][:30]
        log_activity(f"+{earned:,} cowony earned", "earn")

    # ── Battle result ─────────────────────────────────────────────────────────
    if "you won in" in full_text.lower():
        xp     = parse_xp(full_text)
        streak = parse_streak(full_text)
        turns  = parse_turns(full_text)

        if xp > 0:
            state["total_xp"] += xp
        if streak >= 0:
            state["battle_streak"] = streak
        state["battles_won"] += 1

        # Parse team info from battle response
        team = parse_team_from_response(full_text)
        if team:
            state["team_animals"] = team[:3]

        msg = f"Battle won! +{xp:,}xp | Streak: {state['battle_streak']} | Turns: {turns}"
        log_activity(msg, "earn")

    # ── No battle team error ───────────────────────────────────────────────────
    if "do not have an active battle team" in full_text.lower():
        log_activity("No battle team — auto-creating...", "warn")
        state["team_ready"]   = False
        state["team_setup_tried"] = False
        client.loop.create_task(create_battle_team(channel))

    # ── Animals list response → parse for team building ───────────────────────
    if state["_waiting_animals"]:
        names = parse_animals_from_text(full_text)
        if names:
            state["_animals_list"] = names
            log_activity(f"Found animals: {', '.join(names[:5])}", "info")
            client.loop.create_task(try_add_animals_to_team(channel, names))
        elif state["team_setup_tried"] is False:
            # Fallback: try known common animals
            log_activity("Trying common animal names for team...", "info")
            client.loop.create_task(
                try_add_animals_to_team(channel, COMMON_ANIMAL_NAMES)
            )

    # ── Team confirmed ────────────────────────────────────────────────────────
    if "your team" in full_text.lower() and ("added" in full_text.lower() or
                                               "battle team" in full_text.lower()):
        state["team_ready"] = True
        log_activity("Battle team confirmed!", "earn")

    # ── Hunt result → XP for team ─────────────────────────────────────────────
    if "hunt is empowered" in full_text.lower() or "gained" in full_text.lower():
        xp = parse_xp(full_text)
        if xp > 0:
            state["total_xp"] += xp
        state["animals_caught"] += 1

        # Update team animal levels from hunt XP gain lines
        team = parse_team_from_response(full_text)
        if team:
            state["team_animals"] = team[:3]

    # ── Fish result ────────────────────────────────────────────────────────────
    if "caught" in full_text.lower() and ("common" in full_text.lower() or
        "uncommon" in full_text.lower() or "rare" in full_text.lower()):
        state["animals_caught"] += 1

    # ── Lootbox ───────────────────────────────────────────────────────────────
    if "you found a lootbox" in full_text.lower():
        client.loop.create_task(auto_open_lootbox(channel))

    # ── Weapon crate ──────────────────────────────────────────────────────────
    if "weapon crate" in full_text.lower() or "loot crate" in full_text.lower():
        client.loop.create_task(auto_open_crate(channel))

    # ── Level up ──────────────────────────────────────────────────────────────
    if "leveled up" in full_text.lower():
        log_activity("Account leveled up!", "earn")

    # ── Quest completed ───────────────────────────────────────────────────────
    if "quest" in full_text.lower() and "complet" in full_text.lower():
        state["quests_completed"] += 1
        log_activity("Quest completed!", "earn")

    # ── Rate limit ────────────────────────────────────────────────────────────
    if "slow down" in full_text.lower():
        m = re.search(r'in (\d+)\s*(minute|second)', full_text, re.I)
        if m:
            val  = int(m.group(1))
            wait = val * 60 if "minute" in m.group(2).lower() else val
            log_activity(f"Rate limited — {wait}s", "warn")

    # ── Daily collected ───────────────────────────────────────────────────────
    if "here is your daily" in full_text.lower():
        sm = re.search(r'(\d+)\s+daily streak', full_text, re.I)
        streak_info = f" (streak: {sm.group(1)})" if sm else ""
        log_activity(f"Daily collected!{streak_info}", "earn")

# ─── Auto lootbox / crate ─────────────────────────────────────────────────────
async def auto_open_lootbox(channel):
    global send_lock
    await asyncio.sleep(random.uniform(3, 7))
    log_activity("Lootbox found → opening!", "earn")
    try:
        async with send_lock:
            await channel.send("owo lootbox")
            await asyncio.sleep(2)
        state["lootboxes_opened"] += 1
    except Exception as e:
        log_activity(f"Lootbox error: {e}", "warn")

async def auto_open_crate(channel):
    global send_lock
    await asyncio.sleep(random.uniform(3, 7))
    log_activity("Weapon crate → opening!", "earn")
    try:
        async with send_lock:
            await channel.send("owo crate")
            await asyncio.sleep(2)
    except Exception as e:
        log_activity(f"Crate error: {e}", "warn")

# ─── Anti-detect send ─────────────────────────────────────────────────────────
async def human_typing(channel, length=10):
    wpm      = random.uniform(50, 85)
    chars    = length + random.randint(-2, 5)
    duration = min(max((chars / (wpm * 5)) * 60, 0.4), 2.8)
    async with channel.typing():
        await asyncio.sleep(duration)

async def smart_send(channel, cmd: str):
    global send_lock
    async with send_lock:
        await human_typing(channel, len(cmd))
        await channel.send(cmd)
        await asyncio.sleep(random.uniform(2.0, 4.0))

# ─── Per-command farm loop ────────────────────────────────────────────────────
async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]

    # Stagger so commands don't all fire at second 0
    await asyncio.sleep(random.uniform(3, 55))

    # Battle waits for team to be set up
    if name == "Battle":
        await asyncio.sleep(20)

    break_counter = 0

    while True:
        # Respect pause
        while state["paused"]:
            await asyncio.sleep(5)

        # Respect per-command rate limit
        resume_at = state["rate_limits"].get(name, 0)
        if resume_at > time.time():
            await asyncio.sleep(resume_at - time.time() + 2)
            continue

        # 4% random skip (human simulation)
        if random.random() < 0.04:
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            continue

        try:
            await smart_send(channel, cmd)
            state["total_commands"]  += 1
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
                log_activity(f"Rate limited {retry:.0f}s", "warn")
                await asyncio.sleep(retry + random.uniform(2, 6))
            else:
                state["last_error"] = f"HTTP {e.status}"
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            await asyncio.sleep(10)

        # Cooldown + ±20% jitter
        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        await asyncio.sleep(max(10, delay + jitter))

        # Human break every ~90 min of activity
        break_counter += 1
        if break_counter >= random.randint(85, 140):
            break_counter = 0
            dur = random.randint(90, 320)
            state["paused"]       = True
            state["pause_reason"] = f"Human break ({dur//60}m {dur%60}s)"
            log_activity(f"Taking {dur}s break", "info")
            await asyncio.sleep(dur)
            state["paused"]       = False
            state["pause_reason"] = ""
            log_activity("Break over, resuming", "info")

# ─── Startup routine ──────────────────────────────────────────────────────────
async def startup_routine(channel, client):
    """Check team status and run initial setup."""
    await asyncio.sleep(6)
    log_activity("Running startup — checking team...", "info")
    try:
        async with send_lock:
            await channel.send("owo team")
            await asyncio.sleep(4)
    except Exception:
        pass

# ─── Bot core ─────────────────────────────────────────────────────────────────
def run_bot():
    global send_lock

    while True:
        bot_restart_event.clear()

        token    = state["token"]
        ch_str   = CHANNEL_ID_STR

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
            global send_lock
            send_lock = asyncio.Lock()

            state.update({
                "logged_in_as": str(client.user),
                "status": "online", "start_time": datetime.now(),
                "last_error": "", "command_stats": {},
                "real_cowony": 0, "real_events": [], "total_commands": 0,
                "lootboxes_opened": 0, "quests_completed": 0,
                "animals_caught": 0, "activity_log": [],
                "team_ready": False, "team_setup_tried": False,
                "battle_streak": 0, "battles_won": 0, "total_xp": 0,
                "team_animals": [], "_animals_list": [],
                "_waiting_animals": False, "_team_slots_filled": 0,
            })

            log_activity(f"Online as {client.user}", "info")

            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except Exception:
                    pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found"
                log_activity(f"Channel {channel_id} not found!", "warn")
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            log_activity(f"Farming in #{state['channel_name']}", "info")

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
            if not state.get("channel_id"):
                return
            if message.channel.id != channel_id:
                return
            is_owo = (message.author.id == OWO_BOT_ID or
                      message.author.name.lower() in ("owo", "owospace"))
            if not is_owo:
                return
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
            log_activity(f"Bot error: {e}", "warn")
            bot_restart_event.wait(timeout=8)

        time.sleep(3)

# ─── Flask ────────────────────────────────────────────────────────────────────
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
    data = request.get_json()
    email, password = (data.get("email") or "").strip(), data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    token, ticket, err = discord_login(email, password)
    if token:
        state["token"] = token; bot_restart_event.set()
        return jsonify({"success": True})
    if err == "2fa":
        session["mfa_ticket"] = ticket; return jsonify({"mfa": True})
    return jsonify({"error": err or "Login failed"}), 401

@app.route("/api/mfa", methods=["POST"])
def api_mfa():
    data   = request.get_json()
    code   = (data.get("code") or "").strip()
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
    for k in ("token", "_waiting_animals", "_animals_list", "rate_limits"):
        d.pop(k, None)
    return jsonify(d)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Discord auth helpers ─────────────────────────────────────────────────────
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

# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
