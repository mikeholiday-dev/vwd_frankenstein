"""Platform-agnostic chat helpers, shared by every bot channel (Telegram, Discord, ...). Owner: D.

Pure functions only: a chat/channel's "quality" and "busy" state is a plain dict the caller
owns (each platform's own per-chat storage — python-telegram-bot's chat_data, this project's
own per-channel dict for Discord), and approval callback data is just a string every
platform's button mechanism can carry (Telegram's callback_data, Discord's custom_id). This
keeps every bot module thin glue over channels.runner, not a second implementation of this.
"""

from __future__ import annotations

DEFAULT_TRUNCATE_LIMIT = 2000


def quality_for(chat_data: dict) -> str:
    return chat_data.get("quality", "cheap")


def is_busy(chat_data: dict) -> bool:
    return bool(chat_data.get("busy"))


def approval_callback_data(request_id: str, approved: bool) -> str:
    return f"approval:{int(approved)}:{request_id}"


def parse_approval_callback(data: str) -> tuple[str, bool]:
    _, approved, request_id = data.split(":", 2)
    return request_id, bool(int(approved))


def truncate(text: str, limit: int = DEFAULT_TRUNCATE_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
