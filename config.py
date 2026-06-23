import os

# এখানে শুধু আপনার আসল আইডি, হ্যাশ আর বটের টোকেন বসিয়ে দিন
API_ID = int(os.getenv("API_ID", "35297209"))
API_HASH = os.getenv("API_HASH", "06e705c80aac7add5524f4a1cf4324a3")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8740721397:AAHAy2HLLE2EaftTlai0Y5HE53DYxHzXBUc")
SESSION_NAME = os.getenv("SESSION_NAME", "MusicUserBotSession")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@jihad_music_bot")
SUDO_USERS = [int(x) for x in os.getenv("SUDO_USERS", "8179643564").split()]
