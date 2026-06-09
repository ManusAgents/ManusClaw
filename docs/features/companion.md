# Companion Desktop Apps

**Status:** 🏗 Scaffolded

## Description
System tray companion apps for macOS and Windows that connect to the ManusClaw server.

## Components

### macOS Menu Bar (`desktop/macos/menubar.py`)
Uses `rumps` for macOS menu bar integration.

### Windows System Tray (`desktop/windows_hub/hub.py`)
Uses `pystray` for cross-platform system tray icon.

### Mobile Node Client (`desktop/mobile/node_client.py`)
WebSocket client for mobile Live Canvas viewing.

## Install
```bash
pip install manusclaw[companion]
```

## Current State
Scaffolded with basic structure. Provides starting points for:
- Server connection status indicator
- Quick message sending
- Session switching
- Notifications for incoming messages
