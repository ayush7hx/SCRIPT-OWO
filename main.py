import discord
import asyncio
import os
import random
import threading
import time
import json
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request, session, redirect

SECRET_KEY     = os.getenv("FLASK_SECRET", os.urandom(24).hex())
CHANNEL_ID_STR = os.getenv("CHANNEL_ID", "")
DAILY_TARGET   = 85000

app = Flask(__name__)
app.secret_key = SECRET_KEY

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
    "paused":            False,
    "pause_reason":      "",
    "token":             os.getenv("DISCORD_TOKEN", ""),
}

COMMANDS = [
    {"cmd": "owo battle",        "delay": 62,    "name": "Battle",    "earn": 55,    "cat": "GRIND"},
    {"cmd": "owo pray",          "delay": 305,   "name": "Pray",      "earn": 65,    "cat": "GRIND"},
    {"cmd": "owo curse",         "delay": 308,   "name": "Curse",     "earn": 65,    "cat": "GRIND"},
    {"cmd": "owo work",          "delay": 905,   "name": "Work",      "earn": 180,   "cat": "GRIND"},
    {"cmd": "owo crime",         "delay": 1805,  "name": "Crime",     "earn": 220,   "cat": "GRIND"},
    {"cmd": "owo hunt",          "delay": 1808,  "name": "Hunt",      "earn": 130,   "cat": "GRIND"},
    {"cmd": "owo fish",          "delay": 1812,  "name": "Fish",      "earn": 130,   "cat": "GRIND"},
    {"cmd": "owo slots 500",     "delay": 605,   "name": "Slots",     "earn": 220,   "cat": "GAMBLE"},
    {"cmd": "owo coinflip 500",  "delay": 308,   "name": "Coinflip",  "earn": 220,   "cat": "GAMBLE"},
    {"cmd": "owo sell all",      "delay": 7205,  "name": "Sell",      "earn": 600,   "cat": "SELL"},
    {"cmd": "owo sell gems",     "delay": 7210,  "name": "SellGems",  "earn": 300,   "cat": "SELL"},
    {"cmd": "owo checklist",     "delay": 3610,  "name": "Checklist", "earn": 50,    "cat": "BONUS"},
    {"cmd": "owo daily",         "delay": 43205, "name": "Daily",     "earn": 6000,  "cat": "BONUS"},
    {"cmd": "owo weekly",        "delay": 604810,"name": "Weekly",    "earn": 28000, "cat": "BONUS"},
]

send_lock = None

# ─── Discord login via email+password ─────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v9"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

def discord_login(email: str, password: str):
    """Login with email+password, returns (token, mfa_ticket, error)"""
    try:
        r = requests.post(
            f"{DISCORD_API}/auth/login",
            headers=HEADERS,
            json={"login": email, "password": password, "undelete": False, "captcha_key": None},
            timeout=15
        )
        data = r.json()
        if "token" in data:
            return data["token"], None, None
        if data.get("mfa"):
            return None, data.get("ticket"), "2fa"
        msg = data.get("message") or str(data.get("errors", "Login failed"))
        return None, None, msg
    except Exception as e:
        return None, None, str(e)

def discord_mfa(ticket: str, code: str):
    """Submit 2FA code, returns (token, error)"""
    try:
        r = requests.post(
            f"{DISCORD_API}/auth/mfa/totp",
            headers=HEADERS,
            json={"code": code.replace(" ", ""), "ticket": ticket},
            timeout=15
        )
        data = r.json()
        if "token" in data:
            return data["token"], None
        msg = data.get("message") or "Invalid 2FA code"
        return None, msg
    except Exception as e:
        return None, str(e)

# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    if not state["token"] and not session.get("token"):
        return redirect("/login")
    return render_template("index.html")

@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    email    = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    token, ticket, err = discord_login(email, password)

    if token:
        state["token"] = token
        state["status"] = "connecting"
        # Restart bot with new token
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
    if not code:
        return jsonify({"error": "2FA code required"}), 400

    token, err = discord_mfa(ticket, code)
    if token:
        state["token"] = token
        state["status"] = "connecting"
        session.pop("mfa_ticket", None)
        bot_restart_event.set()
        return jsonify({"success": True})

    return jsonify({"error": err or "2FA failed"}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    state["token"] = ""
    state["status"] = "waiting"
    state["logged_in_as"] = ""
    bot_restart_event.set()
    return jsonify({"success": True})

@app.route("/api/set_channel", methods=["POST"])
def api_set_channel():
    data = request.get_json()
    ch   = str(data.get("channel_id") or "").strip()
    if not ch.isdigit():
        return jsonify({"error": "Channel ID must be a number"}), 400
    global CHANNEL_ID_STR
    CHANNEL_ID_STR = ch
    state["status"] = "connecting"
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
        d["daily_rate"]  = int(state["cowony_earned"] / hours * 24)
        d["start_time"]  = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        d["uptime_seconds"] = 0
        d["daily_rate"]     = 0
        d["start_time"]     = None
    if state["last_command_time"]:
        d["last_command_time"] = state["last_command_time"].strftime("%H:%M:%S")
    else:
        d["last_command_time"] = "—"
    d.pop("token", None)  # never expose token to frontend
    return jsonify(d)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Anti-detect helpers ──────────────────────────────────────────────────────

async def human_typing(channel, cmd_len=10):
    wpm      = random.uniform(48, 82)
    chars    = cmd_len + random.randint(-2, 4)
    duration = min(max((chars / (wpm * 5)) * 60, 0.4), 3.2)
    async with channel.typing():
        await asyncio.sleep(duration)

async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    delay = cmd_info["delay"]
    name  = cmd_info["name"]
    earn  = cmd_info["earn"]

    await asyncio.sleep(random.uniform(2, 55))
    break_counter = 0

    while True:
        while state["paused"]:
            await asyncio.sleep(5)

        if random.random() < 0.04:
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            continue

        try:
            async with send_lock:
                await human_typing(channel, len(cmd))
                await channel.send(cmd)
                await asyncio.sleep(random.uniform(2.5, 4.5))

            state["total_commands"]  += 1
            state["cowony_earned"]   += earn
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]       = cmd
            state["last_command_time"]  = datetime.now()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ {cmd}")

        except discord.Forbidden:
            state["last_error"] = "No permission — check channel ID"
            await asyncio.sleep(120)
        except discord.HTTPException as e:
            if e.status == 429:
                retry = getattr(e, "retry_after", 15)
                state["last_error"] = f"Rate limited ({retry}s)"
                await asyncio.sleep(float(retry) + random.uniform(2, 8))
            else:
                state["last_error"] = f"HTTP {e.status}"
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            await asyncio.sleep(10)

        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        await asyncio.sleep(max(10, delay + jitter))

        break_counter += 1
        if break_counter >= random.randint(80, 130):
            break_counter = 0
            dur = random.randint(90, 350)
            state["paused"] = True
            state["pause_reason"] = f"Human break ({dur//60}m {dur%60}s)"
            print(f"[ANTI-DETECT] Break {dur}s")
            await asyncio.sleep(dur)
            state["paused"] = False
            state["pause_reason"] = ""

# ─── Bot core ─────────────────────────────────────────────────────────────────

bot_restart_event = threading.Event()

def run_bot():
    global send_lock

    while True:
        bot_restart_event.clear()

        token      = state["token"]
        channel_id_str = CHANNEL_ID_STR

        if not token:
            state["status"] = "waiting"
            bot_restart_event.wait(timeout=5)
            continue

        if not channel_id_str:
            state["status"]     = "no_channel"
            state["last_error"] = "No channel ID set"
            bot_restart_event.wait(timeout=5)
            continue

        try:
            channel_id = int(channel_id_str)
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

            print(f"\n{'='*60}")
            print(f"  OWO FARMER — Account: {client.user}")
            print(f"  Target: {DAILY_TARGET:,} cowony / day")
            print(f"{'='*60}\n")

            state["logged_in_as"]   = str(client.user)
            state["status"]         = "online"
            state["start_time"]     = datetime.now()
            state["last_error"]     = ""
            state["command_stats"]  = {}
            state["cowony_earned"]  = 0
            state["total_commands"] = 0

            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except Exception:
                    pass

            if channel is None:
                state["status"]     = "error"
                state["last_error"] = f"Channel {channel_id} not found — update Channel ID"
                await client.close()
                return

            state["channel_name"] = getattr(channel, "name", str(channel_id))
            state["channel_id"]   = str(channel_id)
            print(f"[BOT] Farming in #{state['channel_name']}")

            for cmd_info in COMMANDS:
                client.loop.create_task(farm_command(channel, cmd_info))

            # Watch for restart signal
            async def watch_restart():
                while not bot_restart_event.is_set():
                    await asyncio.sleep(2)
                await client.close()

            client.loop.create_task(watch_restart())

        @client.event
        async def on_disconnect():
            state["status"] = "reconnecting"

        try:
            client.run(token, reconnect=True)
        except discord.LoginFailure:
            state["status"]     = "error"
            state["last_error"] = "Invalid token — please login again"
            state["token"]      = ""
            print("[BOT] Login failed: invalid token")
            bot_restart_event.wait(timeout=5)
        except Exception as e:
            state["status"]     = "error"
            state["last_error"] = str(e)
            print(f"[BOT] Error: {e}")
            bot_restart_event.wait(timeout=8)

        print("[BOT] Restarting...\n")
        time.sleep(3)

# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
