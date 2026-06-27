import discord
import asyncio
import os
import random
from datetime import datetime
from flask import Flask

TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_TOKEN_HERE")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "123456789"))

app = Flask(__name__)

@app.route('/')
def home():
    return "OWO Auto-Farmer 24/7 Running!", 200

@app.route('/ping')
def ping():
    return "PONG", 200

def run_webserver():
    app.run(host='0.0.0.0', port=5000)

class AutoFarmer:
    def __init__(self):
        self.client = discord.Client()
        
        self.commands = [
            {"cmd": "owo battle", "delay": 60, "name": "BATTLE"},
            {"cmd": "owo pray", "delay": 300, "name": "PRAY"},
            {"cmd": "owo work", "delay": 900, "name": "WORK"},
            {"cmd": "owo hunt", "delay": 1800, "name": "HUNT"},
            {"cmd": "owo fish", "delay": 1800, "name": "FISH"},
            {"cmd": "owo crime", "delay": 1800, "name": "CRIME"},
            {"cmd": "owo rob", "delay": 1800, "name": "ROB"},
            {"cmd": "owo slots 100", "delay": 600, "name": "SLOTS"},
            {"cmd": "owo daily", "delay": 43200, "name": "DAILY"},
            {"cmd": "owo weekly", "delay": 604800, "name": "WEEKLY"},
        ]
        
        self.total_commands = 0
        self.command_stats = {}
        self.start_time = datetime.now()
        
        @self.client.event
        async def on_ready():
            print("=" * 70)
            print("OWO 24/7 AUTO-FARMER")
            print("=" * 70)
            print(f"Logged in as: {self.client.user}")
            
            channel = self.client.get_channel(CHANNEL_ID)
            if not channel:
                print("Channel not found!")
                return
            
            print(f"Channel: #{channel.name}")
            print("=" * 70)
            print("FULLY AUTOMATIC - NO MANUAL WORK")
            print("=" * 70)
            print("ACTIVE COMMANDS:")
            print("   owo battle  -> Every 1 min")
            print("   owo pray    -> Every 5 min")
            print("   owo work    -> Every 15 min")
            print("   owo hunt    -> Every 30 min")
            print("   owo fish    -> Every 30 min")
            print("   owo crime   -> Every 30 min")
            print("   owo rob     -> Every 30 min")
            print("   owo slots   -> Every 10 min")
            print("   owo daily   -> Every 12 hours")
            print("   owo weekly  -> Every 7 days")
            print("=" * 70)
            print("FARMING STARTED...")
            print("=" * 70)
            
            for cmd_info in self.commands:
                self.client.loop.create_task(
                    self.run_command(channel, cmd_info)
                )
            
            self.client.loop.create_task(self.show_stats(channel))
    
    async def run_command(self, channel, cmd_info):
        cmd = cmd_info["cmd"]
        delay = cmd_info["delay"]
        name = cmd_info["name"]
        
        await asyncio.sleep(random.randint(0, 30))
        
        while True:
            try:
                await channel.send(cmd)
                self.total_commands += 1
                
                if name not in self.command_stats:
                    self.command_stats[name] = 0
                self.command_stats[name] += 1
                
                print(f"[OK] {name} at {datetime.now().strftime('%H:%M:%S')}")
                await asyncio.sleep(2)
                
            except Exception as e:
                print(f"[ERROR] {name}: {e}")
            
            actual_delay = delay + random.randint(-30, 30)
            await asyncio.sleep(max(10, actual_delay))
    
    async def show_stats(self, channel):
        await asyncio.sleep(60)
        
        while True:
            runtime = datetime.now() - self.start_time
            hours = runtime.total_seconds() / 3600
            
            avg_earning = 25
            estimated = self.total_commands * avg_earning
            daily_earning = int(estimated / hours * 24) if hours > 0 else 0
            
            print("=" * 70)
            print(f"FARMING STATS at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 70)
            print(f"Runtime: {int(hours)} hours")
            print(f"Total Commands: {self.total_commands}")
            print(f"Estimated Earnings: ~{estimated:,} cowony")
            print(f"Per Day: ~{daily_earning:,} cowony")
            print("-" * 40)
            print("Command Breakdown:")
            
            for name, count in sorted(self.command_stats.items(), key=lambda x: x[1], reverse=True):
                print(f"   {name}: {count} times")
            
            print("=" * 70)
            await asyncio.sleep(1800)
    
    def run(self):
        try:
            self.client.run(TOKEN, bot=False)
        except discord.LoginFailure:
            print("ERROR: Invalid Token! Check DISCORD_TOKEN env var.")
        except Exception as e:
            print(f"ERROR: {e}")

if __name__ == "__main__":
    print("=" * 70)
    print("OWO AUTO-FARMER")
    print("=" * 70)
    
    if TOKEN == "YOUR_TOKEN_HERE":
        print("ERROR: Set DISCORD_TOKEN environment variable first!")
        print("Web server will still start for uptime monitoring.")
    
    import threading
    web_thread = threading.Thread(target=run_webserver, daemon=True)
    web_thread.start()
    print("Web server started on port 5000")
    
    if TOKEN != "YOUR_TOKEN_HERE":
        farmer = AutoFarmer()
        farmer.run()
    else:
        import time
        while True:
            time.sleep(60)
