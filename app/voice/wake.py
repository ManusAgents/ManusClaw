"""Wake-word detection for voice-activated agent control.

Supports two backends:
    1. **pvporcupine** — low-latency, hardware-accelerated wake-word engine
       (requires PICOVOICE_API_KEY).
    2. **Keyword fallback** — uses ``speech_recognition`` to detect the wake
       phrase in short audio snippets.

When pvporcupine is not installed, the detector enters **stub mode** and
logs what *would* have been detected.
"""

from __future__ import annotations

import asyncio
import os
import struct
from typing import Any, Callable, Coroutine, Optional

from app.logger import logger

# Type alias for the user-supplied callback
WakeCallback = Callable[[], Coroutine[Any, Any, None]]


class VoiceWakeDetector:
    """Async wake-word listener.

    Usage::

        detector = VoiceWakeDetector(wake_word="hey manus")
        await detector.start(lambda: print("WAKE!"))
        ...
        detector.stop()

    The detector runs a background asyncio task that continuously reads audio
    from the default microphone and checks for the configured wake phrase.
    """

    def __init__(
        self,
        wake_word: str = "hey manus",
        sensitivity: float = 0.5,
        audio_device_index: Optional[int] = None,
    ) -> None:
        """Initialise the wake-word detector.

        Parameters
        ----------
        wake_word:
            Phrase to listen for.  With pvporcupine this maps to a built-in
            keyword; with the fallback it is matched as a substring in STT.
        sensitivity:
            Detection sensitivity 0.0 (least) to 1.0 (most).
        audio_device_index:
            Microphone device index (None = system default).
        """
        self._wake_word = wake_word.lower().strip()
        self._sensitivity = sensitivity
        self._device_index = audio_device_index
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callback: Optional[WakeCallback] = None

        # Backend selection
        self._backend: str = "stub"
        self._porcupine: Any = None
        self._pa_stream: Any = None
        self._recognizer: Any = None
        self._microphone: Any = None

        self._init_backend()

    # ------------------------------------------------------------------
    # Backend initialisation
    # ------------------------------------------------------------------

    def _init_backend(self) -> None:
        """Try pvporcupine first, fall back to speech_recognition, then stub."""
        # 1) pvporcupine
        picovoice_key = os.getenv("PICOVOICE_API_KEY", "")
        if picovoice_key:
            try:
                import pvporcupine  # type: ignore[import-untyped]
                self._porcupine = pvporcupine.create(
                    access_key=picovoice_key,
                    keywords=[pvporcupine.KEYWORD_PORCUPINE],
                    sensitivities=[self._sensitivity],
                )
                self._backend = "porcupine"
                logger.info("[WakeWord] Using pvporcupine backend (device=%s)", self._device_index)
                return
            except (ImportError, Exception) as exc:
                logger.warning("[WakeWord] pvporcupine unavailable: %s — trying fallback", exc)

        # 2) speech_recognition keyword fallback
        try:
            import speech_recognition as sr  # type: ignore[import-untyped]
            self._recognizer = sr.Recognizer()
            self._recognizer.energy_threshold = 300
            self._recognizer.dynamic_energy_threshold = True
            self._backend = "keyword"
            logger.info("[WakeWord] Using speech_recognition keyword fallback")
            return
        except ImportError:
            pass

        # 3) Stub
        self._backend = "stub"
        logger.warning(
            "[WakeWord] No wake-word backend available. "
            "Install pvporcupine (PICOVOICE_API_KEY) or speech_recognition. "
            "Running in stub mode (logs detections)."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, callback: WakeCallback) -> None:
        """Start the wake-word detection loop.

        Parameters
        ----------
        callback:
            Async coroutine called whenever the wake word is detected.
            The detector continues listening after the callback returns.
        """
        if self._running:
            logger.warning("[WakeWord] Already running")
            return

        self._callback = callback
        self._running = True

        if self._backend == "porcupine":
            self._task = asyncio.create_task(self._porcupine_loop())
        elif self._backend == "keyword":
            self._task = asyncio.create_task(self._keyword_loop())
        else:
            self._task = asyncio.create_task(self._stub_loop())

        logger.info("[WakeWord] Detection started (backend=%s, word='%s')", self._backend, self._wake_word)

    def stop(self) -> None:
        """Stop the detection loop and release resources."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

        if self._porcupine is not None:
            try:
                self._porcupine.delete()
            except Exception:
                pass
            self._porcupine = None

        if self._pa_stream is not None:
            try:
                self._pa_stream.stop()
                self._pa_stream.close()
            except Exception:
                pass
            self._pa_stream = None

        logger.info("[WakeWord] Detection stopped")

    @property
    def backend(self) -> str:
        """Name of the active backend (porcupine | keyword | stub)."""
        return self._backend

    # ------------------------------------------------------------------
    # Detection loops
    # ------------------------------------------------------------------

    async def _porcupine_loop(self) -> None:
        """Continuous audio capture with pvporcupine frame processing."""
        import pyaudio  # type: ignore[import-untyped]

        pa = pyaudio.PyAudio()
        sample_rate = self._porcupine.sample_rate
        frame_length = self._porcupine.frame_length
        channels = 1
        sample_width = 2  # int16

        try:
            self._pa_stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                input=True,
                input_device_index=self._device_index,
                frames_per_buffer=frame_length,
            )
            self._pa_stream.start_stream()

            logger.info("[WakeWord] Porcupine listening (rate=%d, frames=%d)", sample_rate, frame_length)

            while self._running:
                # Read a frame of audio
                pcm = await asyncio.to_thread(
                    self._pa_stream.read,
                    frame_length,
                    exception_on_overflow=False,
                )
                audio_data = struct.unpack_from(
                    f"<{frame_length}h",
                    bytes(pcm),
                )

                # Check for keyword
                result = self._porcupine.process(audio_data)
                if result >= 0:
                    logger.info("[WakeWord] Porcupine detected keyword (index=%d)", result)
                    if self._callback:
                        try:
                            await self._callback()
                        except Exception as exc:
                            logger.error("[WakeWord] Callback error: %s", exc)

                # Yield to event loop
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[WakeWord] Porcupine loop error: %s", exc)
        finally:
            try:
                if self._pa_stream:
                    self._pa_stream.stop_stream()
                    self._pa_stream.close()
            except Exception:
                pass
            pa.terminate()

    async def _keyword_loop(self) -> None:
        """Speech recognition based wake-word detection loop.

        Records short audio snippets, runs STT, and checks if the wake word
        appears in the transcribed text.
        """
        import speech_recognition as sr  # type: ignore[import-untyped]

        mic = sr.Microphone(device_index=self._device_index)

        try:
            with mic as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
                logger.info("[WakeWord] Keyword fallback listening...")

                while self._running:
                    try:
                        # Listen for a short phrase
                        audio = await asyncio.to_thread(
                            self._recognizer.listen,
                            source,
                            timeout=2.0,
                            phrase_time_limit=3.0,
                        )
                        # Recognize using Google STT
                        text = await asyncio.to_thread(
                            self._recognizer.recognize_google,
                            audio,
                        ).lower()

                        logger.debug("[WakeWord] Heard: '%s'", text)

                        if self._wake_word in text:
                            logger.info("[WakeWord] Wake word detected: '%s'", text)
                            if self._callback:
                                try:
                                    await self._callback()
                                except Exception as exc:
                                    logger.error("[WakeWord] Callback error: %s", exc)

                    except sr.WaitTimeoutError:
                        continue
                    except sr.UnknownValueError:
                        continue
                    except sr.RequestError as exc:
                        logger.warning("[WakeWord] STT request error: %s", exc)
                        await asyncio.sleep(1)
                    except asyncio.CancelledError:
                        break
                    except Exception as exc:
                        logger.error("[WakeWord] Keyword loop error: %s", exc)
                        await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[WakeWord] Keyword loop error: %s", exc)

    async def _stub_loop(self) -> None:
        """Stub loop — simulates a detection every 30s for testing.

        Logs what would happen without any audio processing.
        """
        logger.info("[WakeWord] Stub mode active — simulating detection every 30s")
        counter = 0
        try:
            while self._running:
                await asyncio.sleep(30)
                counter += 1
                logger.info(
                    "[WakeWord] [STUB] Simulated wake-word detection #%d for '%s'",
                    counter,
                    self._wake_word,
                )
                if self._callback:
                    try:
                        await self._callback()
                    except Exception as exc:
                        logger.error("[WakeWord] Callback error: %s", exc)
        except asyncio.CancelledError:
            pass
