"""The Telegram bot. Owner: D.

One process: Telegram polling plus the embedded dashboard (channels/web.py: operator controls + credentials)
on CREDENTIALS_PORT, sharing one CredentialStore. A text or voice message becomes
one channels.runner.run_task() call; progress is relayed into the chat as
it happens, approvals can be decided from an inline keyboard here or from the web
dashboard (both just write to the same event log), and the final answer is spoken
back through ElevenLabs when a key is available. A sent document is saved and attached
to the next task in that chat — immediately, if sent with a caption (the caption becomes
the task), or buffered until a text message follows — the same `attach` a terminal's
`frank run --attach FILE` or the dashboard's upload form would give a run.

  uv run python -m channels.telegram.bot    # needs FRANK_TELEGRAM_KEY or a token entered on the dashboard
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import uvicorn
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from channels import runner, voice, web
from channels.chat import approval_callback_data, is_busy, parse_approval_callback, quality_for
from channels.chat import truncate as _truncate
from channels.credentials import CredentialStore, offered_secrets, wait_for_token
from harness import config
from harness.contracts import EventType
from harness.ops.events import EventLog

CREDENTIALS_PORT = int(os.environ.get("FRANK_CREDENTIALS_PORT", "8001"))
MAX_MESSAGE_CHARS = 3500  # Telegram's limit is 4096; leave room for formatting
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # matches channels/web.py's New task upload cap


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return _truncate(text, limit)


# ---- handlers ---------------------------------------------------------------------------------


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a task (text or voice) and I'll run it through Frankenstein. Attach a file and I'll "
        "pass it along as an input — send it with a caption to run right away, or send it alone and "
        "then your task text. /quality cheap|full picks the model tier (cheap by default). /kill stops "
        "whatever's running right now, everywhere, not just in this chat."
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


def _save_attachment(chat_id: int, filename: str, data: bytes) -> Path:
    """Writes an uploaded document under work/uploads/telegram-<chat_id>/, named by basename
    only so a filename can never write outside that folder — the Telegram-side equivalent of
    channels/web.py's own upload handling for the dashboard's New task form."""
    dest_dir = config.WORK_DIR / "uploads" / f"telegram-{chat_id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(filename).name
    dest.write_bytes(data)
    return dest


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    name = doc.file_name or doc.file_id
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await update.message.reply_text(f"{name}: larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        return
    file = await context.bot.get_file(doc.file_id)
    data = bytes(await file.download_as_bytearray())
    path = _save_attachment(update.effective_chat.id, name, data)
    context.chat_data.setdefault("pending_attachments", []).append(path)
    caption = (update.message.caption or "").strip()
    if caption:
        await handle_task(update, context, caption)
    else:
        await update.message.reply_text(f"Attached {path.name}. Send your task text (or more files) and I'll include it.")


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


async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str) -> None:
    chat_data = context.chat_data
    if is_busy(chat_data):
        await update.message.reply_text("Still working on your last task in this chat — wait for it to finish, or /kill.")
        return
    chat_data["busy"] = True
    attach = chat_data.pop("pending_attachments", [])
    session = f"telegram-{update.effective_chat.id}-{uuid.uuid4().hex[:8]}"
    status_msg = await update.message.reply_text("Starting…")
    lines: list[str] = []
    answer_text: str | None = None
    secrets = offered_secrets(context.bot_data["store"])
    try:
        async for upd in runner.run_task(task, session=session, models=quality_for(chat_data), secrets=secrets, attach=attach):
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
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
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

    token = await wait_for_token(store, "telegram", port=CREDENTIALS_PORT)
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
