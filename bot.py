"""Edit Guard Bot - edit protection, media auto-delete, NSFW filter and group moderation."""
import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
import time

from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatPermissions,
    InlineKeyboardMarkup,
    Update,
)
from telegram import InlineKeyboardButton as Btn
from telegram.constants import ChatMemberStatus, MessageEntityType, ParseMode
from telegram.error import BadRequest, TelegramError
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
DEFAULT_WELCOME = "👋 Welcome {name} to {group}!"

# ----------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS auth(
        chat_id INTEGER, user_id INTEGER, kind TEXT, name TEXT,
        PRIMARY KEY(chat_id, user_id, kind));
    CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, user_id INTEGER, name TEXT);
    CREATE TABLE IF NOT EXISTS pending(
        chat_id INTEGER, message_id INTEGER, due REAL, kind TEXT, mention TEXT,
        PRIMARY KEY(chat_id, message_id));
    CREATE TABLE IF NOT EXISTS warns(
        chat_id INTEGER, user_id INTEGER, reason TEXT, ts REAL);
    """
)
# Per-group settings. New columns are added automatically to existing databases.
CHAT_COLUMNS = {
    "edit_on": "INTEGER DEFAULT 1",
    "edit_delay": "INTEGER DEFAULT 0",
    "edit_scope": "TEXT DEFAULT 'user'",
    "media_delay": "INTEGER DEFAULT 0",
    "media_scope": "TEXT DEFAULT 'all'",
    "nsfw": "INTEGER DEFAULT 0",
    "nsfw_scope": "TEXT DEFAULT 'user'",
    "antilink": "INTEGER DEFAULT 0",
    "antiforward": "INTEGER DEFAULT 0",
    "warn_limit": "INTEGER DEFAULT 3",
    "warn_action": "TEXT DEFAULT 'mute'",
    "welcome_on": "INTEGER DEFAULT 0",
    "welcome_text": "TEXT",
    "rules": "TEXT",
    "clean_service": "INTEGER DEFAULT 0",
    "notice_ttl": "INTEGER DEFAULT 0",
}
_existing = {r["name"] for r in db.execute("PRAGMA table_info(chats)")}
for _col, _decl in CHAT_COLUMNS.items():
    if _col not in _existing:
        db.execute(f"ALTER TABLE chats ADD COLUMN {_col} {_decl}")


def settings(chat_id: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (chat_id,))
    return db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()


def set_setting(chat_id: int, key: str, value) -> None:
    assert key in CHAT_COLUMNS
    settings(chat_id)
    db.execute(f"UPDATE chats SET {key}=? WHERE chat_id=?", (value, chat_id))


def is_authed(chat_id: int, user_id: int, kind: str) -> bool:
    return db.execute(
        "SELECT 1 FROM auth WHERE chat_id=? AND user_id=? AND kind=?", (chat_id, user_id, kind)
    ).fetchone() is not None


def warn_count(chat_id: int, user_id: int) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()[0]


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
_UNIT = {
    1: "s sec secs second seconds",
    60: "m min mins minute minutes",
    3600: "h hr hrs hour hours",
    86400: "d day days",
}
UNITS = {word: mult for mult, words in _UNIT.items() for word in words.split()}
UNITS[""] = 60  # a bare number means minutes


def parse_duration(text: str):
    """'2 minute', '10m', '1h 30m', '45 sec', '0', 'off' -> seconds (None if invalid)."""
    text = text.strip().lower()
    if text in ("0", "off", "none", "disable"):
        return 0
    parts = re.findall(r"(\d+)\s*([a-z]*)", text)
    if not parts or re.sub(r"\d+\s*[a-z]*|\s", "", text):
        return None
    total = 0
    for num, unit in parts:
        if unit not in UNITS:
            return None
        total += int(num) * UNITS[unit]
    return total


def fmt_duration(sec: int) -> str:
    if not sec:
        return "instant"
    out = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if sec >= size:
            out.append(f"{sec // size}{name}")
            sec %= size
    return " ".join(out)


def link(user_id: int, name: str, username: str = None) -> str:
    """Clickable name that opens the user's full profile."""
    url = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
    return f'<a href="{url}">{html.escape(name or "User")}</a>'


def mention_of(msg) -> str:
    if msg.sender_chat:  # anonymous admin / channel
        return html.escape(msg.sender_chat.title or "Admin")
    u = msg.from_user
    return link(u.id, u.first_name, u.username)


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


async def notify(bot, chat_id: int, text: str, markup=None, ttl: int = None) -> None:
    """Send a bot notice; it cleans itself up if the group has a notice lifetime set."""
    try:
        sent = await bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except TelegramError as e:
        log.warning("notify failed in %s: %s", chat_id, e)
        return
    ttl = settings(chat_id)["notice_ttl"] if ttl is None else ttl
    if ttl:
        schedule(chat_id, sent.message_id, ttl, "notice")


async def delete(bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id, message_id)
        return True
    except TelegramError as e:
        log.info("delete failed %s/%s: %s", chat_id, message_id, e)
        return False


async def reply(msg, text: str, markup=None):
    return await msg.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup
    )


EDIT_NOTICE = "⚠️ {}, your message was deleted because you edited it."
NSFW_NOTICE = "🔞 {}, your message was deleted because it contained NSFW content."
LINK_NOTICE = "🔗 {}, links are not allowed in this group."
FORWARD_NOTICE = "↪️ {}, forwarded messages are not allowed in this group."


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


# --------------------------------------------------------- message handlers
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
    if not s["edit_on"]:
        return
    uid, admin = await sender_info(msg, context)
    if (admin and s["edit_scope"] == "user") or is_authed(msg.chat_id, uid, "edit"):
        return
    mention = mention_of(msg)
    if s["edit_delay"] > 0:
        schedule(msg.chat_id, msg.message_id, s["edit_delay"], "edit", mention)
    elif await delete(context.bot, msg.chat_id, msg.message_id):
        await notify(context.bot, msg.chat_id, EDIT_NOTICE.format(mention))


LINK_TYPES = (MessageEntityType.URL, MessageEntityType.TEXT_LINK)


def has_link(msg) -> bool:
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    return any(e.type in LINK_TYPES for e in entities)


def is_media(msg) -> bool:
    return bool(
        msg.photo or msg.video or msg.animation or msg.sticker or msg.document
        or msg.audio or msg.voice or msg.video_note
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or msg.is_automatic_forward:
        return
    s = settings(msg.chat_id)
    media = is_media(msg)
    if not (s["antilink"] or s["antiforward"] or (media and (s["nsfw"] or s["media_delay"]))):
        return
    uid, admin = await sender_info(msg, context)
    bot, chat_id = context.bot, msg.chat_id

    # Locks apply to regular members only; /auth users are trusted.
    if not admin and not is_authed(chat_id, uid, "edit"):
        notice = None
        if s["antiforward"] and msg.forward_origin:
            notice = FORWARD_NOTICE
        elif s["antilink"] and has_link(msg):
            notice = LINK_NOTICE
        if notice:
            if await delete(bot, chat_id, msg.message_id):
                await notify(bot, chat_id, notice.format(mention_of(msg)))
            return

    if not media or is_authed(chat_id, uid, "media"):
        return
    if s["nsfw"] and not (admin and s["nsfw_scope"] == "user") and await is_nsfw(msg):
        if await delete(bot, chat_id, msg.message_id):
            await notify(bot, chat_id, NSFW_NOTICE.format(mention_of(msg)))
        return
    if s["media_delay"] and not (admin and s["media_scope"] == "user"):
        schedule(chat_id, msg.message_id, s["media_delay"], "media")


def render_welcome(s, user, chat_title: str) -> str:
    text = html.escape(s["welcome_text"] or DEFAULT_WELCOME)
    return text.replace("{name}", link(user.id, user.first_name, user.username)).replace(
        "{group}", html.escape(chat_title or "the group")
    )


def rules_button(context, chat_id: int):
    url = f"https://t.me/{context.bot.username}?start=rules_{chat_id}"
    return InlineKeyboardMarkup([[Btn("📜 Read the rules", url=url)]])


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    s = settings(msg.chat_id)
    for user in msg.new_chat_members:
        if user.id == context.bot.id:
            await notify(
                context.bot,
                msg.chat_id,
                "👋 <b>Thanks for adding me!</b>\n"
                "Make me an admin with <i>Delete messages</i> and <i>Ban users</i> permissions, "
                "then send /settings to configure this group.",
                ttl=0,
            )
        elif s["welcome_on"] and not user.is_bot:
            markup = rules_button(context, msg.chat_id) if s["rules"] else None
            await notify(context.bot, msg.chat_id, render_welcome(s, user, msg.chat.title), markup)
    if s["clean_service"]:
        await delete(context.bot, msg.chat_id, msg.message_id)


async def on_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if settings(msg.chat_id)["clean_service"]:
        await delete(context.bot, msg.chat_id, msg.message_id)


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


# --------------------------------------------------------- command plumbing
def group_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == "private":
            await reply(update.effective_message, "ℹ️ This command only works in groups.")
            return
        await func(update, context)

    return wrapper


def admin_only(func):
    @group_only
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        _, admin = await sender_info(update.effective_message, context)
        if not admin:
            await reply(update.effective_message, "🚫 This command is for group admins only.")
            return
        await func(update, context)

    return wrapper


def resolve_target(msg, args):
    """-> (user_id, name, remaining_args) from a reply, a numeric id or an @username."""
    args = list(args or ())
    r = msg.reply_to_message
    if r and not r.forum_topic_created:
        if r.sender_chat:
            return r.sender_chat.id, r.sender_chat.title, args
        return r.from_user.id, r.from_user.first_name, args
    if args:
        arg = args[0]
        if arg.lstrip("-").isdigit():
            return int(arg), arg, args[1:]
        row = db.execute(
            "SELECT * FROM users WHERE username=?", (arg.lstrip("@").lower(),)
        ).fetchone()
        if row:
            return row["user_id"], row["name"], args[1:]
    return None, None, args


NO_TARGET = (
    "ℹ️ Reply to the user's message, or pass their user ID or @username.\n"
    "<i>A @username only works after I have seen that user send a message.</i>"
)


def take_duration(args):
    """Pull an optional leading duration off the args -> (seconds, rest)."""
    for n in (2, 1):
        if len(args) >= n:
            sec = parse_duration(" ".join(args[:n]))
            if sec:
                return min(sec, 366 * 86400), args[n:]
    return 0, args


async def fail(msg, error: TelegramError):
    await reply(
        msg,
        f"❌ I couldn't do that: <code>{html.escape(str(error))}</code>\n"
        "Please check that I am an admin with the required permissions.",
    )


# ------------------------------------------------------- protection commands
def _delay_command(key: str, name: str, on_text: str, off_text: str):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        sec = parse_duration(" ".join(context.args)) if context.args else None
        if sec is None or sec > MAX_DELAY:
            await reply(
                msg,
                f"<b>Usage:</b> <code>/{name} 2 minute</code>\n"
                "Accepted: <code>30 sec</code>, <code>10m</code>, <code>1 hour</code>, "
                "<code>1h 30m</code>, <code>0</code> / <code>off</code> (max 48h)",
            )
            return
        set_setting(msg.chat_id, key, sec)
        await reply(msg, "✅ " + (on_text.format(fmt_duration(sec)) if sec else off_text))

    return handler


cmd_editdelay = _delay_command(
    "edit_delay",
    "editdelay",
    "Edited messages will be deleted <b>{}</b> after the edit.",
    "Edited messages will be deleted <b>instantly</b>.",
)
cmd_mediadelay = _delay_command(
    "media_delay",
    "mediadelay",
    "Media will be auto-deleted <b>{}</b> after it is sent.",
    "Media auto-delete is now <b>off</b>.",
)


def _auth_command(kind: str, grant: bool, text: str):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        uid, name, _ = resolve_target(msg, context.args)
        if uid is None:
            await reply(msg, NO_TARGET)
            return
        if grant:
            db.execute("INSERT OR REPLACE INTO auth VALUES(?,?,?,?)", (msg.chat_id, uid, kind, name))
        else:
            db.execute(
                "DELETE FROM auth WHERE chat_id=? AND user_id=? AND kind=?", (msg.chat_id, uid, kind)
            )
        await reply(msg, text.format(link(uid, name or str(uid))))

    return handler


@admin_only
async def cmd_authlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    lines = ["<b>Authorized users</b>"]
    for kind, title in (("edit", "✏️ May edit messages"), ("media", "🖼 Media exempt")):
        rows = db.execute(
            "SELECT * FROM auth WHERE chat_id=? AND kind=?", (msg.chat_id, kind)
        ).fetchall()
        lines.append(f"\n<b>{title}</b>")
        lines += [
            f"• {link(r['user_id'], r['name'])} (<code>{r['user_id']}</code>)" for r in rows
        ] or ["• <i>none</i>"]
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_nsfw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "on" if settings(msg.chat_id)["nsfw"] else "off"
        await reply(
            msg,
            f"🔞 NSFW protection is currently <b>{state}</b>.\n"
            "<b>Usage:</b> <code>/nsfw on</code> or <code>/nsfw off</code>",
        )
        return
    set_setting(msg.chat_id, "nsfw", int(arg == "on"))
    await reply(
        msg,
        "✅ NSFW protection is <b>on</b>. Adult stickers, photos, videos and GIFs will be removed."
        if arg == "on"
        else "✅ NSFW protection is <b>off</b>.",
    )


# ------------------------------------------------------- moderation commands
async def _mod_target(update, context):
    """Resolve and validate a moderation target -> (uid, name, rest) or None."""
    msg = update.effective_message
    uid, name, rest = resolve_target(msg, context.args)
    if uid is None:
        await reply(msg, NO_TARGET)
        return None
    if uid == context.bot.id:
        await reply(msg, "🙂 I can't moderate myself.")
        return None
    if uid < 0 or await is_admin(context, msg.chat_id, uid):
        await reply(msg, "🚫 Admins and channels can't be moderated with this command.")
        return None
    return uid, name, rest


async def do_mute(bot, chat_id, uid, seconds=0):
    until = int(time.time()) + seconds if seconds else None
    await bot.restrict_chat_member(chat_id, uid, ChatPermissions.no_permissions(), until_date=until)


async def do_unmute(bot, chat_id, uid):
    chat = await bot.get_chat(chat_id)
    perms = chat.permissions or ChatPermissions.all_permissions()
    await bot.restrict_chat_member(chat_id, uid, perms)


async def do_kick(bot, chat_id, uid):
    await bot.ban_chat_member(chat_id, uid)
    await bot.unban_chat_member(chat_id, uid, only_if_banned=True)


def undo_button(label: str, action: str, uid: int):
    return InlineKeyboardMarkup([[Btn(label, callback_data=f"m:{action}:{uid}")]])


def _reason(rest) -> str:
    return f"\n<b>Reason:</b> {html.escape(' '.join(rest))}" if rest else ""


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return
    uid, name, rest = target
    seconds, rest = take_duration(rest)
    try:
        until = int(time.time()) + seconds if seconds else None
        await context.bot.ban_chat_member(msg.chat_id, uid, until_date=until)
    except TelegramError as e:
        return await fail(msg, e)
    period = f" for <b>{fmt_duration(seconds)}</b>" if seconds else ""
    await reply(
        msg,
        f"🚫 {link(uid, name)} has been banned{period}.{_reason(rest)}",
        undo_button("↩️ Unban", "unban", uid),
    )


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    try:
        await context.bot.unban_chat_member(msg.chat_id, uid, only_if_banned=True)
    except TelegramError as e:
        return await fail(msg, e)
    await reply(msg, f"✅ {link(uid, name)} has been unbanned.")


@admin_only
async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return
    uid, name, rest = target
    try:
        await do_kick(context.bot, msg.chat_id, uid)
    except TelegramError as e:
        return await fail(msg, e)
    await reply(msg, f"👢 {link(uid, name)} has been removed from the group.{_reason(rest)}")


@admin_only
async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return
    uid, name, rest = target
    seconds, rest = take_duration(rest)
    try:
        await do_mute(context.bot, msg.chat_id, uid, seconds)
    except TelegramError as e:
        return await fail(msg, e)
    period = f" for <b>{fmt_duration(seconds)}</b>" if seconds else ""
    await reply(
        msg,
        f"🔇 {link(uid, name)} has been muted{period}.{_reason(rest)}",
        undo_button("🔊 Unmute", "unmute", uid),
    )


@admin_only
async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    try:
        await do_unmute(context.bot, msg.chat_id, uid)
    except TelegramError as e:
        return await fail(msg, e)
    await reply(msg, f"🔊 {link(uid, name)} has been unmuted.")


WARN_ACTIONS = {"mute": "muted", "kick": "removed", "ban": "banned"}


@admin_only
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return
    uid, name, rest = target
    s = settings(msg.chat_id)
    db.execute("INSERT INTO warns VALUES(?,?,?,?)", (msg.chat_id, uid, " ".join(rest), time.time()))
    count = warn_count(msg.chat_id, uid)
    if count < s["warn_limit"]:
        await reply(
            msg,
            f"⚠️ {link(uid, name)} has been warned "
            f"(<b>{count}/{s['warn_limit']}</b>).{_reason(rest)}",
            undo_button("↩️ Remove warning", "unwarn", uid),
        )
        return
    action = s["warn_action"]
    try:
        if action == "ban":
            await context.bot.ban_chat_member(msg.chat_id, uid)
        elif action == "kick":
            await do_kick(context.bot, msg.chat_id, uid)
        else:
            await do_mute(context.bot, msg.chat_id, uid)
    except TelegramError as e:
        return await fail(msg, e)
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (msg.chat_id, uid))
    undo = {"ban": ("↩️ Unban", "unban"), "mute": ("🔊 Unmute", "unmute")}.get(action)
    await reply(
        msg,
        f"⛔ {link(uid, name)} reached <b>{count}/{s['warn_limit']}</b> warnings "
        f"and has been <b>{WARN_ACTIONS[action]}</b>.{_reason(rest)}",
        undo_button(undo[0], undo[1], uid) if undo else None,
    )


def remove_last_warn(chat_id: int, uid: int) -> bool:
    row = db.execute(
        "SELECT rowid FROM warns WHERE chat_id=? AND user_id=? ORDER BY ts DESC LIMIT 1",
        (chat_id, uid),
    ).fetchone()
    if row:
        db.execute("DELETE FROM warns WHERE rowid=?", (row[0],))
    return row is not None


@admin_only
async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    if remove_last_warn(msg.chat_id, uid):
        limit = settings(msg.chat_id)["warn_limit"]
        await reply(
            msg,
            f"✅ Removed one warning from {link(uid, name)} "
            f"(<b>{warn_count(msg.chat_id, uid)}/{limit}</b>).",
        )
    else:
        await reply(msg, f"ℹ️ {link(uid, name)} has no warnings.")


@admin_only
async def cmd_resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (msg.chat_id, uid))
    await reply(msg, f"✅ All warnings for {link(uid, name)} have been cleared.")


@group_only
async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        uid, name = msg.from_user.id, msg.from_user.first_name
    rows = db.execute(
        "SELECT reason FROM warns WHERE chat_id=? AND user_id=? ORDER BY ts", (msg.chat_id, uid)
    ).fetchall()
    limit = settings(msg.chat_id)["warn_limit"]
    lines = [f"⚠️ {link(uid, name)} has <b>{len(rows)}/{limit}</b> warnings."]
    lines += [f"{i}. {html.escape(r['reason'] or 'No reason given')}" for i, r in enumerate(rows, 1)]
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await reply(msg, "ℹ️ Reply to the message you want to delete.")
    await delete(context.bot, msg.chat_id, msg.reply_to_message.message_id)
    await delete(context.bot, msg.chat_id, msg.message_id)


@admin_only
async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await reply(msg, "ℹ️ Reply to the first message to purge from (max 500 messages).")
    ids = list(range(msg.reply_to_message.message_id, msg.message_id + 1))[-500:]
    try:
        for i in range(0, len(ids), 100):
            await context.bot.delete_messages(msg.chat_id, ids[i : i + 100])
    except TelegramError as e:
        return await fail(msg, e)
    await notify(context.bot, msg.chat_id, "🧹 Purge complete.", ttl=5)


@admin_only
async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await reply(msg, "ℹ️ Reply to the message you want to pin.")
    loud = bool(context.args) and context.args[0].lower() in ("loud", "notify")
    try:
        await msg.reply_to_message.pin(disable_notification=not loud)
    except TelegramError as e:
        await fail(msg, e)


@admin_only
async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    try:
        if msg.reply_to_message:
            await msg.reply_to_message.unpin()
        else:
            await context.bot.unpin_chat_message(msg.chat_id)
        await reply(msg, "📌 Message unpinned.")
    except TelegramError as e:
        await fail(msg, e)


# ------------------------------------------------ welcome / rules / reports
def _text_after_command(msg) -> str:
    """Text for /setwelcome and /setrules: the command's argument or the replied message."""
    parts = (msg.text or "").split(None, 1)
    if len(parts) > 1:
        return parts[1].strip()
    r = msg.reply_to_message
    return (r.text or r.caption or "").strip() if r else ""


@admin_only
async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = _text_after_command(msg)
    if not text:
        return await reply(
            msg,
            "<b>Usage:</b> <code>/setwelcome Welcome {name} to {group}!</code>\n"
            "<code>{name}</code> = the new member, <code>{group}</code> = the group name.",
        )
    set_setting(msg.chat_id, "welcome_text", text[:1000])
    set_setting(msg.chat_id, "welcome_on", 1)
    preview = render_welcome(settings(msg.chat_id), msg.from_user, msg.chat.title)
    await reply(msg, f"✅ Welcome message saved and enabled. Preview:\n\n{preview}")


@admin_only
async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    arg = context.args[0].lower() if context.args else ""
    if arg in ("on", "off"):
        set_setting(msg.chat_id, "welcome_on", int(arg == "on"))
        return await reply(msg, f"✅ Welcome messages are now <b>{arg}</b>.")
    s = settings(msg.chat_id)
    await reply(
        msg,
        f"👋 Welcome messages are <b>{'on' if s['welcome_on'] else 'off'}</b>. Current message:\n\n"
        f"{render_welcome(s, msg.from_user, msg.chat.title)}\n\n"
        "<code>/welcome on|off</code> · <code>/setwelcome &lt;text&gt;</code>",
    )


@admin_only
async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = _text_after_command(msg)
    if not text:
        return await reply(
            msg, "<b>Usage:</b> <code>/setrules &lt;text&gt;</code> or reply to a message with /setrules."
        )
    set_setting(msg.chat_id, "rules", text[:3500])
    await reply(msg, "✅ Group rules saved. Members can read them with /rules.")


@group_only
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    rules = settings(msg.chat_id)["rules"]
    if not rules:
        return await reply(msg, "ℹ️ No rules have been set for this group yet.")
    await reply(msg, f"📜 <b>Rules of {html.escape(msg.chat.title or 'this group')}</b>\n\n{html.escape(rules)}")


@group_only
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = msg.reply_to_message
    if not target or target.forum_topic_created:
        return await reply(msg, "ℹ️ Reply to the message you want to report to the admins.")
    uid = target.sender_chat.id if target.sender_chat else target.from_user.id
    if target.sender_chat or await is_admin(context, msg.chat_id, uid):
        return await reply(msg, "🚫 Admins can't be reported.")
    try:
        admins = await context.bot.get_chat_administrators(msg.chat_id)
    except TelegramError:
        admins = []
    ping = "".join(
        f'<a href="tg://user?id={a.user.id}">​</a>' for a in admins[:5] if not a.user.is_bot
    )
    mid = target.message_id
    markup = InlineKeyboardMarkup(
        [
            [
                Btn("🗑 Delete", callback_data=f"r:del:{mid}:{uid}"),
                Btn("⚠️ Warn", callback_data=f"r:warn:{mid}:{uid}"),
            ],
            [
                Btn("🔇 Mute", callback_data=f"r:mute:{mid}:{uid}"),
                Btn("🚫 Ban", callback_data=f"r:ban:{mid}:{uid}"),
            ],
            [Btn("✖️ Dismiss", callback_data=f"r:x:{mid}:{uid}")],
        ]
    )
    await target.reply_text(
        f"🚨 <b>Reported to admins</b>{ping}\n"
        f"{mention_of(msg)} reported a message from {mention_of(target)}.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


@group_only
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    lines = [f"<b>Chat ID:</b> <code>{msg.chat_id}</code>"]
    r = msg.reply_to_message
    if r and not r.forum_topic_created and r.from_user:
        lines.append(f"<b>{html.escape(r.from_user.first_name)}'s ID:</b> <code>{r.from_user.id}</code>")
    else:
        lines.append(f"<b>Your ID:</b> <code>{msg.from_user.id}</code>")
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    try:
        status = (await context.bot.get_chat_member(msg.chat_id, uid)).status
    except TelegramError:
        status = "unknown"
    limit = settings(msg.chat_id)["warn_limit"]
    yes_no = lambda v: "yes" if v else "no"  # noqa: E731
    await reply(
        msg,
        f"👤 <b>User info</b>\n"
        f"<b>Name:</b> {link(uid, name)}\n"
        f"<b>ID:</b> <code>{uid}</code>\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Warnings:</b> {warn_count(msg.chat_id, uid)}/{limit}\n"
        f"<b>May edit:</b> {yes_no(is_authed(msg.chat_id, uid, 'edit'))}\n"
        f"<b>Media exempt:</b> {yes_no(is_authed(msg.chat_id, uid, 'media'))}",
    )


# ----------------------------------------------------------- settings panel
SCOPES = (("user", "User"), ("all", "Admin | User"))
CHOICES = {
    "edit_delay": ((0, "Instant"), (30, "30s"), (60, "1m"), (120, "2m"), (300, "5m"), (600, "10m")),
    "media_delay": ((0, "Off"), (60, "1m"), (300, "5m"), (600, "10m"), (1800, "30m"), (3600, "1h")),
    "notice_ttl": ((0, "Keep"), (30, "30s"), (60, "1m"), (300, "5m")),
    "warn_limit": ((2, "2"), (3, "3"), (4, "4"), (5, "5")),
    "warn_action": (("mute", "Mute"), ("kick", "Kick"), ("ban", "Ban")),
    "edit_scope": SCOPES,
    "nsfw_scope": SCOPES,
    "media_scope": SCOPES,
}
TOGGLES = {"edit_on", "nsfw", "antilink", "antiforward", "welcome_on", "clean_service"}
SCOPE_LABEL = dict(SCOPES)


def _state(on) -> str:
    return "✅ On" if on else "❌ Off"


def _toggle(panel, key, label, s):
    return [Btn(f"{label}: {_state(s[key])}", callback_data=f"t:{panel}:{key}")]


def _label(text):
    return [Btn(text, callback_data="noop")]


def _choices(panel, key, s, per_row=3):
    buttons = [
        Btn(("🔘 " if s[key] == value else "") + label, callback_data=f"v:{panel}:{key}:{value}")
        for value, label in CHOICES[key]
    ]
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


BACK = [Btn("« Back", callback_data="s:main")]


def panel_view(chat_id: int, panel: str, title: str = ""):
    s = settings(chat_id)
    media = fmt_duration(s["media_delay"]) if s["media_delay"] else "off"
    if panel == "edit":
        text = (
            "✏️ <b>Edit Guard</b>\n"
            "Deletes any message that gets edited and tells the user why.\n\n"
            f"Current delay: <b>{fmt_duration(s['edit_delay'])}</b>\n"
            "Custom delay: <code>/editdelay 3m</code>\n"
            "Allow a user to edit: <code>/auth</code>"
        )
        rows = [
            _toggle(panel, "edit_on", "Edit Guard", s),
            _label("⏱ Delete after"),
            *_choices(panel, "edit_delay", s),
            _label("👥 Applies to"),
            *_choices(panel, "edit_scope", s),
            BACK,
        ]
    elif panel == "media":
        text = (
            "🖼 <b>Media auto-delete</b>\n"
            "Removes photos, videos, stickers, GIFs, files and voice notes after a delay. "
            "Text messages are never touched.\n\n"
            f"Current delay: <b>{media}</b>\n"
            "Custom delay: <code>/mediadelay 15m</code>\n"
            "Exempt a user: <code>/authmedia</code>"
        )
        rows = [
            _label("⏱ Delete media after"),
            *_choices(panel, "media_delay", s),
            _label("👥 Applies to"),
            *_choices(panel, "media_scope", s),
            BACK,
        ]
    elif panel == "nsfw":
        text = (
            "🔞 <b>NSFW protection</b>\n"
            "Scans stickers, photos, videos and GIFs and removes adult content. "
            "Text is not scanned.\n\nExempt a user: <code>/authmedia</code>"
        )
        rows = [
            _toggle(panel, "nsfw", "NSFW protection", s),
            _label("👥 Applies to"),
            *_choices(panel, "nsfw_scope", s),
            BACK,
        ]
    elif panel == "locks":
        text = (
            "🔒 <b>Locks</b>\n"
            "Block content from regular members. Admins and <code>/auth</code> users are not affected."
        )
        rows = [
            _toggle(panel, "antilink", "🔗 Block links", s),
            _toggle(panel, "antiforward", "↪️ Block forwards", s),
            BACK,
        ]
    elif panel == "warns":
        text = (
            "⚠️ <b>Warnings</b>\n"
            "<code>/warn</code> adds a warning. When a member reaches the limit, "
            "the chosen action is applied automatically."
        )
        rows = [
            _label("🔢 Warning limit"),
            *_choices(panel, "warn_limit", s, per_row=4),
            _label("⚖️ Action at limit"),
            *_choices(panel, "warn_action", s),
            BACK,
        ]
    elif panel == "welcome":
        text = (
            "👋 <b>Welcome &amp; rules</b>\n\n"
            f"<b>Welcome message:</b>\n{html.escape(s['welcome_text'] or DEFAULT_WELCOME)}\n\n"
            f"<b>Rules:</b> {'set' if s['rules'] else 'not set'}\n\n"
            "Change with <code>/setwelcome</code> and <code>/setrules</code>."
        )
        rows = [_toggle(panel, "welcome_on", "Welcome new members", s), BACK]
    elif panel == "clean":
        text = (
            "🧹 <b>Cleanup</b>\n"
            "Keep the chat tidy: hide join/leave messages and let my own notices "
            "disappear after a while."
        )
        rows = [
            _toggle(panel, "clean_service", "Delete join/leave messages", s),
            _label("🗑 Remove my notices after"),
            *_choices(panel, "notice_ttl", s, per_row=4),
            BACK,
        ]
    else:
        text = (
            f"⚙️ <b>Settings{' — ' + html.escape(title) if title else ''}</b>\n\n"
            f"✏️ Edit Guard: <b>{_state(s['edit_on'])}</b> · {fmt_duration(s['edit_delay'])} "
            f"· {SCOPE_LABEL[s['edit_scope']]}\n"
            f"🖼 Media auto-delete: <b>{media}</b> · {SCOPE_LABEL[s['media_scope']]}\n"
            f"🔞 NSFW protection: <b>{_state(s['nsfw'])}</b> · {SCOPE_LABEL[s['nsfw_scope']]}\n"
            f"🔒 Locks: links <b>{_state(s['antilink'])}</b> · forwards <b>{_state(s['antiforward'])}</b>\n"
            f"⚠️ Warnings: limit <b>{s['warn_limit']}</b> → {s['warn_action']}\n"
            f"👋 Welcome: <b>{_state(s['welcome_on'])}</b>\n\n"
            "Choose a section to configure:"
        )
        rows = [
            [Btn("✏️ Edit Guard", callback_data="s:edit"), Btn("🖼 Media", callback_data="s:media")],
            [Btn("🔞 NSFW", callback_data="s:nsfw"), Btn("🔒 Locks", callback_data="s:locks")],
            [Btn("⚠️ Warnings", callback_data="s:warns"), Btn("👋 Welcome", callback_data="s:welcome")],
            [Btn("🧹 Cleanup", callback_data="s:clean"), Btn("✖️ Close", callback_data="s:close")],
        ]
    return text, InlineKeyboardMarkup(rows)


@admin_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text, markup = panel_view(chat.id, "main", chat.title)
    await reply(update.effective_message, text, markup)


# --------------------------------------------------------------------- help
HELP_SECTIONS = {
    "protect": (
        "🛡 Protection",
        "<b>🛡 Protection</b>\n\n"
        "/settings — open the settings panel\n"
        "/editdelay <code>2m</code> — delete edited messages after a delay (<code>0</code> = instant)\n"
        "/mediadelay <code>10m</code> — auto-delete all media after a delay (<code>off</code> to disable)\n"
        "/nsfw <code>on|off</code> — remove adult stickers, photos, videos and GIFs\n\n"
        "<b>Exemptions</b>\n"
        "/auth — allow a user to edit messages\n"
        "/unauth — revoke edit permission\n"
        "/authmedia — never delete this user's media\n"
        "/unauthmedia — revoke media exemption\n"
        "/authlist — list authorized users",
    ),
    "mod": (
        "👮 Moderation",
        "<b>👮 Moderation</b>\n"
        "<i>Reply to a user, or pass an ID / @username. Duration and reason are optional.</i>\n\n"
        "/ban <code>[time] [reason]</code> — ban a user\n"
        "/unban — lift a ban\n"
        "/kick <code>[reason]</code> — remove a user (they can rejoin)\n"
        "/mute <code>[time] [reason]</code> — stop a user from sending messages\n"
        "/unmute — lift a mute\n"
        "/warn <code>[reason]</code> — warn a user\n"
        "/unwarn — remove the latest warning\n"
        "/resetwarns — clear all warnings\n"
        "/warns — show a user's warnings\n"
        "/info — show details about a user",
    ),
    "tools": (
        "🧰 Tools",
        "<b>🧰 Admin tools</b>\n\n"
        "/del — delete the replied message\n"
        "/purge — delete everything from the replied message down\n"
        "/pin <code>[loud]</code> — pin the replied message\n"
        "/unpin — unpin the replied (or latest) message\n"
        "/setwelcome <code>&lt;text&gt;</code> — set the welcome message\n"
        "/welcome <code>on|off</code> — toggle welcome messages\n"
        "/setrules <code>&lt;text&gt;</code> — set the group rules",
    ),
    "members": (
        "👥 Members",
        "<b>👥 For everyone</b>\n\n"
        "/rules — read the group rules\n"
        "/report — report the replied message to the admins\n"
        "/warns — check your warnings\n"
        "/id — show chat and user IDs\n"
        "/help — show this menu",
    ),
}
HELP_HOME = (
    "🛡 <b>Edit Guard Bot</b>\n"
    "I keep your group clean: I remove edited messages, auto-delete media, filter NSFW "
    "content and give admins a full moderation toolkit.\n\n"
    "<b>Setup:</b> add me to a group, make me admin with <i>Delete messages</i> and "
    "<i>Ban users</i>, then send /settings there.\n\n"
    "Pick a category to see the commands:"
)


def help_markup(context, private: bool):
    keys = list(HELP_SECTIONS)
    rows = [
        [Btn(HELP_SECTIONS[k][0], callback_data=f"h:{k}") for k in keys[i : i + 2]]
        for i in range(0, len(keys), 2)
    ]
    if private:
        url = f"https://t.me/{context.bot.username}?startgroup=true"
        rows.append([Btn("➕ Add me to a group", url=url)])
    return InlineKeyboardMarkup(rows)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    private = update.effective_chat.type == "private"
    if private and context.args and context.args[0].startswith("rules_"):
        try:
            chat_id = int(context.args[0][6:])
        except ValueError:
            chat_id = 0
        rules = settings(chat_id)["rules"] if chat_id else None
        return await reply(
            msg, f"📜 <b>Group rules</b>\n\n{html.escape(rules)}" if rules else "ℹ️ No rules found."
        )
    await reply(msg, HELP_HOME, help_markup(context, private))


# ---------------------------------------------------------------- callbacks
async def _safe_edit(q, text, markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    kind = parts[0]
    chat = q.message.chat

    if kind == "noop":
        return await q.answer()

    if kind == "h":  # help pages are open to everyone
        if parts[1] == "home":
            await _safe_edit(q, HELP_HOME, help_markup(context, chat.type == "private"))
        else:
            back = InlineKeyboardMarkup([[Btn("« Back", callback_data="h:home")]])
            await _safe_edit(q, HELP_SECTIONS[parts[1]][1], back)
        return await q.answer()

    if not await is_admin(context, chat.id, q.from_user.id):
        return await q.answer("🚫 Only group admins can use these buttons.", show_alert=True)

    if kind == "s":
        if parts[1] == "close":
            await q.answer()
            return await q.message.delete()
        text, markup = panel_view(chat.id, parts[1], chat.title)
        await _safe_edit(q, text, markup)
        return await q.answer()

    if kind == "t" and parts[2] in TOGGLES:
        panel, key = parts[1], parts[2]
        new = 0 if settings(chat.id)[key] else 1
        set_setting(chat.id, key, new)
        text, markup = panel_view(chat.id, panel, chat.title)
        await _safe_edit(q, text, markup)
        return await q.answer("Enabled" if new else "Disabled")

    if kind == "v" and parts[2] in CHOICES:
        panel, key, raw = parts[1], parts[2], parts[3]
        match = [(v, label) for v, label in CHOICES[key] if str(v) == raw]
        if not match:
            return await q.answer()
        set_setting(chat.id, key, match[0][0])
        text, markup = panel_view(chat.id, panel, chat.title)
        await _safe_edit(q, text, markup)
        return await q.answer(f"Set to {match[0][1]}")

    admin = link(q.from_user.id, q.from_user.first_name, q.from_user.username)
    try:
        if kind == "m":  # undo buttons under moderation notices
            action, uid = parts[1], int(parts[2])
            if action == "unban":
                await context.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
                done = "Ban lifted"
            elif action == "unmute":
                await do_unmute(context.bot, chat.id, uid)
                done = "Mute lifted"
            else:
                done = "Warning removed" if remove_last_warn(chat.id, uid) else "No warnings left"
            await _safe_edit(q, f"{q.message.text_html}\n\n✅ <b>{done}</b> by {admin}.")
            return await q.answer(done)

        if kind == "r":  # report actions
            action, mid, uid = parts[1], int(parts[2]), int(parts[3])
            if action == "x":
                await q.answer("Report dismissed")
                return await q.message.delete()
            if action in ("del", "mute", "ban"):
                await delete(context.bot, chat.id, mid)
            if action == "mute":
                await do_mute(context.bot, chat.id, uid)
            elif action == "ban":
                await context.bot.ban_chat_member(chat.id, uid)
            elif action == "warn":
                db.execute("INSERT INTO warns VALUES(?,?,?,?)", (chat.id, uid, "Reported message", time.time()))
            done = {
                "del": "Message deleted",
                "mute": "User muted",
                "ban": "User banned",
                "warn": f"User warned ({warn_count(chat.id, uid)}/{settings(chat.id)['warn_limit']})",
            }[action]
            await _safe_edit(q, f"🚨 <b>Report handled</b>\n✅ {done} by {admin}.")
            return await q.answer(done)
    except TelegramError as e:
        return await q.answer(f"Failed: {e}. Check my admin permissions.", show_alert=True)
    await q.answer()


# --------------------------------------------------------------------- main
ADMIN_COMMANDS = [
    ("settings", "Open the settings panel"),
    ("editdelay", "Delay before edited messages are deleted"),
    ("mediadelay", "Auto-delete media after a delay"),
    ("nsfw", "Turn NSFW protection on or off"),
    ("auth", "Allow a user to edit messages"),
    ("unauth", "Revoke edit permission"),
    ("authmedia", "Exempt a user's media from deletion"),
    ("unauthmedia", "Revoke media exemption"),
    ("authlist", "List authorized users"),
    ("ban", "Ban a user"),
    ("unban", "Unban a user"),
    ("kick", "Remove a user from the group"),
    ("mute", "Mute a user"),
    ("unmute", "Unmute a user"),
    ("warn", "Warn a user"),
    ("unwarn", "Remove a user's latest warning"),
    ("resetwarns", "Clear all warnings of a user"),
    ("warns", "Show a user's warnings"),
    ("info", "Show details about a user"),
    ("del", "Delete the replied message"),
    ("purge", "Delete messages from the replied one down"),
    ("pin", "Pin the replied message"),
    ("unpin", "Unpin a message"),
    ("setwelcome", "Set the welcome message"),
    ("welcome", "Turn welcome messages on or off"),
    ("setrules", "Set the group rules"),
    ("rules", "Show the group rules"),
    ("id", "Show chat and user IDs"),
    ("help", "Show all commands"),
]
MEMBER_COMMANDS = [
    ("rules", "Show the group rules"),
    ("report", "Report a message to the admins"),
    ("warns", "Check your warnings"),
    ("id", "Show chat and user IDs"),
    ("help", "Show all commands"),
]
PRIVATE_COMMANDS = [("start", "Start the bot"), ("help", "Show all commands")]


async def post_init(app: Application):
    """Publish the '/' command menus: a full list for admins, a short one for members."""
    as_commands = lambda items: [BotCommand(c, d) for c, d in items]  # noqa: E731
    bot = app.bot
    await bot.set_my_commands(as_commands(PRIVATE_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(as_commands(MEMBER_COMMANDS), scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(
        as_commands(ADMIN_COMMANDS), scope=BotCommandScopeAllChatAdministrators()
    )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable (get one from @BotFather).")
    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(TypeHandler(Update, track_user), group=-1)
    groups = filters.ChatType.GROUPS
    # Must be first: every edited message (including edited commands) goes here.
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & groups, on_edit))

    commands = {
        ("start", "help"): cmd_help,
        ("settings",): cmd_settings,
        ("editdelay", "set_delay", "setdelay"): cmd_editdelay,
        ("mediadelay", "set_delaymedia", "setdelaymedia"): cmd_mediadelay,
        ("nsfw",): cmd_nsfw,
        ("auth",): _auth_command("edit", True, "✅ {} may now edit messages."),
        ("unauth",): _auth_command("edit", False, "✅ {} may no longer edit messages."),
        ("authmedia", "auth_media"): _auth_command(
            "media", True, "✅ {}'s media is now exempt from deletion."
        ),
        ("unauthmedia", "unauth_media"): _auth_command(
            "media", False, "✅ {}'s media is no longer exempt."
        ),
        ("authlist",): cmd_authlist,
        ("ban",): cmd_ban,
        ("unban",): cmd_unban,
        ("kick",): cmd_kick,
        ("mute",): cmd_mute,
        ("unmute",): cmd_unmute,
        ("warn",): cmd_warn,
        ("unwarn",): cmd_unwarn,
        ("resetwarns",): cmd_resetwarns,
        ("warns",): cmd_warns,
        ("info",): cmd_info,
        ("del",): cmd_del,
        ("purge",): cmd_purge,
        ("pin",): cmd_pin,
        ("unpin",): cmd_unpin,
        ("setwelcome",): cmd_setwelcome,
        ("welcome",): cmd_welcome,
        ("setrules",): cmd_setrules,
        ("rules",): cmd_rules,
        ("report",): cmd_report,
        ("id",): cmd_id,
    }
    for names, handler in commands.items():
        app.add_handler(CommandHandler(list(names), handler))
    app.add_handler(CallbackQueryHandler(on_button))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & groups, on_join))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER & groups, on_leave))
    app.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & groups & ~filters.StatusUpdate.ALL, on_message)
    )

    app.job_queue.run_repeating(sweep, interval=3, first=3)
    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
