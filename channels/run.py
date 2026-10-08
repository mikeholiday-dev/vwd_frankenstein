"""Run every channel together. Owner: D.

channels/telegram/bot.py and channels/discord_bot.py each work standalone (their own
run_forever() starts the dashboard too), but running both that way would mean two dashboard
servers fighting over the same port, each with its own CredentialStore — a key entered while
only the Telegram bot is up wouldn't be visible to Discord. This starts one dashboard and one
store, then brings up each bot independently as soon as its own token is available, so setting
up Telegram doesn't block waiting on a Discord token nobody's supplied yet (or the other way
round): /quality and /kill both land per-channel regardless of which bot delivered the message.

  uv run python -m channels.run
"""

from __future__ import annotations

import asyncio

import uvicorn

from channels import discord_bot, web
from channels.credentials import CredentialStore, wait_for_token
from channels.telegram import bot as telegram_bot

CREDENTIALS_PORT = telegram_bot.CREDENTIALS_PORT


async def _run_telegram(store: CredentialStore) -> None:
    token = await wait_for_token(store, "telegram", port=CREDENTIALS_PORT)
    application = telegram_bot.build_app(token, store)
    async with application:
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()  # runs until the process stops


async def _run_discord(store: CredentialStore) -> None:
    token = await wait_for_token(store, "discord", port=CREDENTIALS_PORT)
    client = discord_bot.FrankensteinClient(store)
    await client.start(token)


async def run_forever() -> None:
    store = CredentialStore()
    web.store = store
    server = uvicorn.Server(uvicorn.Config(web.app, host="0.0.0.0", port=CREDENTIALS_PORT, log_level="warning"))
    await asyncio.gather(server.serve(), _run_telegram(store), _run_discord(store))


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
