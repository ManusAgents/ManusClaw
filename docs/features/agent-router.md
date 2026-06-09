# Multi-Agent Router

**Status:** ✅ Implemented

## Description
Per-channel multi-agent routing with configurable route rules, LRU-cached agent instances, and per-agent customization.

## Configuration
Add to `config.yaml`:
```yaml
agents:
  defaults:
    sandbox_mode: false
    workspace_dir: workspace
  definitions:
    coder:
      class_path: app.agent.manus.Manus
      system_prompt: "You are a coding specialist..."
      tools: [python_execute, str_replace_editor, bash]
  routes:
    - channel: discord
      agent_name: coder
      user_id: "admin_.*"
      agent_name: manus
    - chat_id: "general"
      agent_name: manus
```

## Route Rule Matching
Priority-ordered evaluation. Supports exact string and regex patterns for channel, user_id, and chat_id.

## AgentRegistry
LRU cache (64 agents, 300s idle TTL). Automatic eviction. Session key: `{route}:{platform}:{user_id}:{channel_id}`.

## Integration
Used by `MessagingGateway` when `use_router=True` (default). Falls back to legacy direct Manus creation.
