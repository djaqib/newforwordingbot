import os
import logging
import asyncio
import random
import time
from telegram import Update, InputMediaVideo, BotCommand
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# -----------------------------
# Admin IDs (ONLY these users can use bot)
# -----------------------------
ADMIN_IDS = [
    7599601301,  # KR
    8637601933,
    8976017144,
]

# -----------------------------
# Global State
# -----------------------------
video_cache = set()
photo_approval_mode = False
batch_count = 0
last_video_time = 0
FLUSH_TIMEOUT = 60  # seconds


# -----------------------------
# Admin-only decorator
# -----------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("This bot is private. Access denied.")
            return
        return await func(update, context)
    return wrapper


# -----------------------------
# Commands (Admins only)
# -----------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot ready.\nYour Telegram ID is: {update.effective_user.id}"
    )


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Available commands:\n"
        "/start – Show your Telegram ID\n"
        "/help – Show this help menu\n"
        "/settings – Show bot settings\n"
        "/admin_commands – Show admin commands\n"
        "/toggle_photo_mode – Toggle photo approval mode\n"
        "/approve – Approve pending photo\n"
        "/reject – Reject pending photo\n"
        "/flush – Flush remaining videos\n"
    )
    await update.message.reply_text(text)


@admin_only
async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Settings:\n"
        f"Photo approval mode: {'ON' if photo_approval_mode else 'OFF'}\n"
        f"Batch timeout: {FLUSH_TIMEOUT} seconds\n"
        "Album size: 10 videos\n"
    )
    await update.message.reply_text(text)


@admin_only
async def admin_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands = [
        "/toggle_photo_mode – Enable/disable photo approval mode",
        "/approve – Approve pending photo",
        "/reject – Reject pending photo",
        "/flush – Manually flush remaining videos",
        "/admin_commands – Show admin commands"
    ]
    text = "Admin commands:\n\n" + "\n".join(commands)
    await update.message.reply_text(text)


@admin_only
async def toggle_photo_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode
    photo_approval_mode = not photo_approval_mode
    status = "ON" if photo_approval_mode else "OFF"
    await update.message.reply_text(f"Photo approval mode is now {status}")


@admin_only
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = context.user_data.get("pending_photo")
    if not file_id:
        await update.message.reply_text("No pending photo.")
        return

    await update.message.reply_photo(file_id)
    context.user_data["pending_photo"] = None


@admin_only
async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pending_photo"] = None
    await update.message.reply_text("Photo rejected.")


@admin_only
async def flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        await update.message.reply_text("No pending videos.")
        return

    await send_album(update, context)
    await update.message.reply_text("Flushed remaining videos.")


# -----------------------------
# Photo Handler (Admins only)
# -----------------------------
@admin_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global photo_approval_mode

    file_id = update.message.photo[-1].file_id

    if not photo_approval_mode:
        await update.message.reply_photo(file_id)
        return

    context.user_data["pending_photo"] = file_id
    await update.message.reply_text("Photo received. Use /approve or /reject.")


# -----------------------------
# Video Handler (Admins only)
# -----------------------------
@admin_only
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_video_time, batch_count

    message = update.message

    try:
        await message.delete()
    except:
        pass

    if not message.video:
        return

    file_id = message.video.file_id

    if file_id in video_cache:
        return

    video_cache.add(file_id)

    last_video_time = time.time()
    batch_count += 1

    await update.message.reply_text(f"Received {batch_count} videos…")

    if "album" not in context.user_data:
        context.user_data["album"] = []

    context.user_data["album"].append(file_id)

    if len(context.user_data["album"]) >= 10:
        await send_album(update, context)


# -----------------------------
# Album Sending
# -----------------------------
async def send_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    album = context.user_data.get("album", [])
    if not album:
        return

    delay = random.uniform(2, 3)
    await asyncio.sleep(delay)

    media_group = [InputMediaVideo(media=fid) for fid in album]

    await update.message.reply_media_group(media_group)
    context.user_data["album"] = []


async def send_album_to_chat(app, chat_id, album):
    media_group = [InputMediaVideo(media=fid) for fid in album]
    await app.bot.send_media_group(chat_id, media_group)


# -----------------------------
# Batch Watcher (JobQueue)
# -----------------------------
async def batch_watcher(app):
    global last_video_time

    now = time.time()

    for chat_id, data in list(app.chat_data.items()):
        album = data.get("album", [])
        if album and now - last_video_time >= FLUSH_TIMEOUT:
            await app.bot.send_message(chat_id, "Batch ended. Sending remaining videos…")
            await send_album_to_chat(app, chat_id, album)
            data["album"] = []


# -----------------------------
# post_init (Fix for async startup)
# -----------------------------
async def post_init(app):
    commands = [
        BotCommand("start", "Show your Telegram ID"),
        BotCommand("help", "Show help menu"),
        BotCommand("settings", "Show bot settings"),
        BotCommand("admin_commands", "Show admin commands"),
        BotCommand("toggle_photo_mode", "Toggle photo approval mode"),
        BotCommand("approve", "Approve pending photo"),
        BotCommand("reject", "Reject pending photo"),
        BotCommand("flush", "Flush remaining videos"),
    ]

    await app.bot.set_my_commands(commands)

    app.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(batch_watcher(app)),
        interval=5,
        first=5
    )

    logger.info("Post-init tasks completed.")


# -----------------------------
# Main (Railway-safe)
# -----------------------------
def main():
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings))
    app.add_handler(CommandHandler("admin_commands", admin_commands))
    app.add_handler(CommandHandler("toggle_photo_mode", toggle_photo_mode))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("flush", flush))

    # Media handlers
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Run async startup tasks AFTER loop starts
    app.post_init = post_init

    logger.info("Bot is now polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
