# ─────────────────────────────────────────────────────────────────────────────
#  ManusClaw — Windows Installer (PowerShell)
#  Works on: Windows 10/11 (PowerShell 5.1+)
#  Usage: Right-click → Run with PowerShell
#         Or: powershell -ExecutionPolicy Bypass -File install.ps1
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$REPO       = "https://github.com/The-JDdev/ManusClaw.git"
$InstallDir = "$env:USERPROFILE\ManusClaw"

function Write-Banner {
    Write-Host ""
    Write-Host "  ███╗   ███╗ █████╗ ███╗   ██╗██╗   ██╗███████╗" -ForegroundColor Cyan
    Write-Host "  ████╗ ████║██╔══██╗████╗  ██║██║   ██║██╔════╝" -ForegroundColor Cyan
    Write-Host "  ██╔████╔██║███████║██╔██╗ ██║██║   ██║███████╗" -ForegroundColor Cyan
    Write-Host "  ██║╚██╔╝██║██╔══██║██║╚██╗██║██║   ██║╚════██║" -ForegroundColor Cyan
    Write-Host "  ██║ ╚═╝ ██║██║  ██║██║ ╚████║╚██████╔╝███████║" -ForegroundColor Cyan
    Write-Host "  ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚══════╝" -ForegroundColor Cyan
    Write-Host "  ManusClaw Installer — by The-JDdev (SHS Shobuj)" -ForegroundColor White
    Write-Host ""
}

Write-Banner

# ── Python check ─────────────────────────────────────────────────────────────
Write-Host "[1/5] Checking Python..." -ForegroundColor Yellow
try {
    $pyVer = python --version 2>&1
    Write-Host "  ✓ $pyVer found" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Python not found. Installing via winget..." -ForegroundColor Red
    winget install --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "  ✓ Python installed" -ForegroundColor Green
}

# ── git check ────────────────────────────────────────────────────────────────
Write-Host "[2/5] Checking git..." -ForegroundColor Yellow
try {
    git --version | Out-Null
    Write-Host "  ✓ git found" -ForegroundColor Green
} catch {
    Write-Host "  Installing git via winget..." -ForegroundColor Yellow
    winget install --id Git.Git --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "  ✓ git installed" -ForegroundColor Green
}

# ── clone / update ───────────────────────────────────────────────────────────
Write-Host "[3/5] Cloning ManusClaw..." -ForegroundColor Yellow
if (Test-Path "$InstallDir\.git") {
    Write-Host "  Existing install found — pulling latest..."
    git -C $InstallDir pull --ff-only
} else {
    git clone $REPO $InstallDir
}
Write-Host "  ✓ Cloned to $InstallDir" -ForegroundColor Green

# ── venv + pip ───────────────────────────────────────────────────────────────
Write-Host "[4/5] Installing dependencies..." -ForegroundColor Yellow
Set-Location $InstallDir
python -m venv .venv
& ".venv\Scripts\pip.exe" install --upgrade pip -q
& ".venv\Scripts\pip.exe" install -r requirements.txt -q
Write-Host "  ✓ Dependencies installed" -ForegroundColor Green

# ── config ───────────────────────────────────────────────────────────────────
if (-not (Test-Path "config.toml")) {
    @"
[llm]
provider    = "mock"
model       = "gpt-4o"
# api_key   = ""

# Agnostic mode (OpenRouter, Ollama, LMStudio, Groq, etc.):
# base_url = "http://localhost:11434/v1"
# api_key  = "none"
# model    = "llama3.2:3b"

[runflow]
mode      = "build"
max_steps = 50
"@ | Out-File -FilePath "config.toml" -Encoding utf8
    Write-Host "  ✓ config.toml created — edit to add your API key" -ForegroundColor Green
}

# ── launcher batch file ───────────────────────────────────────────────────────
Write-Host "[5/5] Creating launcher..." -ForegroundColor Yellow
$LauncherDir = "$env:USERPROFILE\AppData\Local\Programs\ManusClaw"
New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null

$BatchContent = "@echo off`r`ncall `"$InstallDir\.venv\Scripts\activate.bat`"`r`ncd /d `"$InstallDir`"`r`npython main.py %*`r`n"
$BatchContent | Out-File -FilePath "$LauncherDir\manusclaw.bat" -Encoding ascii

# Add to user PATH
$UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$LauncherDir*") {
    [System.Environment]::SetEnvironmentVariable(
        "Path",
        "$UserPath;$LauncherDir",
        "User"
    )
    Write-Host "  ✓ Added to PATH (restart terminal to use 'manusclaw' command)" -ForegroundColor Green
} else {
    Write-Host "  ✓ Already in PATH" -ForegroundColor Green
}

Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host "  ManusClaw installed successfully!" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Green
Write-Host ""
Write-Host "  Quick start (restart terminal first):" -ForegroundColor White
Write-Host "    manusclaw `"Write a Python web scraper`"" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Or use directly:" -ForegroundColor White
Write-Host "    cd $InstallDir" -ForegroundColor Cyan
Write-Host "    .venv\Scripts\activate" -ForegroundColor Cyan
Write-Host "    python main.py `"your task`"" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Edit config: $InstallDir\config.toml" -ForegroundColor White
Write-Host "  Support:     https://github.com/The-JDdev/ManusClaw" -ForegroundColor White
Write-Host ""

Read-Host "Press Enter to close"
