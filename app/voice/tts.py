"""Text-to-Speech provider hierarchy.

Concrete implementations:
    - SystemTTS      — offline TTS via pyttsx3
    - OpenAITTS      — OpenAI TTS API
    - ElevenLabsTTS  — ElevenLabs API

Factory helper ``get_tts_provider()`` selects the best available backend
based on environment variables and installed packages.
"""

from __future__ import annotations

import abc
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

from app.logger import logger


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class TTSProvider(abc.ABC):
    """Abstract text-to-speech provider.

    Subclasses must implement :meth:`synthesize` and may override
    :meth:`speak` for in-place audio playback.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""

    @abc.abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Convert *text* to audio bytes (WAV/MP3)."""

    async def speak(self, text: str) -> None:
        """Synthesize *text* and play the result through speakers.

        Default implementation writes to a temp file and calls the platform
        player.  Subclasses may override for streaming playback.
        """
        audio = await self.synthesize(text)
        if not audio:
            return
        # Write to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                # Try common cross-platform players
                "aplay", tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except FileNotFoundError:
            # Fallback to afplay (macOS) or start (Windows)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "afplay", tmp_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=30)
            except FileNotFoundError:
                logger.warning("[TTS] No audio player found (aplay/afplay). Audio saved to %s", tmp_path)
        except asyncio.TimeoutError:
            logger.warning("[TTS] Audio playback timed out")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# pyttsx3 — system TTS (offline)
# ---------------------------------------------------------------------------

class SystemTTS(TTSProvider):
    """Offline TTS using the system speech engine via pyttsx3.

    Works without network access but quality depends on OS voices.
    """

    @property
    def name(self) -> str:
        return "system (pyttsx3)"

    def __init__(
        self,
        rate: int = 200,
        volume: float = 1.0,
        voice_id: Optional[int] = None,
    ) -> None:
        self._rate = rate
        self._volume = volume
        self._voice_id = voice_id
        self._engine = None

    def _ensure_engine(self):
        """Lazy-init pyttsx3 engine (must run in same thread as speak)."""
        if self._engine is None:
            try:
                import pyttsx3
                self._engine = pyttsx3.init()
                self._engine.setProperty("rate", self._rate)
                self._engine.setProperty("volume", self._volume)
                if self._voice_id is not None:
                    voices = self._engine.getProperty("voices")
                    if self._voice_id < len(voices):
                        self._engine.setProperty("voice", voices[self._voice_id].id)
            except Exception as exc:
                logger.error("[SystemTTS] Failed to init pyttsx3: %s", exc)
                raise

    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to WAV bytes using pyttsx3 (runs in thread)."""
        def _run() -> bytes:
            self._ensure_engine()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            self._engine.save_to_file(text, tmp)
            self._engine.runAndWait()
            data = Path(tmp).read_bytes()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return data

        return await asyncio.to_thread(_run)

    async def speak(self, text: str) -> None:
        """Speak text directly via pyttsx3 engine (runs in thread)."""
        def _run() -> None:
            self._ensure_engine()
            self._engine.say(text)
            self._engine.runAndWait()

        await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# OpenAI TTS
# ---------------------------------------------------------------------------

class OpenAITTS(TTSProvider):
    """OpenAI TTS API (tts-1 / tts-1-hd).

    Requires ``OPENAI_API_KEY`` environment variable.
    """

    @property
    def name(self) -> str:
        return "openai"

    def __init__(
        self,
        model: str = "tts-1",
        voice: str = "alloy",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._base_url = base_url

    async def synthesize(self, text: str) -> bytes:
        """Call OpenAI TTS and return MP3 audio bytes."""
        if not self._api_key:
            raise RuntimeError("OpenAI TTS requires OPENAI_API_KEY")

        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for OpenAI TTS: pip install httpx")

        url = (self._base_url or "https://api.openai.com") + "/v1/audio/speech"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "input": text[:4096],
            "voice": self._voice,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.content


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

class ElevenLabsTTS(TTSProvider):
    """ElevenLabs TTS API.

    Requires ``ELEVENLABS_API_KEY`` environment variable.
    """

    @property
    def name(self) -> str:
        return "elevenlabs"

    def __init__(
        self,
        voice_id: str = "21m00Tcm4TlvDq8ikWAM",  # Rachel (default)
        model_id: str = "eleven_monolingual_v1",
        api_key: Optional[str] = None,
    ) -> None:
        self._voice_id = voice_id
        self._model_id = model_id
        self._api_key = api_key or os.getenv("ELEVENLABS_API_KEY", "")

    async def synthesize(self, text: str) -> bytes:
        """Call ElevenLabs TTS and return MP3 audio bytes."""
        if not self._api_key:
            raise RuntimeError("ElevenLabs TTS requires ELEVENLABS_API_KEY")

        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for ElevenLabs TTS: pip install httpx")

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text[:5000],
            "model_id": self._model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.content


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_tts_provider(preferred: Optional[str] = None) -> TTSProvider:
    """Return the best available TTS provider.

    Priority (overridden by *preferred*):
        1. "elevenlabs" — if ELEVENLABS_API_KEY is set
        2. "openai"     — if OPENAI_API_KEY is set
        3. "system"     — if pyttsx3 is installed
        4. NullTTS      — silent stub that logs output

    Parameters
    ----------
    preferred:
        Force a specific provider name ("elevenlabs", "openai", "system").
    """
    # Check for explicit preference
    if preferred:
        return _create_provider(preferred)

    # Auto-detect based on available keys
    if os.getenv("ELEVENLABS_API_KEY"):
        logger.info("[TTS] Using ElevenLabs provider")
        return _create_provider("elevenlabs")

    if os.getenv("OPENAI_API_KEY"):
        logger.info("[TTS] Using OpenAI provider")
        return _create_provider("openai")

    # Try system TTS
    try:
        import pyttsx3  # noqa: F401
        logger.info("[TTS] Using system pyttsx3 provider")
        return _create_provider("system")
    except ImportError:
        pass

    logger.warning("[TTS] No TTS provider available; using NullTTS (silent)")
    return NullTTS()


def _create_provider(name: str) -> TTSProvider:
    """Instantiate a provider by name, falling back to NullTTS on failure."""
    try:
        if name == "elevenlabs":
            return ElevenLabsTTS()
        if name == "openai":
            return OpenAITTS()
        if name == "system":
            return SystemTTS()
        raise ValueError(f"Unknown TTS provider: {name}")
    except Exception as exc:
        logger.error("[TTS] Failed to create %s provider: %s — falling back to NullTTS", name, exc)
        return NullTTS()


class NullTTS(TTSProvider):
    """Silent TTS stub — logs text instead of speaking."""

    @property
    def name(self) -> str:
        return "null (silent)"

    async def synthesize(self, text: str) -> bytes:
        logger.info("[NullTTS] Would speak: %s", text[:200])
        return b""
