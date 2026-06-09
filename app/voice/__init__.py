"""Voice wake-word detection, continuous talk mode, and TTS providers.

Provides:
    - VoiceWakeDetector  — async wake-word listener (pvporcupine or fallback)
    - TalkMode           — continuous STT → agent → TTS loop
    - TTSProvider        — abstract TTS hierarchy with concrete backends
"""

from app.voice.wake import VoiceWakeDetector
from app.voice.talk import TalkMode
from app.voice.tts import TTSProvider, get_tts_provider

__all__ = [
    "VoiceWakeDetector",
    "TalkMode",
    "TTSProvider",
    "get_tts_provider",
]
