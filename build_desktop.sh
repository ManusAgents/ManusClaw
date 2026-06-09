#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ManusClaw Desktop Builder
# Compiles the GUI into a standalone executable for Linux, macOS, or Windows.
# Usage:
#   bash build_desktop.sh              # auto-detect current platform
#   bash build_desktop.sh --flet       # build Flet GUI (default)
#   bash build_desktop.sh --cli        # build CLI only (PyInstaller, no GUI)
#   bash build_desktop.sh --onedir     # one-folder output (faster cold start)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

VERSION="4.0.0"
MODE="flet"
BUNDLE="onefile"

for arg in "$@"; do
  case "$arg" in
    --cli)     MODE="cli" ;;
    --flet)    MODE="flet" ;;
    --onedir)  BUNDLE="onedir" ;;
  esac
done

# ── detect OS ────────────────────────────────────────────────────────────────
OS="linux"
EXT=""
case "$(uname -s 2>/dev/null || echo Windows)" in
  Darwin)  OS="macos" ;;
  MINGW*|MSYS*|CYGWIN*|Windows*) OS="windows"; EXT=".exe" ;;
esac

ARCH="$(uname -m 2>/dev/null || echo amd64)"
OUT_NAME="manusclaw-v${VERSION}-${OS}-${ARCH}${EXT}"
RELEASE_DIR="release"
mkdir -p "$RELEASE_DIR"

echo "════════════════════════════════════════════"
echo " ManusClaw Desktop Builder v${VERSION}"
echo " Platform : $OS / $ARCH"
echo " Mode     : $MODE"
echo " Bundle   : $BUNDLE"
echo "════════════════════════════════════════════"

# ── install build deps ───────────────────────────────────────────────────────
echo "[1/4] Checking build dependencies…"
pip install --quiet pyinstaller

if [[ "$MODE" == "flet" ]]; then
  pip install --quiet flet
  ENTRY="app/desktop/main.py"
  EXTRA_FLAGS="--collect-all flet --collect-all flet_core"
  WINDOW_FLAGS="--windowed"
else
  ENTRY="main.py"
  EXTRA_FLAGS=""
  WINDOW_FLAGS=""
fi

# ── icon (optional) ──────────────────────────────────────────────────────────
ICON_FLAG=""
if [[ -f "assets/icon.ico" && "$OS" == "windows" ]]; then
  ICON_FLAG="--icon=assets/icon.ico"
elif [[ -f "assets/icon.icns" && "$OS" == "macos" ]]; then
  ICON_FLAG="--icon=assets/icon.icns"
elif [[ -f "assets/icon.png" ]]; then
  ICON_FLAG="--icon=assets/icon.png"
fi

# ── run PyInstaller ──────────────────────────────────────────────────────────
echo "[2/4] Running PyInstaller…"

BUNDLE_FLAG="--${BUNDLE}"

# shellcheck disable=SC2086
pyinstaller \
  $BUNDLE_FLAG \
  $WINDOW_FLAGS \
  $ICON_FLAG \
  --name "$OUT_NAME" \
  --distpath "$RELEASE_DIR" \
  --workpath build/_pyinstaller \
  --specpath build \
  --noconfirm \
  --clean \
  --add-data "app:app" \
  --collect-all tenacity \
  --collect-all pydantic \
  $EXTRA_FLAGS \
  "$ENTRY" 2>&1 | tail -20

# ── compress ─────────────────────────────────────────────────────────────────
echo "[3/4] Packaging…"
if [[ "$BUNDLE" == "onefile" ]]; then
  BIN="$RELEASE_DIR/$OUT_NAME"
  if [[ "$OS" == "windows" ]]; then
    python3 -c "
import zipfile, os
z = zipfile.ZipFile('$RELEASE_DIR/$OUT_NAME.zip','w',zipfile.ZIP_DEFLATED)
z.write('$BIN', os.path.basename('$BIN'))
z.close()
print('Zipped: $RELEASE_DIR/$OUT_NAME.zip')
"
  else
    tar -czf "$RELEASE_DIR/${OUT_NAME}.tar.gz" -C "$RELEASE_DIR" "$OUT_NAME"
    echo "Archived: $RELEASE_DIR/${OUT_NAME}.tar.gz"
  fi
else
  echo "One-dir output: $RELEASE_DIR/$OUT_NAME/"
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo "[4/4] Done."
echo ""
echo "════════════════════════════════════════════"
echo " Build complete!"
echo ""
if [[ "$BUNDLE" == "onefile" ]]; then
  echo " Executable : $RELEASE_DIR/$OUT_NAME"
  echo " Archive    : $RELEASE_DIR/${OUT_NAME}.tar.gz (or .zip)"
else
  echo " Output dir : $RELEASE_DIR/$OUT_NAME/"
fi
echo ""
echo " Run it:"
if [[ "$OS" == "windows" ]]; then
  echo "   .\\$RELEASE_DIR\\$OUT_NAME"
else
  echo "   ./$RELEASE_DIR/$OUT_NAME"
fi
echo "════════════════════════════════════════════"

# ── OS-specific notes ────────────────────────────────────────────────────────
cat <<'NOTES'

────────────────────────────────────────────────────────
 PLATFORM SETUP NOTES
────────────────────────────────────────────────────────

 LINUX
   chmod +x release/manusclaw-*-linux-*
   ./release/manusclaw-*-linux-*

 macOS
   chmod +x release/manusclaw-*-macos-*
   ./release/manusclaw-*-macos-*
   # If Gatekeeper blocks it:
   xattr -cr ./release/manusclaw-*-macos-*

 WINDOWS
   Double-click the .exe  OR  run in PowerShell:
   .\release\manusclaw-*-windows-*.exe
   # If SmartScreen warns: click "More info" → "Run anyway"
   # To sign the exe (removes warning permanently):
   signtool sign /fd SHA256 /a release\manusclaw-*.exe

────────────────────────────────────────────────────────
 CROSS-COMPILE (build all 3 from one machine)
────────────────────────────────────────────────────────
 PyInstaller cannot cross-compile. To build all 3:
   • Linux binary  → run this script on Linux / WSL
   • macOS binary  → run on a Mac (or GitHub Actions macOS runner)
   • Windows .exe  → run on Windows (or Wine with PyInstaller)
 Upload all 3 to GitHub Releases manually.
────────────────────────────────────────────────────────
NOTES
