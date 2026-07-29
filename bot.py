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

# ---------------------- REDIS (Persistent Dedup) ----------------------
REDIS_URL = os.getenv("REDIS_URL")
r = redis.Redis.from_url(REDIS_URL)

# ---------------------- SETTINGS ----------------------
PHOTO_ACCEPT = True
BATCH_TIMEOUT = 120
ALBUM_MIN_COUNT = 10

# ---------------------- ALBUM BUFFER + TIMER ----------------------
album_buffer: dict[int, list[str]] = {}      # chat_id → list of file_ids
album_timer: dict[int, asyncio.Task] = {}    # chat_id → timer task


# ---------------------- FLUSH ALBUM ----------------------
async def flush_album(chat_id: int, bot):
    """Send album using file_id only."""
    if chat_id not in album_buffer or len(album_buffer[chat_id]) == 0:
        return

    media_group = [InputMediaVideo(fid) for fid in album_buffer[chat_id]]

    await bot.send_media_group(chat_id=chat_id, media=media_group)

    album_buffer[chat_id] = []


# ---------------------- START ALBUM TIMER ----------------------
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

    # Detect media
    file_unique_id = None
    file_id = None
    media_type = None

    # Normal Telegram video
    if msg.video:
        file_unique_id = msg.video.file_unique_id
        file_id = msg.video.file_id
        media_type = "video"

    # iPhone MP4 or forwarded MP4 (video sent as document)
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video"):
        file_unique_id = msg.document.file_unique_id
        file_id = msg.document.file_id
        media_type = "video"

    # Photo
    elif msg.photo:
        if not PHOTO_ACCEPT:
            await context.bot.send_message(chat_id, "Photo dropped (disabled).")
            return

        file_unique_id = msg.photo[-1].file_unique_id
        file_id = msg.photo[-1].file_id
        media_type = "photo"

    # Document (non-video)
    elif msg.document:
        file_unique_id = msg.document.file_unique_id
        file_id = msg.document.file_id
        media_type = "document"

    else:
        if msg.text:
            await context.bot.send_message(chat_id, msg.text)
        return

    # Dedup
    if file_unique_id and r.sismember("dedup", file_unique_id):
        await context.bot.send_message(chat_id, "Duplicate ignored.")
        return

    if file_unique_id:
        r.sadd("dedup", file_unique_id)

    # ---------------------- VIDEO ALBUM MODE (file_id only) ----------------------
    if media_type == "video":
        if chat_id not in album_buffer:
            album_buffer[chat_id] = []

        album_buffer[chat_id].append(file_id)

        # If >= ALBUM_MIN_COUNT → send immediately
        if len(album_buffer[chat_id]) >= ALBUM_MIN_COUNT:
            await flush_album(chat_id, context.bot)

            if chat_id in album_timer:
                album_timer[chat_id].cancel()
                album_timer.pop(chat_id, None)

            return

        # Start timer if not running
        if chat_id not in album_timer:
            album_timer[chat_id] = asyncio.create_task(
                start_album_timer(chat_id, context.bot)
            )

        return

    # ---------------------- NON-VIDEO MEDIA (file_id only) ----------------------
    if media_type == "photo":
        await context.bot.send_photo(chat_id, file_id)

    elif media_type == "document":
        await context.bot.send_document(chat_id, file_id)


# ---------------------- COMMAND: TOGGLE PHOTOS ----------------------
async def toggle_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT
    PHOTO_ACCEPT = not PHOTO_ACCEPT
    status = "ON" if PHOTO_ACCEPT else "OFF"
    await update.message.reply_text(f"Photo acceptance is now {status}.")


# ---------------------- MAIN APP (PTB v20) ----------------------
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, clean_forward))
    app.add_handler(CommandHandler("togglephotos", toggle_photos))

    logger.info("Bot starting...")

    await app.initialize()
    await app.start()
    await app.run_polling()   # <-- Correct PTB v20 lifecycle
    await app.stop()
    await app.shutdown()


# ---------------------- ENTRYPOINT ----------------------
if __name__ == "__main__":
    asyncio.run(main(), debug=False)
