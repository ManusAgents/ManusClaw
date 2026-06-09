# Voice Input/Output System

**Status:** ✅ Implemented

## Description
Complete voice pipeline: wake-word detection → STT → agent interaction → TTS playback.

## Components

### TTS Providers
- **NullTTS** — Silent stub (logs text)
- **SystemTTS** — Offline pyttsx3 engine
- **OpenAITTS** — OpenAI TTS API
- **ElevenLabsTTS** — ElevenLabs API

### Wake Word Detection
- **Porcupine** — Hardware-accelerated (requires PICOVOICE_API_KEY)
- **speech_recognition** — Keyword fallback
- **Stub** — Simulated detection every 30s

### Talk Mode
Continuous conversation loop: listen → transcribe → agent → speak. Exits on stop phrases.

## Configuration
| Variable | Description |
|---|---|
| `PICOVOICE_API_KEY` | Picovoice wake-word key |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS key |

## Install
```
pip install manusclaw[voice]
```

## Factory
```python
from app.voice.tts import get_tts_provider
tts = get_tts_provider()  # Auto-selects best available
```
