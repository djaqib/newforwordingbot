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
    filters
)

# ---------------------- LOGGING ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ---------------------- REDIS (Persistent Dedup) ----------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(REDIS_URL)

# ---------------------- SETTINGS ----------------------
PHOTO_ACCEPT = True          # toggle ON/OFF for photos
BATCH_TIMEOUT = 120          # seconds for video batch timeout
ALBUM_MIN_COUNT = 10         # send album when >= 10 videos

# ---------------------- ALBUM BUFFER + TIMER ----------------------
album_buffer = {}            # chat_id -> list of file paths
album_timer = {}             # chat_id -> asyncio.Task


# ---------------------- FLUSH ALBUM ----------------------
async def flush_album(chat_id: int, bot):
    """Send album for a chat_id and clear buffer."""
    if chat_id not in album_buffer or len(album_buffer[chat_id]) == 0:
        return

    media_group = []
    for fp in album_buffer[chat_id]:
        try:
            f = open(fp, "rb")
            media_group.append(InputMediaVideo(f))
        except Exception as e:
            logger.error(f"Error opening file {fp}: {e}")

    if media_group:
        await bot.send_media_group(chat_id=chat_id, media=media_group)

    # Cleanup files
    for fp in album_buffer[chat_id]:
        try:
            os.remove(fp)
        except Exception as e:
            logger.error(f"Error removing file {fp}: {e}")

    album_buffer[chat_id] = []


# ---------------------- START ALBUM TIMER ----------------------
async def start_album_timer(chat_id: int, bot):
    """Start a BATCH_TIMEOUT-second timer for album flush."""
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

    # ---------------------- AUTO DELETE ORIGINAL ----------------------
    try:
        await msg.delete()
    except Exception as e:
        logger.debug(f"Could not delete original message: {e}")

    # ---------------------- MEDIA DETECTION ----------------------
    file_unique_id = None
    file_obj = None
    media_type = None

    if msg.video:
        file_unique_id = msg.video.file_unique_id
        file_obj = msg.video
        media_type = "video"

    elif msg.photo:
        if not PHOTO_ACCEPT:
            await context.bot.send_message(chat_id, "Photo dropped (disabled).")
            return

        file_unique_id = msg.photo[-1].file_unique_id
        file_obj = msg.photo[-1]
        media_type = "photo"

    elif msg.document:
        file_unique_id = msg.document.file_unique_id
        file_obj = msg.document
        media_type = "document"

    else:
        # Text or other non-media → send clean text
        if msg.text:
            await context.bot.send_message(chat_id, msg.text)
        return

    # ---------------------- DEDUP CHECK (Redis) ----------------------
    if file_unique_id and r.sismember("dedup", file_unique_id):
        await context.bot.send_message(chat_id, "Duplicate ignored.")
        return

    if file_unique_id:
        r.sadd("dedup", file_unique_id)

    # ---------------------- DOWNLOAD FILE ----------------------
    file_path = f"temp_{file_unique_id}.bin" if file_unique_id else "temp.bin"
    try:
        await file_obj.get_file().download_to_drive(file_path)
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        await context.bot.send_message(chat_id, "Error processing file.")
        return

    # ---------------------- VIDEO: ALBUM MODE ----------------------
    if media_type == "video":
        if chat_id not in album_buffer:
            album_buffer[chat_id] = []

        album_buffer[chat_id].append(file_path)

        # If >= ALBUM_MIN_COUNT videos → send immediately
        if len(album_buffer[chat_id]) >= ALBUM_MIN_COUNT:
            await flush_album(chat_id, context.bot)

            # Cancel timer if running
            if chat_id in album_timer:
                album_timer[chat_id].cancel()
                album_timer.pop(chat_id, None)

            return

        # If < ALBUM_MIN_COUNT → start timer if not running
        if chat_id not in album_timer:
            album_timer[chat_id] = asyncio.create_task(
                start_album_timer(chat_id, context.bot)
            )

        return

    # ---------------------- NON-VIDEO MEDIA ----------------------
    try:
        if media_type == "photo":
            with open(file_path, "rb") as f:
                await context.bot.send_photo(chat_id, f)

        elif media_type == "document":
            with open(file_path, "rb") as f:
                await context.bot.send_document(chat_id, f)
    finally:
        try:
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Error removing file {file_path}: {e}")


# ---------------------- COMMAND: TOGGLE PHOTOS ----------------------
async def toggle_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT
    PHOTO_ACCEPT = not PHOTO_ACCEPT
    status = "ON" if PHOTO_ACCEPT else "OFF"
    await update.message.reply_text(f"Photo acceptance is now {status}.")


# ---------------------- MAIN APP ----------------------
async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = ApplicationBuilder().token(token).build()

    # Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, clean_forward))
    app.add_handler(CommandHandler("togglephotos", toggle_photos))

    logger.info("Bot starting...")
    await app.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
