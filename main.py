import asyncio
import os
import time
import yt_dlp
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pyrogram.errors import UserAlreadyParticipant, ChatAdminRequired
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioVideoPiped
from pytgcalls.types.stream import StreamAudioVideo

import config

# --- Database & Loggers ---
db_client = AsyncIOMotorClient(config.MONGO_URL)
db = db_client.telegram_music_bot

# --- Global Orchestration State ---
class QueueManager:
    def __init__(self):
        self.queues = {}
        self.current_track = {}
        self.volume = {}

    def get_queue(self, chat_id: int):
        if chat_id not in self.queues:
            self.queues[chat_id] = []
        return self.queues[chat_id]

    def add_to_queue(self, chat_id: int, track: dict):
        q = self.get_queue(chat_id)
        q.append(track)
        return len(q)

    def pop_queue(self, chat_id: int):
        q = self.get_queue(chat_id)
        if q:
            return q.pop(0)
        return None

    def clear_queue(self, chat_id: int):
        self.queues[chat_id] = []

    def set_volume(self, chat_id: int, vol: int):
        self.volume[chat_id] = vol

    def get_volume(self, chat_id: int):
        return self.volume.get(chat_id, 100)

music_queue = QueueManager()

# --- Initialize Pyrogram Clients ---
app = Client("MusicBot", api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
userbot = Client(config.SESSION_NAME, api_id=config.API_ID, api_hash=config.API_HASH)
call_py = PyTgCalls(userbot)

# --- Cooldown Implementation ---
cooldowns = {}

def check_cooldown(user_id: int, command: str, duration: int = 3) -> bool:
    key = f"{user_id}:{command}"
    now = time.time()
    if key in cooldowns and now - cooldowns[key] < duration:
        return False
    cooldowns[key] = now
    return True

# --- Downloader Helper (yt-dlp) ---
ydl_opts = {
    "format": "bestaudio/best",
    "outtmpl": "downloads/%(id)s.%(ext)s",
    "noplaylist": True,
    "quiet": True,
}

async def search_yt(query: str) -> dict:
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(f"ytsearch:{query}", download=False)
            )
            if not info or "entries" not in info or not info["entries"]:
                return {}
            entry = info["entries"][0]
            return {
                "title": entry.get("title", "Unknown"),
                "duration": entry.get("duration", 0),
                "url": entry.get("webpage_url"),
                "uploader": entry.get("uploader", "Unknown"),
                "thumbnail": entry.get("thumbnail"),
                "file_url": entry.get("url")
            }
        except Exception:
            return {}

# --- Formatting Helpers ---
def get_duration_str(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def get_now_playing_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data="cb_pause"),
            InlineKeyboardButton("▶ Resume", callback_data="cb_resume")
        ],
        [
            InlineKeyboardButton("⏭ Skip", callback_data="cb_skip"),
            InlineKeyboardButton("⏹ Stop", callback_data="cb_stop")
        ],
        [
            InlineKeyboardButton("📜 Queue", callback_data="cb_queue")
        ]
    ])

# --- Stream Transition Controller ---
async def start_stream(chat_id: int, track: dict):
    try:
        music_queue.current_track[chat_id] = track
        audio_stream = AudioVideoPiped(track["file_url"])
        await call_py.join_group_call(chat_id, audio_stream)
    except Exception as e:
        print(f"Error starting stream: {e}")

@call_py.on_stream_end()
async def stream_end_handler(client: PyTgCalls, chat_id: int, stream: StreamAudioVideo):
    next_track = music_queue.pop_queue(chat_id)
    if next_track:
        await start_stream(chat_id, next_track)
    else:
        try:
            del music_queue.current_track[chat_id]
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass

# --- Auth Decorator ---
async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id in config.SUDO_USERS:
        return True
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False

# --- User Commands ---

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Message, message: Message):
    user_id = message.from_user.id
    await db.users.update_one({"_id": user_id}, {"$set": {"username": message.from_user.username}}, upsert=True)
    await message.reply_text(
        "👋 **Welcome to Premium Music Streaming Bot!**\n\nAdd me to your group, elevate me to admin, and use `/play <song name>` to stream high-fidelity audio.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Help & Commands", callback_data="cb_help")]])
    )

@app.on_message(filters.command("play") & filters.group)
async def play_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if not check_cooldown(user_id, "play"):
        return await message.reply_text("⚠️ Please slow down! Active spam protection cooling system engaged.")

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.reply_text("❌ **Usage:** `/play <song name / youtube link>`")

    query = args[1]
    mystic = await message.reply_text("🔍 **Searching and resolving media sources...**")
    
    track = await search_yt(query)
    if not track:
        return await mystic.edit_text("❌ Failed to resolve track metadata. Try another keyword.")

    track["requested_by"] = f"@{message.from_user.username}" if message.from_user.username else message.from_user.mention
    await db.groups.update_one({"_id": chat_id}, {"$inc": {"total_plays": 1}}, upsert=True)

    # Check Active Streams
    if chat_id in music_queue.current_track:
        pos = music_queue.add_to_queue(chat_id, track)
        await mystic.delete()
        return await message.reply_text(f"📝 **Added to Queue at Position #{pos}:** `{track['title']}`")

    await mystic.edit_text("📥 **Joining Voice Chat & Initializing Audio Engine Pipeline...**")
    
    # Automate Assistant Userbot joining protocol
    try:
        await userbot.join_chat(chat_id)
    except UserAlreadyParticipant:
        pass
    except Exception:
        return await mystic.edit_text("❌ Assistant Userbot failed to enter the group. Ensure the group is public or userbot is invited.")

    await start_stream(chat_id, track)
    await mystic.delete()

    caption = (
        f"🎵 **Now Playing**\n"
        f"━━━━━━━━━━━━━\n"
        f"🎶 **Title:** {track['title']}\n"
        f"👤 **Artist:** {track['uploader']}\n"
        f"⏱ **Duration:** {get_duration_str(track['duration'])}\n"
        f"👥 **Requested By:** {track['requested_by']}\n"
        f"📊 **Volume:** {music_queue.get_volume(chat_id)}%\n"
        f"🔗 **Source:** [YouTube]({track['url']})\n"
        f"━━━━━━━━━━━━━"
    )

    if track["thumbnail"]:
        await message.reply_photo(photo=track["thumbnail"], caption=caption, reply_markup=get_now_playing_markup())
    else:
        await message.reply_text(caption, reply_markup=get_now_playing_markup(), disable_web_page_preview=False)

@app.on_message(filters.command("queue") & filters.group)
async def queue_cmd(client: Client, message: Message):
    chat_id = message.chat.id
    q = music_queue.get_queue(chat_id)
    if not q and chat_id not in music_queue.current_track:
        return await message.reply_text("📋 The play queue is currently empty.")
    
    out = "📊 **Current Play Queue Tracks:**\n"
    if chat_id in music_queue.current_track:
        out += f"▶️ **Now Playing:** {music_queue.current_track[chat_id]['title']}\n\n"
    
    for idx, t in enumerate(q, start=1):
        out += f"{idx}. `{t['title']}` | Requested by: {t['requested_by']}\n"
    
    await message.reply_text(out)

# --- Admin Commands ---

@app.on_message(filters.command(["pause", "resume", "skip", "stop", "clearqueue"]) & filters.group)
async def admin_controls(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    cmd = message.command[0]

    if not await is_admin(chat_id, user_id):
        return await message.reply_text("❌ Operational Permission Denied: Admin role required.")

    if cmd == "pause":
        await call_py.pause_stream(chat_id)
        await message.reply_text("⏸ Audio playback stream paused successfully.")
    elif cmd == "resume":
        await call_py.resume_stream(chat_id)
        await message.reply_text("▶️ Audio playback stream resumed successfully.")
    elif cmd == "skip":
        next_track = music_queue.pop_queue(chat_id)
        if next_track:
            await start_stream(chat_id, next_track)
            await message.reply_text(f"⏭ Skipped! Now streaming: `{next_track['title']}`")
        else:
            try:
                del music_queue.current_track[chat_id]
                await call_py.leave_group_call(chat_id)
            except Exception:
                pass
            await message.reply_text("⏭ Skipped! No remaining tracks in queue. Engine offline.")
    elif cmd == "stop":
        music_queue.clear_queue(chat_id)
        try:
            del music_queue.current_track[chat_id]
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass
        await message.reply_text("⏹ Audio engine killed. Voice chat pipeline disconnected.")
    elif cmd == "clearqueue":
        music_queue.clear_queue(chat_id)
        await message.reply_text("🗑 Play queue buffer completely purged.")

# --- Callback Keyboards Implementation ---
@app.on_callback_query(filters.regex(r"^cb_"))
async def callback_handler(client: Client, query: CallbackQuery):
    chat_id = query.message.chat.id
    user_id = query.from_user.id
    data = query.data.split("_")[1]

    if data == "help":
        return await query.answer(
            "Commands List:\n/play - Stream Audio\n/queue - Check Playlist\n/pause - Pause\n/resume - Resume\n/skip - Skip Track\n/stop - Destroy Connection", 
            show_alert=True
        )

    if not await is_admin(chat_id, user_id):
        return await query.answer("❌ Admin clearance required.", show_alert=True)

    if data == "pause":
        await call_py.pause_stream(chat_id)
        await query.answer("Stream Paused")
    elif data == "resume":
        await call_py.resume_stream(chat_id)
        await query.answer("Stream Resumed")
    elif data == "skip":
        next_track = music_queue.pop_queue(chat_id)
        if next_track:
            await start_stream(chat_id, next_track)
            await query.message.edit_text(f"⏭ Track skipped dynamically via panel. Now playing: `{next_track['title']}`")
        else:
            try:
                del music_queue.current_track[chat_id]
                await call_py.leave_group_call(chat_id)
            except Exception:
                pass
            await query.message.edit_text("⏭ Queue finished. Engine pipeline cleared.")
    elif data == "stop":
        music_queue.clear_queue(chat_id)
        try:
            del music_queue.current_track[chat_id]
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass
        await query.message.edit_text("⏹ Streaming halted & connection purged.")

# --- Initialization Loop ---
async def main():
    await app.start()
    await userbot.start()
    await call_py.start()
    print("🚀 Execution Successful. Premium High-Fidelity Music Bot is Online.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
