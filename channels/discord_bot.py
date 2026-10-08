"""The Discord bot. Owner: D. Same shape as channels/telegram/bot.py — a text or voice message
becomes one channels.runner.run_task() call, reusing the same runner, voice, credentials and
chat-helper modules, so this file is Discord-specific glue, not a second implementation of any
of that. Named discord_bot.py, not channels/discord/, so nothing here can be confused with (or
accidentally shadow) the third-party `discord` package it imports.

Responds in DMs always, and in a server channel only when @mentioned, so it doesn't talk in
every channel it's been added to. /quality and /kill are plain text commands (not Discord's
native slash commands) so the two bots share one command style and nothing needs a slash-command
sync step — see Creating the bot at https://discord.com/developers/applications: enable the
"Message Content" privileged intent, or on_message never sees the task text.

  uv run python -m channels.discord_bot    # needs FRANK_DISCORD_KEY or a token entered on the dashboard
"""

from __future__ import annotations

import asyncio
import io
import os

import discord
import uvicorn

from channels import runner, voice, web
from channels.chat import approval_callback_data, is_busy, parse_approval_callback, quality_for
from channels.chat import truncate as _truncate
from channels.credentials import CredentialStore, offered_secrets, wait_for_token

CREDENTIALS_PORT = int(os.environ.get("FRANK_CREDENTIALS_PORT", "8001"))
MAX_MESSAGE_CHARS = 1900  # Discord's limit is 2000; leave room for formatting


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return _truncate(text, limit)


def mentions_me(message: discord.Message, me: discord.ClientUser | None) -> bool:
    return me is not None and me in message.mentions


def strip_mention(text: str, me: discord.ClientUser) -> str:
    return text.replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip()


class ApprovalView(discord.ui.View):
    """One per approval card; its two buttons carry the same request id/approved encoding
    channels.chat uses for Telegram's inline keyboard, just read from a Discord custom_id."""

    def __init__(self, request_id: str):
        super().__init__(timeout=None)
        self.add_item(_ApprovalButton(request_id, True))
        self.add_item(_ApprovalButton(request_id, False))


class _ApprovalButton(discord.ui.Button):
    def __init__(self, request_id: str, approved: bool):
        style = discord.ButtonStyle.success if approved else discord.ButtonStyle.danger
        super().__init__(label="Approve" if approved else "Reject", style=style, custom_id=approval_callback_data(request_id, approved))

    async def callback(self, interaction: discord.Interaction) -> None:
        request_id, approved = parse_approval_callback(self.custom_id)
        by = f"discord:{interaction.user.name}"
        runner.decide_approval(request_id, approved, by)
        verdict = "Approved" if approved else "Rejected"
        await interaction.response.edit_message(content=f"{interaction.message.content}\n\n{verdict} by {by}.", view=None)


class FrankensteinClient(discord.Client):
    def __init__(self, store: CredentialStore):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.store = store
        self.chat_state: dict[int, dict] = {}  # channel id -> {"quality":..., "busy":...}

    def _state(self, channel_id: int) -> dict:
        return self.chat_state.setdefault(channel_id, {})

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        is_dm = message.guild is None
        if not is_dm and not mentions_me(message, self.user):
            return
        text = strip_mention(message.content, self.user) if self.user else message.content

        voice_attachment = next((a for a in message.attachments if a.is_voice_message()), None)
        if voice_attachment:
            await self._handle_voice(message, voice_attachment)
            return
        if not text:
            return
        if text.startswith("/quality"):
            await self._handle_quality(message, text)
            return
        if text.startswith("/kill"):
            await self._handle_kill(message)
            return
        if text.startswith(("/start", "/help")):
            await message.reply(
                "Send me a task (text or voice — DM me, or @mention me in a server channel) and I'll run it "
                "through Frankenstein. /quality cheap|full picks the model tier (cheap by default). "
                "/kill stops whatever's running right now, everywhere, not just here."
            )
            return
        await self._handle_task(message, text)

    async def _handle_quality(self, message: discord.Message, text: str) -> None:
        chat = self._state(message.channel.id)
        parts = text.split()
        arg = parts[1].lower() if len(parts) > 1 else ""
        if arg not in ("cheap", "full"):
            await message.reply(f"Current: {quality_for(chat)}. Usage: /quality cheap|full")
            return
        chat["quality"] = arg
        await message.reply(f"This channel now runs on {arg} models.")

    async def _handle_kill(self, message: discord.Message) -> None:
        from harness import config
        from harness.contracts import EventType
        from harness.ops.events import EventLog

        EventLog(config.LOG_PATH, session="discord").emit(EventType.KILL, by=f"discord:{message.author.name}")
        await message.reply("Kill sent. This stops every run in progress right now, not just here.")

    async def _handle_voice(self, message: discord.Message, attachment: discord.Attachment) -> None:
        key = self.store.get("elevenlabs")
        if not key:
            await message.reply(
                f"I need an ElevenLabs key to understand voice messages. Set one at http://localhost:{CREDENTIALS_PORT}/ "
                "(or send your task as text instead)."
            )
            return
        audio = await attachment.read()
        try:
            text = await asyncio.to_thread(voice.transcribe, audio, api_key=key)
        except Exception as e:  # the key might be wrong, or the service down; don't crash the chat over it
            await message.reply(f"Couldn't transcribe that: {e}")
            return
        await message.reply(f"Heard: {text}")
        await self._handle_task(message, text)

    async def _handle_task(self, message: discord.Message, task: str) -> None:
        chat = self._state(message.channel.id)
        if is_busy(chat):
            await message.reply("Still working on your last task here — wait for it to finish, or /kill.")
            return
        chat["busy"] = True
        session = f"discord-{message.channel.id}-{message.id}"
        status_msg = await message.reply("Starting…")
        lines: list[str] = []
        answer_text: str | None = None
        secrets = offered_secrets(self.store)
        try:
            async for upd in runner.run_task(task, session=session, models=quality_for(chat), secrets=secrets):
                if upd.kind == "approval_requested":
                    req = upd.approval
                    await message.reply(truncate(upd.text), view=ApprovalView(req["id"]))
                    continue
                if upd.kind == "answer":
                    answer_text = upd.text
                    continue
                lines.append(upd.text)
                await status_msg.edit(content=truncate("\n".join(lines)))
        finally:
            chat["busy"] = False

        if answer_text:
            await message.reply(truncate(answer_text))
            await self._reply_voice(message, answer_text)

    async def _reply_voice(self, message: discord.Message, text: str) -> None:
        key = self.store.get("elevenlabs")
        if not key:
            return  # no key: text-only reply, not a hard failure
        try:
            audio = await asyncio.to_thread(voice.speak, text, api_key=key)
        except Exception:
            return  # a bad TTS call shouldn't hide the text answer the user already has
        await message.reply(file=discord.File(io.BytesIO(audio), filename="reply.mp3"))


async def run_forever() -> None:
    """The dashboard comes up immediately, even with no Discord token yet — that's the only
    way to supply one, since there's no chat to ask in before the bot can connect at all."""
    store = CredentialStore()
    web.store = store
    server = uvicorn.Server(uvicorn.Config(web.app, host="0.0.0.0", port=CREDENTIALS_PORT, log_level="warning"))
    server_task = asyncio.create_task(server.serve())

    token = await wait_for_token(store, "discord", port=CREDENTIALS_PORT)
    client = FrankensteinClient(store)
    try:
        await client.start(token)
    finally:
        await client.close()
        server_task.cancel()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
