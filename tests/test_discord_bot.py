"""Owner: D.

No real Discord gateway: Message/Attachment/Interaction are small fakes with just the surface
the handlers use (discord.Client, discord.ui.View/Button themselves are real — they need no
network to construct), so the handler logic is tested without connecting to Discord.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from channels.credentials import CredentialStore
from channels.discord_bot import ApprovalView, FrankensteinClient, mentions_me, strip_mention, truncate
from channels.runner import Progress

ME = SimpleNamespace(id=999)


class FakeAttachment:
    def __init__(self, audio: bytes = b"audio", voice: bool = True):
        self._audio = audio
        self._voice = voice

    def is_voice_message(self) -> bool:
        return self._voice

    async def read(self) -> bytes:
        return self._audio


class FakeMessage:
    def __init__(self, content: str = "task", *, author_bot: bool = False, guild=None, mentions=(), attachments=()):
        self.content = content
        self.author = SimpleNamespace(bot=author_bot, name="alice")
        self.guild = guild
        self.mentions = list(mentions)
        self.attachments = list(attachments)
        self.channel = SimpleNamespace(id=1)
        self.id = 42
        self.sent: list[tuple] = []
        self.edits: list[str] = []

    async def reply(self, content=None, file=None, view=None):
        self.sent.append((content, file, view))
        return FakeMessage(content=content or "")

    async def edit(self, content=None):
        self.edits.append(content)


class FakeResponse:
    def __init__(self):
        self.edited: list[tuple] = []

    async def edit_message(self, content=None, view=None):
        self.edited.append((content, view))


class FakeInteraction:
    def __init__(self, message: FakeMessage, username: str = "bob"):
        self.message = message
        self.user = SimpleNamespace(name=username)
        self.response = FakeResponse()


def make_client(store=None) -> FrankensteinClient:
    return FrankensteinClient(store or CredentialStore())


# ---- pure helpers ---------------------------------------------------------------------------


def test_mentions_me():
    assert mentions_me(FakeMessage(mentions=[ME]), ME)
    assert not mentions_me(FakeMessage(mentions=[]), ME)
    assert not mentions_me(FakeMessage(mentions=[ME]), None)


def test_strip_mention_handles_both_mention_forms():
    assert strip_mention("<@999> do the thing", ME) == "do the thing"
    assert strip_mention("<@!999> do the thing", ME) == "do the thing"


def test_truncate_uses_discords_own_default_limit():
    out = truncate("x" * 3000)
    assert len(out) == 1900


# ---- on_message routing ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ignores_messages_from_bots():
    client = make_client()
    message = FakeMessage(author_bot=True)
    await client.on_message(message)
    assert message.sent == []


@pytest.mark.asyncio
async def test_ignores_a_guild_message_that_does_not_mention_it(monkeypatch):
    client = make_client()
    called = []
    monkeypatch.setattr(client, "_handle_task", lambda *a: called.append(1))
    message = FakeMessage(content="hello", guild=SimpleNamespace(), mentions=[])
    await client.on_message(message)
    assert not called


@pytest.mark.asyncio
async def test_dm_without_a_mention_is_still_handled(monkeypatch):
    client = make_client()
    captured = {}

    async def fake_handle_task(message, task):
        captured["task"] = task

    monkeypatch.setattr(client, "_handle_task", fake_handle_task)
    message = FakeMessage(content="a dm task", guild=None)
    await client.on_message(message)
    assert captured["task"] == "a dm task"


@pytest.mark.asyncio
async def test_a_mention_in_a_guild_channel_is_handled_and_stripped(monkeypatch):
    import channels.discord_bot as mod

    monkeypatch.setattr(mod.FrankensteinClient, "user", ME, raising=False)
    client = make_client()
    captured = {}

    async def fake_handle_task(message, task):
        captured["task"] = task

    monkeypatch.setattr(client, "_handle_task", fake_handle_task)
    message = FakeMessage(content="<@999> look this up", guild=SimpleNamespace(), mentions=[ME])
    await client.on_message(message)
    assert captured["task"] == "look this up"


@pytest.mark.asyncio
async def test_quality_command():
    client = make_client()
    message = FakeMessage(content="/quality full", guild=None)
    await client.on_message(message)
    assert client._state(1)["quality"] == "full"
    assert "full" in message.sent[0][0]


@pytest.mark.asyncio
async def test_quality_command_with_no_args_reports_current():
    client = make_client()
    client._state(1)["quality"] = "full"
    message = FakeMessage(content="/quality", guild=None)
    await client.on_message(message)
    assert "full" in message.sent[0][0]


# ---- _handle_task -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_task_sends_the_final_answer(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None):
        yield Progress("gap", "building")
        yield Progress("answer", "the final answer")
        yield Progress("finished", "ok")

    import channels.discord_bot as mod

    monkeypatch.setattr(mod.runner, "run_task", fake_run_task)
    client = make_client()
    message = FakeMessage(content="do it", guild=None)
    await client._handle_task(message, "do it")
    assert not client._state(1).get("busy")
    assert any(s[0] == "the final answer" for s in message.sent)


@pytest.mark.asyncio
async def test_handle_task_refuses_a_second_task_while_busy():
    client = make_client()
    client._state(1)["busy"] = True
    message = FakeMessage(content="x", guild=None)
    await client._handle_task(message, "x")
    assert "wait" in message.sent[0][0].lower() or "kill" in message.sent[0][0].lower()


@pytest.mark.asyncio
async def test_handle_task_posts_an_approval_view(monkeypatch):
    approval = {"id": "req-1", "ref": "x@v1", "manifest": {}, "previous": None, "permissions_diff": {}, "test_report": {}, "code": ""}

    async def fake_run_task(task, *, session, models, secrets=None):
        yield Progress("approval_requested", "Needs your approval: x@v1", approval=approval)
        yield Progress("finished", "ok")

    import channels.discord_bot as mod

    monkeypatch.setattr(mod.runner, "run_task", fake_run_task)
    client = make_client()
    message = FakeMessage(content="x", guild=None)
    await client._handle_task(message, "x")

    [(content, _file, view)] = [s for s in message.sent if s[2] is not None]
    assert "x@v1" in content
    assert isinstance(view, ApprovalView)


@pytest.mark.asyncio
async def test_handle_task_sends_voice_when_a_key_is_present(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None):
        yield Progress("answer", "spoken answer")
        yield Progress("finished", "ok")

    import channels.discord_bot as mod

    monkeypatch.setattr(mod.runner, "run_task", fake_run_task)
    monkeypatch.setattr(mod.voice, "speak", lambda text, *, api_key: b"audio-bytes")
    store = CredentialStore()
    store.set("elevenlabs", "a-key")
    client = make_client(store)
    message = FakeMessage(content="x", guild=None)
    await client._handle_task(message, "x")
    assert any(s[1] is not None for s in message.sent)  # a file= attachment went out


@pytest.mark.asyncio
async def test_handle_task_passes_the_stores_secrets(monkeypatch):
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None):
        captured["secrets"] = secrets
        yield Progress("finished", "ok")

    import channels.discord_bot as mod

    monkeypatch.setattr(mod.runner, "run_task", fake_run_task)
    store = CredentialStore()
    store.set("apify", "apify-tok")
    client = make_client(store)
    message = FakeMessage(content="x", guild=None)
    await client._handle_task(message, "x")
    assert captured["secrets"] == {"APIFY_TOKEN": "apify-tok"}


# ---- voice in -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_message_without_a_key_asks_for_one(monkeypatch):
    import channels.discord_bot as mod

    called = []
    monkeypatch.setattr(mod.voice, "transcribe", lambda *a, **k: called.append(1))
    client = make_client()
    message = FakeMessage(content="", guild=None, attachments=[FakeAttachment()])
    await client.on_message(message)
    assert not called
    assert "elevenlabs" in message.sent[0][0].lower() or "key" in message.sent[0][0].lower()


@pytest.mark.asyncio
async def test_voice_message_with_a_key_transcribes_then_runs_the_task(monkeypatch):
    import channels.discord_bot as mod

    monkeypatch.setattr(mod.voice, "transcribe", lambda audio, *, api_key: "transcribed task")

    async def fake_run_task(task, *, session, models, secrets=None):
        captured_tasks.append(task)
        yield Progress("finished", "ok")

    captured_tasks: list[str] = []
    monkeypatch.setattr(mod.runner, "run_task", fake_run_task)
    store = CredentialStore()
    store.set("elevenlabs", "a-key")
    client = make_client(store)
    message = FakeMessage(content="", guild=None, attachments=[FakeAttachment()])
    await client.on_message(message)
    assert captured_tasks == ["transcribed task"]


# ---- approval button ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_button_decides_and_edits_the_message(monkeypatch):
    import channels.discord_bot as mod

    decided = {}
    monkeypatch.setattr(mod.runner, "decide_approval", lambda request_id, approved, by: decided.update(request_id=request_id, approved=approved, by=by))

    message = FakeMessage(content="Needs your approval: x@v1")
    interaction = FakeInteraction(message, username="bob")
    view = ApprovalView("req-1")
    approve_button = next(b for b in view.children if b.label == "Approve")
    await approve_button.callback(interaction)

    assert decided == {"request_id": "req-1", "approved": True, "by": "discord:bob"}
    assert interaction.response.edited
    content, view_after = interaction.response.edited[0]
    assert "Approved" in content
    assert view_after is None


def test_approval_view_has_distinct_custom_ids():
    view = ApprovalView("req-1")
    ids = {b.custom_id for b in view.children}
    assert ids == {"approval:1:req-1", "approval:0:req-1"}
