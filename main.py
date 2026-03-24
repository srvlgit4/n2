import asyncio
import os
import threading
from flask import Flask
from pyrogram import Client, filters, idle

# ==========================================
# 🌐 RENDER PORT BINDING (REQUIRED)
# ==========================================
# Render needs a web server running to keep the service alive.
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Userbot is alive and running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
API_ID = 35846534
API_HASH = "129ba550b42323af0657fe6383ccd7f9"
SESSION_STRING = "BQIi-YYAeO-_XzMpN7fU06GyC4A9CzoMg4U7wqAh1DPrgmGW-eJWxs1rCgaBTcUPv7116t3qbl6mnOao6_ciyxCUIIGkfUskMcXFDWutV2xb9Yek3DzUsNT89b70exqOSqpN9gyxA6uSNmh4-P9DVTKCGi4U3HtqdGULk4t8pVmcPqa-4o0Pf1ygiqaPZgqgcIU2T47aKqzYxx08ei0F9PHTeLdAc2npe1FOwp7eDqd9yvvPWCWvBp7CIwJn9B_SMmP1t4otemf5Y-tiviaxK6P6MFpP3Hqw7ThMYMbnDHhDwI04yNEIlIpRxrS3QZFEbbvnvMnha1GADFJwz6w5IpDNVV089gAAAAG6jo_iAA" 

GROUP_NAMES = ["H1", "N3", "Y3","Y4","Y1"]

GROUPS = [] 
group_slots = {}
file_queue = asyncio.Queue()

app = Client(
    "render_session",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH,
    in_memory=True
)

# ==========================================
# 🤖 BOT LOGIC
# ==========================================
@app.on_message(filters.document & filters.me & filters.chat("me"))
async def collect_files(client, message):
    await file_queue.put({'id': message.id, 'name': message.document.file_name})
    print(f"📥 Queued: {message.document.file_name}")
    asyncio.create_task(dispatch_logic())

async def dispatch_logic():
    if not GROUPS or file_queue.empty(): return
    for gid in GROUPS:
        while group_slots.get(gid, 0) < 3 and not file_queue.empty():
            job = await file_queue.get()
            group_slots[gid] = group_slots.get(gid, 0) + 1 
            try:
                await app.forward_messages(chat_id=gid, from_chat_id="me", message_ids=job['id'])
                print(f"🚀 Sent '{job['name']}' to {gid}. Slots: {group_slots[gid]}/3")
                await asyncio.sleep(60) # Safety Delay
            except Exception as e:
                print(f"❌ Error in {gid}: {e}")
                group_slots[gid] -= 1

@app.on_message(filters.chat(GROUPS) & filters.text)
async def monitor_completion(client, message):
    if "पढ़ना पूरा हुआ" in (message.text or ""):
        chat_id = message.chat.id
        if chat_id in group_slots and group_slots[chat_id] > 0:
            group_slots[chat_id] -= 1
            print(f"✅ Slot freed in {chat_id}")
            await dispatch_logic()

async def start_userbot():
    print("🤖 Starting Userbot...")
    await app.start()
    
    # Discovery
    async for dialog in app.get_dialogs(limit=100):
        if dialog.chat.title in GROUP_NAMES:
            gid = dialog.chat.id
            if gid not in GROUPS:
                GROUPS.append(gid)
                group_slots[gid] = 0
                print(f"✅ Linked: {dialog.chat.title}")

    print("🚀 LIVE! Userbot is monitoring groups.")
    await idle()
    await app.stop()

# ==========================================
# 🚀 EXECUTION (PYTHON 3.12+ FIX)
# ==========================================
if __name__ == "__main__":
    # 1. Start Flask in a background thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # 2. Setup the asyncio loop correctly for Render
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(start_userbot())
    except KeyboardInterrupt:
        print("Bot Stopped.")
