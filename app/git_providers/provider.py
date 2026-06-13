from __future__ import annotations

"""
Git Provider Router
====================
Central registry and router that dispatches to the correct
:class:`GitProviderService` implementation based on URL pattern,
explicit provider name, or configuration.

Typical usage::

    router = GitProviderRouter()
    service = router.get_service("https://github.com/owner/repo")
    issues = service.get_issues("owner/repo")

The router also serves as a factory that instantiates and caches
provider services.
"""

import re
import threading
from typing import Any, Dict, List, Optional, Type

from app.exceptions import NonRetryableError
from app.logger import logger

from .base import GitProviderService
from .models import AuthInfo, AuthType, Repository, SuggestedTask

# ── lazy imports to avoid crashing when optional deps are missing ─────────


def _import_github() -> Optional[Type[GitProviderService]]:
    try:
        from .github.service import GitHubService
        return GitHubService
    except Exception as exc:
        logger.warning("git_providers.router github_import_failed err=%s", exc)
        return None


def _import_gitlab() -> Optional[Type[GitProviderService]]:
    try:
        from .gitlab.service import GitLabService
        return GitLabService
    except Exception as exc:
        logger.warning("git_providers.router gitlab_import_failed err=%s", exc)
        return None


def _import_azure_devops() -> Optional[Type[GitProviderService]]:
    try:
        from .azure_devops.service import AzureDevOpsService
        return AzureDevOpsService
    except Exception as exc:
        logger.warning("git_providers.router azure_devops_import_failed err=%s", exc)
        return None


def _import_bitbucket() -> Optional[Type[GitProviderService]]:
    try:
        from .bitbucket.service import BitbucketService
        return BitbucketService
    except Exception as exc:
        logger.warning("git_providers.router bitbucket_import_failed err=%s", exc)
        return None


def _import_forgejo() -> Optional[Type[GitProviderService]]:
    try:
        from .forgejo.service import ForgejoService
        return ForgejoService
    except Exception as exc:
        logger.warning("git_providers.router forgejo_import_failed err=%s", exc)
        return None


# ── URL pattern registry ──────────────────────────────────────────────────

_PROVIDER_PATTERNS: List[tuple] = [
    # (compiled_regex, provider_name)
    (re.compile(r"https?://github\.com"), "github"),
    (re.compile(r"https?://gitlab\.com"), "gitlab"),
    (re.compile(r"https?://.+\.gitlab\.com"), "gitlab"),
    (re.compile(r"https?://dev\.azure\.com"), "azure_devops"),
    (re.compile(r"https?://.+\.visualstudio\.com"), "azure_devops"),
    (re.compile(r"https?://bitbucket\.org"), "bitbucket"),
    (re.compile(r"https?://codeberg\.org"), "forgejo"),
    (re.compile(r"https?://.+\.forgejo\..+"), "forgejo"),
    (re.compile(r"https?://.+\.gitea\..+"), "forgejo"),
    # Self-hosted patterns (less certain; user can override)
    (re.compile(r"https?://git\..+"), "gitlab"),  # common convention
    (re.compile(r"https?://code\..+"), "gitlab"),
]

# Map provider name → lazy import callable
_PROVIDER_IMPORTS: Dict[str, callable] = {
    "github": _import_github,
    "gitlab": _import_gitlab,
    "azure_devops": _import_azure_devops,
    "bitbucket": _import_bitbucket,
    "forgejo": _import_forgejo,
}


class GitProviderRouter:
    """
    Routes git operations to the correct :class:`GitProviderService`
    implementation.

    The router:

    1. **Detects** the provider from a repository URL or explicit name.
    2. **Instantiates** the corresponding service class with auth credentials.
    3. **Caches** service instances keyed by ``(provider_name, token_hash)``
       so the same service is reused across calls.
    4. Supports **explicit registration** of custom providers.

    Thread-safe via an internal lock.
    """

    def __init__(self) -> None:
        self._services: Dict[str, GitProviderService] = {}
        self._custom_providers: Dict[str, Type[GitProviderService]] = {}
        self._lock = threading.RLock()

    # ── provider detection ────────────────────────────────────────────────

    @staticmethod
    def detect_provider(url: str) -> Optional[str]:
        """Return the provider name inferred from a repository URL.

        If no pattern matches, returns ``None``.
        """
        url_lower = url.lower()
        for pattern, provider_name in _PROVIDER_PATTERNS:
            if pattern.search(url_lower):
                return provider_name
        return None

    @staticmethod
    def detect_provider_from_repo(repo: Repository) -> Optional[str]:
        """Return the provider name inferred from a :class:`Repository` object."""
        if repo.provider:
            return repo.provider
        if repo.url:
            return GitProviderRouter.detect_provider(repo.url)
        return None

    # ── service creation ──────────────────────────────────────────────────

    def get_service(
        self,
        url_or_provider: str,
        auth_info: Optional[AuthInfo] = None,
    ) -> GitProviderService:
        """Return a :class:`GitProviderService` for the given URL or provider name.

        If ``auth_info`` is not provided, a default (unauthenticated)
        :class:`AuthInfo` is used.

        Services are cached; calling this method twice with the same
        arguments returns the same instance.
        """
        provider_name = self.detect_provider(url_or_provider) or url_or_provider.lower()

        # Build a cache key
        token_hash = ""
        if auth_info and auth_info.token:
            token_hash = auth_info.token[:8]  # first 8 chars is enough for caching
        cache_key = f"{provider_name}:{token_hash}"

        with self._lock:
            if cache_key in self._services:
                return self._services[cache_key]

            service = self._create_service(provider_name, auth_info)
            if service is None:
                available = list(_PROVIDER_IMPORTS.keys()) + list(self._custom_providers.keys())
                raise NonRetryableError(
                    f"Unknown or unavailable git provider '{provider_name}'. "
                    f"Available: {available}"
                )

            self._services[cache_key] = service
            return service

    def _create_service(
        self,
        provider_name: str,
        auth_info: Optional[AuthInfo] = None,
    ) -> Optional[GitProviderService]:
        """Instantiate a provider service, returning None on failure."""

        # Check custom providers first
        if provider_name in self._custom_providers:
            cls = self._custom_providers[provider_name]
            try:
                return cls(auth_info or AuthInfo(auth_type=AuthType.PERSONAL_ACCESS_TOKEN))
            except Exception as exc:
                logger.error(
                    "git_providers.router custom_provider_create name=%s err=%s",
                    provider_name,
                    exc,
                )
                return None

        # Check built-in providers
        import_fn = _PROVIDER_IMPORTS.get(provider_name)
        if import_fn is None:
            return None

        cls = import_fn()
        if cls is None:
            logger.warning(
                "git_providers.router provider_unavailable name=%s", provider_name
            )
            return None

        effective_auth = auth_info or AuthInfo(auth_type=AuthType.PERSONAL_ACCESS_TOKEN)

        # Auto-populate extra fields based on provider
        if provider_name == "azure_devops" and "organization" not in effective_auth.extra:
            # Try to extract org from the URL
            pass  # User should provide organization in auth_info.extra

        try:
            return cls(effective_auth)
        except Exception as exc:
            logger.error(
                "git_providers.router service_create name=%s err=%s",
                provider_name,
                exc,
            )
            return None

    # ── custom provider registration ──────────────────────────────────────

    def register_provider(
        self,
        name: str,
        service_cls: Type[GitProviderService],
        url_pattern: Optional[str] = None,
    ) -> None:
        """Register a custom provider class.

        Optionally also register a URL pattern that maps to this provider.
        """
        with self._lock:
            self._custom_providers[name.lower()] = service_cls
            if url_pattern:
                _PROVIDER_PATTERNS.append(
                    (re.compile(url_pattern, re.IGNORECASE), name.lower())
                )
            logger.info(
                "git_providers.router registered_provider name=%s", name
            )

    # ── multi-repo operations ─────────────────────────────────────────────

    def get_suggested_tasks_for_repos(
        self,
        repo_urls: List[str],
        auth_info: Optional[AuthInfo] = None,
    ) -> List[SuggestedTask]:
        """Aggregate suggested tasks across multiple repositories.

        Duplicates are automatically removed (based on
        :class:`SuggestedTask.__hash__`).
        """
        all_tasks: set = set()
        for url in repo_urls:
            try:
                service = self.get_service(url, auth_info)
                # Extract repo_id from URL
                repo_id = self._extract_repo_id(url)
                if repo_id:
                    tasks = service.get_suggested_tasks(repo_id)
                    all_tasks.update(tasks)
            except Exception as exc:
                logger.warning(
                    "git_providers.router suggested_tasks url=%s err=%s", url, exc
                )
        return sorted(all_tasks, key=lambda t: t.priority, reverse=True)

    def get_suggested_tasks_for_url(
        self,
        repo_url: str,
        auth_info: Optional[AuthInfo] = None,
    ) -> List[SuggestedTask]:
        """Convenience: get suggested tasks for a single repository URL."""
        return self.get_suggested_tasks_for_repos([repo_url], auth_info)

    # ── utility ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_repo_id(url: str) -> Optional[str]:
        """Best-effort extraction of ``owner/repo`` from a git URL.

        Supports:
        - https://github.com/owner/repo
        - https://github.com/owner/repo.git
        - git@github.com:owner/repo.git
        - ssh://git@github.com/owner/repo.git
        """
        # Remove trailing .git
        clean = url.rstrip("/")
        if clean.endswith(".git"):
            clean = clean[:-4]

        # SSH format: git@host:owner/repo
        ssh_match = re.match(r"git@[^:]+:(.+)", clean)
        if ssh_match:
            return ssh_match.group(1)

        # HTTPS format
        https_match = re.match(r"https?://[^/]+/(.+)", clean)
        if https_match:
            path = https_match.group(1)
            # Remove extra path segments (like /tree/main, /issues, etc.)
            parts = path.split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            return path

        return None

    def list_available_providers(self) -> List[str]:
        """Return the names of all available (importable) providers."""
        available: List[str] = []
        for name, import_fn in _PROVIDER_IMPORTS.items():
            cls = import_fn()
            if cls is not None:
                available.append(name)
        available.extend(self._custom_providers.keys())
        return available

    def clear_cache(self) -> None:
        """Remove all cached service instances."""
        with self._lock:
            self._services.clear()

    def get_cached_service(self, provider_name: str) -> Optional[GitProviderService]:
        """Return a cached service by provider name, or None."""
        with self._lock:
            for key, svc in self._services.items():
                if key.startswith(f"{provider_name}:"):
                    return svc
        return None
