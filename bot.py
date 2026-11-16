"""Edit Guard Bot - deletes edited messages, auto-deletes media, blocks NSFW media."""
import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
import time

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("editguard")

DB_PATH = os.environ.get("DB_PATH", "bot.db")
NSFW_THRESHOLD = float(os.environ.get("NSFW_THRESHOLD", "0.5"))
MAX_DELAY = 48 * 3600
NSFW_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}

# ----------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS chats(
        chat_id INTEGER PRIMARY KEY,
        edit_delay INTEGER DEFAULT 0,
        media_delay INTEGER DEFAULT 0,
        nsfw INTEGER DEFAULT 0,
        edit_scope TEXT DEFAULT 'user',
        nsfw_scope TEXT DEFAULT 'user',
        media_scope TEXT DEFAULT 'all');
    CREATE TABLE IF NOT EXISTS auth(
        chat_id INTEGER, user_id INTEGER, kind TEXT, name TEXT,
        PRIMARY KEY(chat_id, user_id, kind));
    CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, user_id INTEGER, name TEXT);
    CREATE TABLE IF NOT EXISTS pending(
        chat_id INTEGER, message_id INTEGER, due REAL, kind TEXT, mention TEXT,
        PRIMARY KEY(chat_id, message_id));
    """
)


def settings(chat_id: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
    return db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()


def set_setting(chat_id: int, key: str, value) -> None:
    settings(chat_id)
    db.execute(f"UPDATE chats SET {key}=? WHERE chat_id=?", (value, chat_id))


def is_authed(chat_id: int, user_id: int, kind: str) -> bool:
    return db.execute(
        "SELECT 1 FROM auth WHERE chat_id=? AND user_id=? AND kind=?", (chat_id, user_id, kind)
    ).fetchone() is not None


def schedule(chat_id: int, message_id: int, delay: int, kind: str, mention: str = "") -> None:
    """Queue a deletion. An earlier deadline for the same message always wins."""
    due = time.time() + delay
    row = db.execute(
        "SELECT due FROM pending WHERE chat_id=? AND message_id=?", (chat_id, message_id)
    ).fetchone()
    if row is None or due < row["due"]:
        db.execute(
            "INSERT OR REPLACE INTO pending VALUES(?,?,?,?,?)",
            (chat_id, message_id, due, kind, mention),
        )


# ------------------------------------------------------------------ helpers
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str):
    """'2 minute', '10m', '1h 30m', '45 sec', '0', 'off' -> seconds (None if invalid)."""
    text = text.strip().lower()
    if text in ("0", "off", "none", "disable"):
        return 0
    parts = re.findall(r"(\d+)\s*([a-z]*)", text)
    if not parts or re.sub(r"(\d+)\s*([a-z]*)|\s", "", text):
        return None
    total = 0
    for num, unit in parts:
        mult = UNITS.get((unit or "m")[0])  # bare number = minutes
        if mult is None:
            return None
        total += int(num) * mult
    return total


def fmt_duration(sec: int) -> str:
    if not sec:
        return "instant"
    out = []
    for name, size in (("h", 3600), ("m", 60), ("s", 1)):
        if sec >= size:
            out.append(f"{sec // size}{name}")
            sec %= size
    return " ".join(out)


def mention_of(msg) -> str:
    """Clickable name that opens the sender's full profile."""
    if msg.sender_chat:  # anonymous admin / channel
        return html.escape(msg.sender_chat.title or "Admin")
    u = msg.from_user
    name = html.escape(u.first_name or "User")
    url = f"https://t.me/{u.username}" if u.username else f"tg://user?id={u.id}"
    return f'<a href="{url}">{name}</a>'


_admin_cache: dict = {}


async def is_admin(context, chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        result = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        result = False
    _admin_cache[key] = (result, time.time() + 60)
    return result


async def sender_info(msg, context):
    """-> (sender_id, is_admin). Anonymous admins / channel posts count as admin."""
    if msg.sender_chat:
        return msg.sender_chat.id, True
    return msg.from_user.id, await is_admin(context, msg.chat_id, msg.from_user.id)


async def notify(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except TelegramError as e:
        log.warning("notify failed in %s: %s", chat_id, e)


async def delete(bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except TelegramError as e:
        log.info("delete failed %s/%s: %s", chat_id, message_id, e)
        return False


EDIT_NOTICE = "⚠️ {}, your message was deleted because you edited it."
NSFW_NOTICE = "🔞 {}, your message was deleted because it contained NSFW content."


# --------------------------------------------------------------- NSFW check
_detector = None


def _nsfw_sync(path: str) -> bool:
    global _detector
    if _detector is None:
        from nudenet import NudeDetector

        _detector = NudeDetector()
    return any(
        d["class"] in NSFW_CLASSES and d["score"] >= NSFW_THRESHOLD for d in _detector.detect(path)
    )


def _scannable(msg):
    """Pick an image to scan: the media itself for stills, the thumbnail otherwise."""
    if msg.photo:
        return msg.photo[-1]
    if msg.sticker:
        s = msg.sticker
        return s.thumbnail if (s.is_animated or s.is_video) else s
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/") and (doc.file_size or 0) < 10_000_000:
        return doc
    media = msg.video or msg.animation or msg.video_note or doc
    return getattr(media, "thumbnail", None)


async def is_nsfw(msg) -> bool:
    target = _scannable(msg)
    if target is None:
        return False
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        await (await target.get_file()).download_to_drive(path)
        return await asyncio.to_thread(_nsfw_sync, path)
    except Exception as e:  # never let a scan failure break the bot
        log.warning("nsfw scan failed: %s", e)
        return False
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ----------------------------------------------------------------- handlers
async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and u.username:
        db.execute(
            "INSERT OR REPLACE INTO users VALUES(?,?,?)", (u.username.lower(), u.id, u.first_name)
        )


_seen_edits: dict = {}


async def on_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    # Telegram also sends "edited" updates for reactions etc. - only real edits carry a new edit_date.
    if not msg or not msg.edit_date or msg.is_automatic_forward:
        return
    if msg.from_user and msg.from_user.is_bot and not msg.sender_chat:
        return
    key = (msg.chat_id, msg.message_id)
    if _seen_edits.get(key) == msg.edit_date:
        return
    if len(_seen_edits) > 5000:
        _seen_edits.clear()
    _seen_edits[key] = msg.edit_date

    s = settings(msg.chat_id)
    uid, admin = await sender_info(msg, context)
    if (admin and s["edit_scope"] == "user") or is_authed(msg.chat_id, uid, "edit"):
        return
    mention = mention_of(msg)
    if s["edit_delay"] > 0:
        schedule(msg.chat_id, msg.message_id, s["edit_delay"], "edit", mention)
    elif await delete(context.bot, msg.chat_id, msg.message_id):
        await notify(context.bot, msg.chat_id, EDIT_NOTICE.format(mention))


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.is_automatic_forward:
        return
    s = settings(msg.chat_id)
    if not s["nsfw"] and not s["media_delay"]:
        return
    uid, admin = await sender_info(msg, context)
    if is_authed(msg.chat_id, uid, "media"):
        return
    if s["nsfw"] and not (admin and s["nsfw_scope"] == "user") and await is_nsfw(msg):
        if await delete(context.bot, msg.chat_id, msg.message_id):
            await notify(context.bot, msg.chat_id, NSFW_NOTICE.format(mention_of(msg)))
        return
    if s["media_delay"] and not (admin and s["media_scope"] == "user"):
        schedule(msg.chat_id, msg.message_id, s["media_delay"], "media")


async def sweep(context: ContextTypes.DEFAULT_TYPE):
    """Runs every few seconds: delete everything whose timer has expired."""
    rows = db.execute("SELECT * FROM pending WHERE due<=?", (time.time(),)).fetchall()
    for r in rows:
        db.execute(
            "DELETE FROM pending WHERE chat_id=? AND message_id=?", (r["chat_id"], r["message_id"])
        )
        ok = await delete(context.bot, r["chat_id"], r["message_id"])
        if ok and r["kind"] == "edit":
            await notify(context.bot, r["chat_id"], EDIT_NOTICE.format(r["mention"]))


# ----------------------------------------------------------------- commands
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if msg.chat.type == "private":
            await msg.reply_text("Use this command in a group.")
            return
        _, admin = await sender_info(msg, context)
        if not admin:
            await msg.reply_text("❌ Only admins can use this command.")
            return
        await func(update, context)

    return wrapper


HELP = (
    "<b>Edit Guard Bot</b>\n"
    "Add me to a group as admin with <i>Delete messages</i> permission.\n\n"
    "/settings - show settings &amp; choose who rules apply to\n"
    "/set_delay 2 minute - delete edited messages after a delay (0 = instant)\n"
    "/set_delaymedia 10 minute - auto-delete all media after a delay (off = disable)\n"
    "/auth - allow a user to edit messages (reply / id / @username)\n"
    "/unauth - remove edit permission\n"
    "/auth_media - bot will never delete this user's media\n"
    "/unauth_media - remove media permission\n"
    "/authlist - list authorized users\n"
    "/nsfw on|off - NSFW protection for stickers, photos, videos, GIFs"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


def _delay_command(key: str, label: str, zero_text: str):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        sec = parse_duration(" ".join(context.args)) if context.args else None
        if sec is None or sec > MAX_DELAY:
            await msg.reply_text(
                f"Usage: /{msg.text.split()[0][1:].split('@')[0]} 2 minute\n"
                "Examples: 30 sec, 10m, 1 hour, 0 / off (max 48h)"
            )
            return
        set_setting(msg.chat_id, key, sec)
        await msg.reply_text(
            f"✅ {label}: {fmt_duration(sec)} after it happens." if sec else f"✅ {zero_text}"
        )

    return handler


cmd_set_delay = _delay_command(
    "edit_delay", "Edited messages will be deleted", "Edited messages will be deleted instantly."
)
cmd_set_delaymedia = _delay_command(
    "media_delay", "Media will be auto-deleted", "Media auto-delete is off."
)


def _resolve_target(msg, args):
    """-> (user_id, name) from a reply, numeric id or @username."""
    reply = msg.reply_to_message
    if reply and not args:
        if reply.sender_chat:
            return reply.sender_chat.id, reply.sender_chat.title
        return reply.from_user.id, reply.from_user.first_name
    if args:
        arg = args[0]
        if arg.lstrip("-").isdigit():
            return int(arg), arg
        row = db.execute(
            "SELECT * FROM users WHERE username=?", (arg.lstrip("@").lower(),)
        ).fetchone()
        if row:
            return row["user_id"], row["name"]
    return None, None


def _auth_command(kind: str, grant: bool, text: str):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        uid, name = _resolve_target(msg, context.args)
        if uid is None:
            await msg.reply_text(
                "Reply to the user's message, or give their user id / @username "
                "(a @username only works after I have seen that user send a message)."
            )
            return
        if grant:
            db.execute("INSERT OR REPLACE INTO auth VALUES(?,?,?,?)", (msg.chat_id, uid, kind, name))
        else:
            db.execute(
                "DELETE FROM auth WHERE chat_id=? AND user_id=? AND kind=?", (msg.chat_id, uid, kind)
            )
        link = f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'
        await msg.reply_text(text.format(link), parse_mode=ParseMode.HTML)

    return handler


@admin_only
async def cmd_authlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    lines = []
    for kind, title in (("edit", "✏️ Can edit messages"), ("media", "🖼 Media protected")):
        rows = db.execute(
            "SELECT * FROM auth WHERE chat_id=? AND kind=?", (msg.chat_id, kind)
        ).fetchall()
        lines.append(f"<b>{title}:</b>")
        lines += [
            f'• <a href="tg://user?id={r["user_id"]}">{html.escape(r["name"] or "user")}</a> '
            f'(<code>{r["user_id"]}</code>)'
            for r in rows
        ] or ["• none"]
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_nsfw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "on" if settings(msg.chat_id)["nsfw"] else "off"
        await msg.reply_text(f"NSFW protection is {state}.\nUsage: /nsfw on | /nsfw off")
        return
    set_setting(msg.chat_id, "nsfw", int(arg == "on"))
    await msg.reply_text(
        "✅ NSFW protection is ON. Adult stickers, photos, videos and GIFs will be deleted."
        if arg == "on"
        else "✅ NSFW protection is OFF."
    )


# ------------------------------------------------------------ settings menu
SCOPE_LABEL = {"user": "User", "all": "Admin | User"}
ROWS = (("edit", "Edit delete ➜"), ("nsfw", "NSFW delete ➜"), ("media", "Media delete ➜"))


def settings_view(chat_id: int):
    s = settings(chat_id)
    text = (
        "<b>Current settings of this group:</b>\n\n"
        f"✏️ Edit delete delay: <b>{fmt_duration(s['edit_delay'])}</b>\n"
        f"🖼 Media auto-delete: <b>{fmt_duration(s['media_delay']) if s['media_delay'] else 'off'}</b>\n"
        f"🔞 NSFW protection: <b>{'on' if s['nsfw'] else 'off'}</b>\n\n"
        "Tap a button on the right to choose who a rule applies to."
    )
    keyboard = [
        [
            Btn(label, callback_data=f"info:{key}"),
            Btn(SCOPE_LABEL[s[f"{key}_scope"]], callback_data=f"scope:{key}"),
        ]
        for key, label in ROWS
    ]
    return text, InlineKeyboardMarkup(keyboard)


@admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = settings_view(update.effective_chat.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


INFO = {
    "edit": "Edited messages are deleted. 'User' = only members, 'Admin | User' = admins too.",
    "nsfw": "NSFW media is deleted (when /nsfw on). 'User' = only members, 'Admin | User' = admins too.",
    "media": "Media is auto-deleted (when /set_delaymedia is set). 'User' = only members, 'Admin | User' = admins too.",
}


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, key = q.data.split(":")
    if action == "info":
        await q.answer(INFO[key], show_alert=True)
        return
    chat_id = q.message.chat.id
    if not await is_admin(context, chat_id, q.from_user.id):
        await q.answer("Only admins can change settings.", show_alert=True)
        return
    column = f"{key}_scope"
    new = "all" if settings(chat_id)[column] == "user" else "user"
    set_setting(chat_id, column, new)
    text, markup = settings_view(chat_id)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    await q.answer(f"Now applies to: {SCOPE_LABEL[new]}")


# --------------------------------------------------------------------- main
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable (get one from @BotFather).")
    app = Application.builder().token(token).build()

    app.add_handler(TypeHandler(Update, track_user), group=-1)
    groups = filters.ChatType.GROUPS
    # Must be first: every edited message (including edited commands) goes here.
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & groups, on_edit))

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set_delay", cmd_set_delay))
    app.add_handler(CommandHandler("set_delaymedia", cmd_set_delaymedia))
    app.add_handler(CommandHandler("auth", _auth_command("edit", True, "✅ {} can now edit messages.")))
    app.add_handler(CommandHandler("unauth", _auth_command("edit", False, "✅ {} can no longer edit messages.")))
    app.add_handler(CommandHandler("auth_media", _auth_command("media", True, "✅ {}'s media will not be deleted.")))
    app.add_handler(CommandHandler("unauth_media", _auth_command("media", False, "✅ {}'s media is no longer protected.")))
    app.add_handler(CommandHandler("authlist", cmd_authlist))
    app.add_handler(CommandHandler("nsfw", cmd_nsfw))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(info|scope):(edit|nsfw|media)$"))

    media = (
        filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Sticker.ALL
        | filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & groups & media, on_media))

    app.job_queue.run_repeating(sweep, interval=3, first=3)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
