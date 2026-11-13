"""
Telegram OTC Utility Bot — Production Grade Upgrade

Features:
- Admin-only reply-based timed /pin, /unpin, /cancelpin, /pinlist (raw clickable commands)
- Temporary Automatic Pinning mode (/apin) with preset & custom duration
- Group addition detection: prompts to register as OTC with admin-only callback button
- Automatic check if group is already registered ("This group is already registered as an OTC.")
- "Add to Group" button in bot DM (/start, /help, /info, and dashboard)
- SQLite persistence, schema migrations, and restart-safe background workers
- Interactive Settings Panel (/settings) for service messages, pins, AI, and bot activity
- Multi-owner system with centralized authorization
- Owner system-wide sponsored message & button injection (/sponsored)
- Owner asynchronous broadcast engine with preview, rate limiting, and detailed reports (/bd)
- Per-OTC AI text insights with structured formatting, admin filtering, and prompt customization (/ai, /prompt)
- Clean Telegram custom premium emoji support with professional unicode fallbacks
- Cloud port-scan listener ($PORT) for reliable zero-downtime hosting (Render, Railway, etc.)
- Bot command scopes configured via setMyCommands

Install: pip install -r requirements.txt
Run:     python otcbot.py
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
from telegram import (
    BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
    BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

from emojis import FALLBACK_EMOJIS, e, e_html, get_emoji_id

# ==========================================
# Configuration
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")

# Multi-owner system: numeric Telegram user IDs from env
BOT_OWNER_IDS: Set[int] = set()
BOT_OWNER_IDS |= {int(x) for x in re.split(r"[,\s]+", os.getenv("BOT_OWNER_IDS", "")) if x.isdigit()}

DATABASE_PATH = os.getenv("DATABASE_PATH", str(Path(__file__).resolve().with_name("otc_bot.sqlite3")))

# AI Providers
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

PIN_WORKER_INTERVAL = 15       # seconds
PIN_RETRY_MAX_SECONDS = 300
PIN_GIVE_UP_ATTEMPTS = 20      # permanent errors only
MESSAGE_HISTORY_LIMIT = 600    # retained messages per OTC
ANALYSIS_MESSAGE_LIMIT = 100   # latest 100 messages for AI analysis
MAX_PROMPT_BYTES = 40_000
MAX_ANALYSIS_CHARS = 12_000

# Button styles
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"

ADMIN_STATUSES = {"administrator", "creator"}
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("otc-bot")

# Global runtime state
db: Optional[sqlite3.Connection] = None
background_task: Optional[asyncio.Task] = None
ai_running: Set[int] = set()
analysis_running = ai_running
active_broadcast_task: Optional[asyncio.Task] = None


# ==========================================
# Database & Schema Migrations
# ==========================================

def init_db() -> None:
    """Initialize database and safely apply schema migrations without data loss."""
    global db
    db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        log.warning("WAL mode unavailable; falling back to standard journal mode.")
    db.execute("PRAGMA foreign_keys=ON")

    db.executescript("""
    CREATE TABLE IF NOT EXISTS groups (
        chat_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        username TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        ai_enabled INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS otc_settings (
        chat_id INTEGER PRIMARY KEY,
        del_join INTEGER NOT NULL DEFAULT 0,
        del_leave INTEGER NOT NULL DEFAULT 0,
        del_service INTEGER NOT NULL DEFAULT 0,
        del_pinned INTEGER NOT NULL DEFAULT 0,
        del_apin INTEGER NOT NULL DEFAULT 0,
        pin_manual INTEGER NOT NULL DEFAULT 1,
        pin_auto INTEGER NOT NULL DEFAULT 1,
        ai_enabled INTEGER NOT NULL DEFAULT 0,
        ai_exclude_admins INTEGER NOT NULL DEFAULT 1,
        bot_active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        is_bot INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_active REAL NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS pins (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        pinned_by INTEGER NOT NULL,
        pinned_by_name TEXT NOT NULL,
        expires_at REAL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        PRIMARY KEY(chat_id, message_id)
    );

    CREATE TABLE IF NOT EXISTS auto_pins (
        chat_id INTEGER PRIMARY KEY,
        admin_id INTEGER NOT NULL,
        duration INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER,
        username TEXT,
        created_at REAL NOT NULL,
        body TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_messages_chat_time
        ON group_messages(chat_id, created_at DESC);

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sponsored_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        text_html TEXT,
        text_enabled INTEGER NOT NULL DEFAULT 0,
        button_style TEXT,
        button_text TEXT,
        button_url TEXT,
        button_enabled INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL,
        target_type TEXT NOT NULL,
        message_id INTEGER NOT NULL,
        from_chat_id INTEGER NOT NULL,
        total_targets INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        blocked_count INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT NOT NULL DEFAULT 'PENDING'
    );

    CREATE TABLE IF NOT EXISTS broadcast_recipients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        broadcast_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        target_name TEXT,
        target_username TEXT,
        is_group INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        error_msg TEXT,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_broadcast_recipients
        ON broadcast_recipients(broadcast_id);
    """)

    # Seed singleton sponsored row
    db.execute("INSERT OR IGNORE INTO sponsored_settings (id) VALUES (1)")
    db.commit()


def q_one(sql: str, args=()) -> Optional[sqlite3.Row]:
    return db.execute(sql, args).fetchone()


def q_all(sql: str, args=()) -> List[sqlite3.Row]:
    return db.execute(sql, args).fetchall()


def execute(sql: str, args=()) -> int:
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


# ==========================================
# Per-OTC Settings System
# ==========================================

DEFAULT_OTC_SETTINGS = {
    "del_join": 0,
    "del_leave": 0,
    "del_service": 0,
    "del_pinned": 0,
    "del_apin": 0,
    "pin_manual": 1,
    "pin_auto": 1,
    "ai_enabled": 0,
    "ai_exclude_admins": 1,
    "bot_active": 1,
}


def get_otc_settings(chat_id: int) -> sqlite3.Row:
    """Retrieve settings for an OTC, inserting defaults if none exist."""
    row = q_one("SELECT * FROM otc_settings WHERE chat_id=?", (chat_id,))
    if not row:
        execute(
            "INSERT OR IGNORE INTO otc_settings(chat_id) VALUES(?)",
            (chat_id,),
        )
        row = q_one("SELECT * FROM otc_settings WHERE chat_id=?", (chat_id,))
    return row


def update_otc_setting(chat_id: int, key: str, value: int) -> None:
    """Update a specific setting for an OTC and keep sync with legacy groups table."""
    if key not in DEFAULT_OTC_SETTINGS:
        return
    execute(f"UPDATE otc_settings SET {key}=? WHERE chat_id=?", (value, chat_id))
    if key == "ai_enabled":
        execute("UPDATE groups SET ai_enabled=? WHERE chat_id=?", (value, chat_id))


# ==========================================
# Permissions System (Centralized)
# ==========================================

def is_owner(user_id: Optional[int]) -> bool:
    """Verify if a Telegram user ID belongs to a configured bot owner."""
    return bool(user_id and user_id in BOT_OWNER_IDS)


async def require_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Server-side owner permission check for privileged commands."""
    user = update.effective_user
    if not user or not is_owner(user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text(
                f"{e_html('error')} <b>Access Restricted:</b> This command is restricted to configured bot owners.",
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


async def is_group_admin(bot, chat_id: int, user_id: int) -> bool:
    """Check if user has administrator rights in the given group."""
    if is_owner(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except TelegramError:
        return False


async def bot_can_pin(bot, chat_id: int) -> bool:
    """Check if the bot has admin rights with pin message permissions."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return member.status == ChatMemberStatus.ADMINISTRATOR and bool(getattr(member, "can_pin_messages", False))
    except TelegramError:
        return False


async def require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Verify that update occurred in an OTC group and sender is an authorized administrator."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not chat:
        return False

    if chat.type not in GROUP_TYPES:
        bot_user = context.bot.username or (await context.bot.get_me()).username
        add_url = f"https://t.me/{bot_user}?startgroup=true"
        await message.reply_text(
            f"{e_html('warning')} This command works in OTC groups only.\n"
            f"Add me to your group with the button below:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn("➕ Add to Group", url=add_url)]]),
        )
        return False

    # Check if group is registered as an OTC
    row = group_row(chat.id)
    if not row:
        text = (
            f"<b>{e_html('warning')} This group is not registered as an OTC yet.</b>\n\n"
            f"Do you want to register this group as an OTC?\n"
            f"<i>Only group administrators can complete this registration.</i>"
        )
        markup = InlineKeyboardMarkup([
            [btn(f"{e('otc')} Register as OTC", f"reg:confirm:{chat.id}", SUCCESS)],
            [btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)],
        ])
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return False

    # Check OTC general active setting
    st = get_otc_settings(chat.id)
    if not st["bot_active"] and not (message.text and message.text.startswith("/settings")):
        return False

    anonymous_admin = bool(message.sender_chat and message.sender_chat.id == chat.id)
    if not anonymous_admin and not (user and await is_group_admin(context.bot, chat.id, user.id)):
        await message.reply_text(f"{e_html('error')} Available to OTC admins only.", parse_mode=ParseMode.HTML)
        return False

    if not await bot_can_pin(context.bot, chat.id):
        await message.reply_text(
            f"{e_html('warning')} I require administrator permissions with <b>'Pin messages'</b> enabled.",
            parse_mode=ParseMode.HTML,
        )
        return False

    return True


# ==========================================
# Force Joining (Mandatory Channel Subscription)
# ==========================================

def get_force_sub_info() -> Tuple[bool, str, str]:
    """Returns (enabled: bool, channel_identifier: str, channel_url: str)."""
    row_ch = q_one("SELECT value FROM settings WHERE key='force_sub_channel'")
    row_en = q_one("SELECT value FROM settings WHERE key='force_sub_enabled'")

    channel = row_ch["value"].strip() if (row_ch and row_ch["value"]) else os.getenv("FORCE_SUB_CHANNEL", "").strip()
    if not channel:
        return False, "", ""

    if row_en:
        enabled = bool(int(row_en["value"]))
    else:
        enabled = True

    # Build clean link
    if channel.startswith("http://") or channel.startswith("https://"):
        url = channel
    elif channel.startswith("@"):
        url = f"https://t.me/{channel[1:]}"
    else:
        url = f"https://t.me/{channel}"

    return enabled, channel, url


async def user_is_subscribed(bot, user_id: int) -> bool:
    """Check if user has joined the required channel. Returns True if exempt, joined, or disabled."""
    if is_owner(user_id):
        return True

    enabled, channel, _ = get_force_sub_info()
    if not enabled or not channel:
        return True

    chat_id_or_user = int(channel) if (channel.startswith("-") and channel[1:].isdigit()) else channel

    try:
        member = await bot.get_chat_member(chat_id_or_user, user_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError as exc:
        log.warning("Force sub verification check for channel %s failed: %s", channel, exc)
        # Fail open if bot is not admin in channel or channel is misconfigured to avoid trapping users
        return True


async def enforce_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Enforces channel membership in private chat. Returns True if allowed, False if blocked."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != ChatType.PRIVATE or not user:
        return True

    if is_owner(user.id):
        return True

    subscribed = await user_is_subscribed(context.bot, user.id)
    if subscribed:
        return True

    enabled, channel, url = get_force_sub_info()
    text = (
        f"<b>{e_html('warning')} Mandatory Channel Subscription Required</b>\n\n"
        f"To use this bot, you must first join our official updates channel: <b>{esc(channel)}</b>.\n\n"
        f"1. Tap <b>'Join Channel'</b> below.\n"
        f"2. Return here and tap <b>'Joined / Verify'</b> to unlock all features."
    )
    markup = InlineKeyboardMarkup([
        [btn(f"{e('broadcast')} Join Channel", url=url)],
        [btn(f"{e('success')} Joined / Verify", "fsub:check", SUCCESS)],
    ])

    if update.callback_query:
        await edit(update.callback_query, text, markup)
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return False


async def callback_force_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback triggered when user taps 'Joined / Verify'."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    subscribed = await user_is_subscribed(context.bot, user_id)
    if subscribed:
        await query.answer("Verification successful! Welcome.", show_alert=False)
        bot_username = context.bot.username or (await context.bot.get_me()).username
        text = (
            f"{e_html('success')} <b>Membership Verified!</b>\n\n"
            f"Welcome to OTC Utility Bot. Choose an option below:"
        )
        text, markup = attach_sponsored_content(text, main_menu(user_id, bot_username))
        await edit(query, text, markup)
    else:
        await query.answer("You have not joined the channel yet! Please join first, then tap Verify.", show_alert=True)


async def cmd_fsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner command to view or configure force joining (/fsub)."""
    if not await require_owner(update, context):
        return

    # Check if channel argument supplied directly: /fsub @mychannel
    if context.args:
        new_ch = context.args[0].strip()
        execute("INSERT INTO settings(key,value) VALUES('force_sub_channel',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_ch,))
        execute("INSERT INTO settings(key,value) VALUES('force_sub_enabled','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        await update.effective_message.reply_text(
            f"{e_html('success')} <b>Force Join Channel set and enabled:</b> <code>{esc(new_ch)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await send_fsub_dashboard(update.effective_message)


async def send_fsub_dashboard(message, query=None):
    enabled, channel, url = get_force_sub_info()
    st_text = f"{e('toggle_on')} ON" if enabled else f"{e('toggle_off')} OFF"
    ch_text = f"<b>{esc(channel)}</b> ({url})" if channel else "<i>None configured</i>"

    text = (
        f"<b>{e_html('broadcast')} Force Joining (FSub) Management</b>\n\n"
        f"• <b>Status:</b> {st_text}\n"
        f"• <b>Target Channel:</b> {ch_text}\n\n"
        f"When enabled, users must join this channel before interacting with the bot in DM."
    )
    markup = InlineKeyboardMarkup([
        [
            btn(f"{e('toggle_on' if enabled else 'toggle_off')} Toggle ON/OFF", "fsub:toggle"),
            btn("📢 Set Channel", "fsub:set_ch", PRIMARY),
        ],
        [
            btn("🗑️ Remove Channel", "fsub:remove", DANGER),
            btn(f"{e('dismiss')} Close", "dismiss", DANGER),
        ],
    ])
    if query:
        await edit(query, text, markup)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_fsub_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle owner callbacks for /fsub dashboard."""
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.answer(f"{e('error')} Owner only.", show_alert=True)
        return

    action = query.data.split(":")[1]
    enabled, channel, _ = get_force_sub_info()

    if action == "toggle":
        new_val = "0" if enabled else "1"
        execute("INSERT INTO settings(key,value) VALUES('force_sub_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (new_val,))
        await send_fsub_dashboard(query.message, query=query)
    elif action == "remove":
        execute("DELETE FROM settings WHERE key IN ('force_sub_channel', 'force_sub_enabled')")
        await send_fsub_dashboard(query.message, query=query)
    elif action == "set_ch":
        context.user_data["awaiting_fsub_channel"] = True
        await edit(
            query,
            f"<b>{e_html('broadcast')} Set Force Join Channel:</b>\n\n"
            f"Send the channel username (e.g. <code>@MyChannel</code>) or invite link in the next message.\n\n"
            f"<i>Make sure to add the bot as an administrator in the channel so it can verify member subscriptions.</i>",
            InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", "fsub:cancel", DANGER)]]),
        )
    elif action == "cancel":
        context.user_data.pop("awaiting_fsub_channel", None)
        await send_fsub_dashboard(query.message, query=query)


# ==========================================
# Sponsored Content System
# ==========================================

def get_sponsored_settings() -> sqlite3.Row:
    row = q_one("SELECT * FROM sponsored_settings WHERE id=1")
    if not row:
        execute("INSERT OR IGNORE INTO sponsored_settings(id) VALUES(1)")
        row = q_one("SELECT * FROM sponsored_settings WHERE id=1")
    return row


def attach_sponsored_content(text: str, reply_markup=None) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """Centralized message wrapper: appends active sponsored text & button without duplicating."""
    sp = get_sponsored_settings()
    if not sp:
        return text, reply_markup

    # Append sponsored text if enabled
    if sp["text_enabled"] and sp["text_html"]:
        text = f"{text}\n\n—\n{sp['text_html']}"

    # Append sponsored button if enabled
    if sp["button_enabled"] and sp["button_url"] and sp["button_text"]:
        sp_btn = btn(sp["button_text"], url=sp["button_url"], style=sp["button_style"])
        if reply_markup and hasattr(reply_markup, "inline_keyboard"):
            new_keyboard = [list(r) for r in reply_markup.inline_keyboard]
            new_keyboard.append([sp_btn])
            reply_markup = InlineKeyboardMarkup(new_keyboard)
        else:
            reply_markup = InlineKeyboardMarkup([[sp_btn]])

    return text, reply_markup


# ==========================================
# UI & Formatting Utilities
# ==========================================

esc = html.escape


def btn(
    text: str,
    data: Optional[str] = None,
    style: Optional[str] = None,
    *,
    url: Optional[str] = None,
    emoji: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
) -> InlineKeyboardButton:
    """Inline button supporting text, callback_data, URLs, Bot API styles,
    and Telegram custom premium emoji icons (icon_custom_emoji_id).

    If an emoji key or leading fallback emoji is matched and a custom emoji ID
    is configured in .env, icon_custom_emoji_id is attached and the fallback
    emoji is automatically stripped from the text to prevent duplicates.
    """
    kwargs = {}
    if url:
        kwargs["url"] = url
    elif data is not None:
        kwargs["callback_data"] = data

    target_emoji_id = icon_custom_emoji_id
    if not target_emoji_id:
        if emoji and get_emoji_id(emoji):
            target_emoji_id = get_emoji_id(emoji)
        else:
            for key, fb in FALLBACK_EMOJIS.items():
                if fb and text.startswith(fb):
                    custom_id = get_emoji_id(key)
                    if custom_id:
                        target_emoji_id = custom_id
                        text = text[len(fb):].lstrip()
                    break

    if target_emoji_id:
        kwargs["icon_custom_emoji_id"] = str(target_emoji_id)

    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kwargs)
        except TypeError:
            api_kw = {"style": style}
            if target_emoji_id:
                api_kw["icon_custom_emoji_id"] = str(target_emoji_id)
                kwargs.pop("icon_custom_emoji_id", None)
            return InlineKeyboardButton(text, api_kwargs=api_kw, **kwargs)

    try:
        return InlineKeyboardButton(text, **kwargs)
    except TypeError:
        api_kw = {}
        if target_emoji_id:
            api_kw["icon_custom_emoji_id"] = str(target_emoji_id)
            kwargs.pop("icon_custom_emoji_id", None)
        return InlineKeyboardButton(text, api_kwargs=api_kw, **kwargs)


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    units = []
    for size, name in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        amount, seconds = divmod(seconds, size)
        if amount:
            units.append(f"{amount}{name}")
        if len(units) == 2:
            break
    return " ".join(units) if units else "0s"


UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
DURATION_TOKEN = re.compile(
    r"(\d+)\s*(weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b",
    re.IGNORECASE,
)


def parse_duration(text: str) -> Optional[int]:
    """Parse flexible duration strings (1m, 1min, 30m, 1h, 5h, 2d, 1day, etc.)."""
    text = text.strip()
    if not text:
        return None
    tokens = DURATION_TOKEN.findall(text)
    residue = DURATION_TOKEN.sub("", text).strip()
    if not tokens or residue:
        raise ValueError("Invalid duration format. Examples: 30s, 10m, 1h, 5h, 2d, 1day.")
    total = sum(int(n) * UNIT_SECONDS[u.lower()] for n, u in tokens)
    if total <= 0 or total > 365 * 86400:
        raise ValueError("Duration must be between 1 second and 365 days.")
    return total


async def say(message, context, text: str, markup=None, attach_sponsor: bool = True):
    """Send HTML message with optional automatic sponsored injection."""
    if attach_sponsor:
        text, markup = attach_sponsored_content(text, markup)
    extra = {"message_thread_id": message.message_thread_id} if message.is_topic_message else {}
    return await context.bot.send_message(
        message.chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, **extra
    )


async def edit(query, text: str, markup=None, plain: bool = False, attach_sponsor: bool = False):
    """Safely edit query message ignoring unmodified errors and injecting sponsored content if asked."""
    if attach_sponsor and not plain:
        text, markup = attach_sponsored_content(text, markup)
    try:
        await query.edit_message_text(
            text, parse_mode=None if plain else ParseMode.HTML, reply_markup=markup
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


def dismiss_keyboard():
    return InlineKeyboardMarkup([[btn(f"{e('dismiss')} Dismiss", "dismiss", style=DANGER)]])


def back_markup(data: str):
    return InlineKeyboardMarkup([[btn(f"{e('back')} Back", data)]])


async def delete_message_safe(message):
    if not message:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


# ==========================================
# Registration & User Tracking
# ==========================================

async def prompt_or_confirm_registration(chat, context: ContextTypes.DEFAULT_TYPE):
    """Prompt group to register as an OTC with an admin-only button, or inform if already registered."""
    row = q_one("SELECT * FROM groups WHERE chat_id=? AND active=1", (chat.id,))
    if row:
        text = f"{e_html('otc')} <b>This group is already registered as an OTC.</b>"
        markup = InlineKeyboardMarkup([
            [btn(f"{e('settings')} OTC Settings", f"st:cat:{chat.id}:main")],
            [btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)],
        ])
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        text = (
            f"<b>{e_html('otc')} OTC Registration</b>\n\n"
            f"Do you want to register this group as an OTC?\n\n"
            f"<i>Only group administrators can complete this registration.</i>"
        )
        markup = InlineKeyboardMarkup([
            [btn(f"{e('otc')} Register as OTC", f"reg:confirm:{chat.id}", SUCCESS)],
            [btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)],
        ])
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only callback to confirm OTC registration."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    chat_id = int(parts[2])

    # 1. Admin check: can only be tapped by admins
    if not await is_group_admin(context.bot, chat_id, query.from_user.id):
        await query.answer("Only group administrators can register this OTC.", show_alert=True)
        return

    # 2. Check if already registered
    row = q_one("SELECT * FROM groups WHERE chat_id=? AND active=1", (chat_id,))
    if row:
        await edit(query, f"{e_html('otc')} <b>This group is already registered as an OTC.</b>")
        return

    # 3. Perform registration
    chat = await context.bot.get_chat(chat_id)
    execute(
        """INSERT INTO groups(chat_id, title, username, active, created_at)
           VALUES(?, ?, ?, 1, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
           title=excluded.title, username=excluded.username, active=1""",
        (chat_id, chat.title or "OTC", chat.username, time.time()),
    )
    get_otc_settings(chat_id)

    text = (
        f"{e_html('success')} <b>Success! This group is now registered as an OTC.</b>\n\n"
        f"Admins can now use /pin, /apin, /settings, /ai, and /help."
    )
    markup = InlineKeyboardMarkup([
        [btn(f"{e('settings')} OTC Settings", f"st:cat:{chat_id}:main")],
        [btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)],
    ])
    await edit(query, text, markup)


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual /register command inside a group or DM."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat:
        return

    if chat.type not in GROUP_TYPES:
        bot_user = context.bot.username or (await context.bot.get_me()).username
        add_url = f"https://t.me/{bot_user}?startgroup=true"
        await message.reply_text(
            f"{e_html('warning')} The /register command works in OTC groups only.\n"
            f"Add me to your group with the button below:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn("➕ Add to Group", url=add_url)]]),
        )
        return

    if not await is_group_admin(context.bot, chat.id, user.id):
        await message.reply_text(f"{e_html('error')} Available to OTC admins only.", parse_mode=ParseMode.HTML)
        return

    await prompt_or_confirm_registration(chat, context)


def group_row(chat_id: int) -> Optional[sqlite3.Row]:
    return q_one("SELECT * FROM groups WHERE chat_id=? AND active=1", (chat_id,))


def track_user(user) -> None:
    """Store users who have interacted with the bot for broadcasting."""
    if not user or user.is_bot:
        return
    execute(
        """INSERT INTO users(chat_id, username, first_name, last_name, is_bot, created_at, last_active, is_active)
           VALUES(?, ?, ?, ?, 0, ?, ?, 1)
           ON CONFLICT(chat_id) DO UPDATE SET
           username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
           last_active=excluded.last_active, is_active=1""",
        (user.id, user.username, user.first_name, user.last_name, time.time(), time.time()),
    )


# ==========================================
# General Commands: /start, /help, /info
# ==========================================

def main_menu(user_id: Optional[int] = None, bot_username: Optional[str] = None):
    rows = []
    if bot_username:
        rows.append([btn("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true")])

    rows.extend([
        [btn(f"{e('otc')} My OTCs", "dash:groups", PRIMARY)],
        [btn(f"{e('settings')} OTC Settings", "dash:settings"),
         btn(f"{e('ai')} AI", "dash:ai", SUCCESS)],
        [btn(f"{e('help')} Help", "dash:help"),
         btn(f"{e('info')} Info", "dash:info")],
    ])
    if is_owner(user_id):
        rows.append([
            btn(f"{e('sponsor')} Sponsored", "dash:sponsored"),
            btn(f"{e('broadcast')} Broadcast", "dash:broadcast"),
        ])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat:
        return
    track_user(user)

    if chat.type in GROUP_TYPES:
        await prompt_or_confirm_registration(chat, context)
        return

    if not await enforce_force_sub(update, context):
        return

    bot_username = context.bot.username or (await context.bot.get_me()).username
    text = (
        f"{e_html('tools')} <b>Welcome to OTC Utility Bot</b>\n\n"
        f"Manage your OTC groups effortlessly with scheduled pins, automatic member pinning, "
        f"AI activity summaries, and full group settings."
    )
    text, markup = attach_sponsored_content(text, main_menu(user.id if user else None, bot_username))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    track_user(user)

    if chat and chat.type == ChatType.PRIVATE:
        if not await enforce_force_sub(update, context):
            return

    bot_username = context.bot.username or (await context.bot.get_me()).username
    add_url = f"https://t.me/{bot_username}?startgroup=true"

    lines = [
        f"{e_html('help')} <b>OTC Utility Bot — Command Guide</b>\n",
        f"<b>General:</b>",
        f"• /start — Open the private dashboard",
        f"• /help — Display this command reference",
        f"• /info — View system stats and bot status\n",
        f"<b>OTC Management:</b>",
        f"• /register — Register group as an OTC\n",
        f"<b>Pin Management (Admins):</b>",
        f"• /pin — Reply to pin indefinitely",
        f"• /pin 10m — Reply to pin for duration (e.g. 30s, 10m, 2h, 1d)",
        f"• /unpin — Reply to unpin message",
        f"• /cancelpin — Cancel scheduled auto-unpin",
        f"• /pinlist — View tracked pins\n",
        f"<b>Automatic Pinning:</b>",
        f"• /apin — Activate auto-pin mode for the next member message\n",
        f"<b>AI Insights:</b>",
        f"• /ai — Run AI insights on recent OTC discussions\n",
        f"<b>Settings:</b>",
        f"• /settings — Configure service message deletions, pin & AI features",
    ]

    if is_owner(user.id if user else None):
        lines.extend([
            f"\n<b>Owner Commands:</b>",
            f"• /prompt — View or update AI system prompt",
            f"• /sponsored — Manage system-wide sponsored text & button",
            f"• /bd — Broadcast message to users or OTC groups",
            f"• /fsub — Manage force join channel subscription",
            f"• /cancel — Cancel active input operation",
        ])

    buttons = []
    if chat and chat.type == ChatType.PRIVATE:
        buttons.append([btn("➕ Add to Group", url=add_url)])
    buttons.append([btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)])

    text, markup = attach_sponsored_content("\n".join(lines), InlineKeyboardMarkup(buttons))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    track_user(user)

    if chat and chat.type == ChatType.PRIVATE:
        if not await enforce_force_sub(update, context):
            return

    otc_count = q_one("SELECT COUNT(*) AS n FROM groups WHERE active=1")["n"]
    user_count = q_one("SELECT COUNT(*) AS n FROM users WHERE is_active=1")["n"]
    pin_count = q_one("SELECT COUNT(*) AS n FROM pins WHERE expires_at > ?", (time.time(),))["n"]

    bot_username = context.bot.username or (await context.bot.get_me()).username
    add_url = f"https://t.me/{bot_username}?startgroup=true"

    text = (
        f"{e_html('info')} <b>OTC Utility Bot Status</b>\n\n"
        f"• {e_html('otc')} <b>Active OTCs:</b> <code>{otc_count}</code>\n"
        f"• {e_html('user')} <b>Registered Users:</b> <code>{user_count}</code>\n"
        f"• {e_html('pin')} <b>Active Timed Pins:</b> <code>{pin_count}</code>\n"
        f"• {e_html('tools')} <b>Health Engine:</b> Port-Scan Safe ($PORT)\n"
        f"• {e_html('ai')} <b>AI Provider:</b> {GEMINI_MODEL if GEMINI_API_KEY else (OPENAI_MODEL if OPENAI_API_KEY else 'Disabled')}\n"
    )

    buttons = []
    if chat and chat.type == ChatType.PRIVATE:
        buttons.append([btn("➕ Add to Group", url=add_url)])
    buttons.append([btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)])

    text, markup = attach_sponsored_content(text, InlineKeyboardMarkup(buttons))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ==========================================
# Pin Commands: /pin, /unpin, /cancelpin, /pinlist
# ==========================================

def reply_target(message: Message) -> Optional[Message]:
    target = message.reply_to_message
    if target is None or target.forum_topic_created:
        return None
    return target


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not await require_group_admin(update, context):
        return

    st = get_otc_settings(message.chat_id)
    if not st["pin_manual"]:
        await message.reply_text(f"{e_html('warning')} Manual pin commands are disabled in OTC Settings.", parse_mode=ParseMode.HTML)
        return

    target = reply_target(message)
    if not target:
        await message.reply_text(
            f"{e_html('warning')} Reply to the message you want to pin.\n<b>Usage:</b> /pin 10m",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        duration = parse_duration(" ".join(context.args))
    except ValueError as exc:
        await message.reply_text(f"{e_html('error')} {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return

    expiry = time.time() + duration if duration is not None else None

    try:
        await context.bot.pin_chat_message(
            chat_id=message.chat_id,
            message_id=target.message_id,
            disable_notification=True,
        )
    except TelegramError as exc:
        await message.reply_text(f"{e_html('error')} Could not pin message: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return

    user = update.effective_user
    execute(
        """INSERT INTO pins(chat_id,message_id,pinned_by,pinned_by_name,expires_at,attempts,next_attempt,last_error)
           VALUES(?,?,?,?,?,0,0,NULL)
           ON CONFLICT(chat_id,message_id) DO UPDATE SET
           pinned_by=excluded.pinned_by, pinned_by_name=excluded.pinned_by_name,
           expires_at=excluded.expires_at, attempts=0, next_attempt=0, last_error=NULL""",
        (
            message.chat_id, target.message_id, user.id if user else 0,
            user.full_name if user else "Admin", expiry,
        ),
    )
    await delete_message_safe(message)

    detail = "Pinned indefinitely." if expiry is None else f"Pinned for <b>{human_time(duration)}</b>."
    await say(message, context, f"{e_html('pin')} {detail}", dismiss_keyboard())


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not await require_group_admin(update, context):
        return

    st = get_otc_settings(message.chat_id)
    if not st["pin_manual"]:
        await message.reply_text(f"{e_html('warning')} Manual pin commands are disabled in OTC Settings.", parse_mode=ParseMode.HTML)
        return

    target = reply_target(message)
    if not target:
        await message.reply_text(f"{e_html('warning')} Reply to the pinned message with /unpin.", parse_mode=ParseMode.HTML)
        return
    try:
        await context.bot.unpin_chat_message(message.chat_id, target.message_id)
    except TelegramError as exc:
        await message.reply_text(f"{e_html('error')} Could not unpin message: {esc(str(exc))}", parse_mode=ParseMode.HTML)
        return

    execute("DELETE FROM pins WHERE chat_id=? AND message_id=?", (message.chat_id, target.message_id))
    await delete_message_safe(message)
    await say(message, context, f"{e_html('success')} Unpinned.", dismiss_keyboard())


async def cmd_cancelpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not await require_group_admin(update, context):
        return

    target = reply_target(message)
    if not target:
        await message.reply_text(f"{e_html('warning')} Reply to the scheduled pin with /cancelpin.", parse_mode=ParseMode.HTML)
        return

    row = q_one(
        "SELECT 1 FROM pins WHERE chat_id=? AND message_id=? AND expires_at IS NOT NULL",
        (message.chat_id, target.message_id),
    )
    if not row:
        await message.reply_text(f"{e_html('info')} There is no scheduled automatic unpin for that message.", parse_mode=ParseMode.HTML)
        return

    execute("UPDATE pins SET expires_at=NULL, attempts=0, next_attempt=0, last_error=NULL WHERE chat_id=? AND message_id=?",
            (message.chat_id, target.message_id))
    await delete_message_safe(message)
    await say(message, context, f"{e_html('success')} Automatic unpin cancelled. Message remains pinned.", dismiss_keyboard())


async def cmd_pinlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not await require_group_admin(update, context):
        return
    thread_id = message.message_thread_id if message.is_topic_message else None
    await send_pinlist_page(message.chat_id, 0, context, thread_id=thread_id)


async def send_pinlist_page(chat_id: int, page: int, context, query=None, thread_id=None):
    rows = q_all("SELECT * FROM pins WHERE chat_id=? ORDER BY COALESCE(expires_at, 9e99), message_id", (chat_id,))
    if not rows:
        text, markup = f"{e_html('info')} No pins are currently tracked in this OTC.", dismiss_keyboard()
    else:
        page_size = 8
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        page = max(0, min(page, pages - 1))
        chunk = rows[page * page_size:(page + 1) * page_size]
        lines = [f"{e_html('pin')} <b>Tracked Pins</b> (Page {page + 1}/{pages})"]
        now = time.time()
        for row in chunk:
            expiry = row["expires_at"]
            remaining = "Indefinite" if expiry is None else (
                "Expired; retry pending" if expiry <= now else f"{human_time(expiry - now)} remaining"
            )
            lines.append(f"• ID <code>{row['message_id']}</code> — {esc(row['pinned_by_name'])}\n  {remaining}")
        nav = []
        if page > 0:
            nav.append(btn(f"{e('back')} Prev", f"pl:{chat_id}:{page - 1}", PRIMARY))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn(f"Next {e('forward')}", f"pl:{chat_id}:{page + 1}", PRIMARY))
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup([nav, [btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)]])

    if query is not None:
        await edit(query, text, markup)
    else:
        extra = {"message_thread_id": thread_id} if thread_id else {}
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, **extra)


async def pinlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, chat_id, page = query.data.split(":")
        chat_id, page = int(chat_id), int(page)
    except ValueError:
        await query.answer()
        return
    if not await is_group_admin(context.bot, chat_id, query.from_user.id):
        await query.answer(f"{e('error')} Administrator access required.", show_alert=True)
        return
    await query.answer()
    await send_pinlist_page(chat_id, page, context, query=query)


# ==========================================
# NEW FEATURE: Automatic Pinning — /apin
# ==========================================

async def cmd_apin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate automatic pinning setup for an OTC group."""
    message = update.effective_message
    if not message or not await require_group_admin(update, context):
        return

    st = get_otc_settings(message.chat_id)
    if not st["pin_auto"]:
        await message.reply_text(f"{e_html('warning')} Automatic pinning is disabled in OTC Settings.", parse_mode=ParseMode.HTML)
        return

    text = (
        f"<b>{e_html('time')} Automatic Pinning</b>\n\n"
        f"Enable automatic pinning for incoming messages from non-admin members.\n\n"
        f"You will be able to choose a duration. The next eligible member message will "
        f"automatically be pinned for that duration.\n\n"
        f"Would you like to proceed?"
    )
    markup = InlineKeyboardMarkup([
        [
            btn(f"{e('proceed')} Proceed", f"apin:proceed:{message.chat_id}:{message.message_id}", SUCCESS),
            btn(f"{e('dismiss')} Cancel", f"apin:cancel:{message.chat_id}:{message.message_id}", DANGER),
        ]
    ])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def apin_duration_keyboard(chat_id: int, cmd_msg_id: int):
    return InlineKeyboardMarkup([
        [
            btn("1 Hour", f"apin:dur:{chat_id}:{cmd_msg_id}:3600", PRIMARY),
            btn("5 Hours", f"apin:dur:{chat_id}:{cmd_msg_id}:18000", PRIMARY),
        ],
        [
            btn("10 Hours", f"apin:dur:{chat_id}:{cmd_msg_id}:36000", PRIMARY),
            btn("12 Hours", f"apin:dur:{chat_id}:{cmd_msg_id}:43200", PRIMARY),
        ],
        [
            btn(f"{e('time')} Custom Duration", f"apin:custom:{chat_id}:{cmd_msg_id}"),
        ],
        [
            btn(f"{e('dismiss')} Cancel", f"apin:cancel:{chat_id}:{cmd_msg_id}", DANGER),
        ],
    ])


async def callback_apin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[1]
    chat_id = int(parts[2])
    cmd_msg_id = int(parts[3])

    if not await is_group_admin(context.bot, chat_id, query.from_user.id):
        await query.answer(f"{e('error')} OTC administrator access required.", show_alert=True)
        return

    if action == "cancel":
        await query.message.delete()
        return

    elif action == "proceed":
        text = (
            f"<b>{e_html('time')} Choose Duration for Automatic Pin:</b>\n\n"
            f"Select a preset duration below, or choose <b>Custom Duration</b> to specify."
        )
        await edit(query, text, apin_duration_keyboard(chat_id, cmd_msg_id))

    elif action == "dur":
        duration = int(parts[4])
        await query.message.delete()
        try:
            await context.bot.delete_message(chat_id, cmd_msg_id)
        except TelegramError:
            pass

        execute(
            """INSERT INTO auto_pins(chat_id, admin_id, duration, status, created_at)
               VALUES(?, ?, ?, 'PENDING', ?)
               ON CONFLICT(chat_id) DO UPDATE SET
               admin_id=excluded.admin_id, duration=excluded.duration, status='PENDING', created_at=excluded.created_at""",
            (chat_id, query.from_user.id, duration, time.time()),
        )
        await context.bot.send_message(
            chat_id,
            f"{e_html('time')} The next eligible member message will be pinned for: <b>{human_time(duration)}</b>.",
            parse_mode=ParseMode.HTML,
        )

    elif action == "custom":
        context.chat_data["awaiting_apin_custom"] = {
            "admin_id": query.from_user.id,
            "cmd_msg_id": cmd_msg_id,
            "setup_msg_id": query.message.message_id,
            "chat_id": chat_id,
        }
        await edit(
            query,
            f"{e_html('time')} <b>Custom Duration:</b>\n"
            f"Reply to this message with your desired duration (e.g. <code>30m</code>, <code>2h</code>, <code>1day</code>).\n\n"
            f"Click Cancel to abort.",
            InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", f"apin:cancel:{chat_id}:{cmd_msg_id}", DANGER)]]),
        )


# ==========================================
# Settings Dashboard (/settings)
# ==========================================

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open settings panel in OTC group or private chat."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    track_user(user)

    if chat.type in GROUP_TYPES:
        row = group_row(chat.id)
        if not row:
            await prompt_or_confirm_registration(chat, context)
            return

        if not await is_group_admin(context.bot, chat.id, user.id):
            await update.effective_message.reply_text(f"{e_html('error')} Available to OTC admins only.", parse_mode=ParseMode.HTML)
            return
        await send_settings_menu(update.effective_message, chat.id, "main")
    else:
        # In private: list eligible OTCs
        await render_group_picker_for_settings(update.effective_message, context, user.id)


async def send_settings_menu(message, chat_id: int, category: str, query=None):
    row = group_row(chat_id)
    title = row["title"] if row else "OTC"
    st = get_otc_settings(chat_id)

    def tg_str(k):
        return f"{e('toggle_on')} ON" if st[k] else f"{e('toggle_off')} OFF"

    if category == "main":
        text = (
            f"<b>{e_html('settings')} OTC Settings — {esc(title)}</b>\n\n"
            f"Configure service message deletions, pin modes, AI analysis, and bot activity."
        )
        markup = InlineKeyboardMarkup([
            [btn(f"{e('delete')} Service Messages", f"st:cat:{chat_id}:service")],
            [btn(f"{e('pin')} Pin Management", f"st:cat:{chat_id}:pins")],
            [btn(f"{e('ai')} AI Settings", f"st:cat:{chat_id}:ai")],
            [btn(f"{e('tools')} General", f"st:cat:{chat_id}:general")],
            [btn(f"{e('dismiss')} Close", "dismiss", DANGER)],
        ])
    elif category == "service":
        text = f"<b>{e_html('delete')} Service Messages — {esc(title)}</b>\nToggle auto-deletion:"
        markup = InlineKeyboardMarkup([
            [btn(f"Delete Join: {tg_str('del_join')}", f"st:toggle:{chat_id}:del_join:service")],
            [btn(f"Delete Leave: {tg_str('del_leave')}", f"st:toggle:{chat_id}:del_leave:service")],
            [btn(f"Delete Other Service: {tg_str('del_service')}", f"st:toggle:{chat_id}:del_service:service")],
            [btn(f"Delete Pinned Notice: {tg_str('del_pinned')}", f"st:toggle:{chat_id}:del_pinned:service")],
            [btn(f"Delete AutoPin Notice: {tg_str('del_apin')}", f"st:toggle:{chat_id}:del_apin:service")],
            [btn(f"{e('back')} Settings", f"st:cat:{chat_id}:main")],
        ])
    elif category == "pins":
        text = f"<b>{e_html('pin')} Pin Management — {esc(title)}</b>\nToggle pin features:"
        markup = InlineKeyboardMarkup([
            [btn(f"Manual Pins (/pin): {tg_str('pin_manual')}", f"st:toggle:{chat_id}:pin_manual:pins")],
            [btn(f"Automatic Pinning (/apin): {tg_str('pin_auto')}", f"st:toggle:{chat_id}:pin_auto:pins")],
            [btn(f"{e('back')} Settings", f"st:cat:{chat_id}:main")],
        ])
    elif category == "ai":
        text = f"<b>{e_html('ai')} AI Settings — {esc(title)}</b>\nConfigure AI collection:"
        markup = InlineKeyboardMarkup([
            [btn(f"Enable AI Analysis: {tg_str('ai_enabled')}", f"st:toggle:{chat_id}:ai_enabled:ai")],
            [btn(f"Exclude Admins: {tg_str('ai_exclude_admins')}", f"st:toggle:{chat_id}:ai_exclude_admins:ai")],
            [btn(f"{e('back')} Settings", f"st:cat:{chat_id}:main")],
        ])
    elif category == "general":
        text = f"<b>{e_html('tools')} General Settings — {esc(title)}</b>\nConfigure bot responses:"
        markup = InlineKeyboardMarkup([
            [btn(f"Bot Active: {tg_str('bot_active')}", f"st:toggle:{chat_id}:bot_active:general")],
            [btn(f"{e('back')} Settings", f"st:cat:{chat_id}:main")],
        ])
    else:
        return

    if query:
        await edit(query, text, markup)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    chat_id = int(parts[2])

    if not await is_group_admin(context.bot, chat_id, query.from_user.id):
        await query.answer(f"{e('error')} Access denied: you are not an OTC admin.", show_alert=True)
        return

    if action == "cat":
        category = parts[3]
        await send_settings_menu(query.message, chat_id, category, query=query)
    elif action == "toggle":
        key = parts[3]
        category = parts[4]
        st = get_otc_settings(chat_id)
        new_val = 0 if st[key] else 1
        update_otc_setting(chat_id, key, new_val)
        await send_settings_menu(query.message, chat_id, category, query=query)


async def render_group_picker_for_settings(message, context, user_id: int):
    groups = await eligible_groups(context.bot, user_id)
    if not groups:
        bot_user = context.bot.username or (await context.bot.get_me()).username
        add_url = f"https://t.me/{bot_user}?startgroup=true"
        await message.reply_text(
            f"{e_html('warning')} <b>No registered OTCs found.</b>\n"
            f"Add me to your group and run /register to register it.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn("➕ Add to Group", url=add_url)]]),
        )
        return
    rows = [[btn(f"{e('otc')} {g['title'][:40]}", f"st:cat:{g['chat_id']}:main", PRIMARY)] for g in groups]
    rows.append([btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)])
    await message.reply_text(
        f"<b>{e_html('settings')} Choose an OTC to configure:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ==========================================
# Owner Feature: Sponsored Messages (/sponsored)
# ==========================================

async def cmd_sponsored(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner interface for managing system-wide sponsored content."""
    if not await require_owner(update, context):
        return
    await send_sponsored_dashboard(update.effective_message)


async def send_sponsored_dashboard(message, query=None):
    sp = get_sponsored_settings()
    text_st = f"{e('toggle_on')} ON" if sp["text_enabled"] else f"{e('toggle_off')} OFF"
    btn_st = f"{e('toggle_on')} ON" if sp["button_enabled"] else f"{e('toggle_off')} OFF"

    preview_text = sp["text_html"] if sp["text_html"] else "<i>None configured</i>"
    btn_preview = f"[{sp['button_text']} -> {sp['button_url']}]" if sp["button_text"] else "<i>None configured</i>"

    text = (
        f"<b>{e_html('sponsor')} Global Sponsored Content Management</b>\n\n"
        f"• <b>Sponsored Text:</b> {text_st}\n"
        f"• <b>Sponsored Button:</b> {btn_st}\n\n"
        f"<b>Current Text:</b>\n{preview_text}\n\n"
        f"<b>Current Button:</b>\n{btn_preview}"
    )
    markup = InlineKeyboardMarkup([
        [
            btn(f"{e('toggle_on' if sp['text_enabled'] else 'toggle_off')} Toggle Text", "sp:toggle_text"),
            btn(f"{e('toggle_on' if sp['button_enabled'] else 'toggle_off')} Toggle Button", "sp:toggle_btn"),
        ],
        [
            btn("📝 Set Text", "sp:set_text", PRIMARY),
            btn("🔗 Set Button", "sp:set_btn", PRIMARY),
        ],
        [
            btn("👁️ Preview", "sp:preview"),
            btn(f"{e('dismiss')} Close", "dismiss", DANGER),
        ],
        [
            btn("🗑️ Clear Text", "sp:clear_text", DANGER),
            btn("🗑️ Clear Button", "sp:clear_btn", DANGER),
        ],
    ])
    if query:
        await edit(query, text, markup)
    else:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_sponsored(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.answer(f"{e('error')} Owner only.", show_alert=True)
        return

    action = query.data.split(":")[1]
    sp = get_sponsored_settings()

    if action == "toggle_text":
        execute("UPDATE sponsored_settings SET text_enabled=? WHERE id=1", (0 if sp["text_enabled"] else 1,))
        await send_sponsored_dashboard(query.message, query=query)
    elif action == "toggle_btn":
        execute("UPDATE sponsored_settings SET button_enabled=? WHERE id=1", (0 if sp["button_enabled"] else 1,))
        await send_sponsored_dashboard(query.message, query=query)
    elif action == "clear_text":
        execute("UPDATE sponsored_settings SET text_html=NULL, text_enabled=0 WHERE id=1")
        await send_sponsored_dashboard(query.message, query=query)
    elif action == "clear_btn":
        execute("UPDATE sponsored_settings SET button_text=NULL, button_url=NULL, button_style=NULL, button_enabled=0 WHERE id=1")
        await send_sponsored_dashboard(query.message, query=query)
    elif action == "preview":
        dummy_text = "<b>Preview Bot Message</b>\nThis is an example bot message showing how sponsored content appears."
        dummy_text, dummy_markup = attach_sponsored_content(dummy_text, dismiss_keyboard())
        await query.message.reply_text(dummy_text, parse_mode=ParseMode.HTML, reply_markup=dummy_markup)
    elif action == "set_text":
        context.user_data["awaiting_sponsored_text"] = True
        await edit(
            query,
            f"<b>{e_html('sponsor')} Set Sponsored Text:</b>\n\n"
            f"Send or quote a formatted message. Bold, italic, links, blockquotes, and "
            f"Telegram Premium custom emojis will be preserved perfectly.\n\n"
            f"Click Cancel to abort.",
            InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", "sp:cancel", DANGER)]]),
        )
    elif action == "set_btn":
        context.user_data["awaiting_sponsored_btn"] = True
        await edit(
            query,
            f"<b>{e_html('link')} Set Sponsored Inline Button:</b>\n\n"
            f"Send your button configuration in this format:\n"
            f"<code>#style Button Text - https://t.me/example</code>\n\n"
            f"<b>Supported styles:</b>\n"
            f"• <code>#primary</code> (blue)\n"
            f"• <code>#success</code> (green)\n"
            f"• <code>#danger</code> (red)\n\n"
            f"<b>Example:</b>\n"
            f"<code>#success Join the #1 OTC - https://t.me/Oncarts</code>",
            InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", "sp:cancel", DANGER)]]),
        )
    elif action == "cancel":
        context.user_data.pop("awaiting_sponsored_text", None)
        context.user_data.pop("awaiting_sponsored_btn", None)
        await send_sponsored_dashboard(query.message, query=query)


# ==========================================
# Owner Feature: Broadcast System (/bd)
# ==========================================

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiate system-wide broadcast wizard (Owner only)."""
    if not await require_owner(update, context):
        return

    user_count = q_one("SELECT COUNT(*) AS n FROM users WHERE is_active=1")["n"]
    otc_count = q_one("SELECT COUNT(*) AS n FROM groups WHERE active=1")["n"]

    text = (
        f"<b>{e_html('broadcast')} Broadcast Engine</b>\n\n"
        f"Select the broadcast destination mode:"
    )
    markup = InlineKeyboardMarkup([
        [btn(f"{e('user')} All Bot Users ({user_count})", "bd:dest:users", PRIMARY)],
        [btn(f"{e('otc')} All Registered OTCs ({otc_count})", "bd:dest:groups", PRIMARY)],
        [btn(f"{e('dismiss')} Cancel", "bd:cancel", DANGER)],
    ])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def callback_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        await query.answer(f"{e('error')} Owner access required.", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "cancel":
        context.user_data.pop("broadcast_setup", None)
        await edit(query, f"{e_html('dismiss')} Broadcast cancelled.")
        return

    elif action == "dest":
        target_type = parts[2]
        context.user_data["broadcast_setup"] = {"target_type": target_type}
        dest_label = "All Bot Users" if target_type == "users" else "All Registered OTC Groups"
        await edit(
            query,
            f"<b>{e_html('broadcast')} Broadcast Content — {dest_label}</b>\n\n"
            f"Now send or forward the message you want to broadcast.\n\n"
            f"Supports text, bold, custom emojis, photos, videos, documents, and media captions.\n"
            f"Click Cancel below to abort.",
            InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", "bd:cancel", DANGER)]]),
        )

    elif action == "confirm":
        setup = context.user_data.pop("broadcast_setup", None)
        if not setup:
            await edit(query, f"{e_html('error')} Broadcast expired. Please run /bd again.")
            return

        global active_broadcast_task
        if active_broadcast_task and not active_broadcast_task.done():
            await edit(query, f"{e_html('warning')} Another broadcast is currently running. Please wait.")
            return

        await edit(query, f"{e_html('time')} Starting broadcast… You will receive live progress updates.")
        active_broadcast_task = asyncio.create_task(
            execute_broadcast(context.bot, query.from_user.id, setup["target_type"], setup["from_chat_id"], setup["message_id"])
        )


async def execute_broadcast(bot, owner_id: int, target_type: str, from_chat_id: int, message_id: int):
    """Reliable rate-limited broadcast engine with recipient tracking and detailed reporting."""
    started_at = time.time()
    if target_type == "users":
        targets = q_all("SELECT chat_id AS id, username, first_name AS name FROM users WHERE is_active=1")
        is_group = 0
    else:
        targets = q_all("SELECT chat_id AS id, username, title AS name FROM groups WHERE active=1")
        is_group = 1

    total = len(targets)
    broadcast_id = execute(
        """INSERT INTO broadcasts(sender_id, target_type, message_id, from_chat_id, total_targets, started_at, status)
           VALUES(?, ?, ?, ?, ?, ?, 'RUNNING')""",
        (owner_id, target_type, message_id, from_chat_id, total, started_at),
    )

    success_cnt = 0
    failed_cnt = 0
    blocked_cnt = 0
    progress_msg = None

    try:
        progress_msg = await bot.send_message(
            owner_id,
            f"{e_html('broadcast')} <b>Broadcast #{broadcast_id} in progress:</b> 0/{total}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

    last_update_time = time.time()

    for idx, t in enumerate(targets, start=1):
        target_id = t["id"]
        status = "SUCCESS"
        error_msg = None

        try:
            await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)
            success_cnt += 1
        except RetryAfter as exc:
            wait = float(getattr(exc, "retry_after", 3.0))
            await asyncio.sleep(wait)
            try:
                await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)
                success_cnt += 1
            except Exception as retry_exc:
                failed_cnt += 1
                status = "FAILED"
                error_msg = str(retry_exc)[:200]
        except Forbidden as exc:
            blocked_cnt += 1
            status = "BLOCKED"
            error_msg = str(exc)[:200]
            if is_group:
                execute("UPDATE groups SET active=0 WHERE chat_id=?", (target_id,))
            else:
                execute("UPDATE users SET is_active=0 WHERE chat_id=?", (target_id,))
        except TelegramError as exc:
            failed_cnt += 1
            status = "FAILED"
            error_msg = str(exc)[:200]

        execute(
            """INSERT INTO broadcast_recipients(broadcast_id, target_id, target_name, target_username, is_group, status, error_msg, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (broadcast_id, target_id, t["name"] or "Unknown", t["username"], is_group, status, error_msg, time.time()),
        )

        # Periodic live progress update
        if progress_msg and (time.time() - last_update_time > 3.0 or idx == total):
            try:
                await progress_msg.edit_text(
                    f"{e_html('broadcast')} <b>Broadcast #{broadcast_id} Progress:</b>\n"
                    f"Processed: <b>{idx}/{total}</b> | Success: <b>{success_cnt}</b> | Failed: <b>{failed_cnt}</b>",
                    parse_mode=ParseMode.HTML,
                )
                last_update_time = time.time()
            except TelegramError:
                pass

        await asyncio.sleep(0.04)

    finished_at = time.time()
    duration = max(0.1, finished_at - started_at)

    execute(
        """UPDATE broadcasts SET success_count=?, failed_count=?, blocked_count=?, finished_at=?, status='COMPLETED'
           WHERE id=?""",
        (success_cnt, failed_cnt, blocked_cnt, finished_at, broadcast_id),
    )

    # Generate summary report
    report_text = (
        f"<b>{e_html('broadcast')} Broadcast #{broadcast_id} Completed!</b>\n\n"
        f"• <b>Destination:</b> {target_type.upper()}\n"
        f"• <b>Total Recipients:</b> {total}\n"
        f"• <b>Successful:</b> {success_cnt}\n"
        f"• <b>Failed:</b> {failed_cnt}\n"
        f"• <b>Blocked/Kicked:</b> {blocked_cnt}\n"
        f"• <b>Total Duration:</b> {human_time(duration)}"
    )

    recipients = q_all("SELECT * FROM broadcast_recipients WHERE broadcast_id=?", (broadcast_id,))
    if len(recipients) <= 20:
        lines = ["\n<b>Recipient Breakdown:</b>"]
        for r in recipients:
            uname = f"@{r['target_username']}" if r["target_username"] else "None"
            lines.append(f"• [{r['status']}] {esc(r['target_name'] or 'Target')} ({uname}, ID <code>{r['target_id']}</code>)")
        report_text += "\n" + "\n".join(lines)
        await bot.send_message(owner_id, report_text, parse_mode=ParseMode.HTML)
    else:
        buf = io.StringIO()
        buf.write("=====================================================\n")
        buf.write(f"OTC UTILITY BOT — BROADCAST REPORT #{broadcast_id}\n")
        buf.write("=====================================================\n")
        buf.write(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
        buf.write(f"Destination: {target_type.upper()}\n")
        buf.write(f"Total: {total} | Success: {success_cnt} | Failed: {failed_cnt} | Blocked: {blocked_cnt}\n")
        buf.write(f"Duration: {human_time(duration)}\n")
        buf.write("-----------------------------------------------------\n")
        buf.write("RECIPIENT BREAKDOWN:\n")
        for r in recipients:
            uname = f"@{r['target_username']}" if r["target_username"] else "None"
            err = f" | Error: {r['error_msg']}" if r["error_msg"] else ""
            buf.write(f"[{r['status']}] ID: {r['target_id']} | Name: {r['target_name']} | Username: {uname}{err}\n")
        buf.seek(0)
        doc = io.BytesIO(buf.getvalue().encode("utf-8"))
        doc.name = f"broadcast_report_{broadcast_id}.txt"
        await bot.send_document(owner_id, document=doc, caption=report_text, parse_mode=ParseMode.HTML)


# ==========================================
# AI Analysis System (/ai, /prompt)
# ==========================================

def get_prompt() -> str:
    row = q_one(
        "SELECT value FROM settings WHERE key IN ('ai_system_prompt', 'analysis_system_prompt') "
        "ORDER BY CASE WHEN key='ai_system_prompt' THEN 0 ELSE 1 END LIMIT 1"
    )
    return row["value"] if row else (
        "You are an expert OTC trading and group moderation analyst. "
        "Analyze only the supplied messages from the selected OTC group. "
        "Never invent facts not present in the messages. "
        "Keep assessments structured, objective, and practical."
    )


def ai_provider() -> Optional[str]:
    if GEMINI_API_KEY:
        return "gemini"
    if OPENAI_API_KEY:
        return "openai"
    return None


async def gemini_generate(system: str, prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25},
    }
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY})
    if resp.status_code != 200:
        try:
            detail = resp.json()["error"]["message"]
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"Gemini error {resp.status_code}: {detail}")
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "empty response")
        raise RuntimeError(f"Gemini returned no answer ({reason})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


async def openai_generate(system: str, prompt: str) -> str:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    try:
        response = await client.responses.create(model=OPENAI_MODEL, instructions=system, input=prompt)
        if hasattr(response, "output_text") and response.output_text:
            return response.output_text.strip()
    except Exception:
        pass
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def sanitize_ai_output(raw: str) -> str:
    """Format AI output cleanly to Telegram HTML, eliminating Markdown artifacts."""
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", raw)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*_]{3,}\s*$", "", t, flags=re.MULTILINE)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run AI analysis directly via command."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type in GROUP_TYPES:
        if not await require_group_admin(update, context):
            return
        await run_group_ai_core(message.chat_id, context, reply_to_msg=message)
    else:
        await render_group_picker_for_ai(message, context, update.effective_user.id)


async def run_group_ai_core(chat_id: int, context: ContextTypes.DEFAULT_TYPE, query=None, reply_to_msg=None):
    row = group_row(chat_id)
    st = get_otc_settings(chat_id)
    if not row or not st["ai_enabled"]:
        msg = f"{e_html('warning')} AI is disabled for this OTC. Enable it in OTC Settings first."
        if query:
            await edit(query, msg, back_markup(f"st:cat:{chat_id}:ai"))
        else:
            await reply_to_msg.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    provider = ai_provider()
    if not provider:
        msg = f"{e_html('warning')} AI is not configured. Set <code>GEMINI_API_KEY</code> or <code>OPENAI_API_KEY</code> in <code>.env</code>."
        if query:
            await edit(query, msg, back_markup("dash:home"))
        else:
            await reply_to_msg.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    if chat_id in ai_running:
        msg = f"{e_html('time')} An AI request is already running for this OTC. Try again in a moment."
        if query:
            await edit(query, msg, back_markup(f"grp:view:{chat_id}"))
        else:
            await reply_to_msg.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    records = q_all(
        "SELECT user_id, username, created_at, body FROM group_messages WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
        (chat_id, ANALYSIS_MESSAGE_LIMIT),
    )
    if not records:
        msg = (
            f"{e_html('info')} There is not enough message history collected in this OTC yet.\n"
            f"Messages are collected only while AI is enabled and bot privacy mode is turned off in @BotFather."
        )
        if query:
            await edit(query, msg, back_markup(f"grp:view:{chat_id}"))
        else:
            await reply_to_msg.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    ai_running.add(chat_id)
    status_msg = None
    if query:
        await edit(query, f"{e_html('ai')} Generating structured AI insights for this OTC…")
    else:
        status_msg = await reply_to_msg.reply_text(f"{e_html('ai')} Generating structured AI insights for this OTC…", parse_mode=ParseMode.HTML)

    try:
        filtered_records = []
        for rec in reversed(records):
            if st["ai_exclude_admins"] and rec["user_id"]:
                if await is_group_admin(context.bot, chat_id, rec["user_id"]):
                    continue
            stamp = datetime.fromtimestamp(rec["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            filtered_records.append(f"[{stamp}] @{rec['username'] or 'member'}: {rec['body']}")

        data = "\n".join(filtered_records)[-MAX_ANALYSIS_CHARS:]

        system_instruction = (
            get_prompt() + "\n\n"
            "Format the output strictly using Telegram HTML tags (<b>bold</b>). "
            "Use the following exact structure:\n\n"
            "<b>OTC Analysis</b>\n\n"
            "<b>Overview</b>\nShort summary of the discussion.\n\n"
            "<b>Important Developments</b>\nKey points from recent messages.\n\n"
            "<b>Member Activity</b>\nRelevant observations regarding non-admin members.\n\n"
            "<b>Concerns</b>\nPotential issues, disagreements, suspicious behaviour, or unresolved matters.\n\n"
            "<b>Conclusion</b>\nConcise assessment."
        )
        prompt_text = (
            f"Selected OTC: {row['title']} (chat ID {chat_id})\n"
            f"Sample: {len(filtered_records)} recent member text messages.\n\n<messages>\n{data}\n</messages>"
        )

        generate = gemini_generate if provider == "gemini" else openai_generate
        raw_answer = (await generate(system_instruction, prompt_text)) or "No analysis was returned."
        clean_answer = sanitize_ai_output(raw_answer[:3500])

        out_text = f"<b>{e_html('ai')} AI Report — {esc(row['title'])}</b>\n\n{clean_answer}"
        out_text, out_markup = attach_sponsored_content(out_text, InlineKeyboardMarkup([[btn(f"{e('back')} Back to OTC", f"grp:view:{chat_id}")]]))

        if query:
            try:
                await edit(query, out_text, out_markup)
            except Exception:
                await edit(query, raw_answer[:3500], out_markup, plain=True)
        else:
            if status_msg:
                await delete_message_safe(status_msg)
            try:
                await reply_to_msg.reply_text(out_text, parse_mode=ParseMode.HTML, reply_markup=out_markup)
            except Exception:
                await reply_to_msg.reply_text(raw_answer[:3500], reply_markup=out_markup)

    except Exception as exc:
        log.exception("AI generation failed")
        err_msg = f"{e_html('error')} AI generation failed: {esc(str(exc)[:250])}"
        if query:
            await edit(query, err_msg, back_markup(f"grp:view:{chat_id}"))
        else:
            if status_msg:
                await status_msg.edit_text(err_msg, parse_mode=ParseMode.HTML)
    finally:
        ai_running.discard(chat_id)


async def render_group_picker_for_ai(message, context, user_id: int):
    groups = await eligible_groups(context.bot, user_id)
    if not groups:
        bot_user = context.bot.username or (await context.bot.get_me()).username
        add_url = f"https://t.me/{bot_user}?startgroup=true"
        await message.reply_text(
            f"{e_html('warning')} <b>No registered OTCs found.</b>\n"
            f"Add me to your group and run /register to register it.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn("➕ Add to Group", url=add_url)]]),
        )
        return
    rows = [[btn(f"{e('otc')} {g['title'][:40]}", f"grp:ai:{g['chat_id']}", PRIMARY)] for g in groups]
    rows.append([btn(f"{e('dismiss')} Dismiss", "dismiss", DANGER)])
    await message.reply_text(
        f"<b>{e_html('ai')} Choose an OTC for AI Analysis:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ==========================================
# Owner Prompt Customization (/prompt, /cancel)
# ==========================================

async def cmd_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View and update AI system prompt (Owner only)."""
    if not await require_owner(update, context):
        return

    current = get_prompt()
    preview = current if len(current) <= 400 else current[:397] + "…"

    context.user_data["awaiting_prompt"] = True
    text = (
        f"<b>{e_html('ai')} AI System Prompt Configuration</b>\n\n"
        f"<b>What is this?</b>\n"
        f"The AI system prompt controls how the AI interprets OTC messages, what it prioritizes, "
        f"and how it formats summaries.\n\n"
        f"<b>Why would you need it?</b>\n"
        f"• Direct the AI to spot specific OTC patterns (volume anomalies, unverified escrows).\n"
        f"• Enforce custom tone or specialized compliance rules.\n\n"
        f"<b>Current Active Prompt:</b>\n"
        f"<blockquote>{esc(preview)}</blockquote>\n\n"
        f"👉 <b>How to update:</b>\n"
        f"Send your new prompt text in the <b>next message</b>, or upload a <code>.txt</code> file."
    )
    markup = InlineKeyboardMarkup([[btn(f"{e('dismiss')} Cancel", "cancel_prompt", DANGER)]])
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancelled = False
    for k in ("awaiting_prompt", "awaiting_sponsored_text", "awaiting_sponsored_btn", "broadcast_setup"):
        if context.user_data.pop(k, None):
            cancelled = True
    if cancelled:
        await update.effective_message.reply_text(f"{e_html('dismiss')} Active operation cancelled.", parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text("There is no active operation to cancel.")


async def callback_cancel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("awaiting_prompt", None)
    await edit(query, f"{e_html('dismiss')} Prompt update cancelled. Existing prompt was kept.")


# ==========================================
# Private Dashboard Navigation
# ==========================================

async def eligible_groups(bot, user_id: int):
    rows = q_all("SELECT * FROM groups WHERE active=1 ORDER BY title COLLATE NOCASE")

    async def check(row):
        try:
            me = await bot.get_chat_member(row["chat_id"], bot.id)
            if me.status != ChatMemberStatus.ADMINISTRATOR:
                return None
            return row if await is_group_admin(bot, row["chat_id"], user_id) else None
        except TelegramError:
            return None

    results = await asyncio.gather(*(check(r) for r in rows))
    return [r for r in results if r]


async def render_group_picker(query, context, action: str):
    groups = await eligible_groups(context.bot, query.from_user.id)
    if not groups:
        bot_user = context.bot.username or (await context.bot.get_me()).username
        add_url = f"https://t.me/{bot_user}?startgroup=true"
        await edit(
            query,
            f"{e_html('warning')} <b>No registered OTCs found.</b>\n"
            f"Add me to your group and run /register to register it.",
            InlineKeyboardMarkup([
                [btn("➕ Add to Group", url=add_url)],
                [btn(f"{e('back')} Back", "dash:home")],
            ]),
        )
        return
    rows = [[btn(f"{e('otc')} {g['title'][:40]}", f"grp:{action}:{g['chat_id']}", PRIMARY)] for g in groups]
    rows.append([btn(f"{e('back')} Back", "dash:home")])
    await edit(query, f"<b>{e_html('otc')} Choose an OTC:</b>", InlineKeyboardMarkup(rows))


async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    bot_username = context.bot.username or (await context.bot.get_me()).username

    # Enforce force subscription for all dashboard interactions
    if not await enforce_force_sub(update, context):
        return

    if data == "dash:home":
        text, markup = attach_sponsored_content(f"<b>{e_html('otc')} OTC Management</b>\nChoose an option:", main_menu(user_id, bot_username))
        await edit(query, text, markup)
    elif data == "dash:groups":
        await render_group_picker(query, context, "view")
    elif data == "dash:settings":
        await render_group_picker(query, context, "settings")
    elif data in ("dash:analysis", "dash:ai"):
        await render_group_picker(query, context, "ai")
    elif data == "dash:sponsored":
        if is_owner(user_id):
            await send_sponsored_dashboard(query.message, query=query)
    elif data == "dash:broadcast":
        if is_owner(user_id):
            await cmd_broadcast(update, context)
    elif data == "dash:info":
        await cmd_info(update, context)
    elif data == "dash:help":
        await edit(
            query,
            f"<b>{e_html('help')} Dashboard Guide</b>\n\n"
            f"• OTCs appear after the bot is added and registered via /register.\n"
            f"• Configure service message deletion and pin management via <b>OTC Settings</b>.\n"
            f"• Run AI activity summaries via <b>AI</b> button.",
            back_markup("dash:home"),
        )


async def group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    chat_id = int(parts[2])

    if not await is_group_admin(context.bot, chat_id, query.from_user.id):
        await edit(query, f"{e_html('error')} Access denied: you are not an OTC admin.")
        return

    row = group_row(chat_id)
    if not row:
        await edit(query, f"{e_html('error')} This OTC is no longer registered.")
        return

    if action == "view":
        pins = q_one("SELECT COUNT(*) AS n FROM pins WHERE chat_id=?", (chat_id,))["n"]
        st = get_otc_settings(chat_id)
        text = (
            f"<b>{e_html('otc')} {esc(row['title'])}</b>\n\n"
            f"• {e_html('pin')} <b>Tracked pins:</b> {pins}\n"
            f"• {e_html('ai')} <b>AI:</b> {'Enabled' if st['ai_enabled'] else 'Disabled'}\n"
            f"• {e_html('tools')} <b>Bot Status:</b> {'Active' if st['bot_active'] else 'Paused'}"
        )
        markup = InlineKeyboardMarkup([
            [btn(f"{e('settings')} OTC Settings", f"st:cat:{chat_id}:main")],
            [btn(f"{e('ai')} AI Insights", f"grp:ai:{chat_id}", SUCCESS)],
            [btn(f"{e('back')} OTCs", "dash:groups")],
        ])
        await edit(query, text, markup)
    elif action == "settings":
        await send_settings_menu(query.message, chat_id, "main", query=query)
    elif action in ("analysis", "ai"):
        await run_group_ai_core(chat_id, context, query=query)


# ==========================================
# Service Messages Handler
# ==========================================

async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle service messages according to OTC settings, and trigger registration when bot is added."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.type not in GROUP_TYPES:
        return

    st = get_otc_settings(chat.id)

    # Bot added to group via service message
    if message.new_chat_members:
        bot_added = any(member.id == context.bot.id for member in message.new_chat_members)
        if bot_added:
            await prompt_or_confirm_registration(chat, context)
            return
        elif st["del_join"]:
            await delete_message_safe(message)
            return

    # Member leave
    if message.left_chat_member and st["del_leave"]:
        await delete_message_safe(message)
        return

    # Pinned message notification
    if message.pinned_message and st["del_pinned"]:
        await delete_message_safe(message)
        return

    # Other service messages
    if st["del_service"]:
        if any((
            message.new_chat_title, message.new_chat_photo, message.delete_chat_photo,
            message.group_chat_created, message.supergroup_chat_created,
            message.video_chat_started, message.video_chat_ended,
            message.video_chat_participants_invited, message.message_auto_delete_timer_changed,
        )):
            await delete_message_safe(message)


# ==========================================
# Group Activity Tracking & Auto-Pin Trigger
# ==========================================

async def track_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type not in GROUP_TYPES:
        return

    track_user(user)

    # 1. Custom duration input for /apin
    if "awaiting_apin_custom" in context.chat_data and user:
        cfg = context.chat_data["awaiting_apin_custom"]
        if cfg["admin_id"] == user.id and cfg["chat_id"] == chat.id:
            raw_text = (message.text or "").strip()
            try:
                duration = parse_duration(raw_text)
                context.chat_data.pop("awaiting_apin_custom", None)
                await delete_message_safe(message)
                try:
                    await context.bot.delete_message(chat.id, cfg["cmd_msg_id"])
                    await context.bot.delete_message(chat.id, cfg["setup_msg_id"])
                except TelegramError:
                    pass

                execute(
                    """INSERT INTO auto_pins(chat_id, admin_id, duration, status, created_at)
                       VALUES(?, ?, ?, 'PENDING', ?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                       admin_id=excluded.admin_id, duration=excluded.duration, status='PENDING', created_at=excluded.created_at""",
                    (chat.id, user.id, duration, time.time()),
                )
                await context.bot.send_message(
                    chat.id,
                    f"{e_html('time')} The next eligible member message will be pinned for: <b>{human_time(duration)}</b>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            except ValueError as exc:
                await message.reply_text(f"{e_html('error')} {esc(str(exc))}", parse_mode=ParseMode.HTML)
                return

    # 2. Check for Automatic Pinning Trigger
    pending_apin = q_one("SELECT * FROM auto_pins WHERE chat_id=? AND status='PENDING'", (chat.id,))
    if pending_apin and user and not user.is_bot:
        is_admin = await is_group_admin(context.bot, chat.id, user.id)
        is_cmd = bool(message.text and message.text.startswith("/"))
        is_anon = bool(message.sender_chat and message.sender_chat.id == chat.id)

        # Non-admin member message found
        if not is_admin and not is_cmd and not is_anon:
            execute("DELETE FROM auto_pins WHERE chat_id=?", (chat.id,))
            duration = pending_apin["duration"]
            expiry = time.time() + duration

            try:
                await context.bot.pin_chat_message(
                    chat_id=chat.id,
                    message_id=message.message_id,
                    disable_notification=True,
                )
                execute(
                    """INSERT INTO pins(chat_id,message_id,pinned_by,pinned_by_name,expires_at,attempts,next_attempt,last_error)
                       VALUES(?,?,?,?,?,0,0,NULL)
                       ON CONFLICT(chat_id,message_id) DO UPDATE SET
                       pinned_by=excluded.pinned_by, pinned_by_name=excluded.pinned_by_name,
                       expires_at=excluded.expires_at, attempts=0, next_attempt=0, last_error=NULL""",
                    (chat.id, message.message_id, user.id, user.full_name or "Member", expiry),
                )
                st = get_otc_settings(chat.id)
                if not st["del_apin"]:
                    await context.bot.send_message(
                        chat.id,
                        f"{e_html('pin')} Pinned member message for <b>{human_time(duration)}</b>.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=dismiss_keyboard(),
                    )
            except TelegramError as exc:
                log.error("Automatic pin failed for chat=%s: %s", chat.id, exc)

    # 3. Retain group text message for AI analysis
    st = get_otc_settings(chat.id)
    if not st["ai_enabled"]:
        return

    body = (message.text or message.caption or "").strip()
    if not body:
        return

    execute(
        "INSERT INTO group_messages(chat_id,user_id,username,created_at,body) VALUES(?,?,?,?,?)",
        (chat.id, user.id if user else None, user.username if user else None, time.time(), body[:4000]),
    )
    count = q_one("SELECT COUNT(*) AS n FROM group_messages WHERE chat_id=?", (chat.id,))["n"]
    if count > MESSAGE_HISTORY_LIMIT + 50:
        execute(
            """DELETE FROM group_messages WHERE chat_id=? AND id NOT IN
               (SELECT id FROM group_messages WHERE chat_id=? ORDER BY created_at DESC, id DESC LIMIT ?)""",
            (chat.id, chat.id, MESSAGE_HISTORY_LIMIT),
        )


# ==========================================
# Private Chat Message Handler (Wizards)
# ==========================================

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    track_user(user)

    # A. Broadcast Content Wizard
    if "broadcast_setup" in context.user_data and is_owner(user.id):
        setup = context.user_data["broadcast_setup"]
        setup["message_id"] = message.message_id
        setup["from_chat_id"] = message.chat_id

        target_type = setup["target_type"]
        if target_type == "users":
            count = q_one("SELECT COUNT(*) AS n FROM users WHERE is_active=1")["n"]
            label = f"All Bot Users ({count})"
        else:
            count = q_one("SELECT COUNT(*) AS n FROM groups WHERE active=1")["n"]
            label = f"All Registered OTC Groups ({count})"

        await message.reply_text("<b>Broadcast Preview:</b>", parse_mode=ParseMode.HTML)
        await context.bot.copy_message(chat_id=message.chat_id, from_chat_id=message.chat_id, message_id=message.message_id)

        confirm_text = (
            f"<b>{e_html('broadcast')} Broadcast Confirmation</b>\n\n"
            f"• <b>Target:</b> {label}\n"
            f"• <b>Total Recipients:</b> <code>{count}</code>\n\n"
            f"Do you want to send this broadcast?"
        )
        markup = InlineKeyboardMarkup([
            [
                btn(f"{e('success')} Confirm Broadcast", "bd:confirm", SUCCESS),
                btn(f"{e('dismiss')} Cancel", "bd:cancel", DANGER),
            ]
        ])
        await message.reply_text(confirm_text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # B. Sponsored Text Wizard
    if context.user_data.get("awaiting_sponsored_text") and is_owner(user.id):
        html_content = message.text_html or message.caption_html or ""
        if not html_content:
            await message.reply_text(f"{e_html('warning')} Empty message. Please send text or press Cancel.")
            return

        execute("UPDATE sponsored_settings SET text_html=?, text_enabled=1 WHERE id=1", (html_content,))
        context.user_data.pop("awaiting_sponsored_text", None)
        await message.reply_text(f"{e_html('success')} <b>Sponsored text saved and enabled!</b>", parse_mode=ParseMode.HTML)
        await send_sponsored_dashboard(message)
        return

    # C. Sponsored Button Wizard
    if context.user_data.get("awaiting_sponsored_btn") and is_owner(user.id):
        content = (message.text or "").strip()
        match = re.match(r"^(?:#(primary|success|danger|blue|green|red)\s+)?(.*?)\s*[-—]\s*(https?://\S+|t\.me/\S+)$", content, re.IGNORECASE)
        if not match:
            await message.reply_text(
                f"{e_html('warning')} <b>Invalid format.</b>\n"
                f"Please send as:\n<code>#success Join OTC - https://t.me/Oncarts</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        raw_style, btn_text, btn_url = match.groups()
        style_map = {"blue": PRIMARY, "primary": PRIMARY, "green": SUCCESS, "success": SUCCESS, "red": DANGER, "danger": DANGER}
        style = style_map.get((raw_style or "").lower(), PRIMARY)

        if not btn_url.startswith("http://") and not btn_url.startswith("https://"):
            btn_url = "https://" + btn_url

        execute(
            "UPDATE sponsored_settings SET button_style=?, button_text=?, button_url=?, button_enabled=1 WHERE id=1",
            (style, btn_text.strip(), btn_url.strip()),
        )
        context.user_data.pop("awaiting_sponsored_btn", None)
        await message.reply_text(f"{e_html('success')} <b>Sponsored button saved and enabled!</b>", parse_mode=ParseMode.HTML)
        await send_sponsored_dashboard(message)
        return

    # D. Prompt Wizard
    if context.user_data.get("awaiting_prompt") and is_owner(user.id):
        content = (message.text or "").strip()
        if not content:
            await message.reply_text(f"{e_html('warning')} The prompt cannot be empty.")
            return
        if len(content.encode("utf-8")) > MAX_PROMPT_BYTES:
            await message.reply_text(f"{e_html('error')} Prompt is too large (max {MAX_PROMPT_BYTES // 1000} KB).")
            return

        execute("INSERT INTO settings(key,value) VALUES('ai_system_prompt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (content[:MAX_PROMPT_BYTES],))
        execute("INSERT INTO settings(key,value) VALUES('analysis_system_prompt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (content[:MAX_PROMPT_BYTES],))
        context.user_data.pop("awaiting_prompt", None)
        await message.reply_text(f"{e_html('success')} <b>AI Prompt saved successfully!</b>", parse_mode=ParseMode.HTML)
        return

    # E. Force Sub Channel Wizard
    if context.user_data.get("awaiting_fsub_channel") and is_owner(user.id):
        new_ch = (message.text or "").strip()
        if not new_ch:
            await message.reply_text(f"{e_html('warning')} Channel cannot be empty. Send a username like @MyChannel or a URL.")
            return
        execute(
            "INSERT INTO settings(key,value) VALUES('force_sub_channel',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (new_ch,),
        )
        execute(
            "INSERT INTO settings(key,value) VALUES('force_sub_enabled','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        context.user_data.pop("awaiting_fsub_channel", None)
        await message.reply_text(
            f"{e_html('success')} <b>Force Join Channel set and enabled:</b> <code>{esc(new_ch)}</code>",
            parse_mode=ParseMode.HTML,
        )
        await send_fsub_dashboard(message)
        return


async def receive_prompt_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    document = message.document
    if not document or not user or not is_owner(user.id):
        return

    filename = (document.file_name or "").lower()
    if not filename.endswith(".txt"):
        await message.reply_text(f"{e_html('warning')} Please upload the AI system prompt as a <code>.txt</code> file.", parse_mode=ParseMode.HTML)
        return
    if document.file_size and document.file_size > MAX_PROMPT_BYTES:
        await message.reply_text(f"{e_html('error')} Prompt file is too large (max {MAX_PROMPT_BYTES // 1000} KB).", parse_mode=ParseMode.HTML)
        return

    file = await context.bot.get_file(document.file_id)
    content = (await file.download_as_bytearray()).decode("utf-8", errors="replace").strip()
    if not content:
        await message.reply_text(f"{e_html('warning')} The prompt file is empty.", parse_mode=ParseMode.HTML)
        return

    execute("INSERT INTO settings(key,value) VALUES('ai_system_prompt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (content[:MAX_PROMPT_BYTES],))
    execute("INSERT INTO settings(key,value) VALUES('analysis_system_prompt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (content[:MAX_PROMPT_BYTES],))
    context.user_data.pop("awaiting_prompt", None)
    await message.reply_text(f"{e_html('success')} <b>Prompt file saved successfully!</b>", parse_mode=ParseMode.HTML)


# ==========================================
# Membership & Migration Handlers
# ==========================================

async def on_bot_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    change = update.my_chat_member
    if not chat or not change or chat.type not in GROUP_TYPES:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status

    if new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER):
        # Trigger interactive registration check whenever bot is added/promoted in group
        await prompt_or_confirm_registration(chat, context)
    elif new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        execute("UPDATE groups SET active=0 WHERE chat_id=?", (chat.id,))
        execute("DELETE FROM pins WHERE chat_id=?", (chat.id,))
        execute("DELETE FROM auto_pins WHERE chat_id=?", (chat.id,))
        execute("DELETE FROM group_messages WHERE chat_id=?", (chat.id,))


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.migrate_to_chat_id:
        return
    old, new = message.chat_id, message.migrate_to_chat_id
    for table in ("groups", "otc_settings", "pins", "auto_pins", "group_messages"):
        execute(f"UPDATE OR IGNORE {table} SET chat_id=? WHERE chat_id=?", (new, old))
    execute("DELETE FROM groups WHERE chat_id=?", (old,))
    execute("DELETE FROM otc_settings WHERE chat_id=?", (old,))
    execute("DELETE FROM pins WHERE chat_id=?", (old,))
    execute("DELETE FROM auto_pins WHERE chat_id=?", (old,))
    execute("DELETE FROM group_messages WHERE chat_id=?", (old,))
    log.info("OTC migrated: %s -> %s", old, new)


# ==========================================
# Persistent Pin Worker
# ==========================================

async def pin_worker(bot):
    """Process due automatic unpins reliably surviving restarts."""
    now = time.time()
    due = q_all(
        "SELECT * FROM pins WHERE expires_at IS NOT NULL AND expires_at<=? AND next_attempt<=? LIMIT 50",
        (now, now),
    )
    for row in due:
        chat_id, message_id = row["chat_id"], row["message_id"]
        try:
            await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
            execute("DELETE FROM pins WHERE chat_id=? AND message_id=?", (chat_id, message_id))
            log.info("Automatically unpinned chat=%s message=%s", chat_id, message_id)
        except BadRequest as exc:
            text = str(exc).lower()
            if any(s in text for s in ("not pinned", "not found", "message_id_invalid", "chat not found")):
                execute("DELETE FROM pins WHERE chat_id=? AND message_id=?", (chat_id, message_id))
                continue
            await schedule_retry(row, str(exc), give_up=True)
        except RetryAfter as exc:
            wait = getattr(exc, "retry_after", 5.0)
            await schedule_retry(row, str(exc), min_delay=float(wait))
        except (TimedOut, NetworkError) as exc:
            await schedule_retry(row, str(exc))
        except Forbidden as exc:
            await schedule_retry(row, f"Permission error: {exc}", give_up=True)
        except TelegramError as exc:
            await schedule_retry(row, str(exc), give_up=True)


async def schedule_retry(row, error: str, min_delay: float = 0, give_up: bool = False):
    attempts = row["attempts"] + 1
    if give_up and attempts >= PIN_GIVE_UP_ATTEMPTS:
        execute("DELETE FROM pins WHERE chat_id=? AND message_id=?", (row["chat_id"], row["message_id"]))
        log.error("Giving up unpin chat=%s message=%s: %s", row["chat_id"], row["message_id"], error)
        return
    delay = max(min(30 * (2 ** min(attempts - 1, 4)), PIN_RETRY_MAX_SECONDS), min_delay)
    execute(
        "UPDATE pins SET attempts=?, next_attempt=?, last_error=? WHERE chat_id=? AND message_id=?",
        (attempts, time.time() + delay, error[:500], row["chat_id"], row["message_id"]),
    )


# ==========================================
# Lifecycle, Cloud Health Check & Bot Setup
# ==========================================

async def post_init(application: Application):
    global background_task

    # 1. Background persistent pin worker loop
    async def worker_loop():
        while True:
            try:
                await pin_worker(application.bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Pin worker cycle error")
            await asyncio.sleep(PIN_WORKER_INTERVAL)

    background_task = asyncio.create_task(worker_loop())

    # 2. Configure Telegram native command menus (setMyCommands)
    try:
        # Default Scope
        await application.bot.set_my_commands([
            BotCommand("start", "Open dashboard"),
            BotCommand("help", "Command guide"),
            BotCommand("info", "Bot status and stats"),
        ], scope=BotCommandScopeDefault())

        # Group Scope
        group_commands = [
            BotCommand("pin", "Reply to pin message"),
            BotCommand("unpin", "Reply to unpin message"),
            BotCommand("cancelpin", "Cancel scheduled unpin"),
            BotCommand("pinlist", "List tracked pins"),
            BotCommand("apin", "Automatic pinning mode"),
            BotCommand("settings", "Group settings panel"),
            BotCommand("ai", "Run AI insights"),
            BotCommand("register", "Register OTC in dashboard"),
            BotCommand("help", "Help and guide"),
        ]
        await application.bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(group_commands, scope=BotCommandScopeAllChatAdministrators())

        # Owner Scopes
        owner_commands = [
            BotCommand("start", "Open dashboard"),
            BotCommand("help", "Help and guide"),
            BotCommand("info", "Bot status and stats"),
            BotCommand("prompt", "Configure AI prompt (Owner)"),
            BotCommand("sponsored", "Manage sponsored content (Owner)"),
            BotCommand("bd", "Send broadcast (Owner)"),
            BotCommand("fsub", "Manage force join channel (Owner)"),
            BotCommand("cancel", "Cancel active operation"),
        ]
        for owner_id in BOT_OWNER_IDS:
            try:
                await application.bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=owner_id))
            except TelegramError:
                pass

        log.info("Telegram native bot command scopes configured successfully.")
    except Exception as exc:
        log.warning("Could not set bot commands: %s", exc)

    log.info("OTC Bot initialized. Persistent workers active.")


async def post_shutdown(application: Application):
    if background_task:
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            pass
    if active_broadcast_task and not active_broadcast_task.done():
        active_broadcast_task.cancel()
    if db:
        db.close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled update exception", exc_info=context.error)


async def callback_dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except TelegramError:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server to satisfy cloud platform port scans (Render, Railway, etc.)."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Telegram OTC Bot is active\n")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    """Start HTTP health-check server on $PORT if specified (Render Web Service)."""
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        log.info("Cloud port-scan health server listening on 0.0.0.0:%s", port)
    except Exception as exc:
        log.warning("Could not start port health-check server on port %s: %s", port_str, exc)


def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # General commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))

    # Pin & Auto-pin commands
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("cancelpin", cmd_cancelpin))
    app.add_handler(CommandHandler("pinlist", cmd_pinlist))
    app.add_handler(CommandHandler("apin", cmd_apin))
    app.add_handler(CommandHandler("register", cmd_register))

    # Settings & AI
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler(["ai", "analysis"], cmd_ai))

    # Owner commands
    app.add_handler(CommandHandler("prompt", cmd_prompt))
    app.add_handler(CommandHandler("sponsored", cmd_sponsored))
    app.add_handler(CommandHandler(["bd", "broadcast"], cmd_broadcast))
    app.add_handler(CommandHandler(["fsub", "forcejoin"], cmd_fsub))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(callback_dismiss, pattern=r"^dismiss$"))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(pinlist_callback, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(dashboard_callback, pattern=r"^dash:"))
    app.add_handler(CallbackQueryHandler(group_callback, pattern=r"^grp:"))
    app.add_handler(CallbackQueryHandler(callback_apin, pattern=r"^apin:"))
    app.add_handler(CallbackQueryHandler(callback_settings, pattern=r"^st:"))
    app.add_handler(CallbackQueryHandler(callback_sponsored, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(callback_broadcast, pattern=r"^bd:"))
    app.add_handler(CallbackQueryHandler(callback_cancel_prompt, pattern=r"^cancel_prompt$"))
    app.add_handler(CallbackQueryHandler(callback_register, pattern=r"^reg:"))
    app.add_handler(CallbackQueryHandler(callback_force_sub, pattern=r"^fsub:check$"))
    app.add_handler(CallbackQueryHandler(callback_fsub_owner, pattern=r"^fsub:(?!check).*$"))

    # Chat status, migration & membership
    app.add_handler(ChatMemberHandler(on_bot_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, on_service_message))

    # Activity tracking & message wizards
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND & filters.UpdateType.MESSAGE, track_group_activity))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, receive_prompt_file))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))

    app.add_error_handler(error_handler)
    return app


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN in .env (or as an environment variable) before starting.")
    start_health_server()
    init_db()
    try:
        log.info("Starting OTC Bot polling…")
        build_app().run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        log.exception("FATAL: Bot polling crashed. Exiting.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
