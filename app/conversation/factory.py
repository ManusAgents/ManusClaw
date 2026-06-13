"""
ManusClaw Conversation System — ConversationFactory
======================================================

Factory for creating the appropriate conversation implementation based
on configuration.

Two conversation types are supported:

  - **LocalConversation**: The agent runs locally with direct tool
    execution and file-backed event persistence.  This is the default
    for single-machine deployments.

  - **RemoteConversation**: The agent runs on a remote server accessed
    via WebSocket.  This is for distributed deployments where the
    agent runtime is separated from the client.

The factory reads configuration from the manusclaw :class:`Config`
system and constructs the appropriate conversation with all
dependencies wired up.

Usage::

    from app.conversation.factory import ConversationFactory

    # Create a local conversation with defaults
    conv = ConversationFactory.create()

    # Create a remote conversation
    conv = ConversationFactory.create(mode="remote", server_url="ws://agent:3000/ws")

    # Create with explicit config
    conv = ConversationFactory.create(
        mode="local",
        agent=my_agent,
        event_log_dir="/tmp/my_events",
    )
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.conversation.base import BaseConversation
from app.conversation.local_conversation import LocalConversation
from app.conversation.remote_conversation import RemoteConversation
from app.conversation.stuck_detector import StuckDetector
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# Factory configuration
# ──────────────────────────────────────────────────────────────────────────────

class ConversationConfig:
    """
    Typed configuration for conversation creation.

    This is a simple dataclass that captures all the parameters needed
    to construct a conversation.  It can be built manually or derived
    from the global manusclaw :class:`Config`.

    Attributes:
        mode:                "local" or "remote".
        conversation_id:     Optional explicit ID.
        event_log_dir:       Directory for event logs (local only).
        agent:               Pre-configured agent instance (local only).
        server_url:          WebSocket URL (remote only).
        confirmation_policy: Security confirmation policy.
        security_analyzer:   Security analyzer instance.
        hook_manager:        Hook manager for lifecycle events.
        stuck_detector:      Stuck pattern detector.
        auto_title:          Whether to auto-generate titles (local only).
        preload_tools:       Tools to preload (local only).
        max_reconnect_attempts: Max reconnect attempts (remote only).
        base_reconnect_delay_s: Base reconnect delay (remote only).
        max_reconnect_delay_s:  Max reconnect delay (remote only).
        event_buffer_size:   Event buffer size (remote only).
    """

    def __init__(
        self,
        mode: str = "local",
        conversation_id: Optional[str] = None,
        event_log_dir: Optional[str] = None,
        agent: Optional[Any] = None,
        server_url: str = "ws://localhost:3000/ws",
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
        hook_manager: Optional[Any] = None,
        stuck_detector: Optional[StuckDetector] = None,
        auto_title: bool = True,
        preload_tools: Optional[list] = None,
        max_reconnect_attempts: int = 10,
        base_reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
        event_buffer_size: int = 1000,
    ) -> None:
        self.mode = mode
        self.conversation_id = conversation_id
        self.event_log_dir = event_log_dir
        self.agent = agent
        self.server_url = server_url
        self.confirmation_policy = confirmation_policy
        self.security_analyzer = security_analyzer
        self.hook_manager = hook_manager
        self.stuck_detector = stuck_detector
        self.auto_title = auto_title
        self.preload_tools = preload_tools
        self.max_reconnect_attempts = max_reconnect_attempts
        self.base_reconnect_delay_s = base_reconnect_delay_s
        self.max_reconnect_delay_s = max_reconnect_delay_s
        self.event_buffer_size = event_buffer_size


# ──────────────────────────────────────────────────────────────────────────────
# ConversationFactory
# ──────────────────────────────────────────────────────────────────────────────

class ConversationFactory:
    """
    Factory for creating the appropriate conversation implementation.

    The factory encapsulates the logic for deciding between local and
    remote conversation types and wiring up all dependencies.

    Usage::

        # Simple creation
        conv = ConversationFactory.create(mode="local")

        # With config object
        config = ConversationConfig(mode="remote", server_url="ws://agent:3000/ws")
        conv = ConversationFactory.create_from_config(config)

        # From global config
        conv = ConversationFactory.create_from_app_config()
    """

    @staticmethod
    def create(
        mode: str = "local",
        conversation_id: Optional[str] = None,
        event_log_dir: Optional[str] = None,
        agent: Optional[Any] = None,
        server_url: str = "ws://localhost:3000/ws",
        confirmation_policy: Optional[Any] = None,
        security_analyzer: Optional[Any] = None,
        hook_manager: Optional[Any] = None,
        stuck_detector: Optional[StuckDetector] = None,
        auto_title: bool = True,
        preload_tools: Optional[list] = None,
        max_reconnect_attempts: int = 10,
        base_reconnect_delay_s: float = 1.0,
        max_reconnect_delay_s: float = 30.0,
        event_buffer_size: int = 1000,
    ) -> BaseConversation:
        """
        Create a conversation instance.

        This is the primary entry point.  Based on *mode*, it creates
        either a :class:`LocalConversation` or a
        :class:`RemoteConversation` with all dependencies wired up.

        Args:
            mode:                "local" or "remote".
            conversation_id:     Optional explicit conversation ID.
            event_log_dir:       Directory for event logs (local only).
            agent:               Agent instance (local only).
            server_url:          WebSocket URL (remote only).
            confirmation_policy: Security confirmation policy.
            security_analyzer:   Security analyzer instance.
            hook_manager:        Hook manager for lifecycle events.
            stuck_detector:      Stuck pattern detector.
            auto_title:          Auto-generate titles (local only).
            preload_tools:       Tools to preload (local only).
            max_reconnect_attempts: Max reconnect attempts (remote only).
            base_reconnect_delay_s: Base reconnect delay (remote only).
            max_reconnect_delay_s:  Max reconnect delay (remote only).
            event_buffer_size:   Event buffer size (remote only).

        Returns:
            A :class:`BaseConversation` subclass instance.

        Raises:
            ValueError: If *mode* is not "local" or "remote".
        """
        config = ConversationConfig(
            mode=mode,
            conversation_id=conversation_id,
            event_log_dir=event_log_dir,
            agent=agent,
            server_url=server_url,
            confirmation_policy=confirmation_policy,
            security_analyzer=security_analyzer,
            hook_manager=hook_manager,
            stuck_detector=stuck_detector,
            auto_title=auto_title,
            preload_tools=preload_tools,
            max_reconnect_attempts=max_reconnect_attempts,
            base_reconnect_delay_s=base_reconnect_delay_s,
            max_reconnect_delay_s=max_reconnect_delay_s,
            event_buffer_size=event_buffer_size,
        )
        return ConversationFactory.create_from_config(config)

    @staticmethod
    def create_from_config(config: ConversationConfig) -> BaseConversation:
        """
        Create a conversation from a :class:`ConversationConfig`.

        Args:
            config: Fully specified configuration.

        Returns:
            A :class:`BaseConversation` subclass instance.

        Raises:
            ValueError: If config.mode is not "local" or "remote".
        """
        if config.mode == "local":
            return ConversationFactory._create_local(config)
        elif config.mode == "remote":
            return ConversationFactory._create_remote(config)
        else:
            raise ValueError(
                f"Invalid conversation mode: {config.mode!r}. "
                f"Must be 'local' or 'remote'."
            )

    @staticmethod
    def _create_local(config: ConversationConfig) -> LocalConversation:
        """
        Create a :class:`LocalConversation` from config.
        """
        conv = LocalConversation(
            conversation_id=config.conversation_id,
            event_log_dir=config.event_log_dir,
            agent=config.agent,
            confirmation_policy=config.confirmation_policy,
            security_analyzer=config.security_analyzer,
            stuck_detector=config.stuck_detector,
            hook_manager=config.hook_manager,
            auto_title=config.auto_title,
            preload_tools=config.preload_tools,
        )

        logger.info(
            f"[ConversationFactory] Created LocalConversation "
            f"id={conv.conversation_id[:8]}"
        )

        return conv

    @staticmethod
    def _create_remote(config: ConversationConfig) -> RemoteConversation:
        """
        Create a :class:`RemoteConversation` from config.
        """
        conv = RemoteConversation(
            conversation_id=config.conversation_id,
            server_url=config.server_url,
            confirmation_policy=config.confirmation_policy,
            security_analyzer=config.security_analyzer,
            stuck_detector=config.stuck_detector,
            hook_manager=config.hook_manager,
            max_reconnect_attempts=config.max_reconnect_attempts,
            base_reconnect_delay_s=config.base_reconnect_delay_s,
            max_reconnect_delay_s=config.max_reconnect_delay_s,
            event_buffer_size=config.event_buffer_size,
        )

        logger.info(
            f"[ConversationFactory] Created RemoteConversation "
            f"id={conv.conversation_id[:8]} "
            f"server={config.server_url}"
        )

        return conv

    @staticmethod
    def create_from_app_config(
        conversation_id: Optional[str] = None,
        agent: Optional[Any] = None,
        hook_manager: Optional[Any] = None,
    ) -> BaseConversation:
        """
        Create a conversation using the global manusclaw :class:`Config`.

        This method reads the manusclaw configuration to determine
        whether to create a local or remote conversation, and wires up
        security analyzers, confirmation policies, and other dependencies.

        Args:
            conversation_id: Optional explicit conversation ID.
            agent:           Optional pre-configured agent instance.
            hook_manager:    Optional hook manager.

        Returns:
            A :class:`BaseConversation` subclass instance.
        """
        from app.config import Config

        app_config = Config.get()

        # Determine mode from environment or config
        import os
        mode = os.getenv("MANUSCLAW_CONVERSATION_MODE", "local").lower()
        if mode not in ("local", "remote"):
            mode = "local"

        # Build security analyzer if available
        security_analyzer = None
        confirmation_policy = None
        try:
            from app.security import (
                EnsembleSecurityAnalyzer,
                PatternSecurityAnalyzer,
                PolicyRailSecurityAnalyzer,
                NeverConfirm,
                ConfirmRisky,
                SecurityRisk,
            )
            security_analyzer = EnsembleSecurityAnalyzer([
                PatternSecurityAnalyzer(),
                PolicyRailSecurityAnalyzer(),
            ])
            # Default to ConfirmRisky for production, NeverConfirm for dev
            if app_config.is_prod():
                confirmation_policy = ConfirmRisky(
                    threshold=SecurityRisk.MEDIUM,
                    block_high=True,
                )
            else:
                confirmation_policy = NeverConfirm()
        except Exception as e:
            logger.debug(
                f"[ConversationFactory] Security setup skipped: {e}"
            )

        # Build stuck detector with defaults
        stuck_detector = StuckDetector(
            window_size=20,
            repeat_threshold=3,
            monologue_threshold=8,
        )

        config = ConversationConfig(
            mode=mode,
            conversation_id=conversation_id,
            agent=agent,
            server_url=os.getenv(
                "MANUSCLAW_AGENT_SERVER_URL",
                "ws://localhost:3000/ws",
            ),
            confirmation_policy=confirmation_policy,
            security_analyzer=security_analyzer,
            hook_manager=hook_manager,
            stuck_detector=stuck_detector,
            auto_title=True,
        )

        return ConversationFactory.create_from_config(config)
