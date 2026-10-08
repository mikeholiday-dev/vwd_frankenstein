"""Owner: D. No real ElevenLabs key: the client is stubbed, not called over the network."""

from __future__ import annotations

from types import SimpleNamespace

from channels import voice


class FakeSTT:
    def __init__(self):
        self.calls = []

    def convert(self, *, model_id, file):
        self.calls.append((model_id, file))
        return SimpleNamespace(text="hello from the stub")


class FakeTTS:
    def __init__(self):
        self.calls = []

    def stream(self, voice_id, *, text, model_id, output_format):
        self.calls.append((voice_id, text, model_id, output_format))
        yield b"chunk1"
        yield b"chunk2"


class FakeElevenLabs:
    def __init__(self, api_key):
        self.api_key = api_key
        self.speech_to_text = FakeSTT()
        self.text_to_speech = FakeTTS()


def test_transcribe_returns_the_text(monkeypatch):
    monkeypatch.setattr(voice, "ElevenLabs", FakeElevenLabs)
    text = voice.transcribe(b"audio bytes", api_key="k", filename="msg.ogg")
    assert text == "hello from the stub"


def test_transcribe_sends_the_filename_and_bytes(monkeypatch):
    monkeypatch.setattr(voice, "ElevenLabs", FakeElevenLabs)
    client_holder = {}
    real_init = FakeElevenLabs.__init__

    def capture_init(self, api_key):
        real_init(self, api_key)
        client_holder["client"] = self

    monkeypatch.setattr(FakeElevenLabs, "__init__", capture_init)
    voice.transcribe(b"audio bytes", api_key="k", filename="msg.ogg")
    assert client_holder["client"].speech_to_text.calls == [(voice.STT_MODEL, ("msg.ogg", b"audio bytes"))]


def test_speak_joins_the_streamed_chunks(monkeypatch):
    monkeypatch.setattr(voice, "ElevenLabs", FakeElevenLabs)
    audio = voice.speak("say this", api_key="k")
    assert audio == b"chunk1chunk2"


def test_speak_uses_the_given_voice_id(monkeypatch):
    monkeypatch.setattr(voice, "ElevenLabs", FakeElevenLabs)
    client_holder = {}
    real_init = FakeElevenLabs.__init__

    def capture_init(self, api_key):
        real_init(self, api_key)
        client_holder["client"] = self

    monkeypatch.setattr(FakeElevenLabs, "__init__", capture_init)
    voice.speak("say this", api_key="k", voice_id="custom-voice")
    assert client_holder["client"].text_to_speech.calls[0][0] == "custom-voice"
