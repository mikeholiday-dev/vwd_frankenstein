"""Owner: D.

No real Telegram: Update/Message/Chat/User are small fakes with just the surface the
handlers use, so the handler logic (busy state, approval buttons, voice fallback,
message truncation) is tested without a live bot or network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from channels import telegram as _telegram_pkg  # noqa: F401  (ensures the package imports cleanly)
from channels.credentials import CredentialStore
from channels.runner import Progress
from channels.telegram import bot


class FakeMessage:
    def __init__(self, text: str = "", voice=None, document=None, caption: str = ""):
        self.text = text
        self.voice = voice
        self.document = document
        self.caption = caption
        self.sent: list[tuple] = []
        self.edits: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return FakeMessage(text=text)

    async def reply_voice(self, voice):
        self.sent.append(("<voice>", voice))

    async def edit_text(self, text):
        self.edits.append(text)


class FakeDocument:
    def __init__(self, file_id: str = "doc-1", file_name: str = "invoice.pdf", file_size: int = 10):
        self.file_id = file_id
        self.file_name = file_name
        self.file_size = file_size


class FakeTgFile:
    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class FakeBot:
    def __init__(self, data: bytes = b"%PDF-1.4 fake"):
        self._data = data

    async def get_file(self, file_id):
        return FakeTgFile(self._data)


class FakeQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answered = False
        self.edited: list[str] = []

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text):
        self.edited.append(text)


def make_update(text: str = "task", username: str = "alice", chat_id: int = 1, document=None, caption: str = ""):
    message = FakeMessage(text=text, document=document, caption=caption)
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(username=username, id=42),
        callback_query=None,
    )


def make_context(chat_data=None, store=None, args=None, bot=None):
    return SimpleNamespace(
        chat_data=chat_data if chat_data is not None else {}, bot_data={"store": store or CredentialStore()}, args=args or [], bot=bot or FakeBot()
    )


# ---- pure helpers: see tests/test_chat.py for quality_for/is_busy/approval_callback_data/parse_approval_callback -------


def test_truncate_uses_telegrams_own_default_limit():
    text = "x" * 4000
    out = bot.truncate(text)
    assert len(out) == bot.MAX_MESSAGE_CHARS
    assert bot.truncate("short") == "short"


# ---- /quality ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quality_command_sets_the_chat_tier():
    update = make_update()
    context = make_context(args=["full"])
    await bot.on_quality(update, context)
    assert context.chat_data["quality"] == "full"
    assert "full" in update.message.sent[0][0]


@pytest.mark.asyncio
async def test_quality_command_with_no_args_reports_current():
    update = make_update()
    context = make_context(chat_data={"quality": "full"})
    await bot.on_quality(update, context)
    assert context.chat_data["quality"] == "full"  # unchanged
    assert "full" in update.message.sent[0][0]


# ---- handle_task ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_task_streams_progress_then_sends_the_answer(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("gap", "building a thing")
        yield Progress("install", "installed x@v1")
        yield Progress("answer", "the final answer")
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update()
    context = make_context()

    await bot.handle_task(update, context, "do the thing")

    assert not bot.is_busy(context.chat_data)
    # the final reply (not the status edits) carries the answer text
    assert any(sent[0] == "the final answer" for sent in update.message.sent)


@pytest.mark.asyncio
async def test_handle_task_passes_the_stores_secrets_to_run_task(monkeypatch):
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        captured["secrets"] = secrets
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    store = CredentialStore()
    store.set("apify", "apify-tok")
    update = make_update()
    context = make_context(store=store)
    await bot.handle_task(update, context, "x")
    assert captured["secrets"] == {"APIFY_TOKEN": "apify-tok"}


@pytest.mark.asyncio
async def test_handle_task_refuses_a_second_task_while_busy():
    update = make_update()
    context = make_context(chat_data={"busy": True})
    await bot.handle_task(update, context, "another task")
    assert "wait" in update.message.sent[0][0].lower() or "kill" in update.message.sent[0][0].lower()


@pytest.mark.asyncio
async def test_handle_task_clears_busy_even_if_run_task_raises(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("gap", "building")
        raise RuntimeError("boom")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update()
    context = make_context()
    with pytest.raises(RuntimeError):
        await bot.handle_task(update, context, "x")
    assert not bot.is_busy(context.chat_data)


@pytest.mark.asyncio
async def test_handle_task_posts_an_approval_keyboard(monkeypatch):
    approval = {"id": "req-1", "ref": "x@v1", "manifest": {}, "previous": None, "permissions_diff": {}, "test_report": {}, "code": ""}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("approval_requested", "Needs your approval: x@v1", approval=approval)
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update()
    context = make_context()
    await bot.handle_task(update, context, "x")

    [(text, markup)] = [s for s in update.message.sent if s[1] is not None]
    assert "x@v1" in text
    buttons = markup.inline_keyboard[0]
    assert bot.parse_approval_callback(buttons[0].callback_data) == ("req-1", True)
    assert bot.parse_approval_callback(buttons[1].callback_data) == ("req-1", False)


@pytest.mark.asyncio
async def test_handle_task_skips_voice_reply_without_a_key(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("answer", "spoken answer")
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update()
    context = make_context()  # no elevenlabs key in the store
    await bot.handle_task(update, context, "x")
    assert not any(s[0] == "<voice>" for s in update.message.sent)


@pytest.mark.asyncio
async def test_handle_task_sends_voice_when_a_key_is_present(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("answer", "spoken answer")
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    monkeypatch.setattr(bot.voice, "speak", lambda text, **kw: b"audio-bytes")
    store = CredentialStore()
    store.set("elevenlabs", "a-key")
    update = make_update()
    context = make_context(store=store)
    await bot.handle_task(update, context, "x")
    assert any(s[0] == "<voice>" and s[1] == b"audio-bytes" for s in update.message.sent)


# ---- document upload ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_without_caption_is_buffered_not_run(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "WORK_DIR", tmp_path / "work")
    update = make_update(document=FakeDocument())
    context = make_context()
    await bot.on_document(update, context)

    assert context.chat_data["pending_attachments"][0].name == "invoice.pdf"
    assert "attached" in update.message.sent[0][0].lower()


@pytest.mark.asyncio
async def test_document_with_caption_runs_immediately_with_the_attachment(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "WORK_DIR", tmp_path / "work")
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        captured["task"] = task
        captured["attach"] = list(attach)
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update(document=FakeDocument(), caption="check this invoice")
    context = make_context()
    await bot.on_document(update, context)

    assert captured["task"] == "check this invoice"
    assert captured["attach"][0].name == "invoice.pdf"
    assert "pending_attachments" not in context.chat_data  # consumed by handle_task


@pytest.mark.asyncio
async def test_a_buffered_attachment_is_included_when_text_follows(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "WORK_DIR", tmp_path / "work")
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        captured["attach"] = list(attach)
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    context = make_context()
    await bot.on_document(make_update(document=FakeDocument()), context)
    await bot.on_text(make_update(text="check this invoice"), context)

    assert captured["attach"][0].name == "invoice.pdf"
    assert "pending_attachments" not in context.chat_data  # consumed, not left for the next task


@pytest.mark.asyncio
async def test_document_over_the_size_cap_is_rejected(monkeypatch):
    update = make_update(document=FakeDocument(file_size=bot.MAX_UPLOAD_BYTES + 1))
    context = make_context()
    await bot.on_document(update, context)

    assert "larger than" in update.message.sent[0][0]
    assert "pending_attachments" not in context.chat_data


@pytest.mark.asyncio
async def test_saved_attachment_writes_only_the_basename(monkeypatch, tmp_path):
    monkeypatch.setattr(bot.config, "WORK_DIR", tmp_path / "work")
    path = bot._save_attachment(1, "../../etc/passwd", b"x")
    assert path.name == "passwd"
    assert path.parent == tmp_path / "work" / "uploads" / "telegram-1"


# ---- approval callback --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_callback_decides_and_edits_the_message(monkeypatch):
    decided = {}
    monkeypatch.setattr(bot.runner, "decide_approval", lambda request_id, approved, by: decided.update(request_id=request_id, approved=approved, by=by))

    message = FakeMessage(text="Needs your approval: x@v1")
    query = FakeQuery(bot.approval_callback_data("req-1", True), message)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(username="alice", id=1))
    context = make_context()

    await bot.on_approval_callback(update, context)

    assert decided == {"request_id": "req-1", "approved": True, "by": "telegram:alice"}
    assert query.answered
    assert "Approved" in query.edited[0]


# ---- voice input --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_message_without_a_key_asks_for_one_and_does_not_transcribe(monkeypatch):
    called = []
    monkeypatch.setattr(bot.voice, "transcribe", lambda *a, **k: called.append(1))
    update = make_update()
    update.message.voice = SimpleNamespace()
    context = make_context()  # no elevenlabs key
    await bot.on_voice(update, context)
    assert not called
    assert "elevenlabs" in update.message.sent[0][0].lower() or "key" in update.message.sent[0][0].lower()


# ---- /start, /kill, plain text, voice with a key ------------------------------------------------


@pytest.mark.asyncio
async def test_start_command_greets_and_mentions_the_commands():
    update = make_update()
    await bot.on_start(update, make_context())
    text = update.message.sent[0][0]
    assert "/quality" in text and "/kill" in text


@pytest.mark.asyncio
async def test_kill_command_emits_a_kill_event(monkeypatch, tmp_path):
    from harness import config
    from harness.contracts import EventType

    monkeypatch.setattr(config, "LOG_PATH", tmp_path / "events.jsonl")
    update = make_update(username="alice")
    await bot.on_kill(update, make_context())
    assert "Kill sent" in update.message.sent[0][0]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == EventType.KILL.value
    assert events[-1]["data"]["by"] == "telegram:alice"


@pytest.mark.asyncio
async def test_plain_text_message_becomes_a_task(monkeypatch):
    seen = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        seen["task"] = task
        yield Progress("answer", "pong")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update(text="ping")
    await bot.on_text(update, make_context())
    assert seen["task"] == "ping"
    assert any(sent[0] == "pong" for sent in update.message.sent)


@pytest.mark.asyncio
async def test_voice_message_with_a_key_transcribes_and_runs_the_task(monkeypatch):
    seen = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        seen["task"] = task
        yield Progress("answer", "done")

    class FakeVoiceFile:
        async def download_as_bytearray(self):
            return bytearray(b"ogg")

    async def get_file():
        return FakeVoiceFile()

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    monkeypatch.setattr(bot.voice, "transcribe", lambda audio, **kw: "convert 10 eur")
    monkeypatch.setattr(bot.voice, "speak", lambda text, **kw: b"mp3")
    store = CredentialStore()
    store.set("elevenlabs", "el-key")
    update = make_update()
    update.message.voice = SimpleNamespace(get_file=get_file)
    await bot.on_voice(update, make_context(store=store))
    assert seen["task"] == "convert 10 eur"
    texts = [s[0] for s in update.message.sent]
    assert "Heard: convert 10 eur" in texts and "done" in texts and "<voice>" in texts


@pytest.mark.asyncio
async def test_voice_transcription_failure_is_reported_not_raised(monkeypatch):
    def boom(audio, **kw):
        raise RuntimeError("bad key")

    class FakeVoiceFile:
        async def download_as_bytearray(self):
            return bytearray(b"ogg")

    async def get_file():
        return FakeVoiceFile()

    monkeypatch.setattr(bot.voice, "transcribe", boom)
    store = CredentialStore()
    store.set("elevenlabs", "el-key")
    update = make_update()
    update.message.voice = SimpleNamespace(get_file=get_file)
    await bot.on_voice(update, make_context(store=store))
    assert "bad key" in update.message.sent[0][0]


# ---- routing through the real Application: raw Telegram JSON in, bot replies out ------------------


def _raw_update(update_id: int, **message_fields) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": 7, "type": "private", "first_name": "Al"},
            "from": {"id": 42, "is_bot": False, "first_name": "Al", "username": "alice"},
            **message_fields,
        },
    }


@pytest.fixture
def routed_app(monkeypatch):
    """build_app() with the Telegram network calls replaced by recorders, so a raw update goes
    through the same handler/filter routing the live bot uses."""
    from telegram import Message, User
    from telegram.ext import ExtBot

    sent: list[str] = []

    async def send_message(self, chat_id, text, *a, **k):
        sent.append(text)
        return Message(message_id=1000 + len(sent), date=None, chat=SimpleNamespace(id=chat_id), text=text)

    async def edit_message_text(self, text, *a, **k):
        sent.append(f"[edit] {text}")

    monkeypatch.setattr(ExtBot, "send_message", send_message)
    monkeypatch.setattr(ExtBot, "edit_message_text", edit_message_text)
    application = bot.build_app("123456:TEST-TOKEN", CredentialStore())
    application._initialized = True
    application.bot._bot_user = User(id=1, first_name="frank", is_bot=True, username="frank_bot")
    return application, sent


async def _feed(application, raw: dict) -> None:
    from telegram import Update

    await application.process_update(Update.de_json(raw, application.bot))


@pytest.mark.asyncio
async def test_routed_start_command_gets_a_reply(routed_app):
    application, sent = routed_app
    await _feed(application, _raw_update(1, text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
    assert sent and "Frankenstein" in sent[0]


@pytest.mark.asyncio
async def test_routed_text_message_runs_a_task_and_replies_with_the_answer(routed_app, monkeypatch):
    application, sent = routed_app
    seen = {}

    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        seen.update(task=task, session=session)
        yield Progress("answer", "hello from frank")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    await _feed(application, _raw_update(2, text="say hello"))
    assert seen["task"] == "say hello"
    assert seen["session"].startswith("telegram-7-")
    assert "hello from frank" in sent


@pytest.mark.asyncio
async def test_routed_slash_command_is_not_treated_as_a_task(routed_app, monkeypatch):
    application, sent = routed_app
    called = []

    async def fake_run_task(*a, **k):
        called.append(1)
        yield Progress("answer", "x")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    await _feed(application, _raw_update(3, text="/quality full", entities=[{"type": "bot_command", "offset": 0, "length": 8}]))
    assert not called
    assert any("full" in s for s in sent)


# ---- no "Starting…" placeholder; approvals tappable while a run is in flight -----------------------


@pytest.mark.asyncio
async def test_no_starting_message_and_first_reply_is_the_first_real_progress_line(monkeypatch):
    async def fake_run_task(task, *, session, models, secrets=None, attach=()):
        yield Progress("started", "Starting: do it")
        yield Progress("plan", "Plan: call a tool")
        yield Progress("answer", "result")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    update = make_update()
    await bot.handle_task(update, make_context(), "do it")
    texts = [s[0] for s in update.message.sent]
    assert texts == ["Plan: call a tool", "result"]
    assert not any("Starting" in t for t in texts)


def test_updates_are_processed_concurrently_so_approval_taps_are_not_queued_behind_a_running_task():
    application = bot.build_app("123456:TEST-TOKEN", CredentialStore())
    assert application.update_processor.max_concurrent_updates > 1


# ---- /voice per-chat settings ------------------------------------------------------------------


async def _voice_cmd(*args, chat_data=None):
    update, context = make_update(), make_context(chat_data=chat_data if chat_data is not None else {}, args=list(args))
    await bot.on_voice_setting(update, context)
    return update.message.sent[0][0], context.chat_data


@pytest.mark.asyncio
async def test_voice_command_with_no_args_shows_defaults_and_usage():
    reply, data = await _voice_cmd()
    assert "replies on" in reply and "language auto" in reply and "/voice" in reply
    assert data == {}


@pytest.mark.asyncio
async def test_voice_off_and_on_toggle_spoken_replies():
    _, data = await _voice_cmd("off")
    assert data["voice_reply"] is False
    _, data = await _voice_cmd("ON", chat_data=data)
    assert data["voice_reply"] is True


@pytest.mark.asyncio
async def test_voice_id_keeps_its_case_and_default_resets_it():
    _, data = await _voice_cmd("id", "AbC123xyz")
    assert data["voice_id"] == "AbC123xyz"
    _, data = await _voice_cmd("id", "default", chat_data=data)
    assert "voice_id" not in data


@pytest.mark.asyncio
async def test_voice_lang_accepts_a_code_and_auto_clears_it():
    _, data = await _voice_cmd("lang", "CS")
    assert data["voice_lang"] == "cs"
    _, data = await _voice_cmd("lang", "auto", chat_data=data)
    assert "voice_lang" not in data


@pytest.mark.asyncio
async def test_voice_lang_rejects_garbage():
    reply, data = await _voice_cmd("lang", "english!!")
    assert "voice_lang" not in data and "Usage" in reply


@pytest.mark.asyncio
async def test_voice_settings_are_per_chat():
    _, a = await _voice_cmd("off")
    _, b = await _voice_cmd("lang", "uk")
    assert "voice_lang" not in a and "voice_reply" not in b


@pytest.mark.asyncio
async def test_spoken_reply_uses_the_chats_voice_and_language(monkeypatch):
    seen = {}
    monkeypatch.setattr(bot.voice, "speak", lambda text, **kw: seen.update(kw) or b"mp3")
    store = CredentialStore()
    store.set("elevenlabs", "el-key")
    update = make_update()
    context = make_context(store=store, chat_data={"voice_id": "V1", "voice_lang": "cs"})
    await bot._reply_voice(update, context, "ahoj")
    assert seen == {"api_key": "el-key", "voice_id": "V1", "language_code": "cs"}


@pytest.mark.asyncio
async def test_no_spoken_reply_when_voice_is_off(monkeypatch):
    called = []
    monkeypatch.setattr(bot.voice, "speak", lambda *a, **k: called.append(1) or b"mp3")
    store = CredentialStore()
    store.set("elevenlabs", "el-key")
    update = make_update()
    await bot._reply_voice(update, make_context(store=store, chat_data={"voice_reply": False}), "ahoj")
    assert not called and not update.message.sent


@pytest.mark.asyncio
async def test_transcription_uses_the_chats_language(monkeypatch):
    seen = {}

    class FakeVoiceFile:
        async def download_as_bytearray(self):
            return bytearray(b"ogg")

    async def get_file():
        return FakeVoiceFile()

    async def fake_run_task(task, **k):
        yield Progress("finished", "ok")

    monkeypatch.setattr(bot.runner, "run_task", fake_run_task)
    monkeypatch.setattr(bot.voice, "transcribe", lambda audio, **kw: seen.update(kw) or "text")
    store = CredentialStore()
    store.set("elevenlabs", "el-key")
    update = make_update()
    update.message.voice = SimpleNamespace(get_file=get_file)
    await bot.on_voice(update, make_context(store=store, chat_data={"voice_lang": "uk"}))
    assert seen["language_code"] == "uk"


def test_voice_command_is_registered_on_the_app():
    from telegram.ext import CommandHandler

    application = bot.build_app("123456:TEST-TOKEN", CredentialStore())
    commands = {c for h in application.handlers[0] if isinstance(h, CommandHandler) for c in h.commands}
    assert "voice" in commands
