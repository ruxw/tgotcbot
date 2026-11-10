"""
emojis.py - Centralized emoji configuration for the Telegram OTC Bot.

Provides clean, professional unicode fallback emojis along with support for
Telegram custom emoji IDs (<tg-emoji emoji-id="...">fallback</tg-emoji>).

Usage:
    from emojis import e, e_html

    # In HTML messages:
    text = f"{e_html('pin')} Pinned message"

    # In buttons, alerts, or plain text:
    button_text = f"{e('settings')} Settings"
"""

import os
from typing import Dict, Optional

# Custom Emoji IDs (supply Telegram custom emoji 64-bit integer IDs here or in .env).
# If empty or not set, the fallback unicode emoji is used automatically.
CUSTOM_EMOJI_IDS: Dict[str, str] = {
    "pin": os.getenv("EMOJI_PIN_ID", ""),
    "unpin": os.getenv("EMOJI_UNPIN_ID", ""),
    "otc": os.getenv("EMOJI_OTC_ID", ""),
    "settings": os.getenv("EMOJI_SETTINGS_ID", ""),
    "ai": os.getenv("EMOJI_AI_ID", ""),
    "help": os.getenv("EMOJI_HELP_ID", ""),
    "success": os.getenv("EMOJI_SUCCESS_ID", ""),
    "warning": os.getenv("EMOJI_WARNING_ID", ""),
    "error": os.getenv("EMOJI_ERROR_ID", ""),
    "time": os.getenv("EMOJI_TIME_ID", ""),
    "admin": os.getenv("EMOJI_ADMIN_ID", ""),
    "tools": os.getenv("EMOJI_TOOLS_ID", ""),
    "dismiss": os.getenv("EMOJI_DISMISS_ID", ""),
    "info": os.getenv("EMOJI_INFO_ID", ""),
}

# Clean, professional unicode fallback emojis
FALLBACK_EMOJIS: Dict[str, str] = {
    "pin": "📌",
    "unpin": "📍",
    "otc": "🏛️",
    "settings": "⚙️",
    "ai": "🤖",
    "help": "ℹ️",
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
    "time": "⏱️",
    "admin": "🛡️",
    "tools": "🛠️",
    "dismiss": "✕",
    "back": "‹",
    "forward": "›",
}


def e(name: str) -> str:
    """
    Return the plain unicode fallback emoji.
    Always safe for InlineKeyboardButton text, alerts, and plain text strings.
    """
    return FALLBACK_EMOJIS.get(name, "")


def e_html(name: str) -> str:
    """
    Return an emoji suitable for Telegram HTML-formatted messages.
    If a valid numeric custom emoji ID is configured, wraps with <tg-emoji>.
    Otherwise returns the clean unicode fallback emoji.
    """
    fallback = FALLBACK_EMOJIS.get(name, "")
    custom_id = str(CUSTOM_EMOJI_IDS.get(name, "")).strip()

    if custom_id and custom_id.isdigit():
        return f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'
    return fallback


def set_custom_emoji_id(name: str, emoji_id: str) -> None:
    """Dynamically set or override a custom emoji ID at runtime."""
    CUSTOM_EMOJI_IDS[name] = str(emoji_id).strip()
