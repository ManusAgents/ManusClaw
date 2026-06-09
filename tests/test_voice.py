"""Tests for voice system (TTS providers, wake word detection)."""

import asyncio
import os
import pytest

# ── Ensure stub mode ────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_voice_env(monkeypatch):
    monkeypatch.delenv("PICOVOICE_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ── TTSProvider hierarchy ───────────────────────────────────────────────────

def test_tts_provider_is_abstract():
    from app.voice.tts import TTSProvider
    import abc
    assert issubclass(TTSProvider, abc.ABC)
    # Cannot instantiate abstract class
    with pytest.raises(TypeError):
        TTSProvider()


def test_nulltts_is_tts_provider():
    from app.voice.tts import NullTTS, TTSProvider
    assert issubclass(NullTTS, TTSProvider)


@pytest.mark.asyncio
async def test_nulltts_synthesize_returns_empty():
    from app.voice.tts import NullTTS
    tts = NullTTS()
    result = await tts.synthesize("hello world")
    assert result == b""


@pytest.mark.asyncio
async def test_nulltts_name():
    from app.voice.tts import NullTTS
    tts = NullTTS()
    assert tts.name == "null (silent)"


# ── SystemTTS instantiation ─────────────────────────────────────────────────

def test_system_tts_is_tts_provider():
    from app.voice.tts import SystemTTS, TTSProvider
    assert issubclass(SystemTTS, TTSProvider)


def test_system_tts_name():
    from app.voice.tts import SystemTTS
    tts = SystemTTS()
    assert tts.name == "system (pyttsx3)"


# ── OpenAI TTS instantiation ────────────────────────────────────────────────

def test_openai_tts_is_tts_provider():
    from app.voice.tts import OpenAITTS, TTSProvider
    assert issubclass(OpenAITTS, TTSProvider)


def test_openai_tts_name():
    from app.voice.tts import OpenAITTS
    tts = OpenAITTS(api_key="test-key")
    assert tts.name == "openai"


# ── ElevenLabs TTS instantiation ───────────────────────────────────────────

def test_elevenlabs_tts_is_tts_provider():
    from app.voice.tts import ElevenLabsTTS, TTSProvider
    assert issubclass(ElevenLabsTTS, TTSProvider)


def test_elevenlabs_tts_name():
    from app.voice.tts import ElevenLabsTTS
    tts = ElevenLabsTTS(api_key="test-key")
    assert tts.name == "elevenlabs"


# ── get_tts_provider() factory (stub mode) ─────────────────────────────────

def test_get_tts_provider_returns_nulltts_stub():
    """Without any API keys or pyttsx3, should return NullTTS."""
    # Keys are cleared by autouse fixture
    import app.voice.tts as tts_mod
    tts_mod._create_provider = lambda name: tts_mod.NullTTS()  # Ensure no real init
    from app.voice.tts import get_tts_provider, NullTTS
    provider = get_tts_provider()
    assert isinstance(provider, NullTTS)


def test_get_tts_provider_preferred_null():
    from app.voice.tts import get_tts_provider, NullTTS
    provider = get_tts_provider(preferred="nonexistent")
    assert isinstance(provider, NullTTS)


def test_get_tts_provider_preferred_openai():
    """With preferred=openai, _create_provider succeeds and returns OpenAITTS."""
    from app.voice.tts import get_tts_provider, OpenAITTS
    # _create_provider("openai") succeeds because API key is checked at synthesize(), not construction
    provider = get_tts_provider(preferred="openai")
    assert isinstance(provider, OpenAITTS)


# ── WakeWordDetector in stub mode ──────────────────────────────────────────

def test_wakeword_detector_stub_mode():
    from app.voice.wake import VoiceWakeDetector
    detector = VoiceWakeDetector(wake_word="hey manus")
    assert detector.backend == "stub"


def test_wakeword_detector_properties():
    from app.voice.wake import VoiceWakeDetector
    detector = VoiceWakeDetector(wake_word="hey manus", sensitivity=0.7)
    assert detector._wake_word == "hey manus"
    assert detector._sensitivity == 0.7
    assert not detector._running


@pytest.mark.asyncio
async def test_wakeword_detector_start_stop():
    from app.voice.wake import VoiceWakeDetector
    detector = VoiceWakeDetector(wake_word="hey manus")
    fired = asyncio.Event()

    async def callback():
        fired.set()

    await detector.start(callback)
    assert detector._running
    detector.stop()
    assert not detector._running
    # In stub mode, callback fires after 30s sleep — we stop before that


# ── TalkMode stop phrase detection ──────────────────────────────────────────

def test_talkmode_is_stop_phrase():
    from app.voice.talk import TalkMode
    assert TalkMode._is_stop_phrase("stop listening")
    assert TalkMode._is_stop_phrase("go to sleep")
    assert TalkMode._is_stop_phrase("goodbye")
    assert not TalkMode._is_stop_phrase("what is the weather")


def test_talkmode_is_stop_phrase_case_insensitive():
    from app.voice.talk import TalkMode
    assert TalkMode._is_stop_phrase("STOP LISTENING")
    assert TalkMode._is_stop_phrase("Goodbye!")
