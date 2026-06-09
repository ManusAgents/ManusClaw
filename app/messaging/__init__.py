"""Messaging gateway — Telegram, Discord, Slack, WhatsApp, Signal, Teams,
Matrix, IRC, Google Chat, WebChat, Email, Twitch adapters."""
from app.messaging.base import BaseMessagingAdapter, IncomingMessage
from app.messaging.telegram import TelegramAdapter
from app.messaging.discord import DiscordAdapter
from app.messaging.slack import SlackAdapter
from app.messaging.whatsapp import WhatsAppAdapter
from app.messaging.signal import SignalAdapter
from app.messaging.teams import TeamsAdapter
from app.messaging.matrix import MatrixAdapter
from app.messaging.irc import IRCAdapter
from app.messaging.google_chat import GoogleChatAdapter
from app.messaging.webchat import WebChatAdapter
from app.messaging.email import EmailAdapter
from app.messaging.twitch import TwitchAdapter

__all__ = [
    "BaseMessagingAdapter",
    "IncomingMessage",
    "TelegramAdapter",
    "DiscordAdapter",
    "SlackAdapter",
    "WhatsAppAdapter",
    "SignalAdapter",
    "TeamsAdapter",
    "MatrixAdapter",
    "IRCAdapter",
    "GoogleChatAdapter",
    "WebChatAdapter",
    "EmailAdapter",
    "TwitchAdapter",
]


def main_channels() -> None:
    """Entry point for ``manusclaw-channels`` — list configured messaging channels."""
    from app.messaging.gateway import MessagingGateway
    gw = MessagingGateway(use_router=False)
    configured = [a.platform_name for a in gw._adapters if a.is_configured()]
    if not configured:
        print("No messaging channels configured.")
        print("Set TELEGRAM_TOKEN, DISCORD_TOKEN, etc. to enable.")
    else:
        print(f"Configured messaging channels ({len(configured)}):")
        for name in configured:
            print(f"  ● {name}")
