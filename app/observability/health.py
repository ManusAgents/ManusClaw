"""
Health Check System with Liveness and Readiness Probes
========================================================

Implements Kubernetes-style health checking with liveness and readiness
probes, component-level health checkers, and aggregated health responses.

Endpoints:
    - ``/healthz`` — Liveness probe: is the process running?
    - ``/ready`` — Readiness probe: is it ready to serve requests?

Health statuses:
    - ``HEALTHY``   — Component is fully operational
    - ``DEGRADED``  — Component is partially functional (e.g., retries)
    - ``UNHEALTHY`` — Component is non-functional

Built-in component health checkers:
    - :class:`DatabaseHealthChecker` — Checks SQLite database connectivity
    - :class:`LLMHealthChecker` — Checks LLM provider availability
    - :class:`SandboxHealthChecker` — Checks sandbox backend availability

Usage::

    from app.observability.health import HealthCheckSystem, DatabaseHealthChecker

    health = HealthCheckSystem()
    health.register_checker("database", DatabaseHealthChecker())

    # Liveness
    liveness = health.liveness()  # always returns HEALTHY if the process is running

    # Readiness
    readiness = health.readiness()  # checks all registered components
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sqlite3
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

# Use a separate logger namespace to avoid inheriting the manusclaw root
# logger's handlers (which require ContextFilter attributes).
_logger = logging.getLogger("manusclaw_observability.health")

# ---------------------------------------------------------------------------
# Health status enum
# ---------------------------------------------------------------------------


class HealthStatus(str, Enum):
    """Health status levels for component checks."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"

    def __lt__(self, other: object) -> bool:
        """Order: UNHEALTHY < DEGRADED < HEALTHY."""
        if not isinstance(other, HealthStatus):
            return NotImplemented
        order = {HealthStatus.UNHEALTHY: 0, HealthStatus.DEGRADED: 1, HealthStatus.HEALTHY: 2}
        return order[self] < order[other]

    def __le__(self, other: object) -> bool:
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, HealthStatus):
            return NotImplemented
        return not (self < other or self == other)

    def __ge__(self, other: object) -> bool:
        return self == other or self > other


# ---------------------------------------------------------------------------
# Component health result
# ---------------------------------------------------------------------------


class ComponentHealth:
    """Health check result for a single component.

    Attributes:
        name: Component name.
        status: Health status.
        message: Human-readable status message.
        details: Arbitrary details dict.
        checked_at: ISO 8601 timestamp of the check.
        duration_ms: Time taken for the check in milliseconds.
    """

    __slots__ = ("name", "status", "message", "details", "checked_at", "duration_ms")

    def __init__(
        self,
        name: str,
        status: HealthStatus,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
        duration_ms: float = 0.0,
    ) -> None:
        self.name = name
        self.status = status
        self.message = message
        self.details = details or {}
        self.checked_at = datetime.now(timezone.utc).isoformat()
        self.duration_ms = duration_ms

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict suitable for JSON responses."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "checked_at": self.checked_at,
            "duration_ms": round(self.duration_ms, 2),
        }


# ---------------------------------------------------------------------------
# Abstract health checker
# ---------------------------------------------------------------------------


class HealthChecker(ABC):
    """Abstract base class for component health checkers.

    Subclasses must implement :meth:`check` (sync) and optionally
    :meth:`check_async` (async).  If :meth:`check_async` is not
    overridden, it falls back to running :meth:`check` in a thread.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Component name used in health reports."""
        ...

    @abstractmethod
    def check(self) -> ComponentHealth:
        """Perform a synchronous health check.

        Returns:
            A :class:`ComponentHealth` result.
        """
        ...

    async def check_async(self) -> ComponentHealth:
        """Perform an asynchronous health check.

        Default implementation runs :meth:`check` in a thread pool.

        Returns:
            A :class:`ComponentHealth` result.
        """
        return await asyncio.to_thread(self.check)


# ---------------------------------------------------------------------------
# Built-in health checkers
# ---------------------------------------------------------------------------


class DatabaseHealthChecker(HealthChecker):
    """Checks SQLite database connectivity.

    Verifies that the database file is accessible and that a simple
    query (``SELECT 1``) executes successfully.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or "workspace/.sessions/manusclaw.db"

    @property
    def name(self) -> str:
        return "database"

    def check(self) -> ComponentHealth:
        start = time.monotonic()
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            try:
                cursor = conn.execute("SELECT 1")
                cursor.fetchone()
                conn.close()
            except Exception as e:
                conn.close()
                raise

            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message="Database is accessible",
                details={"db_path": self._db_path},
                duration_ms=duration_ms,
            )
        except sqlite3.OperationalError as e:
            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Database operational error: {e}",
                details={"db_path": self._db_path, "error": str(e)},
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Database check failed: {e}",
                details={"db_path": self._db_path, "error": str(e)},
                duration_ms=duration_ms,
            )


class LLMHealthChecker(HealthChecker):
    """Checks LLM provider availability.

    Verifies that the LLM configuration is valid and (optionally)
    that a minimal API call succeeds.
    """

    def __init__(self, perform_api_call: bool = False) -> None:
        self._perform_api_call = perform_api_call

    @property
    def name(self) -> str:
        return "llm"

    def check(self) -> ComponentHealth:
        start = time.monotonic()
        try:
            from app.config import Config

            cfg = Config.get()
            provider = cfg.llm.provider
            model = cfg.llm.model
            has_api_key = bool(cfg.llm.api_key)
            has_base_url = bool(cfg.llm.base_url)

            details: Dict[str, Any] = {
                "provider": provider,
                "model": model,
                "has_api_key": has_api_key,
                "has_base_url": has_base_url,
            }

            if provider == "mock":
                duration_ms = (time.monotonic() - start) * 1000
                return ComponentHealth(
                    name=self.name,
                    status=HealthStatus.DEGRADED,
                    message="LLM provider is 'mock' — no real LLM available",
                    details=details,
                    duration_ms=duration_ms,
                )

            if not has_api_key and not has_base_url and provider not in (
                "ollama", "lmstudio", "openai-compat", "universal", "gguf",
                "huggingface", "hf",
            ):
                duration_ms = (time.monotonic() - start) * 1000
                return ComponentHealth(
                    name=self.name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"LLM provider '{provider}' configured but no API key or base URL",
                    details=details,
                    duration_ms=duration_ms,
                )

            # Optionally make a test API call
            if self._perform_api_call:
                try:
                    return self._test_api_call(details, start)
                except Exception as e:
                    duration_ms = (time.monotonic() - start) * 1000
                    details["api_call_error"] = str(e)
                    return ComponentHealth(
                        name=self.name,
                        status=HealthStatus.DEGRADED,
                        message=f"LLM API call failed: {e}",
                        details=details,
                        duration_ms=duration_ms,
                    )

            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message=f"LLM provider '{provider}' configured",
                details=details,
                duration_ms=duration_ms,
            )

        except ImportError:
            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message="Config module not available",
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"LLM health check failed: {e}",
                details={"error": str(e)},
                duration_ms=duration_ms,
            )

    def _test_api_call(self, details: Dict[str, Any], start: float) -> ComponentHealth:
        """Attempt a minimal LLM API call to verify connectivity."""
        try:
            from app.llm.llm import LLM

            llm = LLM()
            # Use a very small prompt and token limit
            result = llm.ask("Say 'ok' and nothing else.", max_tokens=5)
            duration_ms = (time.monotonic() - start) * 1000
            details["api_call_success"] = True
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message="LLM API call succeeded",
                details=details,
                duration_ms=duration_ms,
            )
        except Exception as e:
            raise


class SandboxHealthChecker(HealthChecker):
    """Checks sandbox backend availability.

    Verifies that at least one sandbox backend (Docker, SSH, OpenShell)
    is available and configured.
    """

    @property
    def name(self) -> str:
        return "sandbox"

    def check(self) -> ComponentHealth:
        start = time.monotonic()
        details: Dict[str, Any] = {}

        try:
            from app.sandbox.factory import list_available_backends

            available = list_available_backends()
            details["available_backends"] = available
            details["sandbox_enabled"] = False

            try:
                from app.config import Config
                cfg = Config.get()
                details["sandbox_enabled"] = cfg.sandbox.enabled
            except Exception:
                pass

            duration_ms = (time.monotonic() - start) * 1000

            if not available:
                return ComponentHealth(
                    name=self.name,
                    status=HealthStatus.DEGRADED,
                    message="No sandbox backends available — tool execution may be limited",
                    details=details,
                    duration_ms=duration_ms,
                )

            return ComponentHealth(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message=f"Sandbox backends available: {', '.join(available)}",
                details=details,
                duration_ms=duration_ms,
            )

        except ImportError:
            duration_ms = (time.monotonic() - start) * 1000
            details["available_backends"] = []
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Sandbox module not available",
                details=details,
                duration_ms=duration_ms,
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Sandbox health check failed: {e}",
                details={"error": str(e)},
                duration_ms=duration_ms,
            )


# ---------------------------------------------------------------------------
# Health check system
# ---------------------------------------------------------------------------


class HealthCheckSystem:
    """Central health check system that manages component checkers.

    Registers component health checkers and provides liveness and
    readiness endpoints.

    Usage::

        health = HealthCheckSystem()
        health.register_checker("database", DatabaseHealthChecker())
        health.register_checker("llm", LLMHealthChecker())
        health.register_checker("sandbox", SandboxHealthChecker())

        # Kubernetes liveness probe
        result = health.liveness()

        # Kubernetes readiness probe
        result = health.readiness()
    """

    def __init__(self) -> None:
        self._checkers: Dict[str, HealthChecker] = {}
        self._start_time = time.monotonic()
        self._start_timestamp = datetime.now(timezone.utc).isoformat()

    def register_checker(self, name: str, checker: HealthChecker) -> None:
        """Register a component health checker.

        Args:
            name: Unique name for the component.
            checker: The :class:`HealthChecker` instance.
        """
        self._checkers[name] = checker

    def unregister_checker(self, name: str) -> None:
        """Remove a previously registered health checker.

        Args:
            name: The component name to remove.
        """
        self._checkers.pop(name, None)

    def liveness(self) -> Dict[str, Any]:
        """Liveness probe — checks if the process is running.

        This always returns HEALTHY as long as the process can respond.
        If it cannot respond, the orchestrator (Kubernetes) will restart it.

        Returns:
            A dict with status ``"HEALTHY"`` and basic process info.
        """
        return {
            "status": HealthStatus.HEALTHY.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - self._start_time, 2),
            "process": {
                "pid": os.getpid(),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "started_at": self._start_timestamp,
            },
        }

    def readiness(self) -> Dict[str, Any]:
        """Readiness probe — checks if the service is ready to serve.

        Runs all registered component health checkers and aggregates
        the results.  The overall status is the worst individual status.

        Returns:
            A dict with the overall status and individual component results.
        """
        component_results: List[Dict[str, Any]] = []
        overall_status = HealthStatus.HEALTHY

        for name, checker in self._checkers.items():
            try:
                result = checker.check()
            except Exception as e:
                result = ComponentHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check raised exception: {e}",
                    details={"error": str(e), "error_type": type(e).__name__},
                )

            component_results.append(result.to_dict())

            # Aggregate: worst status wins
            if result.status == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            elif result.status == HealthStatus.DEGRADED and overall_status == HealthStatus.HEALTHY:
                overall_status = HealthStatus.DEGRADED

        return {
            "status": overall_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - self._start_time, 2),
            "components": component_results,
        }

    async def readiness_async(self) -> Dict[str, Any]:
        """Async readiness probe — runs all checkers concurrently.

        Returns:
            A dict with the overall status and individual component results.
        """
        component_results: List[Dict[str, Any]] = []
        overall_status = HealthStatus.HEALTHY

        # Run all checkers concurrently
        tasks = []
        names = []
        for name, checker in self._checkers.items():
            tasks.append(checker.check_async())
            names.append(name)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for name, result in zip(names, results):
            if isinstance(result, Exception):
                health = ComponentHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check raised exception: {result}",
                    details={"error": str(result), "error_type": type(result).__name__},
                )
            elif isinstance(result, ComponentHealth):
                health = result
            else:
                health = ComponentHealth(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message="Health check returned unexpected type",
                    details={"result_type": type(result).__name__},
                )

            component_results.append(health.to_dict())

            if health.status == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
            elif health.status == HealthStatus.DEGRADED and overall_status == HealthStatus.HEALTHY:
                overall_status = HealthStatus.DEGRADED

        return {
            "status": overall_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(time.monotonic() - self._start_time, 2),
            "components": component_results,
        }

    def get_registered_checkers(self) -> List[str]:
        """Return a list of registered checker names."""
        return list(self._checkers.keys())


# ---------------------------------------------------------------------------
# Default health check system singleton
# ---------------------------------------------------------------------------

_default_health_system: Optional[HealthCheckSystem] = None
_health_lock = __import__("threading").Lock()


def get_health_system() -> HealthCheckSystem:
    """Return the default :class:`HealthCheckSystem` singleton.

    On first call, registers the built-in health checkers (database,
    LLM, sandbox).
    """
    global _default_health_system
    with _health_lock:
        if _default_health_system is None:
            _default_health_system = HealthCheckSystem()
            _default_health_system.register_checker("database", DatabaseHealthChecker())
            _default_health_system.register_checker("llm", LLMHealthChecker())
            _default_health_system.register_checker("sandbox", SandboxHealthChecker())
    return _default_health_system
