import discord
import asyncio
import os
import random
import threading
import time
from datetime import datetime
from flask import Flask, render_template, jsonify

TOKEN          = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
DAILY_TARGET   = 75000  # cowony/day target

app = Flask(__name__)

# ─── Shared state (bot ↔ web) ─────────────────────────────────────────────────
state = {
    "status":           "waiting",
    "logged_in_as":     "",
    "channel_name":     "",
    "channel_id":       "",
    "total_commands":   0,
    "command_stats":    {},
    "cowony_earned":    0,
    "start_time":       None,
    "last_error":       "",
    "last_command":     "",
    "last_command_time": None,
}

# ─── OWO Commands (ALL money-making commands) ─────────────────────────────────
# delay = base cooldown in seconds (actual = delay + random jitter)
# earn  = estimated avg cowony per use (for tracking)
COMMANDS = [
    # ── Core grind (highest frequency) ───────────────────────────────────────
    {"cmd": "owo battle",       "delay": 60,     "name": "Battle",    "earn": 50},
    {"cmd": "owo pray",         "delay": 300,    "name": "Pray",      "earn": 60},
    {"cmd": "owo curse",        "delay": 300,    "name": "Curse",     "earn": 60},

    # ── Work / crime ─────────────────────────────────────────────────────────
    {"cmd": "owo work",         "delay": 900,    "name": "Work",      "earn": 150},
    {"cmd": "owo crime",        "delay": 1800,   "name": "Crime",     "earn": 200},

    # ── Animal hunting (sell for cowony) ─────────────────────────────────────
    {"cmd": "owo hunt",         "delay": 1800,   "name": "Hunt",      "earn": 120},
    {"cmd": "owo fish",         "delay": 1800,   "name": "Fish",      "earn": 120},

    # ── Gambling ─────────────────────────────────────────────────────────────
    {"cmd": "owo slots 500",    "delay": 600,    "name": "Slots",     "earn": 200},
    {"cmd": "owo coinflip 500", "delay": 300,    "name": "Coinflip",  "earn": 200},

    # ── Daily / weekly bonuses ────────────────────────────────────────────────
    {"cmd": "owo daily",        "delay": 43200,  "name": "Daily",     "earn": 5000},
    {"cmd": "owo weekly",       "delay": 604800, "name": "Weekly",    "earn": 25000},

    # ── Auto sell animals for cowony ─────────────────────────────────────────
    {"cmd": "owo sell all",     "delay": 7200,   "name": "Sell",      "earn": 500},
]

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/stats")
def stats():
    data = dict(state)
    data["daily_target"] = DAILY_TARGET
    data["command_meta"] = [
        {"name": c["name"], "cmd": c["cmd"], "delay": c["delay"]}
        for c in COMMANDS
    ]

    if state["start_time"]:
        elapsed = (datetime.now() - state["start_time"]).total_seconds()
        data["uptime_seconds"] = int(elapsed)
        hours = max(elapsed / 3600, 0.001)
        data["daily_rate"]    = int(state["cowony_earned"] / hours * 24)
        data["start_time"]    = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
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

# ─── Bot command loop ─────────────────────────────────────────────────────────

async def run_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]
    earn  = cmd_info["earn"]

    # Stagger startup so all commands don't fire at once
    await asyncio.sleep(random.uniform(1, 45))

    while True:
        try:
            await channel.send(cmd)
            state["total_commands"]  += 1
            state["cowony_earned"]   += earn
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]     = cmd
            state["last_command_time"] = datetime.now()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ {cmd}")

            # Small pause after sending so OWO bot can respond
            await asyncio.sleep(random.uniform(1.5, 3.5))

        except discord.Forbidden:
            print(f"[ERROR] No permission to send in channel — check CHANNEL_ID")
            state["last_error"] = "No permission in channel"
            await asyncio.sleep(60)
        except discord.HTTPException as e:
            print(f"[ERROR] HTTP {e.status}: {e.text}")
            state["last_error"] = f"HTTP {e.status}"
            await asyncio.sleep(15)
        except Exception as e:
            print(f"[ERROR] {name}: {e}")
            state["last_error"] = str(e)
            await asyncio.sleep(10)

        # Wait for cooldown + human-like random jitter (±20%)
        jitter = random.uniform(-delay * 0.15, delay * 0.20)
        wait   = max(10, delay + jitter)
        await asyncio.sleep(wait)


# ─── Bot runner (auto-reconnect forever) ─────────────────────────────────────

def run_bot():
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

        state["status"] = "connecting"
        state["last_error"] = ""

        intents = discord.Intents.default()
        client  = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"\n{'='*60}")
            print(f"  OWO AUTO-FARMER STARTED")
            print(f"  Account  : {client.user}")
            print(f"  Target   : {DAILY_TARGET:,} cowony/day")
            print(f"{'='*60}\n")

            state["logged_in_as"] = str(client.user)
            state["status"]       = "online"
            state["start_time"]   = datetime.now()
            state["last_error"]   = ""
            state["command_stats"]  = {}
            state["cowony_earned"]  = 0
            state["total_commands"] = 0

            channel = client.get_channel(channel_id)
            if channel is None:
                # Try fetching if not in cache
                try:
                    channel = await client.fetch_channel(channel_id)
                except Exception:
                    pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found. Check CHANNEL_ID."
                print(f"[ERROR] Channel {channel_id} not found!")
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            print(f"[BOT] Farming in #{state['channel_name']}")

            for cmd_info in COMMANDS:
                client.loop.create_task(run_command(channel, cmd_info))

        @client.event
        async def on_disconnect():
            state["status"] = "reconnecting"
            print("[BOT] Disconnected — will reconnect...")

        try:
            client.run(TOKEN, bot=False, reconnect=True)
        except discord.LoginFailure:
            state["status"]     = "error"
            state["last_error"] = "Invalid token — double-check DISCORD_TOKEN"
            print("[BOT] Login failed: invalid token")
            time.sleep(60)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            print(f"[BOT] Crashed: {e}")
            time.sleep(10)

        print("[BOT] Restarting in 5 seconds...\n")
        time.sleep(5)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
