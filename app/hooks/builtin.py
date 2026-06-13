from __future__ import annotations

"""
ManusClaw Hooks — Built-in Hooks
==================================
Production-ready hooks shipped with manusclaw, inspired by OpenHands's
hook architecture.

Built-in hooks:
    LoggingHook   — Logs every hook event to the manusclaw logger.
    SecurityHook  — Integrates with the security analyzer subsystem.
    AuditHook     — Writes a persistent audit trail to file / database.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.hooks.base import HookBase
from app.hooks.types import (
    HookContext,
    HookDecision,
    HookEventType,
    HookResult,
)
from app.logger import logger


# ──────────────────────────────────────────────────────────────────────────────
# LoggingHook
# ──────────────────────────────────────────────────────────────────────────────

class LoggingHook(HookBase):
    """
    Logs all hook events to the manusclaw structured logger.

    This is a passive (observation-only) hook that always returns ALLOW.
    It subscribes to all event types and logs:
        - Event type and source
        - Tool name / args (for tool events)
        - User message (for prompt events)
        - Conversation / agent metadata

    Configuration:
        log_level:      Logging level (default "DEBUG").
        log_tool_args:  Whether to include tool arguments in the log (default True).
        log_user_message: Whether to include user messages in the log (default True).
        max_arg_length: Truncate tool args / messages beyond this length (default 500).
    """

    name = "logging"
    description = "Logs all hook events to the manusclaw logger"
    priority = 200  # Run after decision-making hooks
    timeout_s = 5.0
    fail_open = True

    subscribed_events = {
        HookEventType.SESSION_START,
        HookEventType.SESSION_END,
        HookEventType.PRE_TOOL_USE,
        HookEventType.POST_TOOL_USE,
        HookEventType.USER_PROMPT_SUBMIT,
        HookEventType.STOP,
    }

    # ── Configurable attributes ──────────────────────────────────────────

    log_level: str = "DEBUG"
    log_tool_args: bool = True
    log_user_message: bool = True
    max_arg_length: int = 500

    async def on_event(self, context: HookContext) -> HookResult:
        """Log the hook event and return ALLOW."""
        try:
            log_fn = getattr(logger, self.log_level.lower(), logger.debug)

            parts: list[str] = [
                f"[Hook:Logging] event={context.event_type.value}",
                f"source={context.source}",
                f"conversation_id={context.conversation_id}",
            ]

            if context.agent_name:
                parts.append(f"agent={context.agent_name}")

            # Tool-specific fields
            if context.tool_name:
                parts.append(f"tool={context.tool_name}")
            if context.tool_args and self.log_tool_args:
                args_str = json.dumps(context.tool_args, default=str)
                if len(args_str) > self.max_arg_length:
                    args_str = args_str[: self.max_arg_length] + "..."
                parts.append(f"args={args_str}")

            # Post-tool result summary
            if context.tool_result is not None:
                result_str = str(context.tool_result)
                if len(result_str) > self.max_arg_length:
                    result_str = result_str[: self.max_arg_length] + "..."
                parts.append(f"result={result_str}")

            # User message
            if context.user_message and self.log_user_message:
                msg = context.user_message
                if len(msg) > self.max_arg_length:
                    msg = msg[: self.max_arg_length] + "..."
                parts.append(f"message={msg}")

            log_fn(" | ".join(parts))

        except Exception as e:
            # Never let logging crash the agent
            logger.warning(f"[Hook:Logging] Failed to log event: {e}")

        return HookResult.allow()


# ──────────────────────────────────────────────────────────────────────────────
# SecurityHook
# ──────────────────────────────────────────────────────────────────────────────

class SecurityHook(HookBase):
    """
    Integrates with the manusclaw security analyzer subsystem.

    For PRE_TOOL_USE events, this hook:
        1. Extracts the action string from tool arguments.
        2. Runs it through all registered security analyzers.
        3. Returns DENY if the risk level meets or exceeds the threshold.

    For USER_PROMPT_SUBMIT events, this hook:
        1. Scans the user message for prompt-injection patterns.
        2. Returns DENY if a HIGH-risk injection is detected.

    Configuration:
        deny_threshold:  Minimum risk level to block (default HIGH).
                         One of "LOW", "MEDIUM", "HIGH".
        enabled_tools:   Set of tool names to scan. Empty set = scan all.
        scan_prompts:    Whether to scan user prompts for injection (default True).
    """

    name = "security"
    description = "Integrates with security analyzers to block risky actions"
    priority = 10  # Run early — security should decide before other hooks
    timeout_s = 5.0
    fail_open = True  # Fail-open: if the analyzer crashes, allow the action

    subscribed_events = {
        HookEventType.PRE_TOOL_USE,
        HookEventType.USER_PROMPT_SUBMIT,
    }

    # ── Configurable attributes ──────────────────────────────────────────

    deny_threshold: str = "HIGH"
    enabled_tools: set[str] = set()   # Empty = scan all tools
    scan_prompts: bool = True

    # ── Lazy-loaded analyzer reference ───────────────────────────────────

    _analyzers: Optional[list[Any]] = None

    def _get_analyzers(self) -> list[Any]:
        """
        Lazily import and instantiate security analyzers.
        Deferred import avoids circular dependencies at module load time.
        """
        if self._analyzers is not None:
            return self._analyzers

        analyzers: list[Any] = []
        try:
            from app.security.base import SecurityAnalyzerBase
            from app.security.base import analyze_sequence
            from app.security.pattern import PatternSecurityAnalyzer

            analyzers.append(PatternSecurityAnalyzer())
            # Attempt to load policy rails if available
            try:
                from app.security.policy_rails import PolicyRailAnalyzer
                analyzers.append(PolicyRailAnalyzer())
            except (ImportError, Exception):
                pass

            self._analyze_sequence = analyze_sequence
        except ImportError:
            logger.warning(
                "[Hook:Security] Security analyzer module not available — "
                "security hook will be passive"
            )
            self._analyze_sequence = None

        self._analyzers = analyzers
        return analyzers

    def _get_threshold(self) -> Any:
        """Parse the deny_threshold string into a SecurityRisk enum."""
        try:
            from app.security.base import SecurityRisk
            return SecurityRisk.from_name(self.deny_threshold)
        except ImportError:
            return None

    def _extract_action(self, context: HookContext) -> str:
        """Extract the actionable content from a tool context."""
        if not context.tool_args:
            return ""
        tool = context.tool_name or ""
        tool_lower = tool.lower()
        if tool_lower == "bash":
            return context.tool_args.get("command", "")
        if tool_lower == "python_execute":
            return context.tool_args.get("code", "")
        # Generic: serialize all args
        return json.dumps(context.tool_args, default=str)

    async def on_event(self, context: HookContext) -> HookResult:
        """Scan actions for security risks and DENY if threshold is met."""
        try:
            analyzers = self._get_analyzers()
            if not analyzers:
                return HookResult.allow(reason="No security analyzers available")

            # ── PRE_TOOL_USE ──────────────────────────────────────────────
            if context.event_type == HookEventType.PRE_TOOL_USE:
                # Skip if enabled_tools is non-empty and this tool isn't in it
                if self.enabled_tools and context.tool_name not in self.enabled_tools:
                    return HookResult.allow()

                action = self._extract_action(context)
                if not action:
                    return HookResult.allow(reason="Empty action — nothing to scan")

                ctx = {
                    "tool": context.tool_name or "",
                    "conversation_id": context.conversation_id,
                    "agent_name": context.agent_name,
                }
                assessment = self._analyze_sequence(analyzers, action, context=ctx)
                threshold = self._get_threshold()

                if threshold is not None and assessment.is_risky(threshold):
                    logger.warning(
                        f"[Hook:Security] BLOCKED — risk={assessment.risk.name} "
                        f"detector={assessment.detector_id} "
                        f"reason={assessment.reason}"
                    )
                    return HookResult.deny(
                        reason=f"Security risk: {assessment.reason}",
                        detector_id=assessment.detector_id,
                        risk_level=assessment.risk.name,
                    )

                # Risk below threshold — allow but attach assessment metadata
                return HookResult.allow(
                    reason=f"Security scan: {assessment.risk.name} risk",
                    risk_level=assessment.risk.name,
                    detector_id=assessment.detector_id,
                )

            # ── USER_PROMPT_SUBMIT ────────────────────────────────────────
            if context.event_type == HookEventType.USER_PROMPT_SUBMIT and self.scan_prompts:
                message = context.user_message or ""
                if not message:
                    return HookResult.allow()

                ctx = {
                    "tool": "user_prompt",
                    "conversation_id": context.conversation_id,
                }
                assessment = self._analyze_sequence(analyzers, message, context=ctx)
                threshold = self._get_threshold()

                if threshold is not None and assessment.is_risky(threshold):
                    logger.warning(
                        f"[Hook:Security] BLOCKED prompt — risk={assessment.risk.name} "
                        f"detector={assessment.detector_id} "
                        f"reason={assessment.reason}"
                    )
                    return HookResult.deny(
                        reason=f"Prompt security risk: {assessment.reason}",
                        detector_id=assessment.detector_id,
                        risk_level=assessment.risk.name,
                    )

                return HookResult.allow(
                    reason=f"Prompt scan: {assessment.risk.name} risk",
                )

        except Exception as e:
            logger.error(
                f"[Hook:Security] Error during security scan: {e}",
                exc_info=True,
            )
            # fail_open=True → ALLOW on error
            return HookResult.allow(reason=f"Security scan error: {e}")

        return HookResult.allow()


# ──────────────────────────────────────────────────────────────────────────────
# AuditHook
# ──────────────────────────────────────────────────────────────────────────────

class AuditHook(HookBase):
    """
    Writes a persistent audit trail of hook events to a file and/or database.

    Every event is recorded as a structured JSON line containing:
        - Timestamp (UTC ISO-8601)
        - Event type
        - Conversation and agent identifiers
        - Tool name / args (sanitised)
        - User message (sanitised)
        - Source
        - Hook decision and reason

    This is a passive (observation-only) hook that always returns ALLOW.

    Configuration:
        audit_file:     Path to the JSONL audit log file (default "logs/audit.jsonl").
                        Set to "" to disable file logging.
        audit_db:       If True, also write to SessionDB (default False).
        max_entry_size: Maximum size of a single audit entry in bytes (default 8192).
        sanitize:       Whether to redact potential secrets (default True).
    """

    name = "audit"
    description = "Writes a persistent audit trail of hook events"
    priority = 300  # Run last — audit the final state
    timeout_s = 5.0
    fail_open = True

    subscribed_events = {
        HookEventType.SESSION_START,
        HookEventType.SESSION_END,
        HookEventType.PRE_TOOL_USE,
        HookEventType.POST_TOOL_USE,
        HookEventType.USER_PROMPT_SUBMIT,
        HookEventType.STOP,
    }

    # ── Configurable attributes ──────────────────────────────────────────

    audit_file: str = "logs/audit.jsonl"
    audit_db: bool = False
    max_entry_size: int = 8192
    sanitize: bool = True

    # ── Internal state ───────────────────────────────────────────────────

    _file_handle: Optional[Any] = None
    _db: Optional[Any] = None

    async def setup(self) -> None:
        """Open the audit file for appending."""
        if self.audit_file:
            try:
                path = Path(self.audit_file)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._file_handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
                logger.debug(f"[Hook:Audit] Audit file opened: {path}")
            except Exception as e:
                logger.error(f"[Hook:Audit] Failed to open audit file: {e}")
                self._file_handle = None

        if self.audit_db:
            try:
                from app.db.session import SessionDB
                self._db = SessionDB()
            except Exception as e:
                logger.error(f"[Hook:Audit] Failed to initialize SessionDB: {e}")
                self._db = None

    async def teardown(self) -> None:
        """Close the audit file handle."""
        if self._file_handle:
            try:
                self._file_handle.close()
            except Exception:
                pass
            self._file_handle = None

        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    def _sanitize_content(self, content: str) -> str:
        """Remove potential secrets from content before writing to the audit log."""
        if not self.sanitize:
            return content
        try:
            from app.security.base import sanitise_message
            return sanitise_message(content)
        except ImportError:
            # Fallback: basic regex sanitisation
            import re
            return re.sub(
                r"(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-+/=]{8,}",
                r"\1=<REDACTED>",
                content,
                flags=re.IGNORECASE,
            )

    def _build_entry(self, context: HookContext) -> dict[str, Any]:
        """Build a structured audit entry from a hook context."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": context.event_type.value,
            "conversation_id": context.conversation_id,
            "agent_name": context.agent_name,
            "source": context.source,
        }

        if context.tool_name:
            entry["tool_name"] = context.tool_name
        if context.tool_args:
            args_str = json.dumps(context.tool_args, default=str)
            if len(args_str) > 2000:
                args_str = args_str[:2000] + "...(truncated)"
            entry["tool_args"] = self._sanitize_content(args_str)
        if context.tool_result is not None:
            result_str = str(context.tool_result)
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(truncated)"
            entry["tool_result"] = self._sanitize_content(result_str)
        if context.user_message:
            msg = context.user_message
            if len(msg) > 2000:
                msg = msg[:2000] + "...(truncated)"
            entry["user_message"] = self._sanitize_content(msg)

        if context.extra:
            entry["extra"] = context.extra

        return entry

    def _write_to_file(self, entry: dict[str, Any]) -> None:
        """Append a JSON line to the audit file."""
        if not self._file_handle:
            return
        try:
            line = json.dumps(entry, default=str, ensure_ascii=False)
            if len(line) > self.max_entry_size:
                line = line[: self.max_entry_size] + "..."
            self._file_handle.write(line + "\n")
            self._file_handle.flush()
        except Exception as e:
            logger.warning(f"[Hook:Audit] Failed to write to audit file: {e}")

    async def _write_to_db(self, entry: dict[str, Any], context: HookContext) -> None:
        """Write the audit entry to SessionDB."""
        if not self._db:
            return
        try:
            session_id = context.conversation_id or "unknown"
            event = context.event_type.value
            content = json.dumps(entry, default=str, ensure_ascii=False)

            await self._db.log_message(
                session_id=session_id,
                role=f"audit:{event}",
                content=content[:4096],
            )
        except Exception as e:
            logger.warning(f"[Hook:Audit] Failed to write to SessionDB: {e}")

    async def on_event(self, context: HookContext) -> HookResult:
        """Record the event to the audit trail."""
        try:
            entry = self._build_entry(context)

            # File write is synchronous but fast
            self._write_to_file(entry)

            # DB write is async
            if self.audit_db:
                await self._write_to_db(entry, context)

        except Exception as e:
            logger.warning(f"[Hook:Audit] Error recording audit entry: {e}")

        # Audit hook always allows — it's observation-only
        return HookResult.allow()
