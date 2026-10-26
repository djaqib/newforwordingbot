import asyncio
import functools
import logging
import os
import random
import sqlite3
import time

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# -----------------------------
# Config
# -----------------------------
# Set DATA_DIR to a persistent volume (e.g. /data) so dedup history and
# settings survive redeploys. Without a volume they reset on every deploy.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

# Admin IDs: set ADMIN_IDS="123,456" as an env var, or edit the fallback below.
_env_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x) for x in _env_ids.replace(" ", "").split(",") if x.isdigit()} or {
    7599601301,  # KR
    8637601933,
    8976017144,
    7467197752,
}

# Per-user setting defaults (the type of the default decides how it's parsed)
DEFAULTS = {
    "delete_original": True,   # delete the message you sent after processing
    "dedup": True,             # skip media already seen
    "strict_dedup": False,     # also match re-uploads by size/duration/resolution
    "album_size": 10,          # 1-10 items per album
    "timeout": 60,             # seconds of inactivity before auto-send
    "shuffle": False,          # shuffle order of the batch before sending
    "protect": False,          # protect_content: block forwarding/saving of output
    "photo_approval": False,   # hold photos until /approve or /reject
}
TOGGLES = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
RANGES = {"album_size": (1, 10, 1), "timeout": (15, 600, 15)}  # min, max, step

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# -----------------------------
# Storage (SQLite: settings, dedup history, counters)
# -----------------------------
class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER, key TEXT, value TEXT,
                PRIMARY KEY (user_id, key));
            CREATE TABLE IF NOT EXISTS seen (
                user_id INTEGER, key TEXT, ts REAL,
                PRIMARY KEY (user_id, key));
            CREATE TABLE IF NOT EXISTS counters (
                user_id INTEGER, key TEXT, value INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, key));
            """
        )
        self.db.commit()

    # settings
    def get(self, uid, key):
        default = DEFAULTS[key]
        row = self.db.execute(
            "SELECT value FROM settings WHERE user_id=? AND key=?", (uid, key)
        ).fetchone()
        if row is None:
            return default
        return row[0] == "1" if isinstance(default, bool) else int(row[0])

    def set(self, uid, key, value):
        raw = "1" if value is True else "0" if value is False else str(value)
        self.db.execute(
            "INSERT INTO settings VALUES (?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (uid, key, raw),
        )
        self.db.commit()

    # dedup
    def is_dup(self, uid, uniq, fingerprint, strict):
        keys = ["u:" + uniq]
        if strict:
            keys.append("f:" + fingerprint)
        marks = ",".join("?" * len(keys))
        row = self.db.execute(
            f"SELECT 1 FROM seen WHERE user_id=? AND key IN ({marks}) LIMIT 1",
            (uid, *keys),
        ).fetchone()
        return row is not None

    def mark_seen(self, uid, uniq, fingerprint):
        now = time.time()
        self.db.executemany(
            "INSERT OR IGNORE INTO seen VALUES (?,?,?)",
            [(uid, "u:" + uniq, now), (uid, "f:" + fingerprint, now)],
        )
        self.db.commit()

    def seen_count(self, uid):
        return self.db.execute(
            "SELECT COUNT(*) FROM seen WHERE user_id=? AND key LIKE 'u:%'", (uid,)
        ).fetchone()[0]

    def clear_seen(self, uid):
        self.db.execute("DELETE FROM seen WHERE user_id=?", (uid,))
        self.db.commit()

    # counters
    def incr(self, uid, key, n=1):
        self.db.execute(
            "INSERT INTO counters VALUES (?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=value+excluded.value",
            (uid, key, n),
        )
        self.db.commit()

    def counters(self, uid):
        rows = self.db.execute(
            "SELECT key, value FROM counters WHERE user_id=?", (uid,)
        ).fetchall()
        return dict(rows)


store = Store(os.path.join(DATA_DIR, "bot.db"))


# -----------------------------
# Per-user batch queue (in memory)
# -----------------------------
class UserQueue:
    def __init__(self):
        self.items = []          # list of (kind, file_id)
        self.chat_id = None
        self.timer = None        # asyncio TimerHandle for auto-flush
        self.lock = asyncio.Lock()
        self.skipped = 0         # duplicates skipped in the current batch
        self.status_id = None    # message id of the live status message
        self.last_status = 0.0


QUEUES = {}
BG_TASKS = set()


def queue_for(uid):
    return QUEUES.setdefault(uid, UserQueue())


def spawn(coro):
    task = asyncio.create_task(coro)
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)
    return task


# -----------------------------
# Helpers
# -----------------------------
def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            if update.callback_query:
                await update.callback_query.answer("Access denied.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("This bot is private. Access denied.")
            return
        return await func(update, context)
    return wrapper


async def with_retry(fn, tries=3):
    for attempt in range(tries):
        try:
            return await fn()
        except RetryAfter as e:
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            if attempt == tries - 1:
                raise
            logger.warning("Flood control, sleeping %.0fs", wait + 1)
            await asyncio.sleep(wait + 1)


def extract_media(msg):
    """Return (kind, file_id, file_unique_id, fingerprint) or None."""
    if msg.video:
        v = msg.video
        return "video", v.file_id, v.file_unique_id, f"v{v.duration}-{v.file_size}-{v.width}x{v.height}"
    if msg.document and (msg.document.mime_type or "").startswith("video/"):
        d = msg.document
        return "video", d.file_id, d.file_unique_id, f"d{d.file_size}-{d.mime_type}"
    if msg.photo:
        p = msg.photo[-1]
        return "photo", p.file_id, p.file_unique_id, f"p{p.width}x{p.height}-{p.file_size}"
    return None


async def try_delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


# -----------------------------
# Sending
# -----------------------------
async def send_one(bot, chat_id, item, protect):
    kind, fid = item
    if kind == "photo":
        return await with_retry(lambda: bot.send_photo(chat_id, fid, protect_content=protect))
    try:
        return await with_retry(lambda: bot.send_video(chat_id, fid, protect_content=protect))
    except BadRequest:
        # videos sent as "files" can need send_document
        return await with_retry(lambda: bot.send_document(chat_id, fid, protect_content=protect))


async def send_chunk(bot, chat_id, chunk, protect):
    """Send up to 10 items as one album. Returns how many were sent."""
    if len(chunk) == 1:
        try:
            await send_one(bot, chat_id, chunk[0], protect)
            return 1
        except TelegramError as e:
            logger.warning("Single send failed: %s", e)
            return 0

    media = [
        InputMediaPhoto(fid) if kind == "photo" else InputMediaVideo(fid)
        for kind, fid in chunk
    ]
    try:
        await with_retry(lambda: bot.send_media_group(chat_id, media, protect_content=protect))
        return len(chunk)
    except TelegramError as e:
        logger.warning("Album failed (%s), falling back to one-by-one", e)

    sent = 0
    for item in chunk:
        try:
            await send_one(bot, chat_id, item, protect)
            sent += 1
        except TelegramError as e:
            logger.warning("Item failed: %s", e)
        await asyncio.sleep(0.5)
    return sent


async def flush_queue(bot, uid):
    q = queue_for(uid)
    async with q.lock:
        if q.timer:
            q.timer.cancel()
            q.timer = None

        items, q.items = q.items, []
        skipped, q.skipped = q.skipped, 0
        status_id, q.status_id = q.status_id, None

        if not items and not skipped:
            return 0

        sent = 0
        if items:
            if store.get(uid, "shuffle"):
                random.shuffle(items)
            size = store.get(uid, "album_size")
            protect = store.get(uid, "protect")
            for i in range(0, len(items), size):
                if i:
                    await asyncio.sleep(random.uniform(1.5, 3))
                sent += await send_chunk(bot, q.chat_id, items[i:i + size], protect)
            store.incr(uid, "sent", sent)

        parts = []
        if items:
            parts.append(f"✅ Sent {sent} item(s)")
            if sent < len(items):
                parts.append(f"⚠️ {len(items) - sent} failed")
        if skipped:
            parts.append(f"♻️ {skipped} duplicate(s) skipped")
        summary = "\n".join(parts)

        try:
            if status_id:
                await bot.edit_message_text(summary, q.chat_id, status_id)
            else:
                await bot.send_message(q.chat_id, summary)
        except TelegramError:
            pass
        return sent


async def refresh_status(bot, uid):
    q = queue_for(uid)
    now = time.time()
    if now - q.last_status < 2:  # throttle edits
        return
    q.last_status = now

    text = f"📥 Queued: {len(q.items)}"
    if q.skipped:
        text += f"\n♻️ Duplicates skipped: {q.skipped}"
    text += f"\n⏳ Auto-send after {store.get(uid, 'timeout')}s idle · /flush to send now"
    try:
        if q.status_id:
            await bot.edit_message_text(text, q.chat_id, q.status_id)
        else:
            m = await bot.send_message(q.chat_id, text)
            q.status_id = m.message_id
    except BadRequest as e:
        if "not found" in str(e).lower() or "can't be edited" in str(e).lower():
            q.status_id = None
    except TelegramError as e:
        logger.warning("Status update failed: %s", e)


async def touch(bot, uid):
    """Re-arm the inactivity timer and refresh the status message."""
    q = queue_for(uid)
    if q.timer:
        q.timer.cancel()
    loop = asyncio.get_running_loop()
    q.timer = loop.call_later(
        store.get(uid, "timeout"), lambda: spawn(flush_queue(bot, uid))
    )
    await refresh_status(bot, uid)


async def enqueue(bot, chat_id, uid, kind, fid, uniq, fp):
    q = queue_for(uid)
    q.chat_id = chat_id
    q.items.append((kind, fid))
    store.mark_seen(uid, uniq, fp)
    if len(q.items) >= store.get(uid, "album_size"):
        spawn(flush_queue(bot, uid))
    else:
        await touch(bot, uid)


# -----------------------------
# Media handler
# -----------------------------
@admin_only
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    media = extract_media(msg)
    if not media:
        return
    kind, fid, uniq, fp = media

    store.incr(uid, "received")
    q = queue_for(uid)
    q.chat_id = msg.chat_id

    # 1) Dedup
    if store.get(uid, "dedup") and store.is_dup(uid, uniq, fp, store.get(uid, "strict_dedup")):
        store.incr(uid, "dupes")
        q.skipped += 1
        if store.get(uid, "delete_original"):
            await try_delete(context.bot, msg.chat_id, msg.message_id)
        await touch(context.bot, uid)
        return

    # 2) Photo approval (single pending slot)
    if kind == "photo" and store.get(uid, "photo_approval"):
        context.user_data["pending_photo"] = {
            "fid": fid, "uniq": uniq, "fp": fp,
            "chat_id": msg.chat_id, "msg_id": msg.message_id,
        }
        await msg.reply_text("📷 Photo held. Use /approve or /reject.")
        return

    # 3) Queue it
    if store.get(uid, "delete_original"):
        await try_delete(context.bot, msg.chat_id, msg.message_id)
    await enqueue(context.bot, msg.chat_id, uid, kind, fid, uniq, fp)


# -----------------------------
# Commands
# -----------------------------
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Media Batcher</b>\n"
        "Send me videos or photos. I skip duplicates, strip captions and "
        "forwarding info, and send them back as clean albums.\n\n"
        f"🆔 Your ID: <code>{update.effective_user.id}</code>\n\n"
        "/settings · /help",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Commands</b>\n"
        "/settings – Settings menu\n"
        "/stats – Your counters\n"
        "/queue – What's waiting to be sent\n"
        "/flush – Send the queue now\n"
        "/clear – Discard the queue\n"
        "/resetdedup – Forget all seen media\n"
        "/approve · /reject – Photo approval mode\n"
        "/start – Show your Telegram ID",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = queue_for(update.effective_user.id)
    await update.message.reply_text(
        f"📥 Queued: {len(q.items)}\n♻️ Duplicates skipped this batch: {q.skipped}"
    )


@admin_only
async def flush(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    q = queue_for(uid)
    if not q.items:
        await update.message.reply_text("Nothing queued.")
        return
    q.chat_id = update.effective_chat.id
    await flush_queue(context.bot, uid)


@admin_only
async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    q = queue_for(uid)
    n = len(q.items)
    async with q.lock:
        q.items = []
        q.skipped = 0
        q.status_id = None
        if q.timer:
            q.timer.cancel()
            q.timer = None
    await update.message.reply_text(f"🗑 Discarded {n} queued item(s).")


@admin_only
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = context.user_data.pop("pending_photo", None)
    if not p:
        await update.message.reply_text("No pending photo.")
        return
    if store.get(uid, "delete_original"):
        await try_delete(context.bot, p["chat_id"], p["msg_id"])
    await enqueue(context.bot, p["chat_id"], uid, "photo", p["fid"], p["uniq"], p["fp"])
    await update.message.reply_text("✅ Photo approved and queued.")


@admin_only
async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("pending_photo", None):
        await update.message.reply_text("🚫 Photo rejected.")
    else:
        await update.message.reply_text("No pending photo.")


def stats_text(uid):
    c = store.counters(uid)
    q = queue_for(uid)
    return (
        "📊 <b>Stats</b>\n\n"
        f"Received: <b>{c.get('received', 0)}</b>\n"
        f"Sent: <b>{c.get('sent', 0)}</b>\n"
        f"Duplicates skipped: <b>{c.get('dupes', 0)}</b>\n"
        f"Currently queued: <b>{len(q.items)}</b>\n"
        f"Dedup history: <b>{store.seen_count(uid)}</b> items"
    )


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(update.effective_user.id), parse_mode=ParseMode.HTML)


# -----------------------------
# Settings menu (inline keyboard, edits in place)
# -----------------------------
def settings_text(uid):
    return (
        "⚙️ <b>Settings</b>\n"
        "Tap to change — applies instantly.\n\n"
        f"📦 Album size: <b>{store.get(uid, 'album_size')}</b> items\n"
        f"⏱ Auto-send after: <b>{store.get(uid, 'timeout')}s</b> idle\n"
        f"♻️ Dedup history: <b>{store.seen_count(uid)}</b> items\n\n"
        "<i>Strict dedup also catches re-uploads by matching size, length and "
        "resolution (rarely, two different videos could match).</i>"
    )


def settings_kb(uid):
    def tog(label, key):
        on = store.get(uid, key)
        return InlineKeyboardButton(f"{label} {'✅' if on else '❌'}", callback_data=f"t:{key}")

    def stepper(label, key, unit=""):
        return [
            InlineKeyboardButton("➖", callback_data=f"a:{key}:-1"),
            InlineKeyboardButton(f"{label}: {store.get(uid, key)}{unit}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"a:{key}:1"),
        ]

    return InlineKeyboardMarkup([
        [tog("🗑 Delete originals", "delete_original")],
        [tog("♻️ Dedup", "dedup"), tog("🔍 Strict", "strict_dedup")],
        stepper("📦 Album", "album_size"),
        stepper("⏱ Timeout", "timeout", "s"),
        [tog("🔀 Shuffle", "shuffle"), tog("🔒 Protect", "protect")],
        [tog("📷 Photo approval", "photo_approval")],
        [
            InlineKeyboardButton("📊 Stats", callback_data="m:stats"),
            InlineKeyboardButton("🧹 Clear dedup", callback_data="m:clear_ask"),
        ],
        [InlineKeyboardButton("✖ Close", callback_data="m:close")],
    ])


CONFIRM_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Yes, clear it", callback_data="m:clear_yes"),
    InlineKeyboardButton("Cancel", callback_data="m:menu"),
]])
BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="m:menu")]])


@admin_only
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        settings_text(uid), parse_mode=ParseMode.HTML, reply_markup=settings_kb(uid)
    )


@admin_only
async def resetdedup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ Forget all seen media? Previously sent items will be accepted again.",
        reply_markup=CONFIRM_KB,
    )


async def edit_menu(cq, text, kb):
    try:
        await cq.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


@admin_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    uid = cq.from_user.id
    data = cq.data or ""
    parts = data.split(":")

    if data == "noop":
        await cq.answer()
        return

    if parts[0] == "t" and len(parts) == 2 and parts[1] in TOGGLES:
        store.set(uid, parts[1], not store.get(uid, parts[1]))
        await cq.answer()
        await edit_menu(cq, settings_text(uid), settings_kb(uid))

    elif parts[0] == "a" and len(parts) == 3 and parts[1] in RANGES:
        lo, hi, step = RANGES[parts[1]]
        value = store.get(uid, parts[1]) + int(parts[2]) * step
        store.set(uid, parts[1], max(lo, min(hi, value)))
        await cq.answer()
        await edit_menu(cq, settings_text(uid), settings_kb(uid))

    elif data == "m:menu":
        await cq.answer()
        await edit_menu(cq, settings_text(uid), settings_kb(uid))

    elif data == "m:stats":
        await cq.answer()
        await edit_menu(cq, stats_text(uid), BACK_KB)

    elif data == "m:clear_ask":
        await cq.answer()
        await edit_menu(
            cq,
            "⚠️ <b>Clear dedup history?</b>\nPreviously sent items will be accepted again.",
            CONFIRM_KB,
        )

    elif data == "m:clear_yes":
        store.clear_seen(uid)
        await cq.answer("Dedup history cleared")
        await edit_menu(cq, settings_text(uid), settings_kb(uid))

    elif data == "m:close":
        await cq.answer()
        try:
            await cq.message.delete()
        except TelegramError:
            pass

    else:
        await cq.answer()


# -----------------------------
# Startup
# -----------------------------
async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("settings", "Open settings menu"),
        BotCommand("stats", "Show counters"),
        BotCommand("queue", "Show queued items"),
        BotCommand("flush", "Send the queue now"),
        BotCommand("clear", "Discard the queue"),
        BotCommand("resetdedup", "Forget all seen media"),
        BotCommand("approve", "Approve pending photo"),
        BotCommand("reject", "Reject pending photo"),
        BotCommand("help", "Show help"),
        BotCommand("start", "Show your Telegram ID"),
    ])
    logger.info("Post-init tasks completed.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("flush", flush))
    app.add_handler(CommandHandler("clear", clear_queue))
    app.add_handler(CommandHandler("resetdedup", resetdedup_cmd))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO | filters.PHOTO, handle_media
    ))

    app.add_error_handler(on_error)

    logger.info("Bot is now polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
