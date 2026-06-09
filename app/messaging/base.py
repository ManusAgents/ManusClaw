from __future__ import annotations
"""Base adapter contract for all messaging platform adapters."""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from app.logger import logger


@dataclass
class IncomingMessage:
    platform: str
    user_id: str
    channel_id: str
    text: str
    message_id: Optional[str] = None

    @property
    def session_key(self) -> str:
        return f"{self.platform}:{self.user_id}:{self.channel_id}"


class BaseMessagingAdapter(ABC):
    platform_name: str = "base"

    def __init__(self, token: str = "") -> None:
        self.token = token
        self._running = False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def start(self, on_message) -> None: ...

    @abstractmethod
    async def send(self, channel_id: str, text: str) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    def is_configured(self) -> bool:
        return bool(self.token)
