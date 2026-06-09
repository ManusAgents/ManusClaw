# Model Failover / Profile Rotation

**Status:** ✅ Implemented

## Description
Cross-provider model failover with configurable priority ordering, cooldown management, and per-session profile selection.

## Features
- Priority-ordered model entries (lower = higher priority)
- Automatic cooldown on failure (configurable per model, default 60s)
- Per-session profile overrides
- Success/failure statistics tracking
- Provider filtering

## Usage
```python
from app.llm.profile_rotation import ModelProfile, ProfileRotator

profile = ModelProfile.from_config([
    {"provider": "openai", "model": "gpt-4o", "priority": 0},
    {"provider": "anthropic", "model": "claude-sonnet-4", "priority": 1},
    {"provider": "ollama", "model": "llama3.2:3b", "priority": 2},
])

rotator = ProfileRotator(profile)
entry = await rotator.get_next()      # → openai/gpt-4o
await rotator.mark_failed(entry)       # Put in cooldown
entry = await rotator.get_next()      # → anthropic/claude-sonnet-4
await rotator.mark_success(entry, latency_s=1.5)
await rotator.reset_all()              # Clear all cooldowns
```

## Entry Fields
`provider`, `model`, `api_key`, `base_url`, `priority`, `fallback_weight`, `max_tokens`, `temperature`, `timeout`, `cooldown_s`, `extra_headers`
