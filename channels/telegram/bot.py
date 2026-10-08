"""The Telegram bot. Owner: D.

One process: Telegram polling plus the embedded dashboard (channels/web.py: console controls + credentials)
on CREDENTIALS_PORT, sharing one CredentialStore. A text or voice message becomes
one channels.telegram.runner.run_task() call; progress is relayed into the chat as
it happens, approvals can be decided from an inline keyboard here or from the web
console (both just write to the same event log), and the final answer is spoken
back through ElevenLabs when a key is available.

  uv run python -m channels.telegram.bot    # needs FRANK_TELEGRAM_KEY or a token entered on the dashboard
"""

from __future__ import annotations

import asyncio
import os
import uuid

import uvicorn
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from channels import voice, web
from channels.credentials import CredentialStore
from channels.telegram import runner
from harness import config
from harness.contracts import EventType
from harness.ops.events import EventLog

CREDENTIALS_PORT = int(os.environ.get("FRANK_CREDENTIALS_PORT", "8001"))
MAX_MESSAGE_CHARS = 3500  # Telegram's limit is 4096; leave room for formatting

# ---- pure helpers (unit-tested without touching Telegram or the harness) --------------------


def quality_for(chat_data: dict) -> str:
    return chat_data.get("quality", "cheap")


def is_busy(chat_data: dict) -> bool:
    return bool(chat_data.get("busy"))


def approval_callback_data(request_id: str, approved: bool) -> str:
    return f"approval:{int(approved)}:{request_id}"


def parse_approval_callback(data: str) -> tuple[str, bool]:
    _, approved, request_id = data.split(":", 2)
    return request_id, bool(int(approved))


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- handlers ---------------------------------------------------------------------------------


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a task (text or voice) and I'll run it through Frankenstein. "
        "/quality cheap|full picks the model tier (cheap by default). /kill stops whatever's running "
        "right now, everywhere, not just in this chat."
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("cheap", "full"):
        await update.message.reply_text(f"Current: {quality_for(context.chat_data)}. Usage: /quality cheap|full")
        return
    context.chat_data["quality"] = arg
    await update.message.reply_text(f"This chat now runs on {arg} models.")


async def on_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    EventLog(config.LOG_PATH, session="telegram").emit(EventType.KILL, by=f"telegram:{update.effective_user.username or update.effective_user.id}")
    await update.message.reply_text("Kill sent. This stops every run in progress right now, not just this chat's.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_task(update, context, update.message.text)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = context.bot_data["store"].get("elevenlabs")
    if not key:
        await update.message.reply_text(
            f"I need an ElevenLabs key to understand voice messages. Set one at http://localhost:{CREDENTIALS_PORT}/ "
            "(or send your task as text instead)."
        )
        return
    file = await update.message.voice.get_file()
    audio = bytes(await file.download_as_bytearray())
    try:
        text = await asyncio.to_thread(voice.transcribe, audio, api_key=key)
    except Exception as e:  # the key might be wrong, or the service down; don't crash the chat over it
        await update.message.reply_text(f"Couldn't transcribe that: {e}")
        return
    await update.message.reply_text(f"Heard: {text}")
    await handle_task(update, context, text)


def offered_secrets(store: CredentialStore) -> dict[str, str]:
    """Pass the operator's own keys through to Frankenstein's vault (gateway mode), so a
    bot-triggered run can build and call a keyed-API capability the same way a terminal
    operator with a .env file could — separate from the bot's own direct ElevenLabs calls."""
    out = {}
    if tok := store.get("apify"):
        out["APIFY_TOKEN"] = tok
    if key := store.get("elevenlabs"):
        out["ELEVENLABS_API_KEY"] = key
    return out


async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str) -> None:
    chat_data = context.chat_data
    if is_busy(chat_data):
        await update.message.reply_text("Still working on your last task in this chat — wait for it to finish, or /kill.")
        return
    chat_data["busy"] = True
    session = f"telegram-{update.effective_chat.id}-{uuid.uuid4().hex[:8]}"
    status_msg = await update.message.reply_text("Starting…")
    lines: list[str] = []
    answer_text: str | None = None
    secrets = offered_secrets(context.bot_data["store"])
    try:
        async for upd in runner.run_task(task, session=session, models=quality_for(chat_data), secrets=secrets):
            if upd.kind == "approval_requested":
                await _ask_approval(update, upd)
                continue
            if upd.kind == "answer":
                answer_text = upd.text
                continue
            lines.append(upd.text)
            await status_msg.edit_text(truncate("\n".join(lines)))
    finally:
        chat_data["busy"] = False

    if answer_text:
        await update.message.reply_text(truncate(answer_text))
        await _reply_voice(update, context, answer_text)


async def _ask_approval(update: Update, upd: runner.Progress) -> None:
    req = upd.approval
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve", callback_data=approval_callback_data(req["id"], True)),
        InlineKeyboardButton("Reject", callback_data=approval_callback_data(req["id"], False)),
    ]])
    await update.message.reply_text(truncate(upd.text), reply_markup=keyboard)


async def on_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    request_id, approved = parse_approval_callback(query.data)
    by = f"telegram:{update.effective_user.username or update.effective_user.id}"
    runner.decide_approval(request_id, approved, by)
    await query.answer()
    await query.edit_message_text(f"{query.message.text}\n\n{'Approved' if approved else 'Rejected'} by {by}.")


async def _reply_voice(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    key = context.bot_data["store"].get("elevenlabs")
    if not key:
        return  # no key: text-only reply, not a hard failure
    try:
        audio = await asyncio.to_thread(voice.speak, text, api_key=key)
    except Exception:
        return  # a bad TTS call shouldn't hide the text answer the user already has
    await update.message.reply_voice(voice=audio)


# ---- wiring -------------------------------------------------------------------------------


def build_app(token: str, store: CredentialStore) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["store"] = store
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("quality", on_quality))
    application.add_handler(CommandHandler("kill", on_kill))
    application.add_handler(MessageHandler(filters.VOICE, on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(CallbackQueryHandler(on_approval_callback))
    return application


async def run_forever() -> None:
    """The dashboard comes up immediately, even with no Telegram token yet — that's the
    only way to supply one, since there's no chat to ask in before the bot can connect at all."""
    store = CredentialStore()
    web.store = store
    server = uvicorn.Server(uvicorn.Config(web.app, host="0.0.0.0", port=CREDENTIALS_PORT, log_level="warning"))
    server_task = asyncio.create_task(server.serve())

    token = store.get("telegram")
    if not token:
        print(f"Waiting for a Telegram bot token: set FRANK_TELEGRAM_KEY, or open http://localhost:{CREDENTIALS_PORT}/ and save one.")
    while not token:
        await asyncio.sleep(1.0)
        token = store.get("telegram")

    application = build_app(token, store)
    async with application:
        await application.start()
        await application.updater.start_polling()
        try:
            await server_task
        finally:
            await application.updater.stop()
            await application.stop()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
