"""ManusClaw Automation Module.

Provides email automation via Gmail Pub/Sub and the EmailTool agent tool.

Exports:
    GmailWatcher  — watches Gmail inbox via Google API Pub/Sub push notifications
    EmailTool     — BaseTool implementation for send/read/search/reply actions
"""
from __future__ import annotations

from app.automation.gmail import GmailWatcher
from app.automation.email_tool import EmailTool

__all__ = ["GmailWatcher", "EmailTool"]
