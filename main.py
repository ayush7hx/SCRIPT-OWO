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
    "status":           "waiting",
    "logged_in_as":     "",
    "channel_name":     "",
    "channel_id":       "",
    "token":            os.getenv("DISCORD_TOKEN", ""),
    "real_cowony":      0,
    "real_events":      [],
    "battle_streak":    0,
    "battles_won":      0,
    "battles_lost":     0,
    "total_xp":         0,
    "team_animals":     [],     # [{"emoji":"chipmunk","level":13,"has_weapon":False},...]
    "team_ready":       False,
    "weapons_equipped": 0,
    "total_commands":   0,
    "command_stats":    {},
    "start_time":       None,
    "last_error":       "",
    "last_command":     "",
    "last_command_time":None,
    "paused":           False,
    "pause_reason":     "",
    "lootboxes_opened": 0,
    "quests_completed": 0,
    "animals_caught":   0,
    "activity_log":     [],
    # internals
    "_cooldowns":       {},     # name -> resume_at
    "_team_creating":   False,
    "_team_add_ok":     False,
    "_equipping":       False,
    "_weapon_step":     0,      # 0=idle,1=waiting weapon list,2=waiting slot choice
    "_weapon_slot":     1,      # which team slot to equip to next (1-3)
    "_weapon_channel":  None,   # channel ref for interactive replies
}

send_lock         = None
last_sent_at      = 0.0
MIN_GAP           = 5.0          # minimum seconds between any two commands
bot_restart_event = threading.Event()

# ─── Common animals — tried first (what most players catch via hunt/fish) ─────
TEAM_ANIMALS = [
    # Try both base names AND "2" variants (OWO uses :rabbit2:, :mouse2:, :cat2: etc.)
    "chipmunk",
    "rabbit","rabbit2",
    "mouse","mouse2",
    "cat","cat2",
    "dog","dog2",
    "pig","pig2",
    "bee","butterfly","snail","beetle","bug","baby_chick","chick",
    "rooster","sheep","duck","frog","cow","cow2","horse","hamster",
    "parrot","otter","penguin","panda",
    "fox","bear","wolf","deer","shrimp","lion","tiger","tiger2",
    "monkey","eagle","owl","camel","crocodile","shark",
    "whale","whale2","dolphin","turtle","elephant","giraffe","zebra","gorilla",
    "gfox","gshrimp","gdeer","gcamel","gcat","gdog","gbear",
]

# ─── Commands ─────────────────────────────────────────────────────────────────
# hunt + battle = core XP+cowony loop (run most frequently)
# gambling uses RANDOM amounts (set at send time) for human-like behavior
COMMANDS = [
    {"cmd": "owo hunt",       "delay": 65,     "name": "Hunt"},
    {"cmd": "owo battle",     "delay": 68,     "name": "Battle"},
    {"cmd": "owo fish",       "delay": 72,     "name": "Fish"},
    {"cmd": "owo pray",       "delay": 305,    "name": "Pray"},
    {"cmd": "owo curse",      "delay": 310,    "name": "Curse"},
    {"cmd": "owo work",       "delay": 910,    "name": "Work"},
    {"cmd": "owo crime",      "delay": 1810,   "name": "Crime"},
    {"cmd": "owo lootbox",    "delay": 14410,  "name": "Lootbox"},   # try every ~4h
    {"cmd": "owo SLOTS",      "delay": 610,    "name": "Slots"},     # amount set at send time
    {"cmd": "owo COINFLIP",   "delay": 315,    "name": "Coinflip"},  # amount set at send time
    {"cmd": "owo sell all",   "delay": 7210,   "name": "Sell"},
    {"cmd": "owo daily",      "delay": 43210,  "name": "Daily"},
    {"cmd": "owo weekly",     "delay": 604815, "name": "Weekly"},
    {"cmd": "owo checklist",  "delay": 7210,   "name": "Checklist"},
]

# ─── Helpers ──────────────────────────────────────────────────────────────────
def log_activity(msg: str, kind: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "kind": kind}
    state["activity_log"].insert(0, entry)
    state["activity_log"] = state["activity_log"][:30]
    print(f"[{entry['time']}] {msg}")

def get_text(message) -> str:
    parts = [message.content or ""]
    for e in message.embeds:
        if e.title:       parts.append(e.title)
        if e.description: parts.append(e.description)
        if e.footer and e.footer.text: parts.append(e.footer.text)
        for f in e.fields: parts.append(f"{f.name} {f.value}")
    return "\n".join(parts)

def parse_earned(text: str) -> int:
    for p in [
        r'daily\s*:cowoncy:\s*(\d[\d,]+)',
        r'weekly\s*:cowoncy:\s*(\d[\d,]+)',
        r'total of\s*:cowoncy:\s*(\d[\d,]+)',
        r'won\s*:cowoncy:\s*(\d[\d,]+)',
        r'earn[ed]*\s*:cowoncy:\s*(\d[\d,]+)',
        r'Here is your\s+\w+\s+:cowoncy:\s*(\d[\d,]+)',
        r'\+\s*:cowoncy:\s*(\d[\d,]+)',
    ]:
        m = re.search(p, text, re.I)
        if m: return int(m.group(1).replace(",", ""))
    return 0

def parse_xp(text: str) -> int:
    m = re.search(r'\+\s*([\d,]+)\s*xp', text, re.I)
    return int(m.group(1).replace(",", "")) if m else 0

def parse_streak(text: str) -> int:
    m = re.search(r'Streak:\s*(\d+)', text, re.I)
    return int(m.group(1)) if m else -1

def team_animal_names() -> list:
    """Get current team animal emoji names for weapon equipping."""
    return [a["emoji"] for a in state.get("team_animals", [])]

# ─── Anti-detect send ─────────────────────────────────────────────────────────
async def smart_send(channel, cmd: str):
    global send_lock, last_sent_at
    async with send_lock:
        # Enforce minimum gap between all sends
        gap = time.time() - last_sent_at
        if gap < MIN_GAP:
            await asyncio.sleep(MIN_GAP - gap + random.uniform(1.0, 2.5))

        # Simulate human typing speed
        wpm      = random.uniform(50, 85)
        chars    = len(cmd) + random.randint(-2, 4)
        duration = min(max((chars / (wpm * 5)) * 60, 0.6), 2.8)
        async with channel.typing():
            await asyncio.sleep(duration)

        await channel.send(cmd)
        last_sent_at = time.time()
        await asyncio.sleep(random.uniform(1.5, 2.5))

# ─── Team creation: try animals one by one ────────────────────────────────────
async def auto_create_team(channel):
    if state["_team_creating"]:
        return
    state["_team_creating"] = True
    state["team_ready"]     = False
    log_activity("Auto-creating battle team...", "info")

    slots = 0
    for animal in TEAM_ANIMALS:
        if slots >= 3:
            break
        state["_team_add_ok"] = False

        log_activity(f"Team slot {slots+1}: trying '{animal}'", "info")
        try:
            await smart_send(channel, f"owo team add {animal}")
            await asyncio.sleep(random.uniform(6, 10))  # wait for OWO reply + avoid rate limit
        except Exception as e:
            log_activity(f"Team add err: {e}", "warn")
            await asyncio.sleep(3)
            continue

        if state["_team_add_ok"]:
            slots += 1
            state["team_ready"] = True
            log_activity(f"'{animal}' added! Slots: {slots}/3", "earn")
            await asyncio.sleep(random.uniform(2, 4))

    state["_team_creating"] = False
    if state["team_ready"]:
        log_activity(f"Battle team ready! ({slots} animals)", "earn")
        # Immediately try to equip weapons
        await asyncio.sleep(3)
        await auto_equip_weapons(channel)
    else:
        log_activity("No animals to add — hunt/fish first to catch some!", "warn")

# ─── Auto-equip weapons (interactive OWO flow) ───────────────────────────────
async def auto_equip_weapons(channel):
    """
    OWO weapon equip interactive flow:
      1. Bot sends `owo weapon equip`
      2. OWO shows numbered weapon list → bot replies "1"
      3. OWO asks which team slot → bot replies "1", "2", or "3"
      4. Repeat for each team slot (up to 3 times)
    """
    if state["_equipping"]:
        return
    state["_equipping"]       = True
    state["_weapon_channel"]  = channel
    state["_weapon_slot"]     = 1  # start at slot 1

    log_activity("Auto-equipping weapons to team...", "info")

    # Run equip loop for each team slot
    for slot in range(1, 4):
        state["_weapon_step"] = 1           # step 1 = waiting for OWO weapon list
        state["_weapon_slot"] = slot
        try:
            await smart_send(channel, "owo weapon equip")
        except Exception as e:
            log_activity(f"Weapon equip send err: {e}", "warn")
            break
        # Wait up to 20s for the full interaction to complete (steps 1→2→done)
        for _ in range(20):
            await asyncio.sleep(1)
            if state["_weapon_step"] == 0:  # completed for this slot
                break
        if state["_weapon_step"] != 0:
            # OWO didn't respond — no weapons in inventory, stop
            state["_weapon_step"] = 0
            log_activity("No weapons in inventory to equip", "info")
            break
        await asyncio.sleep(random.uniform(3, 6))  # pause between slots

    state["_equipping"]      = False
    state["_weapon_channel"] = None

# ─── Auto open lootbox / crate ────────────────────────────────────────────────
async def auto_open_lootbox(channel):
    await asyncio.sleep(random.uniform(4, 8))
    log_activity("Lootbox → opening!", "earn")
    try:
        await smart_send(channel, "owo lootbox")
        state["lootboxes_opened"] += 1
        await asyncio.sleep(4)
        await auto_equip_weapons(channel)  # equip any new weapons
    except Exception as e:
        log_activity(f"Lootbox err: {e}", "warn")

async def auto_open_crate(channel):
    await asyncio.sleep(random.uniform(4, 8))
    log_activity("Weapon crate → opening!", "earn")
    try:
        await smart_send(channel, "owo crate")
        await asyncio.sleep(4)
        await auto_equip_weapons(channel)  # equip the new weapon
    except Exception as e:
        log_activity(f"Crate err: {e}", "warn")

# ─── OWO message handler ──────────────────────────────────────────────────────
async def handle_owo_message(message, channel, client):
    full = get_text(message)
    low  = full.lower()

    # ── Rate limit ────────────────────────────────────────────────────────────
    if "slow down" in low:
        m = re.search(r'in (\d+)\s*(minute|second)', full, re.I)
        wait = 30  # default
        if m:
            val  = int(m.group(1))
            wait = val * 60 if "minute" in m.group(2).lower() else val
        backoff = wait + random.randint(10, 25)
        log_activity(f"OWO rate limit — backing off {backoff}s", "warn")
        state["_cooldowns"]["__global__"] = time.time() + backoff

    # ── Real cowony ───────────────────────────────────────────────────────────
    earned = parse_earned(full)
    if earned > 0:
        state["real_cowony"] += earned
        state["real_events"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "amount": f"+{earned:,}", "kind": "earn"
        })
        state["real_events"] = state["real_events"][:30]
        log_activity(f"+{earned:,} cowony", "earn")

    # ── Battle result ─────────────────────────────────────────────────────────
    if "goes into battle" in low:
        xp     = parse_xp(full)
        streak = parse_streak(full)
        if xp > 0: state["total_xp"] += xp
        if streak >= 0: state["battle_streak"] = streak

        # Parse team animal info (level + has weapon)
        team = []
        for m2 in re.finditer(r'L\.\s*(\d+)\s*:(\w+):\s*-\s*(.*?)(?:\n|$)', full):
            level   = int(m2.group(1))
            emoji   = m2.group(2)
            weapon  = m2.group(3).strip()
            has_wpn = weapon != "no weapon" and bool(weapon)
            team.append({"emoji": emoji, "level": level, "has_weapon": has_wpn})

        if team:
            # First 3 entries before the enemy team
            sections = full.split("\n")
            my_team = []
            in_my   = False
            for line in sections:
                if "goes into battle" in line.lower():
                    in_my = True
                if in_my and ("enemy team" in line.lower() or "🔥" in line or "cat" in line.lower()):
                    break
                if in_my:
                    for mt in team:
                        if mt["emoji"] in line and mt not in my_team:
                            my_team.append(mt)
            state["team_animals"] = my_team[:3] if my_team else team[:3]

        if "you won" in low or "won in" in low:
            state["battles_won"] += 1
            log_activity(f"Battle won! +{xp:,}xp | Streak {state['battle_streak']}", "earn")
        elif "you lost" in low or "lost in" in low:
            state["battles_lost"] += 1
            log_activity(f"Battle lost. +{xp:,}xp", "info")
            # If team has no weapons, try equipping
            no_weapons = all(not a["has_weapon"] for a in state["team_animals"])
            if no_weapons and not state["_equipping"]:
                client.loop.create_task(auto_equip_weapons(channel))

    # ── Detect ANY existing team members in OWO response (L.X :animal: pattern) ─
    # This catches: owo team response, battle embed, and team add confirmation
    level_matches = re.findall(r'L\.\s*(\d+)\s*:(\w+):', full)
    if level_matches and not state["team_ready"]:
        state["team_ready"]     = True
        state["_team_add_ok"]   = True
        state["_team_creating"] = False
        log_activity("Team detected from OWO response!", "earn")

    # ── Team add success phrases ──────────────────────────────────────────────
    team_success_phrases = [
        "added", "successfully", "joined your team",
        "is now on your team", "battle team", "team member",
    ]
    if any(p in low for p in team_success_phrases) and "team" in low:
        state["_team_add_ok"] = True
        state["team_ready"]   = True
        if not level_matches:
            log_activity("Animal added to team!", "earn")

    # ── Already in team / full ────────────────────────────────────────────────
    if "team is full" in low or "already on your team" in low or "already in your team" in low:
        state["_team_add_ok"] = True
        state["team_ready"]   = True
        state["_team_creating"] = False
        log_activity("Team already exists / is full!", "earn")

    # ── Animal not found — silently move to next in loop ──────────────────────
    if "could not find this animal" in low or "do not own this animal" in low:
        state["_team_add_ok"] = False  # try_create_team loop moves to next animal

    # ── No battle team error ───────────────────────────────────────────────────
    if "do not have an active battle team" in low:
        log_activity("No battle team! Auto-creating...", "warn")
        state["team_ready"] = False
        if not state["_team_creating"]:
            client.loop.create_task(auto_create_team(channel))

    # ── Weapon equip interactive flow ─────────────────────────────────────────
    # Step 1: OWO shows numbered weapon list → reply "1" to pick first weapon
    if state["_weapon_step"] == 1:
        weapon_list = (
            re.search(r'1\.\s*\*{0,2}.{1,40}\*{0,2}', full) or  # "1. SwordName"
            "select" in low or "choose" in low or
            ("1." in full and ("weapon" in low or "equip" in low or "inventory" in low))
        )
        if weapon_list:
            state["_weapon_step"] = 2
            ch = state.get("_weapon_channel")
            if ch:
                await asyncio.sleep(random.uniform(1.5, 3.0))
                await ch.send("1")
                log_activity("Weapon equip: picked weapon #1", "info")

    # Step 2: OWO asks which team slot → reply with slot number
    elif state["_weapon_step"] == 2:
        slot_prompt = (
            "which team member" in low or "which slot" in low or
            "which animal" in low or "pick a slot" in low or
            "team slot" in low or ("1." in full and "2." in full and "team" in low)
        )
        if slot_prompt:
            slot = state.get("_weapon_slot", 1)
            ch   = state.get("_weapon_channel")
            if ch:
                await asyncio.sleep(random.uniform(1.5, 3.0))
                await ch.send(str(slot))
                log_activity(f"Weapon equip: picked slot {slot}", "info")
            state["_weapon_step"] = 0  # done for this slot

    # ── Weapon equipped confirmation ────────────────────────────────────────────
    if "equipped" in low and ("weapon" in low or "sword" in low or "staff" in low
                               or "bow" in low or "wand" in low or "gun" in low):
        state["weapons_equipped"] += 1
        state["_weapon_step"] = 0   # ensure state resets on any equip confirm
        log_activity("✅ Weapon equipped on team animal!", "earn")

    # ── Hunt result ───────────────────────────────────────────────────────────
    if "spent" in low and "caught" in low:
        xp = parse_xp(full)
        if xp > 0: state["total_xp"] += xp
        state["animals_caught"] += 1

    # ── Lootbox found → open immediately ─────────────────────────────────────
    if "you found a lootbox" in low:
        client.loop.create_task(auto_open_lootbox(channel))

    # ── Weapon/loot crate ─────────────────────────────────────────────────────
    if "weapon crate" in low or "loot crate" in low:
        client.loop.create_task(auto_open_crate(channel))

    # ── Level up ──────────────────────────────────────────────────────────────
    if "leveled up" in low:
        log_activity("Level up!", "earn")

    # ── Daily collected ───────────────────────────────────────────────────────
    if "here is your daily" in low:
        sm = re.search(r'(\d+)\s+daily streak', full, re.I)
        log_activity(f"Daily done!{' Streak:'+sm.group(1) if sm else ''}", "earn")

    # ── Quest complete ─────────────────────────────────────────────────────────
    if "quest" in low and "complet" in low:
        state["quests_completed"] += 1
        log_activity("Quest completed!", "earn")

# ─── Per-command farming loop ─────────────────────────────────────────────────
async def farm_command(channel, cmd_info):
    cmd   = cmd_info["cmd"]
    name  = cmd_info["name"]
    delay = cmd_info["delay"]

    # Stagger startup so all loops don't fire at once
    idx = COMMANDS.index(cmd_info)
    await asyncio.sleep(random.uniform(idx * 10 + 8, idx * 15 + 35))

    # Battle waits extra for team setup
    if name == "Battle":
        await asyncio.sleep(45)

    break_counter = 0

    while True:
        # Respect global pause (human break)
        while state["paused"]:
            await asyncio.sleep(5)

        # Respect global rate-limit backoff
        global_resume = state["_cooldowns"].get("__global__", 0)
        if global_resume > time.time():
            await asyncio.sleep(global_resume - time.time() + random.uniform(3, 8))
            continue

        # Respect per-command cooldown
        cmd_resume = state["_cooldowns"].get(name, 0)
        if cmd_resume > time.time():
            await asyncio.sleep(cmd_resume - time.time() + random.uniform(2, 5))
            continue

        # 4% human skip
        if random.random() < 0.04:
            await asyncio.sleep(delay * random.uniform(0.5, 0.9))
            continue

        # Build actual command (gambling uses random amounts)
        actual_cmd = cmd
        if name == "Slots":
            amt = random.choice([100, 200, 300, 500, 600, 800, 1000])
            actual_cmd = f"owo slots {amt}"
        elif name == "Coinflip":
            amt  = random.choice([100, 200, 250, 500, 750, 1000])
            side = random.choice(["heads", "tails"])
            actual_cmd = f"owo coinflip {amt} {side}"

        try:
            await smart_send(channel, actual_cmd)
            state["total_commands"] += 1
            state["command_stats"][name] = state["command_stats"].get(name, 0) + 1
            state["last_command"]        = actual_cmd
            state["last_command_time"]   = datetime.now()

        except discord.Forbidden:
            state["last_error"] = "No permission in channel"
            log_activity("No permission in channel!", "warn")
            await asyncio.sleep(120)
        except discord.HTTPException as e:
            if e.status == 429:
                retry = float(getattr(e, "retry_after", 15))
                log_activity(f"HTTP 429 — {retry:.0f}s wait", "warn")
                state["_cooldowns"]["__global__"] = time.time() + retry + 12
                await asyncio.sleep(retry + 15)
            else:
                state["last_error"] = f"HTTP {e.status}"
                await asyncio.sleep(15)
        except Exception as e:
            state["last_error"] = str(e)
            log_activity(f"Send err: {e}", "warn")
            await asyncio.sleep(10)

        # Cooldown + ±20% jitter
        jitter = random.uniform(-delay * 0.18, delay * 0.22)
        await asyncio.sleep(max(15, delay + jitter))

        # Human break every ~90 min
        break_counter += 1
        if break_counter >= random.randint(85, 140):
            break_counter = 0
            dur = random.randint(90, 360)
            state["paused"]       = True
            state["pause_reason"] = f"Human break {dur//60}m {dur%60}s"
            log_activity(f"Human break: {dur}s", "info")
            await asyncio.sleep(dur)
            state["paused"]       = False
            state["pause_reason"] = ""
            log_activity("Break over, resuming", "info")

# ─── Startup ──────────────────────────────────────────────────────────────────
async def startup(channel, client):
    await asyncio.sleep(8)
    log_activity("Startup: checking battle team...", "info")
    try:
        await smart_send(channel, "owo team")
        await asyncio.sleep(5)
    except Exception:
        pass
    if not state["team_ready"]:
        log_activity("No team found — auto-creating...", "info")
        await auto_create_team(channel)

# ─── Periodic weapon equip (every 3h) ────────────────────────────────────────
async def periodic_weapon_equip(channel):
    await asyncio.sleep(600)   # wait 10 min after start
    while True:
        if state["team_animals"] and not state["_equipping"]:
            await auto_equip_weapons(channel)
        await asyncio.sleep(10800 + random.randint(-600, 600))  # ~3h

# ─── Bot core ─────────────────────────────────────────────────────────────────
def run_bot():
    global send_lock, last_sent_at
    while True:
        bot_restart_event.clear()
        token  = state["token"]
        ch_str = CHANNEL_ID_STR

        if not token:
            state["status"] = "waiting"; bot_restart_event.wait(timeout=5); continue
        if not ch_str:
            state["status"] = "no_channel"; state["last_error"] = "No channel ID set"
            bot_restart_event.wait(timeout=5); continue
        try:
            channel_id = int(ch_str)
        except ValueError:
            state["status"] = "error"; state["last_error"] = "Channel ID must be a number"
            bot_restart_event.wait(timeout=30); continue

        state["status"] = "connecting"; state["last_error"] = ""
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
                "quests_completed": 0, "animals_caught": 0, "activity_log": [],
                "team_ready": False, "battle_streak": 0, "battles_won": 0,
                "battles_lost": 0, "total_xp": 0, "team_animals": [],
                "weapons_equipped": 0, "_cooldowns": {},
                "_team_creating": False, "_team_add_ok": False, "_equipping": False,
                "_weapon_step": 0, "_weapon_slot": 1, "_weapon_channel": None,
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

            client.loop.create_task(startup(channel, client))
            client.loop.create_task(periodic_weapon_equip(channel))
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
            state["status"] = "error"; state["last_error"] = "Invalid token"
            state["token"]  = ""; bot_restart_event.wait(timeout=5)
        except Exception as e:
            state["status"] = "error"; state["last_error"] = str(e)
            log_activity(f"Bot crashed: {e}", "warn")
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
    d = request.get_json()
    code, ticket = (d.get("code") or "").strip(), session.get("mfa_ticket")
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
    d["daily_target"]    = DAILY_TARGET
    d["channel_id_set"]  = bool(CHANNEL_ID_STR)
    if state["start_time"]:
        elapsed = (datetime.now() - state["start_time"]).total_seconds()
        d["uptime_seconds"] = int(elapsed)
        d["daily_rate"]     = int(state["real_cowony"] / max(elapsed / 3600, 0.001) * 24)
        d["start_time"]     = state["start_time"].strftime("%Y-%m-%d %H:%M:%S")
    else:
        d["uptime_seconds"] = 0; d["daily_rate"] = 0; d["start_time"] = None
    d["last_command_time"] = (state["last_command_time"].strftime("%H:%M:%S")
                               if state["last_command_time"] else "—")
    for k in ("token", "_cooldowns", "_team_creating", "_team_add_ok", "_equipping",
               "_weapon_step", "_weapon_slot", "_weapon_channel"):
        d.pop(k, None)
    return jsonify(d)

@app.route("/ping")
def ping():
    return "PONG", 200

# ─── Discord auth ─────────────────────────────────────────────────────────────
DISCORD_API = "https://discord.com/api/v9"
REQ_HDR     = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def discord_login(email, pw):
    try:
        r = requests.post(f"{DISCORD_API}/auth/login", headers=REQ_HDR,
                          json={"login": email, "password": pw, "undelete": False, "captcha_key": None}, timeout=15)
        d = r.json()
        if "token" in d: return d["token"], None, None
        if d.get("mfa"):  return None, d.get("ticket"), "2fa"
        return None, None, d.get("message") or str(d.get("errors", "Login failed"))
    except Exception as e:
        return None, None, str(e)

def discord_mfa(ticket, code):
    try:
        r = requests.post(f"{DISCORD_API}/auth/mfa/totp", headers=REQ_HDR,
                          json={"code": code.replace(" ", ""), "ticket": ticket}, timeout=15)
        d = r.json()
        if "token" in d: return d["token"], None
        return None, d.get("message") or "Invalid 2FA code"
    except Exception as e:
        return None, str(e)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
