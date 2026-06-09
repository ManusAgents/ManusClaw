#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  ManusClaw — Termux (Android) Installer
#  Usage: bash setup-termux.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

REPO="https://github.com/The-JDdev/ManusClaw.git"
INSTALL_DIR="$HOME/ManusClaw"

echo -e "${CYAN}${BOLD}"
echo "  ███╗   ███╗ █████╗ ███╗   ██╗██╗   ██╗███████╗"
echo "  ManusClaw — Termux Setup — by The-JDdev"
echo -e "${NC}"

# ── Update packages ──────────────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Updating Termux packages...${NC}"
pkg update -y && pkg upgrade -y

# ── Install deps ─────────────────────────────────────────────────────────────
echo -e "${BOLD}[2/5] Installing system packages...${NC}"
pkg install -y python python-pip git clang libffi openssl libjpeg-turbo

# ── Clone ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}[3/5] Cloning ManusClaw...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Updating existing install..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi
echo -e "  ${GREEN}✓ $INSTALL_DIR${NC}"

# ── pip install ──────────────────────────────────────────────────────────────
echo -e "${BOLD}[4/5] Installing Python packages...${NC}"
cd "$INSTALL_DIR"
pip install --upgrade pip -q

# Termux-safe subset (skip playwright — not available on Android)
# Install core + optional extras commonly useful on mobile
pip install -q \
    pydantic \
    openai \
    anthropic \
    aiohttp \
    duckduckgo-search \
    fastapi \
    "uvicorn[standard]" \
    rich

echo -e "  ${GREEN}✓ Packages installed${NC}"

# ── config ───────────────────────────────────────────────────────────────────
if [ ! -f "config.toml" ]; then
    cat > config.toml << 'CONF'
[llm]
# Termux tip: point to Ollama running on your PC (same WiFi)
# base_url = "http://192.168.1.X:11434/v1"
# api_key  = "none"
# model    = "llama3.2:3b"

# Or use OpenRouter (free tier available)
# base_url = "https://openrouter.ai/api/v1"
# api_key  = "sk-or-..."
# model    = "mistralai/mistral-7b-instruct"

# Default: mock (no API key, works offline for testing)
provider    = "mock"
model       = "gpt-4o"

[runflow]
mode      = "build"
max_steps = 30
CONF
    echo -e "  ${GREEN}✓ config.toml created${NC}"
fi

# ── launcher alias ───────────────────────────────────────────────────────────
echo -e "${BOLD}[5/5] Adding alias...${NC}"
ALIAS_LINE="alias manusclaw='cd $INSTALL_DIR && python main.py'"
BASHRC="$HOME/.bashrc"

if ! grep -q "alias manusclaw" "$BASHRC" 2>/dev/null; then
    echo "" >> "$BASHRC"
    echo "# ManusClaw" >> "$BASHRC"
    echo "$ALIAS_LINE" >> "$BASHRC"
    echo -e "  ${GREEN}✓ Alias added to ~/.bashrc${NC}"
fi

echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ManusClaw installed for Termux!"
echo -e "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}Quick start (new session):${NC}"
echo -e "    manusclaw \"Your task here\""
echo ""
echo -e "  ${BOLD}Or directly:${NC}"
echo -e "    cd ~/ManusClaw"
echo -e "    python main.py \"Your task here\""
echo ""
echo -e "  ${BOLD}Start server (access from phone browser):${NC}"
echo -e "    cd ~/ManusClaw && python run_server.py --port 8765"
echo -e "    Then open: http://localhost:8765"
echo ""
echo -e "  ${BOLD}Connect to PC Ollama (same WiFi):${NC}"
echo -e "    Edit config.toml → set base_url = \"http://YOUR-PC-IP:11434/v1\""
echo ""
source "$HOME/.bashrc" 2>/dev/null || true
