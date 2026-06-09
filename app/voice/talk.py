"""Continuous talk mode — wake-word triggered voice conversation.

After the wake word fires, :class:`TalkMode` enters a loop that:

    1. Records audio from the microphone
    2. Transcribes via STT (Google or Whisper)
    3. Sends the text to the agent for processing
    4. Plays the agent's response via TTS

The loop exits on:
    - An explicit stop command ("stop listening", "go to sleep")
    - :meth:`stop` being called
    - Network / audio device errors
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import TYPE_CHECKING, Optional

from app.logger import logger
from app.voice.tts import TTSProvider, get_tts_provider

if TYPE_CHECKING:
    from app.agent.base import BaseAgent

# Stop phrases that end the talk loop
_STOP_PHRASES = frozenset({
    "stop listening",
    "go to sleep",
    "stop talking",
    "end conversation",
    "goodbye",
    "bye bye",
})


class TalkMode:
    """Continuous voice conversation mode.

    Parameters
    ----------
    stt_engine:
        "google" (default, speech_recognition), "whisper" (openai-whisper),
        or "stub" (text input via stdin).
    language:
        BCP-47 language code for STT.
    silence_threshold:
        Energy threshold for silence detection (0.0 - 1.0).
    tts_provider:
        Pre-built TTS provider instance.  If ``None``, one is created
        via :func:`get_tts_provider`.
    wake_word:
        Phrase that ended the wake-word detection and started this mode.
    """

    def __init__(
        self,
        stt_engine: str = "google",
        language: str = "en-US",
        silence_threshold: float = 0.3,
        tts_provider: Optional[TTSProvider] = None,
        wake_word: str = "hey manus",
    ) -> None:
        self._stt_engine = stt_engine
        self._language = language
        self._silence_threshold = silence_threshold
        self._wake_word = wake_word
        self._tts: TTSProvider = tts_provider or get_tts_provider()

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Lazy-loaded STT resources
        self._recognizer: Any = None
        self._microphone: Any = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, agent: BaseAgent) -> None:
        """Start the talk loop with the given agent.

        Records → transcribes → sends to agent → plays TTS response.

        Parameters
        ----------
        agent:
            Any agent instance with an async ``run(prompt)`` method that
            returns a string response.
        """
        if self._running:
            logger.warning("[TalkMode] Already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._talk_loop(agent))
        logger.info(
            "[TalkMode] Conversation started (stt=%s, tts=%s, lang=%s)",
            self._stt_engine,
            self._tts.name,
            self._language,
        )

    def stop(self) -> None:
        """Stop the talk loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[TalkMode] Conversation stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _talk_loop(self, agent: BaseAgent) -> None:
        """Core conversation loop: listen → transcribe → respond."""
        self._init_stt()

        try:
            while self._running:
                # 1. Listen for user speech
                text = await self._listen()
                if text is None:
                    # Audio read timeout or error, retry
                    continue

                text = text.strip()
                if not text:
                    continue

                logger.info("[TalkMode] User said: '%s'", text)

                # 2. Check for stop command
                if self._is_stop_phrase(text):
                    logger.info("[TalkMode] Stop phrase detected, ending conversation")
                    # Play a farewell
                    await self._tts.speak("Going back to sleep. Say the wake word to continue.")
                    break

                # 3. Send to agent
                try:
                    response = await agent.run(text)
                except Exception as exc:
                    logger.error("[TalkMode] Agent error: %s", exc)
                    response = "Sorry, I encountered an error processing your request."

                # 4. Speak the response
                if response:
                    logger.info("[TalkMode] Agent response: '%s'", response[:200])
                    await self._tts.speak(response)

                # Brief pause before next listen
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[TalkMode] Talk loop error: %s", exc)
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # STT initialisation & recording
    # ------------------------------------------------------------------

    def _init_stt(self) -> None:
        """Initialise the speech-to-text engine."""
        if self._stt_engine == "stub":
            logger.info("[TalkMode] Using stub STT (text via stdin)")
            return

        try:
            import speech_recognition as sr  # type: ignore[import-untyped]
            self._recognizer = sr.Recognizer()
            self._recognizer.energy_threshold = int(self._silence_threshold * 4000)
            self._recognizer.dynamic_energy_threshold = True
            self._recognizer.pause_threshold = 0.8
            self._microphone = sr.Microphone()
            logger.info("[TalkMode] Speech recognition initialized (engine=%s)", self._stt_engine)
        except ImportError:
            logger.warning("[TalkMode] speech_recognition not installed; falling back to stub")
            self._stt_engine = "stub"

    async def _listen(self) -> Optional[str]:
        """Record audio and return transcribed text.

        Returns ``None`` on timeout or audio error (caller should retry).
        """
        if self._stt_engine == "stub":
            return await self._stub_listen()

        if self._stt_engine == "whisper":
            return await self._whisper_listen()

        return await self._google_listen()

    async def _google_listen(self) -> Optional[str]:
        """Listen and transcribe using Google STT via speech_recognition."""
        try:
            import speech_recognition as sr  # type: ignore[import-untyped]

            with self._microphone as source:
                # Adjust for ambient noise on first call
                self._recognizer.adjust_for_ambient_noise(source, duration=0.3)
                audio = await asyncio.to_thread(
                    self._recognizer.listen,
                    source,
                    timeout=5.0,
                    phrase_time_limit=15.0,
                )

            text = await asyncio.to_thread(
                self._recognizer.recognize_google,
                audio,
                language=self._language,
            )
            return text

        except sr.WaitTimeoutError:
            logger.debug("[TalkMode] Listen timeout (no speech)")
            return None
        except sr.UnknownValueError:
            logger.debug("[TalkMode] Could not understand audio")
            return None
        except sr.RequestError as exc:
            logger.error("[TalkMode] Google STT request failed: %s", exc)
            return None
        except Exception as exc:
            logger.error("[TalkMode] Google listen error: %s", exc)
            return None

    async def _whisper_listen(self) -> Optional[str]:
        """Listen and transcribe using OpenAI Whisper (local)."""
        import wave

        try:
            import speech_recognition as sr  # type: ignore[import-untyped]

            with self._microphone as source:
                audio = await asyncio.to_thread(
                    self._recognizer.listen,
                    source,
                    timeout=5.0,
                    phrase_time_limit=15.0,
                )

            # Save audio to temp WAV file for Whisper
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name
                f.write(audio.get_wav_data())

            try:
                # Run Whisper in a thread (CPU-bound)
                def _transcribe() -> Optional[str]:
                    import whisper  # type: ignore[import-untyped]
                    model = whisper.load_model("base")
                    result = model.transcribe(tmp_path, language=self._language.split("-")[0])
                    return result.get("text")

                text = await asyncio.to_thread(_transcribe)
                return text.strip() if text else None
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        except sr.WaitTimeoutError:
            return None
        except sr.UnknownValueError:
            return None
        except ImportError as exc:
            logger.error("[TalkMode] Whisper not installed: %s", exc)
            self._stt_engine = "stub"
            return None
        except Exception as exc:
            logger.error("[TalkMode] Whisper listen error: %s", exc)
            return None

    async def _stub_listen(self) -> Optional[str]:
        """Stub mode — read text from stdin for testing without audio."""
        logger.info("[TalkMode] [STUB] Waiting for text input (type 'stop listening' to end)...")
        try:
            text = await asyncio.to_thread(input, "You: ")
            return text.strip() if text.strip() else None
        except (EOFError, KeyboardInterrupt):
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stop_phrase(text: str) -> bool:
        """Check if the text contains a stop command."""
        normalized = text.lower().strip()
        return any(phrase in normalized for phrase in _STOP_PHRASES)
