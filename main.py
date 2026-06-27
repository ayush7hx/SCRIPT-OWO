import discord
import asyncio
import os
import random
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify

TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")

app = Flask(__name__)

# Shared state between bot and web server
state = {
    "status": "waiting",  # waiting, connecting, online, error, no_token
    "logged_in_as": "",
    "channel_name": "",
    "channel_id": "",
    "total_commands": 0,
    "command_stats": {},
    "start_time": None,
    "last_error": "",
    "last_command_time": None,
    "uptime_seconds": 0,
}

COMMANDS = [
    {"cmd": "owo battle",   "delay": 60,     "name": "Battle"},
    {"cmd": "owo pray",     "delay": 300,    "name": "Pray"},
    {"cmd": "owo work",     "delay": 900,    "name": "Work"},
    {"cmd": "owo hunt",     "delay": 1800,   "name": "Hunt"},
    {"cmd": "owo fish",     "delay": 1800,   "name": "Fish"},
    {"cmd": "owo crime",    "delay": 1800,   "name": "Crime"},
    {"cmd": "owo rob",      "delay": 1800,   "name": "Rob"},
    {"cmd": "owo slots 100","delay": 600,    "name": "Slots"},
    {"cmd": "owo daily",    "delay": 43200,  "name": "Daily"},
    {"cmd": "owo weekly",   "delay": 604800, "name": "Weekly"},
]

# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/stats")
def stats():
    data = dict(state)
    if state["start_time"]:
        elapsed = (datetime.now() - state["start_time"]).total_seconds()
        data["uptime_seconds"] = int(elapsed)
        hours = elapsed / 3600
        avg = 25
        estimated = state["total_commands"] * avg
        data["estimated_earnings"] = int(estimated)
        data["daily_earnings"] = int(estimated / hours * 24) if hours > 0 else 0
        data["start_time"] = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        data["estimated_earnings"] = 0
        data["daily_earnings"] = 0
        data["start_time"] = None

    if state["last_command_time"]:
        data["last_command_time"] = state["last_command_time"].strftime("%H:%M:%S")
    else:
        data["last_command_time"] = "—"

    return jsonify(data)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Bot Logic ────────────────────────────────────────────────────────────────

async def run_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]

    await asyncio.sleep(random.randint(0, 30))

    while True:
        try:
            await channel.send(cmd)
            state["total_commands"] += 1
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command_time"] = datetime.now()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent: {cmd}")
        except Exception as e:
            print(f"[ERROR] {name}: {e}")
            state["last_error"] = str(e)

        jitter = random.randint(-30, 30)
        await asyncio.sleep(max(10, delay + jitter))


def run_bot():
    while True:
        if not TOKEN:
            state["status"] = "no_token"
            print("[BOT] No DISCORD_TOKEN set — bot is idle.")
            time.sleep(10)
            continue

        if not CHANNEL_ID_STR:
            state["status"] = "no_token"
            print("[BOT] No CHANNEL_ID set — bot is idle.")
            time.sleep(10)
            continue

        try:
            channel_id = int(CHANNEL_ID_STR)
        except ValueError:
            state["status"] = "error"
            state["last_error"] = "CHANNEL_ID must be a number"
            print("[BOT] Invalid CHANNEL_ID")
            time.sleep(30)
            continue

        state["status"] = "connecting"

        intents = discord.Intents.default()
        client  = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            state["logged_in_as"] = str(client.user)
            state["status"]       = "online"
            state["start_time"]   = datetime.now()
            state["last_error"]   = ""
            print(f"[BOT] Logged in as {client.user}")

            channel = client.get_channel(channel_id)
            if not channel:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found"
                print(f"[BOT] Channel {channel_id} not found!")
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
            print("[BOT] Disconnected — reconnecting...")

        try:
            client.run(TOKEN, bot=False)
        except discord.LoginFailure:
            state["status"]     = "error"
            state["last_error"] = "Invalid token"
            print("[BOT] Login failed: invalid token")
            time.sleep(30)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            print(f"[BOT] Crashed: {e}")
            time.sleep(10)

        print("[BOT] Restarting in 5s...")
        time.sleep(5)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=False)
