"""ElevenLabs voice in and out. Owner: D.

The SDK is a blocking HTTP client, so every function here is plain sync; bot.py
runs these through asyncio.to_thread so they don't block the event loop. A key
never reaches this module except as an argument — callers pull it from the
CredentialStore per call, nothing here holds or caches one.
"""

from __future__ import annotations

from elevenlabs.client import ElevenLabs

STT_MODEL = "scribe_v1"
TTS_MODEL = "eleven_turbo_v2_5"  # low-latency model: the reply is spoken once, not re-listened to
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs' standard "Rachel" voice; override via speak(voice_id=...)


def transcribe(audio: bytes, *, api_key: str, filename: str = "voice.ogg", language_code: str | None = None) -> str:
    """Speech to text for one incoming Telegram voice message. No language code: the model detects it."""
    client = ElevenLabs(api_key=api_key)
    extra = {"language_code": language_code} if language_code else {}
    result = client.speech_to_text.convert(model_id=STT_MODEL, file=(filename, audio), **extra)
    return result.text


def speak(text: str, *, api_key: str, voice_id: str = DEFAULT_VOICE_ID, language_code: str | None = None) -> bytes:
    """Text to speech for one reply. Uses the streaming endpoint for a faster time-to-first-byte
    even though the result is buffered whole here — Telegram's API needs a complete file to send
    a voice note, there's no such thing as a partial upload it can start playing."""
    client = ElevenLabs(api_key=api_key)
    extra = {"language_code": language_code} if language_code else {}
    return b"".join(client.text_to_speech.stream(voice_id, text=text, model_id=TTS_MODEL, output_format="mp3_44100_128", **extra))
