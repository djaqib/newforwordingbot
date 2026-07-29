import logging
import os
import redis
import asyncio
from telegram import Update, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# ---------------------- LOGGING ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------- YOUR CHAT ID (for error reports) ----------------------
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # set in Railway variables

# ---------------------- REDIS ----------------------
REDIS_URL = os.getenv("REDIS_URL")
r = redis.Redis.from_url(REDIS_URL)

# ---------------------- SETTINGS ----------------------
PHOTO_ACCEPT = True
BATCH_TIMEOUT = 120
ALBUM_MIN_COUNT = 10

album_buffer: dict[int, list[str]] = {}
album_timer: dict[int, asyncio.Task] = {}

# ---------------------- SAFE SEND WRAPPER ----------------------
async def safe_send(bot_method, *args, **kwargs):
    try:
        return await bot_method(*args, **kwargs)
    except Exception as e:
        logger.error(f"Telegram API error: {e}")
        if ADMIN_CHAT_ID:
            await bot_method.__self__.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ Telegram API error:\n{e}"
            )
        return None

# ---------------------- FLUSH ALBUM ----------------------
async def flush_album(chat_id: int, bot):
    if chat_id not in album_buffer or len(album_buffer[chat_id]) == 0:
        return

    logger.info(f"Flushing album with {len(album_buffer[chat_id])} videos")

    media_group = [InputMediaVideo(fid) for fid in album_buffer[chat_id]]

    await safe_send(bot.send_media_group, chat_id, media_group)

    album_buffer[chat_id] = []

# ---------------------- ALBUM TIMER ----------------------
async def start_album_timer(chat_id: int, bot):
    try:
        await asyncio.sleep(BATCH_TIMEOUT)
        await flush_album(chat_id, bot)
    finally:
        album_timer.pop(chat_id, None)

# ---------------------- MAIN HANDLER ----------------------
async def clean_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT

    msg = update.effective_message
    chat_id = msg.chat_id

    # Delete original
    try:
        await msg.delete()
    except Exception:
        pass

    file_unique_id = None
    file_id = None
    media_type = None

    # ---------------------- VIDEO DETECTION ----------------------
    if msg.video:
        file_unique_id = msg.video.file_unique_id
        file_id = msg.video.file_id
        media_type = "video"
        logger.info(f"Detected VIDEO: file_id={file_id}")

    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video"):
        file_unique_id = msg.document.file_unique_id
        file_id = msg.document.file_id
        media_type = "video"
        logger.info(f"Detected VIDEO-DOC (mime video/*): file_id={file_id}")

    elif msg.document and msg.document.file_name and msg.document.file_name.lower().endswith(".mp4"):
        file_unique_id = msg.document.file_unique_id
        file_id = msg.document.file_id
        media_type = "video"
        logger.info(f"Detected VIDEO-DOC (.mp4 filename): file_id={file_id}")

    # ---------------------- PHOTO ----------------------
    elif msg.photo:
        if not PHOTO_ACCEPT:
            await safe_send(context.bot.send_message, chat_id, "Photo dropped (disabled).")
            return

        file_unique_id = msg.photo[-1].file_unique_id
        file_id = msg.photo[-1].file_id
        media_type = "photo"
        logger.info(f"Detected PHOTO: file_id={file_id}")

    # ---------------------- DOCUMENT ----------------------
    elif msg.document:
        file_unique_id = msg.document.file_unique_id
        file_id = msg.document.file_id
        media_type = "document"
        logger.info(f"Detected DOCUMENT: file_id={file_id}")

    else:
        if msg.text:
            await safe_send(context.bot.send_message, chat_id, msg.text)
        return

    # ---------------------- DEDUP ----------------------
    if file_unique_id and r.sismember("dedup", file_unique_id):
        await safe_send(context.bot.send_message, chat_id, "Duplicate ignored.")
        return

    if file_unique_id:
        r.sadd("dedup", file_unique_id)

    # ---------------------- VIDEO ALBUM MODE ----------------------
    if media_type == "video":
        if chat_id not in album_buffer:
            album_buffer[chat_id] = []

        album_buffer[chat_id].append(file_id)
        logger.info(f"Added to album: {file_id}. Total now: {len(album_buffer[chat_id])}")

        if len(album_buffer[chat_id]) >= ALBUM_MIN_COUNT:
            await flush_album(chat_id, context.bot)

            if chat_id in album_timer:
                album_timer[chat_id].cancel()
                album_timer.pop(chat_id, None)

            return

        if chat_id not in album_timer:
            album_timer[chat_id] = asyncio.create_task(
                start_album_timer(chat_id, context.bot)
            )

        return

    # ---------------------- NON-VIDEO MEDIA ----------------------
    if media_type == "photo":
        await safe_send(context.bot.send_photo, chat_id, file_id)

    elif media_type == "document":
        await safe_send(context.bot.send_document, chat_id, file_id)

# ---------------------- COMMAND: TOGGLE PHOTOS ----------------------
async def toggle_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT
    PHOTO_ACCEPT = not PHOTO_ACCEPT
    status = "ON" if PHOTO_ACCEPT else "OFF"
    await safe_send(update.message.reply_text, f"Photo acceptance is now {status}.")

# ---------------------- DEBUG COMMAND ----------------------
async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update.message.reply_text, "Debug mode active. Check Railway logs.")

# ---------------------- MAIN APP ----------------------
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, clean_forward))
    app.add_handler(CommandHandler("togglephotos", toggle_photos))
    app.add_handler(CommandHandler("debug", debug))

    logger.info("Bot starting...")

    await app.initialize()
    await app.start()

    # Startup message
    if ADMIN_CHAT_ID:
        await app.bot.send_message(ADMIN_CHAT_ID, "🚀 Bot is now running!")

    await app.run_polling()
    await app.stop()
    await app.shutdown()

# ---------------------- ENTRYPOINT ----------------------
if __name__ == "__main__":
    asyncio.run(main(), debug=False)
