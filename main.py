import discord
import asyncio
import os
import random
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify

TOKEN          = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
DAILY_TARGET   = 85000

app = Flask(__name__)

# ─── Shared state ─────────────────────────────────────────────────────────────
state = {
    "status":            "waiting",
    "logged_in_as":      "",
    "channel_name":      "",
    "channel_id":        "",
    "total_commands":    0,
    "command_stats":     {},
    "cowony_earned":     0,
    "start_time":        None,
    "last_error":        "",
    "last_command":      "",
    "last_command_time": None,
    "anti_detect_mode":  True,
    "paused":            False,
    "pause_reason":      "",
}

# ─── Anti-detection config ────────────────────────────────────────────────────
MIN_GAP_BETWEEN_CMDS = 2.5   # seconds minimum between any two sends
TYPING_BEFORE_SEND   = True  # simulate human typing
RANDOM_SKIP_CHANCE   = 0.04  # 4% chance to "forget" a command (human-like)
HUMAN_BREAK_INTERVAL = 5400  # every ~90 min take a short break
HUMAN_BREAK_DURATION = (120, 400)  # break length: 2–7 min

# Global lock so two tasks never send simultaneously
send_lock = None  # will be set inside the async context

# ─── OWO command list (all money-making commands, optimal cooldowns) ───────────
# earn = estimated cowony per use (for dashboard tracking)
COMMANDS = [
    # ── High-frequency grind ──────────────────────────────────────────────────
    {"cmd": "owo battle",        "delay": 62,    "name": "Battle",    "earn": 55,    "cat": "GRIND"},
    {"cmd": "owo pray",          "delay": 305,   "name": "Pray",      "earn": 65,    "cat": "GRIND"},
    {"cmd": "owo curse",         "delay": 308,   "name": "Curse",     "earn": 65,    "cat": "GRIND"},
    {"cmd": "owo work",          "delay": 905,   "name": "Work",      "earn": 180,   "cat": "GRIND"},

    # ── Medium-frequency ──────────────────────────────────────────────────────
    {"cmd": "owo crime",         "delay": 1805,  "name": "Crime",     "earn": 220,   "cat": "GRIND"},
    {"cmd": "owo hunt",          "delay": 1808,  "name": "Hunt",      "earn": 130,   "cat": "GRIND"},
    {"cmd": "owo fish",          "delay": 1812,  "name": "Fish",      "earn": 130,   "cat": "GRIND"},

    # ── Gambling (nets positive on average with fixed small bets) ─────────────
    {"cmd": "owo slots 500",     "delay": 605,   "name": "Slots",     "earn": 220,   "cat": "GAMBLE"},
    {"cmd": "owo coinflip 500",  "delay": 308,   "name": "Coinflip",  "earn": 220,   "cat": "GAMBLE"},

    # ── Sell animals regularly ────────────────────────────────────────────────
    {"cmd": "owo sell all",      "delay": 7205,  "name": "Sell",      "earn": 600,   "cat": "SELL"},
    {"cmd": "owo sell gems",     "delay": 7210,  "name": "SellGems",  "earn": 300,   "cat": "SELL"},

    # ── Daily checklist ───────────────────────────────────────────────────────
    {"cmd": "owo checklist",     "delay": 3610,  "name": "Checklist", "earn": 50,    "cat": "BONUS"},

    # ── Daily / weekly bonuses ────────────────────────────────────────────────
    {"cmd": "owo daily",         "delay": 43205, "name": "Daily",     "earn": 6000,  "cat": "BONUS"},
    {"cmd": "owo weekly",        "delay": 604810,"name": "Weekly",    "earn": 28000, "cat": "BONUS"},
]

# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/stats")
def stats():
    data = dict(state)
    data["daily_target"] = DAILY_TARGET
    if state["start_time"]:
        elapsed = (datetime.now() - state["start_time"]).total_seconds()
        data["uptime_seconds"] = int(elapsed)
        hours = max(elapsed / 3600, 0.001)
        data["daily_rate"]  = int(state["cowony_earned"] / hours * 24)
        data["start_time"]  = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        data["uptime_seconds"] = 0
        data["daily_rate"]     = 0
        data["start_time"]     = None

    if state["last_command_time"]:
        data["last_command_time"] = state["last_command_time"].strftime("%H:%M:%S")
    else:
        data["last_command_time"] = "—"

    return jsonify(data)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Anti-detection helpers ───────────────────────────────────────────────────

async def human_typing(channel, base_len: int = 10):
    """Simulate typing for a realistic duration before sending."""
    if not TYPING_BEFORE_SEND:
        return
    wpm   = random.uniform(45, 80)
    chars = base_len + random.randint(-3, 5)
    duration = (chars / (wpm * 5)) * 60  # seconds
    duration = min(max(duration, 0.4), 3.5)
    async with channel.typing():
        await asyncio.sleep(duration)

async def human_delay():
    """Small random pause between actions — feels natural."""
    await asyncio.sleep(random.uniform(0.8, 2.8))

async def random_break():
    """Simulate a human taking a short break."""
    duration = random.randint(*HUMAN_BREAK_DURATION)
    state["paused"]       = True
    state["pause_reason"] = f"Short break ({duration//60}m {duration%60}s)"
    print(f"[ANTI-DETECT] Taking a {duration}s human break...")
    await asyncio.sleep(duration)
    state["paused"]       = False
    state["pause_reason"] = ""
    print("[ANTI-DETECT] Break over, resuming farming.")

# ─── Command sender ───────────────────────────────────────────────────────────

async def send_command(channel, cmd: str):
    """Thread-safe, anti-detect message sender."""
    global send_lock
    async with send_lock:
        # Simulate human typing
        await human_typing(channel, len(cmd))
        await channel.send(cmd)
        # Mandatory gap after send so we never spam
        await asyncio.sleep(random.uniform(MIN_GAP_BETWEEN_CMDS, MIN_GAP_BETWEEN_CMDS + 2))

# ─── Per-command farming loop ─────────────────────────────────────────────────

async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]
    earn  = cmd_info["earn"]

    # Stagger all tasks so they don't all fire at second 0
    await asyncio.sleep(random.uniform(2, 60))

    # Per-command random break counter
    break_counter = 0

    while True:
        # ── Pause if global pause active ──────────────────────────────────────
        while state["paused"]:
            await asyncio.sleep(5)

        # ── Random skip (human forgets sometimes) ─────────────────────────────
        if random.random() < RANDOM_SKIP_CHANCE:
            print(f"[ANTI-DETECT] Skipping {name} this cycle (human simulation)")
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            continue

        try:
            await send_command(channel, cmd)
            state["total_commands"]  += 1
            state["cowony_earned"]   += earn
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]      = cmd
            state["last_command_time"] = datetime.now()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ {cmd}")

        except discord.Forbidden:
            state["last_error"] = "No permission in channel"
            print(f"[ERROR] No permission — check CHANNEL_ID")
            await asyncio.sleep(120)
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry = e.retry_after if hasattr(e, 'retry_after') else 15
                print(f"[ANTI-DETECT] Rate limited — waiting {retry}s")
                state["last_error"] = f"Rate limited ({retry}s)"
                await asyncio.sleep(retry + random.uniform(2, 8))
            else:
                state["last_error"] = f"HTTP {e.status}"
                print(f"[ERROR] HTTP {e.status}: {e.text}")
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            print(f"[ERROR] {name}: {e}")
            await asyncio.sleep(10)

        # ── Cooldown with ±20% human jitter ───────────────────────────────────
        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        wait   = max(10, delay + jitter)
        await asyncio.sleep(wait)

        # ── Occasional random break (every ~90 min of active commands) ────────
        break_counter += 1
        threshold = random.randint(80, 130)
        if break_counter >= threshold:
            break_counter = 0
            await random_break()

# ─── Bot runner ───────────────────────────────────────────────────────────────

def run_bot():
    global send_lock

    while True:
        if not TOKEN:
            state["status"] = "no_token"
            time.sleep(5)
            continue

        if not CHANNEL_ID_STR:
            state["status"]     = "no_token"
            state["last_error"] = "CHANNEL_ID not set"
            time.sleep(5)
            continue

        try:
            channel_id = int(CHANNEL_ID_STR)
        except ValueError:
            state["status"]     = "error"
            state["last_error"] = "CHANNEL_ID must be a number"
            time.sleep(30)
            continue

        state["status"]     = "connecting"
        state["last_error"] = ""

        client = discord.Client()

        @client.event
        async def on_ready():
            global send_lock
            send_lock = asyncio.Lock()  # create lock in the bot's event loop

            print(f"\n{'='*60}")
            print(f"  OWO AUTO-FARMER — ANTI-DETECT MODE ON")
            print(f"  Account  : {client.user}")
            print(f"  Target   : {DAILY_TARGET:,} cowony / day")
            print(f"  Commands : {len(COMMANDS)}")
            print(f"{'='*60}\n")

            state["logged_in_as"]   = str(client.user)
            state["status"]         = "online"
            state["start_time"]     = datetime.now()
            state["last_error"]     = ""
            state["command_stats"]  = {}
            state["cowony_earned"]  = 0
            state["total_commands"] = 0

            # Fetch channel
            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except Exception:
                    pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found — check CHANNEL_ID"
                print(f"[ERROR] Channel {channel_id} not found!")
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            print(f"[BOT] Farming in #{state['channel_name']}")

            for cmd_info in COMMANDS:
                client.loop.create_task(farm_command(channel, cmd_info))

        @client.event
        async def on_disconnect():
            state["status"] = "reconnecting"
            print("[BOT] Disconnected — reconnecting...")

        try:
            client.run(TOKEN, reconnect=True)
        except discord.LoginFailure:
            state["status"]     = "error"
            state["last_error"] = "Invalid token — double check DISCORD_TOKEN"
            print("[BOT] Login failed: invalid token")
            time.sleep(60)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            print(f"[BOT] Crashed: {e}")
            time.sleep(10)

        print("[BOT] Restarting in 5s...\n")
        time.sleep(5)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
