#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  ManusClaw — Linux & macOS Installer
#  Works on: Ubuntu, Debian, Fedora, Arch, macOS (Intel + Apple Silicon)
#  Usage:  bash install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

REPO="https://github.com/The-JDdev/ManusClaw.git"
INSTALL_DIR="$HOME/ManusClaw"
BIN_LINK="/usr/local/bin/manusclaw"

echo -e "${CYAN}${BOLD}"
echo "  ███╗   ███╗ █████╗ ███╗   ██╗██╗   ██╗███████╗"
echo "  ████╗ ████║██╔══██╗████╗  ██║██║   ██║██╔════╝"
echo "  ██╔████╔██║███████║██╔██╗ ██║██║   ██║███████╗"
echo "  ██║╚██╔╝██║██╔══██║██║╚██╗██║██║   ██║╚════██║"
echo "  ██║ ╚═╝ ██║██║  ██║██║ ╚████║╚██████╔╝███████║"
echo "  ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚══════╝"
echo -e "  ManusClaw Installer — by The-JDdev (SHS Shobuj)${NC}"
echo ""

# ── Python check ─────────────────────────────────────────────────────────────
echo -e "${BOLD}[1/5] Checking Python...${NC}"
if command -v python3 &>/dev/null; then
    PY=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo -e "  ${GREEN}✓ Python $PY found${NC}"
else
    echo -e "  ${RED}✗ Python 3.10+ required. Install it first:${NC}"
    echo "    Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
    echo "    macOS:         brew install python3"
    echo "    Fedora:        sudo dnf install python3"
    exit 1
fi

# ── git check ────────────────────────────────────────────────────────────────
echo -e "${BOLD}[2/5] Checking git...${NC}"
if ! command -v git &>/dev/null; then
    echo -e "  ${YELLOW}Installing git...${NC}"
    if command -v apt-get &>/dev/null; then sudo apt-get install -y git
    elif command -v dnf &>/dev/null; then sudo dnf install -y git
    elif command -v brew &>/dev/null; then brew install git
    else echo -e "  ${RED}Please install git manually.${NC}"; exit 1; fi
fi
echo -e "  ${GREEN}✓ git found${NC}"

# ── clone / update ───────────────────────────────────────────────────────────
echo -e "${BOLD}[3/5] Cloning ManusClaw...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "  Existing install found — pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO" "$INSTALL_DIR"
fi
echo -e "  ${GREEN}✓ Cloned to $INSTALL_DIR${NC}"

# ── venv + pip ───────────────────────────────────────────────────────────────
echo -e "${BOLD}[4/5] Installing dependencies...${NC}"
cd "$INSTALL_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "  ${GREEN}✓ Dependencies installed${NC}"

# ── config ───────────────────────────────────────────────────────────────────
if [ ! -f "config.toml" ]; then
    cat > config.toml << 'CONF'
[llm]
# Set your provider. Options: openai | anthropic | google | mock
# Or use base_url for any OpenAI-compatible endpoint (Ollama, OpenRouter, etc.)
provider    = "mock"       # works out-of-the-box, no API key needed
model       = "gpt-4o"
# api_key   = ""           # or set OPENAI_API_KEY env var

# Agnostic mode (OpenRouter, Ollama, LMStudio, Groq, etc.):
# base_url = "http://localhost:11434/v1"
# api_key  = "none"
# model    = "llama3.2:3b"

[runflow]
mode      = "build"
max_steps = 50
CONF
    echo -e "  ${GREEN}✓ config.toml created — edit to add your API key${NC}"
fi

# ── launcher script ──────────────────────────────────────────────────────────
echo -e "${BOLD}[5/5] Creating launcher...${NC}"
LAUNCHER="$INSTALL_DIR/.venv/bin/manusclaw"
cat > "$LAUNCHER" << LAUNCHER
#!/usr/bin/env bash
source "$INSTALL_DIR/.venv/bin/activate"
cd "$INSTALL_DIR"
exec python main.py "\$@"
LAUNCHER
chmod +x "$LAUNCHER"

# Try to symlink to /usr/local/bin
if [ -w "/usr/local/bin" ] || sudo -n true 2>/dev/null; then
    sudo ln -sf "$LAUNCHER" "$BIN_LINK" 2>/dev/null || ln -sf "$LAUNCHER" "$HOME/.local/bin/manusclaw" 2>/dev/null || true
    echo -e "  ${GREEN}✓ 'manusclaw' command available globally${NC}"
else
    echo -e "  ${YELLOW}Add to PATH manually:${NC}"
    echo "    export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\""
fi

echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ManusClaw installed successfully!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}Quick start:${NC}"
echo -e "    manusclaw \"Write a Python web scraper\""
echo ""
echo -e "  ${BOLD}Edit config:${NC}"
echo -e "    nano $INSTALL_DIR/config.toml"
echo ""
echo -e "  ${BOLD}Start server:${NC}"
echo -e "    cd $INSTALL_DIR && source .venv/bin/activate"
echo -e "    python run_server.py"
echo ""
echo -e "  ${BOLD}Support:${NC} https://github.com/The-JDdev/ManusClaw"
echo ""
