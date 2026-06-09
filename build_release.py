#!/usr/bin/env python3
"""
ManusClaw — Local Release Builder
Builds standalone executables for the current platform using PyInstaller.

Usage:
    python build_release.py

Output:
    release/manusclaw          (Linux/macOS)
    release/manusclaw.exe      (Windows)

Requirements:
    pip install pyinstaller

Run this script on each target platform separately,
or in a VM/Docker container for cross-platform builds.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT    = Path(__file__).parent
RELEASE = ROOT / "release"
DIST    = ROOT / "dist"
BUILD   = ROOT / "build"

OS      = platform.system()           # Linux | Darwin | Windows
ARCH    = platform.machine().lower()  # x86_64 | arm64 | amd64

EXE_NAME = "manusclaw.exe" if OS == "Windows" else "manusclaw"
TAG_NAME = f"v4.0.0-{OS.lower()}-{ARCH}"


def run(cmd: list[str]) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        run([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])


def clean() -> None:
    for d in [DIST, BUILD, RELEASE]:
        if d.exists():
            shutil.rmtree(d)
    for spec in ROOT.glob("*.spec"):
        spec.unlink()


def build() -> None:
    print(f"\n{'='*60}")
    print(f"  Building ManusClaw {TAG_NAME}")
    print(f"  Platform: {OS} / {ARCH}")
    print(f"{'='*60}\n")

    ensure_pyinstaller()
    clean()
    RELEASE.mkdir(parents=True)

    hidden = [
        "app.agent",
        "app.agent.roles",
        "app.llm",
        "app.memory",
        "app.db",
        "app.permissions",
        "app.server",
        "app.tool",
        "app.flow",
        "app.mcp",
        "app.sandbox",
        "pydantic",
        "openai",
        "anthropic",
        "aiohttp",
    ]

    hidden_imports = []
    for h in hidden:
        hidden_imports += ["--hidden-import", h]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "main.py",
        "--name", "manusclaw",
        "--onefile",
        "--clean",
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--add-data", f"config.toml{os.pathsep}.",
        "--add-data", f"requirements.txt{os.pathsep}.",
        *hidden_imports,
        "--strip" if OS != "Windows" else "",
    ]
    cmd = [c for c in cmd if c]  # remove empty strings

    run(cmd)

    # Move to release/
    src = DIST / EXE_NAME
    dst = RELEASE / f"manusclaw-{TAG_NAME}{'.exe' if OS == 'Windows' else ''}"
    shutil.copy2(src, dst)

    # Also copy config example and README
    shutil.copy2(ROOT / "config.toml", RELEASE / "config.toml")
    shutil.copy2(ROOT / "README.md",   RELEASE / "README.md")

    # Create a zip archive
    archive = RELEASE / f"manusclaw-{TAG_NAME}"
    shutil.make_archive(str(archive), "zip", RELEASE, dst.name)

    print(f"\n{'='*60}")
    print(f"  ✓ Build complete!")
    print(f"  Executable : {dst}")
    print(f"  Archive    : {archive}.zip")
    print(f"{'='*60}\n")
    print("  Upload these files to your GitHub Release manually:")
    print(f"    https://github.com/The-JDdev/ManusClaw/releases/new")
    print(f"\n  Tag: {TAG_NAME}\n")


if __name__ == "__main__":
    build()
