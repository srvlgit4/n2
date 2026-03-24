import asyncio
import os
import threading
from flask import Flask
from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
API_ID = 35846534
API_HASH = "129ba550b42323af0657fe6383ccd7f9"
# ❗ PASTE YOUR SESSION STRING HERE
SESSION_STRING = "BQIi-YYAeO-_XzMpN7fU06GyC4A9CzoMg4U7wqAh1DPrgmGW-eJWxs1rCgaBTcUPv7116t3qbl6mnOao6_ciyxCUIIGkfUskMcXFDWutV2xb9Yek3DzUsNT89b70exqOSqpN9gyxA6uSNmh4-P9DVTKCGi4U3HtqdGULk4t8pVmcPqa-4o0Pf1ygiqaPZgqgcIU2T47aKqzYxx08ei0F9PHTeLdAc2npe1FOwp7eDqd9yvvPWCWvBp7CIwJn9B_SMmP1t4otemf5Y-tiviaxK6P6MFpP3Hqw7ThMYMbnDHhDwI04yNEIlIpRxrS3QZFEbbvnvMnha1GADFJwz6w5IpDNVV089gAAAAG6jo_iAA" 

GROUP_NAMES = ["H1", "N3", "Y3"]

GROUPS = [] 
group_slots = {}
file_queue = asyncio.Queue()

# Initialize Client using the Session String
app = Client(
    "render_userbot",
    session_string=SESSION_STRING,
    api_id=API_ID,
    api_hash=API_HASH
)

# ==========================================
# 🌐 RENDER HEALTH CHECK (Required)
# ==========================================
server = Flask(__name__)
@server.route('/')
def ping(): return "Userbot is Live!", 200

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server.run(host='0.0.0.0', port=port)

# ==========================================
# 🤖 DISPATCH & MONITOR LOGIC
# ==========================================
@app.on_message(filters.document & filters.me & filters.chat("me"))
async def collect_files(client, message):
    await file_queue.put({'id': message.id, 'name': message.document.file_name})
    asyncio.create_task(dispatch_logic())

async def dispatch_logic():
    if not GROUPS or file_queue.empty(): return
    for gid in GROUPS:
        while group_slots.get(gid, 0) < 3 and not file_queue.empty():
            job = await file_queue.get()
            group_slots[gid] = group_slots.get(gid, 0) + 1 
            try:
                await app.forward_messages(chat_id=gid, from_chat_id="me", message_ids=job['id'])
                print(f"🚀 Sent '{job['name']}' to {gid}")
                await asyncio.sleep(60) # Safety Gap
            except Exception as e:
                print(f"❌ Error: {e}")
                group_slots[gid] -= 1

@app.on_message(filters.chat(GROUPS) & filters.text)
async def monitor_completion(client, message):
    if "पढ़ना पूरा हुआ" in (message.text or ""):
        chat_id = message.chat.id
        if chat_id in group_slots and group_slots[chat_id] > 0:
            group_slots[chat_id] -= 1
            await dispatch_logic()

async def start_userbot():
    # Start Web Server in background
    threading.Thread(target=run_server, daemon=True).start()
    await app.start()
    
    # Auto-resolve group IDs from names
    async for dialog in app.get_dialogs(limit=100):
        if dialog.chat.title in GROUP_NAMES:
            gid = dialog.chat.id
            if gid not in GROUPS:
                GROUPS.append(gid)
                group_slots[gid] = 0
                print(f"✅ Linked: {dialog.chat.title}")

    print("🚀 Userbot is 24/7 on Render!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_userbot())
