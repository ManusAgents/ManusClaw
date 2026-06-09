# AI Providers Configuration Templates

This directory contains configuration templates for various free and paid AI providers supported by ManusClaw.

## How to use

You can use these templates in several ways:

1. **Directly**: Copy the contents of any `.toml` file to your `config.toml` in the project root.
   ```bash
   cp providers/ollama.toml config.toml
   ```

2. **As Profiles**: Move them to your ManusClaw profiles directory and use the `MANUSCLAW_PROFILE` environment variable.
   ```bash
   mkdir -p ~/.manusclaw/profiles/ollama
   cp providers/ollama.toml ~/.manusclaw/profiles/ollama/config.toml
   MANUSCLAW_PROFILE=ollama manusclaw "Your task"
   ```

## Included Providers

- **Ollama** (Free / Local)
- **Ollama Cloud** (Paid/Free / API)
- **OpenRouter** (Paid / API)
- **7LLM** (Paid / API)
- **Pollinations** (Free / API)
