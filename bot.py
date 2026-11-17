"""Edit Guard Bot - edit protection, content control, NSFW filter and group moderation."""
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
LOG_RETENTION = 48 * 3600  # how long message IDs are remembered for /purgeuser
NSFW_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
    "BUTTOCKS_EXPOSED",
}
DEFAULT_WELCOME = "Hey {name}, welcome to {group}. Glad you're here!"

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
    CREATE TABLE IF NOT EXISTS blocklist(
        chat_id INTEGER, term TEXT, PRIMARY KEY(chat_id, term));
    CREATE TABLE IF NOT EXISTS msglog(
        chat_id INTEGER, message_id INTEGER, user_id INTEGER, ts REAL,
        PRIMARY KEY(chat_id, message_id));
    CREATE INDEX IF NOT EXISTS msglog_user ON msglog(chat_id, user_id);
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
    "lock_files": "INTEGER DEFAULT 0",
    "lock_videos": "INTEGER DEFAULT 0",
    "lock_audio": "INTEGER DEFAULT 0",
    "lock_channels": "INTEGER DEFAULT 0",
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


def blocked_terms(chat_id: int) -> list:
    return [r["term"] for r in db.execute("SELECT term FROM blocklist WHERE chat_id=?", (chat_id,))]


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
        return "immediately"
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
    """-> (sender_id, is_admin). Anonymous admins post as the group itself."""
    if msg.sender_chat:
        return msg.sender_chat.id, msg.sender_chat.id == msg.chat_id
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


async def bulk_delete(bot, chat_id: int, ids: list) -> None:
    for i in range(0, len(ids), 100):
        await bot.delete_messages(chat_id, ids[i : i + 100])


async def reply(msg, text: str, markup=None):
    return await msg.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup
    )


EDIT_NOTICE = "✏️ {} — edited messages aren't kept in this chat, so yours was removed."
NSFW_NOTICE = "🔞 {} — that media was flagged as explicit and has been removed."
LOCK_NOTICE = "🔒 {} — {} are switched off in this chat. Your message was removed."
BLOCK_NOTICE = "⛔ {} — your message matched this chat's blocklist and was removed."


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


# ---------------------------------------------------------- content control
LINK_TYPES = (MessageEntityType.URL, MessageEntityType.TEXT_LINK)


def has_link(msg) -> bool:
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    return any(e.type in LINK_TYPES for e in entities)


# name -> (settings column, button label, plural noun for notices, test)
LOCKS = {
    "links": ("antilink", "🔗 Links", "links", has_link),
    "forwards": ("antiforward", "↪️ Forwards", "forwarded messages", lambda m: bool(m.forward_origin)),
    "files": ("lock_files", "📁 Files", "file uploads", lambda m: bool(m.document and not m.animation)),
    "videos": ("lock_videos", "🎬 Videos", "videos", lambda m: bool(m.video or m.video_note)),
    "audio": ("lock_audio", "🎵 Audio", "audio and voice messages", lambda m: bool(m.audio or m.voice)),
    "channels": ("lock_channels", "📢 Channel posts", "posts sent as a channel", None),
}


def searchable_text(msg) -> str:
    """Everything a blocklist term can match: text, caption, file names, forward source."""
    parts = [msg.text, msg.caption]
    for media in (msg.document, msg.video, msg.audio):
        parts.append(getattr(media, "file_name", None))
        parts.append(getattr(media, "title", None))
    origin = msg.forward_origin
    if origin:
        src = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        parts += [getattr(src, "title", None), getattr(src, "username", None)]
    return " ".join(p for p in parts if p).casefold()


def content_violation(msg, s, terms):
    """-> notice template args for the first lock or blocklist rule the message breaks."""
    for name, (column, _, noun, test) in LOCKS.items():
        if test and s[column] and test(msg):
            return LOCK_NOTICE, noun
    if terms:
        haystack = searchable_text(msg)
        if any(t in haystack for t in terms):
            return BLOCK_NOTICE, None
    return None


async def enforce_content(msg, s, context, terms) -> bool:
    """Delete the message if it breaks a lock or the blocklist. True when removed."""
    hit = content_violation(msg, s, terms)
    if not hit:
        return False
    template, noun = hit
    if await delete(context.bot, msg.chat_id, msg.message_id):
        who = mention_of(msg)
        await notify(context.bot, msg.chat_id, template.format(who, noun) if noun else template.format(who))
    return True


# --------------------------------------------------------- message handlers
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember usernames (for @username lookups) and recent message IDs (for /purgeuser)."""
    u = update.effective_user
    if u and u.username:
        db.execute(
            "INSERT OR REPLACE INTO users VALUES(?,?,?)", (u.username.lower(), u.id, u.first_name)
        )
    m = update.message
    if m and m.chat.type in ("group", "supergroup") and (m.sender_chat or m.from_user):
        uid = m.sender_chat.id if m.sender_chat else m.from_user.id
        db.execute(
            "INSERT OR REPLACE INTO msglog VALUES(?,?,?,?)", (m.chat_id, m.message_id, uid, time.time())
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
    trusted = is_authed(msg.chat_id, uid, "edit")
    if s["edit_on"] and not trusted and not (admin and s["edit_scope"] == "user"):
        mention = mention_of(msg)
        if s["edit_delay"] > 0:
            schedule(msg.chat_id, msg.message_id, s["edit_delay"], "edit", mention)
        elif await delete(context.bot, msg.chat_id, msg.message_id):
            await notify(context.bot, msg.chat_id, EDIT_NOTICE.format(mention))
        return
    # The edit is allowed to stay - make sure it didn't sneak in restricted content.
    if not admin and not trusted:
        await enforce_content(msg, s, context, blocked_terms(msg.chat_id))


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
    bot, chat_id = context.bot, msg.chat_id
    media = is_media(msg)
    terms = blocked_terms(chat_id)
    locks_on = any(s[column] for column, *_ in LOCKS.values())
    if not (locks_on or terms or (media and (s["nsfw"] or s["media_delay"]))):
        return

    if s["lock_channels"] and msg.sender_chat and msg.sender_chat.id != chat_id:
        if await delete(bot, chat_id, msg.message_id):
            await notify(bot, chat_id, LOCK_NOTICE.format(mention_of(msg), LOCKS["channels"][2]))
        return

    uid, admin = await sender_info(msg, context)
    # Locks and the blocklist apply to regular members; trusted users are skipped.
    if not admin and not is_authed(chat_id, uid, "edit"):
        if await enforce_content(msg, s, context, terms):
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
    return InlineKeyboardMarkup([[Btn("📜 Group rules", url=url)]])


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    s = settings(msg.chat_id)
    for user in msg.new_chat_members:
        if user.id == context.bot.id:
            await notify(
                context.bot,
                msg.chat_id,
                "<b>Thanks for the invite.</b>\n"
                "To get started, promote me to admin with <i>Delete messages</i>, "
                "<i>Ban users</i> and <i>Pin messages</i>, then run /settings.",
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


_last_prune = 0.0


async def sweep(context: ContextTypes.DEFAULT_TYPE):
    """Runs every few seconds: delete everything whose timer has expired."""
    global _last_prune
    now = time.time()
    rows = db.execute("SELECT * FROM pending WHERE due<=?", (now,)).fetchall()
    for r in rows:
        db.execute(
            "DELETE FROM pending WHERE chat_id=? AND message_id=?", (r["chat_id"], r["message_id"])
        )
        ok = await delete(context.bot, r["chat_id"], r["message_id"])
        if ok and r["kind"] == "edit":
            await notify(context.bot, r["chat_id"], EDIT_NOTICE.format(r["mention"]))
    if now - _last_prune > 3600:
        _last_prune = now
        db.execute("DELETE FROM msglog WHERE ts<?", (now - LOG_RETENTION,))


# --------------------------------------------------------- command plumbing
def group_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == "private":
            await reply(update.effective_message, "This command is meant for group chats.")
            return
        await func(update, context)

    return wrapper


def admin_only(func):
    @group_only
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        _, admin = await sender_info(update.effective_message, context)
        if not admin:
            await reply(update.effective_message, "This command is reserved for chat admins.")
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
    "<b>Who should this apply to?</b>\n"
    "Reply to one of their messages, or add their user ID or @username after the command.\n"
    "<i>Usernames resolve only for people I've already seen in a chat.</i>"
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
        "<b>That didn't go through.</b>\n"
        f"Telegram said: <code>{html.escape(str(error))}</code>\n"
        "Usually this means I'm missing an admin permission for this action.",
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
                f"<b>How to use /{name}</b>\n"
                f"<code>/{name} 2m</code> — set a delay\n"
                f"<code>/{name} off</code> — remove the delay\n\n"
                "Time can be written as <code>30s</code>, <code>10m</code>, <code>1h 30m</code> "
                "and so on, up to 48 hours.",
            )
            return
        set_setting(msg.chat_id, key, sec)
        await reply(msg, on_text.format(fmt_duration(sec)) if sec else off_text)

    return handler


cmd_editdelay = _delay_command(
    "edit_delay",
    "editdelay",
    "<b>Edit delay updated.</b>\nEdited messages will now be removed <b>{}</b> after the edit.",
    "<b>Edit delay cleared.</b>\nEdited messages will now be removed immediately.",
)
cmd_mediadelay = _delay_command(
    "media_delay",
    "mediadelay",
    "<b>Media timer updated.</b>\nMedia will now disappear <b>{}</b> after it's posted.",
    "<b>Media timer switched off.</b>\nMedia will stay in the chat.",
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
async def cmd_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    lines = ["<b>Exemptions in this chat</b>"]
    for kind, title in (("edit", "Can edit messages"), ("media", "Media is never removed")):
        rows = db.execute(
            "SELECT * FROM auth WHERE chat_id=? AND kind=?", (msg.chat_id, kind)
        ).fetchall()
        lines.append(f"\n<b>{title}</b>")
        lines += [
            f"  • {link(r['user_id'], r['name'])} · <code>{r['user_id']}</code>" for r in rows
        ] or ["  <i>nobody yet</i>"]
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_nsfw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    arg = context.args[0].lower() if context.args else ""
    if arg not in ("on", "off"):
        state = "active" if settings(msg.chat_id)["nsfw"] else "inactive"
        await reply(
            msg,
            f"<b>Explicit-media filter is {state}.</b>\n"
            "Switch it with <code>/nsfw on</code> or <code>/nsfw off</code>.",
        )
        return
    set_setting(msg.chat_id, "nsfw", int(arg == "on"))
    await reply(
        msg,
        "<b>Explicit-media filter enabled.</b>\n"
        "I'll scan stickers, photos, videos and GIFs and remove anything explicit."
        if arg == "on"
        else "<b>Explicit-media filter disabled.</b>",
    )


@admin_only
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = (msg.text or "").split(None, 1)
    terms = [t.strip().casefold() for t in raw[1].split(",")] if len(raw) > 1 else []
    terms = [t[:80] for t in terms if len(t) >= 2]
    if not terms:
        return await reply(
            msg,
            "<b>How to use /block</b>\n"
            "<code>/block movie name, leaked, camrip</code>\n\n"
            "Separate several words or phrases with commas. I match them in messages, "
            "captions, file names and the names of channels a message was forwarded from.",
        )
    for term in terms:
        db.execute("INSERT OR IGNORE INTO blocklist VALUES(?,?)", (msg.chat_id, term))
    listed = ", ".join(f"<code>{html.escape(t)}</code>" for t in terms)
    await reply(msg, f"<b>Blocklist updated.</b>\nAdded: {listed}")


@admin_only
async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    raw = (msg.text or "").split(None, 1)
    terms = [t.strip().casefold() for t in raw[1].split(",") if t.strip()] if len(raw) > 1 else []
    if not terms:
        return await reply(msg, "Tell me what to remove, e.g. <code>/unblock camrip</code>.")
    removed = 0
    for term in terms:
        removed += db.execute(
            "DELETE FROM blocklist WHERE chat_id=? AND term=?", (msg.chat_id, term)
        ).rowcount
    await reply(
        msg,
        f"<b>Blocklist updated.</b>\nRemoved {removed} entr{'y' if removed == 1 else 'ies'}."
        if removed
        else "None of those were on the blocklist.",
    )


@admin_only
async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    terms = sorted(blocked_terms(msg.chat_id))
    if not terms:
        return await reply(msg, "The blocklist is empty. Add entries with <code>/block</code>.")
    body = "\n".join(f"  • <code>{html.escape(t)}</code>" for t in terms)
    await reply(msg, f"<b>Blocklist · {len(terms)} entries</b>\n{body}")


def _lock_command(value: int):
    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        names = [a.lower() for a in context.args]
        if names == ["all"]:
            names = list(LOCKS)
        if not names or any(n not in LOCKS for n in names):
            options = ", ".join(f"<code>{n}</code>" for n in LOCKS)
            verb = "lock" if value else "unlock"
            return await reply(
                msg,
                f"<b>How to use /{verb}</b>\n"
                f"<code>/{verb} files videos</code> or <code>/{verb} all</code>\n\n"
                f"Available types: {options}",
            )
        for n in names:
            set_setting(msg.chat_id, LOCKS[n][0], value)
        listed = ", ".join(names)
        await reply(msg, f"<b>{'Locked' if value else 'Unlocked'}:</b> {listed}")

    return handler


@admin_only
async def cmd_locks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    s = settings(msg.chat_id)
    lines = [f"  {'🔒' if s[col] else '🔓'} {name}" for name, (col, *_) in LOCKS.items()]
    await reply(
        msg,
        "<b>Content locks</b>\n" + "\n".join(lines) + "\n\nChange with <code>/lock</code> "
        "and <code>/unlock</code>, or from /settings.",
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
        await reply(msg, "I'd rather not do that to myself.")
        return None
    if uid < 0 or await is_admin(context, msg.chat_id, uid):
        await reply(msg, "That account is an admin or a channel, so I'll leave it alone.")
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


def _card(title: str, uid: int, name: str, rest, extra: str = "") -> str:
    """Uniform moderation notice."""
    lines = [f"<b>{title}</b>", f"User: {link(uid, name)} · <code>{uid}</code>"]
    if extra:
        lines.append(extra)
    if rest:
        lines.append(f"Reason: {html.escape(' '.join(rest))}")
    return "\n".join(lines)


async def _ban(update, context):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return False
    uid, name, rest = target
    seconds, rest = take_duration(rest)
    try:
        until = int(time.time()) + seconds if seconds else None
        await context.bot.ban_chat_member(msg.chat_id, uid, until_date=until)
    except TelegramError as e:
        await fail(msg, e)
        return False
    extra = f"Duration: {fmt_duration(seconds)}" if seconds else "Duration: permanent"
    await reply(msg, _card("🚫 Banned", uid, name, rest, extra), undo_button("Lift ban", "unban", uid))
    return True


async def _kick(update, context):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return False
    uid, name, rest = target
    try:
        await do_kick(context.bot, msg.chat_id, uid)
    except TelegramError as e:
        await fail(msg, e)
        return False
    await reply(msg, _card("👢 Removed from the chat", uid, name, rest, "They can rejoin later."))
    return True


async def _mute(update, context):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return False
    uid, name, rest = target
    seconds, rest = take_duration(rest)
    try:
        await do_mute(context.bot, msg.chat_id, uid, seconds)
    except TelegramError as e:
        await fail(msg, e)
        return False
    extra = f"Duration: {fmt_duration(seconds)}" if seconds else "Duration: until unmuted"
    await reply(msg, _card("🔇 Muted", uid, name, rest, extra), undo_button("Unmute", "unmute", uid))
    return True


WARN_ACTIONS = {"mute": "muted", "kick": "removed", "ban": "banned"}


async def _warn(update, context):
    msg = update.effective_message
    target = await _mod_target(update, context)
    if not target:
        return False
    uid, name, rest = target
    s = settings(msg.chat_id)
    db.execute("INSERT INTO warns VALUES(?,?,?,?)", (msg.chat_id, uid, " ".join(rest), time.time()))
    count = warn_count(msg.chat_id, uid)
    if count < s["warn_limit"]:
        await reply(
            msg,
            _card("⚠️ Warning issued", uid, name, rest, f"Count: {count} of {s['warn_limit']}"),
            undo_button("Withdraw warning", "unwarn", uid),
        )
        return True
    action = s["warn_action"]
    try:
        if action == "ban":
            await context.bot.ban_chat_member(msg.chat_id, uid)
        elif action == "kick":
            await do_kick(context.bot, msg.chat_id, uid)
        else:
            await do_mute(context.bot, msg.chat_id, uid)
    except TelegramError as e:
        await fail(msg, e)
        return False
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (msg.chat_id, uid))
    undo = {"ban": ("Lift ban", "unban"), "mute": ("Unmute", "unmute")}.get(action)
    await reply(
        msg,
        _card(
            "⛔ Warning limit reached",
            uid,
            name,
            rest,
            f"Count: {count} of {s['warn_limit']} — user has been {WARN_ACTIONS[action]}.",
        ),
        undo_button(undo[0], undo[1], uid) if undo else None,
    )
    return True


def _mod_command(action, also_delete: bool = False):
    """Wrap a moderation action; the 'd' variants also delete the replied message."""

    @admin_only
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        target = msg.reply_to_message
        if also_delete and (not target or target.forum_topic_created):
            return await reply(msg, "Reply to the offending message so I know what to delete.")
        if await action(update, context) and also_delete:
            await delete(context.bot, msg.chat_id, target.message_id)

    return handler


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
    await reply(msg, f"<b>Ban lifted.</b>\n{link(uid, name)} is free to rejoin.")


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
    await reply(msg, f"<b>Unmuted.</b>\n{link(uid, name)} can send messages again.")


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
            f"<b>Warning withdrawn.</b>\n{link(uid, name)} is now at "
            f"{warn_count(msg.chat_id, uid)} of {limit}.",
        )
    else:
        await reply(msg, f"{link(uid, name)} has a clean record — nothing to withdraw.")


@admin_only
async def cmd_clearwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (msg.chat_id, uid))
    await reply(msg, f"<b>Record cleared.</b>\n{link(uid, name)} starts fresh with zero warnings.")


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
    if not rows:
        return await reply(msg, f"{link(uid, name)} has no warnings on record.")
    lines = [f"<b>Warnings for {link(uid, name)}</b> · {len(rows)} of {limit}"]
    lines += [f"  {i}. {html.escape(r['reason'] or 'no reason noted')}" for i, r in enumerate(rows, 1)]
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await reply(msg, "Reply to the message you'd like removed.")
    await delete(context.bot, msg.chat_id, msg.reply_to_message.message_id)
    await delete(context.bot, msg.chat_id, msg.message_id)


@admin_only
async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/purge (reply) = from that message down; /purge N = the last N messages."""
    msg = update.effective_message
    count = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    start = msg.reply_to_message.message_id if msg.reply_to_message else None
    if start is None and not count:
        return await reply(
            msg,
            "<b>How to use /purge</b>\n"
            "Reply to a message with <code>/purge</code> to clear everything from there down, "
            "or send <code>/purge 50</code> to clear the latest 50 messages.\n"
            "Up to 500 messages per run.",
        )
    count = min(count, 500)
    if start is None:
        ids = list(range(max(1, msg.message_id - count), msg.message_id + 1))
    else:
        end = start + count - 1 if count else msg.message_id
        ids = list(range(start, min(end, msg.message_id) + 1))[-500:]
        if msg.message_id not in ids:
            ids.append(msg.message_id)
    try:
        await bulk_delete(context.bot, msg.chat_id, ids)
    except TelegramError as e:
        return await fail(msg, e)
    await notify(context.bot, msg.chat_id, "🧹 Cleanup finished.", ttl=5)


@admin_only
async def cmd_purgeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    ids = [
        r["message_id"]
        for r in db.execute(
            "SELECT message_id FROM msglog WHERE chat_id=? AND user_id=? "
            "ORDER BY message_id DESC LIMIT 500",
            (msg.chat_id, uid),
        )
    ]
    if not ids:
        return await reply(msg, f"I have no recent messages from {link(uid, name)} on record.")
    try:
        await bulk_delete(context.bot, msg.chat_id, ids)
    except TelegramError as e:
        return await fail(msg, e)
    db.execute("DELETE FROM msglog WHERE chat_id=? AND user_id=?", (msg.chat_id, uid))
    await reply(
        msg,
        f"<b>Messages cleared.</b>\nRemoved {len(ids)} recent message"
        f"{'' if len(ids) == 1 else 's'} from {link(uid, name)}.",
    )


@admin_only
async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg.reply_to_message:
        return await reply(msg, "Reply to the message you'd like pinned.")
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
        await reply(msg, "<b>Unpinned.</b>")
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
            "<b>How to use /setwelcome</b>\n"
            "<code>/setwelcome Hi {name}, welcome to {group}!</code>\n\n"
            "<code>{name}</code> becomes the new member's name and "
            "<code>{group}</code> the chat title.",
        )
    set_setting(msg.chat_id, "welcome_text", text[:1000])
    set_setting(msg.chat_id, "welcome_on", 1)
    preview = render_welcome(settings(msg.chat_id), msg.from_user, msg.chat.title)
    await reply(msg, f"<b>Greeting saved and switched on.</b> Here's how it looks:\n\n{preview}")


@admin_only
async def cmd_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    arg = context.args[0].lower() if context.args else ""
    if arg in ("on", "off"):
        set_setting(msg.chat_id, "welcome_on", int(arg == "on"))
        return await reply(msg, f"<b>Greetings switched {arg}.</b>")
    s = settings(msg.chat_id)
    await reply(
        msg,
        f"<b>Greetings are {'on' if s['welcome_on'] else 'off'}.</b> Current text:\n\n"
        f"{render_welcome(s, msg.from_user, msg.chat.title)}\n\n"
        "Toggle with <code>/welcome on|off</code>, edit with <code>/setwelcome</code>.",
    )


@admin_only
async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = _text_after_command(msg)
    if not text:
        return await reply(
            msg,
            "Add the rules after the command, or reply to a message containing them "
            "with <code>/setrules</code>.",
        )
    set_setting(msg.chat_id, "rules", text[:3500])
    await reply(msg, "<b>Rules saved.</b>\nAnyone can read them with /rules.")


@group_only
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    rules = settings(msg.chat_id)["rules"]
    if not rules:
        return await reply(msg, "This chat hasn't published any rules yet.")
    await reply(msg, f"<b>{html.escape(msg.chat.title or 'Chat')} · Rules</b>\n\n{html.escape(rules)}")


@group_only
async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    target = msg.reply_to_message
    if not target or target.forum_topic_created:
        return await reply(msg, "Reply to the message you want the admins to look at.")
    uid = target.sender_chat.id if target.sender_chat else target.from_user.id
    if target.sender_chat or await is_admin(context, msg.chat_id, uid):
        return await reply(msg, "That message is from an admin, so there's nothing to flag.")
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
                Btn("Delete", callback_data=f"r:del:{mid}:{uid}"),
                Btn("Warn", callback_data=f"r:warn:{mid}:{uid}"),
            ],
            [
                Btn("Mute", callback_data=f"r:mute:{mid}:{uid}"),
                Btn("Ban", callback_data=f"r:ban:{mid}:{uid}"),
            ],
            [Btn("Ignore", callback_data=f"r:x:{mid}:{uid}")],
        ]
    )
    await target.reply_text(
        f"🚩 <b>Flagged for review</b>{ping}\n"
        f"Reported by {mention_of(msg)} · Author: {mention_of(target)}\n"
        "<i>Admins: choose an action below.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


@group_only
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    lines = [f"Chat · <code>{msg.chat_id}</code>"]
    r = msg.reply_to_message
    if r and not r.forum_topic_created and r.from_user:
        lines.append(f"{html.escape(r.from_user.first_name)} · <code>{r.from_user.id}</code>")
    else:
        lines.append(f"You · <code>{msg.from_user.id}</code>")
    await reply(msg, "\n".join(lines))


@admin_only
async def cmd_whois(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid, name, _ = resolve_target(msg, context.args)
    if uid is None:
        return await reply(msg, NO_TARGET)
    try:
        status = (await context.bot.get_chat_member(msg.chat_id, uid)).status
    except TelegramError:
        status = "unknown"
    limit = settings(msg.chat_id)["warn_limit"]
    mark = lambda v: "yes" if v else "no"  # noqa: E731
    await reply(
        msg,
        f"<b>Profile · {link(uid, name)}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Role: {status}\n"
        f"Warnings: {warn_count(msg.chat_id, uid)} of {limit}\n"
        f"Edit exemption: {mark(is_authed(msg.chat_id, uid, 'edit'))}\n"
        f"Media exemption: {mark(is_authed(msg.chat_id, uid, 'media'))}",
    )


# ----------------------------------------------------------- settings panel
SCOPES = (("user", "Members"), ("all", "Everyone"))
CHOICES = {
    "edit_delay": ((0, "Now"), (30, "30s"), (60, "1m"), (120, "2m"), (300, "5m"), (600, "10m")),
    "media_delay": ((0, "Never"), (60, "1m"), (300, "5m"), (600, "10m"), (1800, "30m"), (3600, "1h")),
    "notice_ttl": ((0, "Never"), (30, "30s"), (60, "1m"), (300, "5m")),
    "warn_limit": ((2, "2"), (3, "3"), (4, "4"), (5, "5")),
    "warn_action": (("mute", "Mute"), ("kick", "Remove"), ("ban", "Ban")),
    "edit_scope": SCOPES,
    "nsfw_scope": SCOPES,
    "media_scope": SCOPES,
}
TOGGLES = {"edit_on", "nsfw", "welcome_on", "clean_service"} | {col for col, *_ in LOCKS.values()}
SCOPE_LABEL = dict(SCOPES)


def _state(on) -> str:
    return "on" if on else "off"


def _tbtn(panel, key, label, s):
    return Btn(f"{'🟢' if s[key] else '⚪️'} {label}", callback_data=f"t:{panel}:{key}")


def _label(text):
    return [Btn(f"— {text} —", callback_data="noop")]


def _choices(panel, key, s, per_row=3):
    buttons = [
        Btn(("● " if s[key] == value else "") + label, callback_data=f"v:{panel}:{key}:{value}")
        for value, label in CHOICES[key]
    ]
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


BACK = [Btn("‹ Overview", callback_data="s:main")]


def panel_view(chat_id: int, panel: str, title: str = ""):
    s = settings(chat_id)
    media = fmt_duration(s["media_delay"]) if s["media_delay"] else "off"
    active_locks = [name for name, (col, *_) in LOCKS.items() if s[col]]
    if panel == "edit":
        text = (
            "<b>Edit protection</b>\n"
            "When someone edits a message, I remove it and let them know why.\n\n"
            f"Removal: <b>{fmt_duration(s['edit_delay'])}</b>\n"
            "Need a different delay? <code>/editdelay 3m</code>\n"
            "Let someone edit freely: <code>/allowedit</code>"
        )
        rows = [
            [_tbtn(panel, "edit_on", "Edit protection", s)],
            _label("Remove after"),
            *_choices(panel, "edit_delay", s),
            _label("Enforce on"),
            *_choices(panel, "edit_scope", s),
            BACK,
        ]
    elif panel == "media":
        text = (
            "<b>Media timer</b>\n"
            "Photos, videos, stickers, GIFs, files and voice notes vanish after the time "
            "you choose. Plain text is left alone.\n\n"
            f"Timer: <b>{media}</b>\n"
            "Need a different time? <code>/mediadelay 15m</code>\n"
            "Keep someone's media: <code>/allowmedia</code>"
        )
        rows = [
            _label("Remove media after"),
            *_choices(panel, "media_delay", s),
            _label("Enforce on"),
            *_choices(panel, "media_scope", s),
            BACK,
        ]
    elif panel == "nsfw":
        text = (
            "<b>Explicit-media filter</b>\n"
            "Every sticker, photo, video and GIF is scanned on arrival; explicit material "
            "is removed automatically. Text isn't scanned.\n\n"
            "Skip scanning for someone: <code>/allowmedia</code>"
        )
        rows = [
            [_tbtn(panel, "nsfw", "Explicit-media filter", s)],
            _label("Enforce on"),
            *_choices(panel, "nsfw_scope", s),
            BACK,
        ]
    elif panel == "locks":
        terms = len(blocked_terms(chat_id))
        text = (
            "<b>Content locks</b>\n"
            "Stop members from posting the kinds of content that put a group at risk — "
            "pirated files, movie and music uploads, spam links, channel reposts.\n\n"
            f"Blocklist: <b>{terms}</b> entr{'y' if terms == 1 else 'ies'} "
            "(<code>/block</code>, <code>/blocklist</code>)\n"
            "<i>Admins and /allowedit users are never restricted.</i>"
        )
        buttons = [_tbtn(panel, col, label, s) for col, label, *_ in LOCKS.values()]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows += [
            [Btn("Lock everything", callback_data="la:1"), Btn("Unlock everything", callback_data="la:0")],
            BACK,
        ]
    elif panel == "warns":
        text = (
            "<b>Warning system</b>\n"
            "Each <code>/warn</code> adds to a member's record. Once they hit the limit, "
            "the penalty below is applied and their record resets."
        )
        rows = [
            _label("Limit"),
            *_choices(panel, "warn_limit", s, per_row=4),
            _label("Penalty"),
            *_choices(panel, "warn_action", s),
            BACK,
        ]
    elif panel == "welcome":
        text = (
            "<b>Greetings &amp; rules</b>\n\n"
            f"Greeting text:\n<i>{html.escape(s['welcome_text'] or DEFAULT_WELCOME)}</i>\n\n"
            f"Rules: <b>{'published' if s['rules'] else 'not set'}</b>\n\n"
            "Edit with <code>/setwelcome</code> and <code>/setrules</code>."
        )
        rows = [[_tbtn(panel, "welcome_on", "Greet new members", s)], BACK]
    elif panel == "clean":
        text = (
            "<b>Housekeeping</b>\n"
            "Keep the chat tidy by hiding join/leave notices and letting my own "
            "notices expire."
        )
        rows = [
            [_tbtn(panel, "clean_service", "Hide join/leave notices", s)],
            _label("Expire my notices after"),
            *_choices(panel, "notice_ttl", s, per_row=4),
            BACK,
        ]
    else:
        text = (
            f"<b>Control panel{' · ' + html.escape(title) if title else ''}</b>\n\n"
            f"Edit protection — <b>{_state(s['edit_on'])}</b>, removes {fmt_duration(s['edit_delay'])}, "
            f"{SCOPE_LABEL[s['edit_scope']].lower()}\n"
            f"Media timer — <b>{media}</b>, {SCOPE_LABEL[s['media_scope']].lower()}\n"
            f"Explicit-media filter — <b>{_state(s['nsfw'])}</b>, {SCOPE_LABEL[s['nsfw_scope']].lower()}\n"
            f"Content locks — <b>{', '.join(active_locks) or 'none'}</b>\n"
            f"Warnings — <b>{s['warn_limit']}</b> then {s['warn_action']}\n"
            f"Greetings — <b>{_state(s['welcome_on'])}</b>\n\n"
            "Select a module to configure."
        )
        rows = [
            [Btn("Edit protection", callback_data="s:edit"), Btn("Media timer", callback_data="s:media")],
            [Btn("Explicit filter", callback_data="s:nsfw"), Btn("Content locks", callback_data="s:locks")],
            [Btn("Warnings", callback_data="s:warns"), Btn("Greetings", callback_data="s:welcome")],
            [Btn("Housekeeping", callback_data="s:clean"), Btn("Close", callback_data="s:close")],
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
        "Protection",
        "<b>Protection</b>\n\n"
        "/settings — open the control panel\n"
        "/editdelay <code>2m</code> — wait before removing an edited message\n"
        "/mediadelay <code>10m</code> — remove media after a set time\n"
        "/nsfw <code>on|off</code> — explicit-media filter\n\n"
        "<b>Exemptions</b>\n"
        "/allowedit — let a member edit their messages\n"
        "/revokeedit — take that back\n"
        "/allowmedia — never remove a member's media\n"
        "/revokemedia — take that back\n"
        "/allowlist — see everyone with an exemption",
    ),
    "content": (
        "Content control",
        "<b>Content control</b>\n"
        "<i>Tools for keeping risky or infringing material out of the chat.</i>\n\n"
        "/lock <code>files videos</code> — stop members posting a content type\n"
        "/unlock <code>files</code> — allow it again\n"
        "/locks — see what's locked\n"
        "Types: links, forwards, files, videos, audio, channels, all\n\n"
        "/block <code>word, phrase</code> — remove messages matching these terms\n"
        "/unblock <code>word</code> — drop a term\n"
        "/blocklist — show all terms\n\n"
        "/del — delete the replied message\n"
        "/purge — clear from the replied message down, or <code>/purge 50</code>\n"
        "/purgeuser — clear a member's recent messages",
    ),
    "mod": (
        "Moderation",
        "<b>Moderation</b>\n"
        "<i>Reply to a member, or add their ID / @username. Time and reason are optional.</i>\n\n"
        "/ban <code>[time] [reason]</code> — ban\n"
        "/dban — ban and delete the replied message\n"
        "/unban — lift a ban\n"
        "/kick · /dkick — remove from the chat\n"
        "/mute <code>[time] [reason]</code> · /dmute — silence\n"
        "/unmute — lift a mute\n"
        "/warn <code>[reason]</code> · /dwarn — add a warning\n"
        "/unwarn — withdraw the latest warning\n"
        "/clearwarns — reset a member's record\n"
        "/whois — member profile",
    ),
    "tools": (
        "Chat tools",
        "<b>Chat tools</b>\n\n"
        "/pin <code>[loud]</code> — pin the replied message\n"
        "/unpin — unpin it\n"
        "/setwelcome <code>&lt;text&gt;</code> — greeting for new members\n"
        "/welcome <code>on|off</code> — toggle the greeting\n"
        "/setrules <code>&lt;text&gt;</code> — publish chat rules\n\n"
        "<b>Open to everyone</b>\n"
        "/rules — read the rules\n"
        "/report — flag the replied message for admins\n"
        "/warns — check a warning record\n"
        "/id — show chat and user IDs",
    ),
}


def help_home(context) -> str:
    name = html.escape(context.bot.first_name or "this bot")
    return (
        f"<b>{name}</b>\n"
        "Automated protection and moderation for Telegram groups.\n\n"
        "• Removes edited messages\n"
        "• Clears media on a timer and filters explicit content\n"
        "• Locks risky content types and blocks chosen keywords\n"
        "• Full moderation suite with warnings and reports\n\n"
        "<b>Getting started</b>\n"
        "Add me to your group, promote me to admin, then run /settings.\n\n"
        "Browse the commands below."
    )


def help_markup(context, private: bool):
    keys = list(HELP_SECTIONS)
    rows = [
        [Btn(HELP_SECTIONS[k][0], callback_data=f"h:{k}") for k in keys[i : i + 2]]
        for i in range(0, len(keys), 2)
    ]
    if private:
        url = f"https://t.me/{context.bot.username}?startgroup=true"
        rows.append([Btn("Add to a group", url=url)])
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
            msg,
            f"<b>Chat rules</b>\n\n{html.escape(rules)}" if rules else "No rules are published for that chat.",
        )
    await reply(msg, help_home(context), help_markup(context, private))


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
            await _safe_edit(q, help_home(context), help_markup(context, chat.type == "private"))
        elif parts[1] in HELP_SECTIONS:
            back = InlineKeyboardMarkup([[Btn("‹ All categories", callback_data="h:home")]])
            await _safe_edit(q, HELP_SECTIONS[parts[1]][1], back)
        return await q.answer()

    if not await is_admin(context, chat.id, q.from_user.id):
        return await q.answer("These controls are for chat admins.", show_alert=True)

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
        return await q.answer("Switched on" if new else "Switched off")

    if kind == "la":  # lock / unlock every content type
        value = int(parts[1] == "1")
        for column, *_ in LOCKS.values():
            set_setting(chat.id, column, value)
        text, markup = panel_view(chat.id, "locks", chat.title)
        await _safe_edit(q, text, markup)
        return await q.answer("All content types locked" if value else "All content types unlocked")

    if kind == "v" and parts[2] in CHOICES:
        panel, key, raw = parts[1], parts[2], parts[3]
        match = [(v, label) for v, label in CHOICES[key] if str(v) == raw]
        if not match:
            return await q.answer()
        set_setting(chat.id, key, match[0][0])
        text, markup = panel_view(chat.id, panel, chat.title)
        await _safe_edit(q, text, markup)
        return await q.answer("Saved")

    admin = link(q.from_user.id, q.from_user.first_name, q.from_user.username)
    try:
        if kind == "m":  # undo buttons under moderation notices
            action, uid = parts[1], int(parts[2])
            if action == "unban":
                await context.bot.unban_chat_member(chat.id, uid, only_if_banned=True)
                done = "Ban lifted"
            elif action == "unmute":
                await do_unmute(context.bot, chat.id, uid)
                done = "Unmuted"
            else:
                done = "Warning withdrawn" if remove_last_warn(chat.id, uid) else "No warnings left"
            await _safe_edit(q, f"{q.message.text_html}\n\n<i>{done} by {admin}.</i>")
            return await q.answer(done)

        if kind == "r":  # report actions
            action, mid, uid = parts[1], int(parts[2]), int(parts[3])
            if action == "x":
                await q.answer("Report closed")
                return await q.message.delete()
            if action in ("del", "mute", "ban"):
                await delete(context.bot, chat.id, mid)
            if action == "mute":
                await do_mute(context.bot, chat.id, uid)
            elif action == "ban":
                await context.bot.ban_chat_member(chat.id, uid)
            elif action == "warn":
                db.execute(
                    "INSERT INTO warns VALUES(?,?,?,?)", (chat.id, uid, "Flagged message", time.time())
                )
            done = {
                "del": "message deleted",
                "mute": "author muted",
                "ban": "author banned",
                "warn": f"author warned ({warn_count(chat.id, uid)} of {settings(chat.id)['warn_limit']})",
            }[action]
            await _safe_edit(q, f"🚩 <b>Report resolved</b>\nOutcome: {done}\nHandled by {admin}")
            return await q.answer("Done")
    except TelegramError as e:
        return await q.answer(f"Couldn't complete that: {e}", show_alert=True)
    await q.answer()


# --------------------------------------------------------------------- main
ADMIN_COMMANDS = [
    ("settings", "Open the control panel"),
    ("editdelay", "Set the delay before edited messages are removed"),
    ("mediadelay", "Set the media auto-remove timer"),
    ("nsfw", "Toggle the explicit-media filter"),
    ("allowedit", "Let a member edit messages"),
    ("revokeedit", "Withdraw a member's edit exemption"),
    ("allowmedia", "Exempt a member's media from removal"),
    ("revokemedia", "Withdraw a member's media exemption"),
    ("allowlist", "List members with exemptions"),
    ("lock", "Lock a content type (files, videos, links...)"),
    ("unlock", "Unlock a content type"),
    ("locks", "Show content locks"),
    ("block", "Add words or phrases to the blocklist"),
    ("unblock", "Remove a blocklist entry"),
    ("blocklist", "Show the blocklist"),
    ("ban", "Ban a member"),
    ("dban", "Ban a member and delete their message"),
    ("unban", "Lift a ban"),
    ("kick", "Remove a member from the chat"),
    ("dkick", "Remove a member and delete their message"),
    ("mute", "Mute a member"),
    ("dmute", "Mute a member and delete their message"),
    ("unmute", "Lift a mute"),
    ("warn", "Warn a member"),
    ("dwarn", "Warn a member and delete their message"),
    ("unwarn", "Withdraw the latest warning"),
    ("clearwarns", "Reset a member's warnings"),
    ("warns", "Show a member's warnings"),
    ("whois", "Show a member's profile"),
    ("del", "Delete the replied message"),
    ("purge", "Bulk-delete recent messages"),
    ("purgeuser", "Delete a member's recent messages"),
    ("pin", "Pin the replied message"),
    ("unpin", "Unpin a message"),
    ("setwelcome", "Set the greeting for new members"),
    ("welcome", "Toggle the greeting"),
    ("setrules", "Publish chat rules"),
    ("rules", "Show chat rules"),
    ("id", "Show chat and user IDs"),
    ("help", "Browse all commands"),
]
MEMBER_COMMANDS = [
    ("rules", "Show chat rules"),
    ("report", "Flag a message for the admins"),
    ("warns", "Check your warnings"),
    ("id", "Show chat and user IDs"),
    ("help", "Browse all commands"),
]
PRIVATE_COMMANDS = [("start", "Get started"), ("help", "Browse all commands")]


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

    app.add_handler(TypeHandler(Update, track), group=-1)
    groups = filters.ChatType.GROUPS
    # Must be first: every edited message (including edited commands) goes here.
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & groups, on_edit))

    # First name in each tuple is the public one; the rest are legacy aliases.
    commands = {
        ("start", "help"): cmd_help,
        ("settings", "panel"): cmd_settings,
        ("editdelay", "set_delay", "setdelay"): cmd_editdelay,
        ("mediadelay", "set_delaymedia", "setdelaymedia"): cmd_mediadelay,
        ("nsfw",): cmd_nsfw,
        ("allowedit", "auth"): _auth_command(
            "edit", True, "<b>Exemption granted.</b>\n{} can now edit messages freely."
        ),
        ("revokeedit", "unauth"): _auth_command(
            "edit", False, "<b>Exemption withdrawn.</b>\n{}'s edits will be removed again."
        ),
        ("allowmedia", "authmedia", "auth_media"): _auth_command(
            "media", True, "<b>Exemption granted.</b>\nI'll leave {}'s media untouched."
        ),
        ("revokemedia", "unauthmedia", "unauth_media"): _auth_command(
            "media", False, "<b>Exemption withdrawn.</b>\n{}'s media follows the chat rules again."
        ),
        ("allowlist", "authlist"): cmd_allowlist,
        ("lock",): _lock_command(1),
        ("unlock",): _lock_command(0),
        ("locks",): cmd_locks,
        ("block",): cmd_block,
        ("unblock",): cmd_unblock,
        ("blocklist",): cmd_blocklist,
        ("ban",): _mod_command(_ban),
        ("dban",): _mod_command(_ban, also_delete=True),
        ("unban",): cmd_unban,
        ("kick",): _mod_command(_kick),
        ("dkick",): _mod_command(_kick, also_delete=True),
        ("mute",): _mod_command(_mute),
        ("dmute",): _mod_command(_mute, also_delete=True),
        ("unmute",): cmd_unmute,
        ("warn",): _mod_command(_warn),
        ("dwarn",): _mod_command(_warn, also_delete=True),
        ("unwarn",): cmd_unwarn,
        ("clearwarns", "resetwarns"): cmd_clearwarns,
        ("warns",): cmd_warns,
        ("whois", "info"): cmd_whois,
        ("del",): cmd_del,
        ("purge",): cmd_purge,
        ("purgeuser",): cmd_purgeuser,
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
