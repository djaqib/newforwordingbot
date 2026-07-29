import logging
import os
import redis
import asyncio
from telegram import Update, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters
)

# ---------------------- LOGGING ----------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ---------------------- REDIS (Persistent Dedup) ----------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(REDIS_URL)

# ---------------------- SETTINGS ----------------------
PHOTO_ACCEPT = True  # toggle ON/OFF
BATCH_TIMEOUT = 120  # seconds

# ---------------------- ALBUM BUFFER + TIMER ----------------------
album_buffer = {}      # chat_id → list of file paths
album_timer = {}       # chat_id → asyncio.Task


# ---------------------- SEND ALBUM ----------------------
async def flush_album(chat_id, bot):
    """Send album for a chat_id and clear buffer."""
    if chat_id not in album_buffer or len(album_buffer[chat_id]) == 0:
        return

    media_group = [
        InputMediaVideo(open(fp, "rb"))
        for fp in album_buffer[chat_id]
    ]

    await bot.send_media_group(chat_id, media_group)

    # Cleanup files
    for fp in album_buffer[chat_id]:
        try:
            os.remove(fp)
        except:
            pass

    album_buffer[chat_id] = []


# ---------------------- START TIMER ----------------------
async def start_album_timer(chat_id, bot):
    """Start a 120-second timer for album flush."""
    await asyncio.sleep(BATCH_TIMEOUT)

    # Timer expired → flush album
    await flush_album(chat_id, bot)

    # Remove timer reference
    album_timer.pop(chat_id, None)


# ---------------------- CLEAN FORWARD ----------------------
async def clean_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT

    msg = update.effective_message
    chat_id = msg.chat_id

    # ---------------------- AUTO DELETE ORIGINAL ----------------------
    try:
        await msg.delete()
    except:
        pass

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
        if msg.text:
            await context.bot.send_message(chat_id, msg.text)
        return

    # ---------------------- DEDUP CHECK (Redis) ----------------------
    if r.sismember("dedup", file_unique_id):
        await context.bot.send_message(chat_id, "Duplicate ignored.")
        return

    r.sadd("dedup", file_unique_id)

    # ---------------------- DOWNLOAD FILE ----------------------
    file_path = f"temp_{file_unique_id}.bin"
    await file_obj.get_file().download_to_drive(file_path)

    # ---------------------- ALBUM MODE FOR VIDEOS ----------------------
    if media_type == "video":
        if chat_id not in album_buffer:
            album_buffer[chat_id] = []

        album_buffer[chat_id].append(file_path)

        # If 10+ videos → send immediately
        if len(album_buffer[chat_id]) >= 10:
            await flush_album(chat_id, context.bot)

            # Cancel timer if running
            if chat_id in album_timer:
                album_timer[chat_id].cancel()
                album_timer.pop(chat_id, None)

            return

        # If <10 videos → start timer if not running
        if chat_id not in album_timer:
            album_timer[chat_id] = asyncio.create_task(
                start_album_timer(chat_id, context.bot)
            )

        return

    # ---------------------- NON-VIDEO MEDIA ----------------------
    if media_type == "photo":
        await context.bot.send_photo(chat_id, open(file_path, "rb"))

    elif media_type == "document":
        await context.bot.send_document(chat_id, open(file_path, "rb"))

    try:
        os.remove(file_path)
    except:
        pass


# ---------------------- COMMANDS ----------------------
async def toggle_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PHOTO_ACCEPT
    PHOTO_ACCEPT = not PHOTO_ACCEPT
    status = "ON" if PHOTO_ACCEPT else "OFF"
    await update.message.reply_text(f"Photo acceptance is now {status}.")


# ---------------------- MAIN ----------------------
async def main():
    TOKEN = os.getenv("BOT_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, clean_forward))
    app.add_handler(MessageHandler(filters.Command("togglephotos"), toggle_photos))

    print("Bot running...")
    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
