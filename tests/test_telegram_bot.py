"""Owner: D.

No real Telegram: Update/Message/Chat/User are small fakes with just the surface the
handlers use, so the handler logic (busy state, approval buttons, voice fallback,
message truncation) is tested without a live bot or network.
"""

from __future__ import annotations

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
    monkeypatch.setattr(bot.voice, "speak", lambda text, *, api_key: b"audio-bytes")
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
