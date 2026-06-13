from __future__ import annotations

"""
Jinja2 Prompt Template System for Integrations Resolver
=======================================================
Provides provider-specific Jinja2 templates for constructing LLM prompts
when resolving issues, updating PRs, handling merge conflicts, and
responding to issue comments.

Templates are organized by:
  - **Provider** (github, gitlab, azure_devops, bitbucket)
  - **Action**  (issue_prompt, pr_update_prompt, merge_conflict_prompt,
                 issue_comment_prompt)

Features:
  - Built-in default templates for every action/provider combination.
  - Per-provider customization via filesystem overlay.
  - Template validation (all required variables present).
  - In-memory caching for compiled templates.
  - Thread-safe access.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.logger import logger

# ──────────────────────────────────────────────────────────────────────────────
# Optional Jinja2 import
# ──────────────────────────────────────────────────────────────────────────────

try:
    from jinja2 import BaseLoader, Environment, Template, TemplateError
    _JINJA2_AVAILABLE = True
except ImportError:
    _JINJA2_AVAILABLE = False

    class _DummyTemplate:  # type: ignore[no-redef]
        """Fallback when Jinja2 is not installed — simple {{ var }} replacement."""

        def __init__(self, source: str) -> None:
            self._source = source

        def render(self, **kwargs: Any) -> str:
            import re
            result = self._source
            for key, value in kwargs.items():
                result = result.replace("{{ " + key + " }}", str(value))
                result = result.replace("{{" + key + "}}", str(value))
            return result

    class _DummyEnv:  # type: ignore[no-redef]
        def from_string(self, source: str) -> _DummyTemplate:
            return _DummyTemplate(source)

        def get_template(self, name: str) -> _DummyTemplate:
            return _DummyTemplate("")

    # Stub out the module-level names used later
    TemplateError = Exception  # type: ignore[assignment,misc]

    def _make_env() -> Any:
        return _DummyEnv()


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────


class TemplateAction(str, Enum):
    """Actions for which templates can be rendered."""

    ISSUE_PROMPT = "issue_prompt"
    PR_UPDATE_PROMPT = "pr_update_prompt"
    MERGE_CONFLICT_PROMPT = "merge_conflict_prompt"
    ISSUE_COMMENT_PROMPT = "issue_comment_prompt"


class GitProvider(str, Enum):
    """Supported git providers."""

    GITHUB = "github"
    GITLAB = "gitlab"
    AZURE_DEVOPS = "azure_devops"
    BITBUCKET = "bitbucket"


# ──────────────────────────────────────────────────────────────────────────────
# Required variables per action
# ──────────────────────────────────────────────────────────────────────────────

_REQUIRED_VARS: Dict[TemplateAction, Set[str]] = {
    TemplateAction.ISSUE_PROMPT: {
        "repo_id",
        "issue_title",
        "issue_body",
        "issue_number",
        "issue_labels",
        "issue_assignees",
    },
    TemplateAction.PR_UPDATE_PROMPT: {
        "repo_id",
        "pr_title",
        "pr_body",
        "pr_number",
        "source_branch",
        "target_branch",
        "changed_files",
    },
    TemplateAction.MERGE_CONFLICT_PROMPT: {
        "repo_id",
        "pr_number",
        "pr_title",
        "source_branch",
        "target_branch",
        "conflict_files",
    },
    TemplateAction.ISSUE_COMMENT_PROMPT: {
        "repo_id",
        "issue_number",
        "issue_title",
        "comment_body",
        "comment_author",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# Built-in default templates
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_TEMPLATES: Dict[str, str] = {
    # ── GitHub ──────────────────────────────────────────────────────────────
    f"github.{TemplateAction.ISSUE_PROMPT.value}": """\
You are an expert software engineer tasked with resolving a GitHub issue.

## Repository
**Repo:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Issue Description
{{ issue_body }}

## Labels
{% if issue_labels %}{{ issue_labels | join(', ') }}{% else %}None{% endif %}

## Assignees
{% if issue_assignees %}{{ issue_assignees | join(', ') }}{% else %}Unassigned{% endif %}

## Instructions
1. Analyze the issue carefully and understand the root cause.
2. Identify all files that need to be changed.
3. Make minimal, focused changes that address the issue.
4. Ensure existing tests still pass.
5. If new tests are needed, add them.

Respond with a detailed plan and the exact code changes required.
""",

    f"github.{TemplateAction.PR_UPDATE_PROMPT.value}": """\
You are an expert software engineer updating a GitHub Pull Request.

## Repository
**Repo:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## PR Description
{{ pr_body }}

## Branch Information
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}
- **Changed Files:** {{ changed_files }}

## Instructions
1. Review the current PR changes.
2. Address any outstanding review comments.
3. Ensure CI checks pass.
4. Make the minimal changes needed to improve the PR.
5. Do not introduce breaking changes.

Provide the exact modifications needed.
""",

    f"github.{TemplateAction.MERGE_CONFLICT_PROMPT.value}": """\
You are an expert software engineer resolving merge conflicts in a GitHub Pull Request.

## Repository
**Repo:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## Branch Information
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}

## Conflicting Files
{{ conflict_files }}

## Instructions
1. Analyze the merge conflicts carefully.
2. Understand the intent of both branches.
3. Resolve conflicts by combining both changes when possible.
4. If changes conflict logically, prefer the source branch intent.
5. Ensure the resolved code compiles and tests pass.

Provide the resolved file contents for each conflicting file.
""",

    f"github.{TemplateAction.ISSUE_COMMENT_PROMPT.value}": """\
You are an expert software engineer responding to a GitHub issue comment.

## Repository
**Repo:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Comment by {{ comment_author }}
{{ comment_body }}

## Instructions
1. Understand the comment and its relation to the issue.
2. If the comment requests changes, implement them.
3. If the comment provides additional context, incorporate it.
4. Respond constructively and take appropriate action.

Provide your response and any code changes needed.
""",

    # ── GitLab ──────────────────────────────────────────────────────────────
    f"gitlab.{TemplateAction.ISSUE_PROMPT.value}": """\
You are an expert software engineer tasked with resolving a GitLab issue.

## Project
**Project:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Issue Description
{{ issue_body }}

## Labels
{% if issue_labels %}{{ issue_labels | join(', ') }}{% else %}None{% endif %}

## Assignees
{% if issue_assignees %}{{ issue_assignees | join(', ') }}{% else %}Unassigned{% endif %}

## Instructions
1. Analyze the issue and understand the problem.
2. Identify affected files in the repository.
3. Implement a minimal fix.
4. Verify the fix does not break existing functionality.

Provide the complete resolution with code changes.
""",

    f"gitlab.{TemplateAction.PR_UPDATE_PROMPT.value}": """\
You are an expert software engineer updating a GitLab Merge Request.

## Project
**Project:** {{ repo_id }}
**MR !{{ pr_number }}:** {{ pr_title }}

## MR Description
{{ pr_body }}

## Branch Information
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}
- **Changed Files:** {{ changed_files }}

## Instructions
1. Review the current MR changes.
2. Address review feedback.
3. Ensure CI/CD pipelines pass.
4. Make minimal, targeted improvements.

Provide the exact modifications required.
""",

    f"gitlab.{TemplateAction.MERGE_CONFLICT_PROMPT.value}": """\
You are an expert software engineer resolving merge conflicts in a GitLab Merge Request.

## Project
**Project:** {{ repo_id }}
**MR !{{ pr_number }}:** {{ pr_title }}

## Branches
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}

## Conflicting Files
{{ conflict_files }}

## Instructions
1. Analyze the merge conflicts.
2. Resolve by intelligently combining both branch changes.
3. Ensure resolved code is correct and tests pass.

Provide the resolved file contents.
""",

    f"gitlab.{TemplateAction.ISSUE_COMMENT_PROMPT.value}": """\
You are an expert software engineer responding to a GitLab issue comment.

## Project
**Project:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Comment by {{ comment_author }}
{{ comment_body }}

## Instructions
1. Understand the comment in context.
2. Take appropriate action (implement changes, clarify, etc.).
3. Provide your response and any code modifications.

Provide your response and any code changes needed.
""",

    # ── Azure DevOps ────────────────────────────────────────────────────────
    f"azure_devops.{TemplateAction.ISSUE_PROMPT.value}": """\
You are an expert software engineer tasked with resolving an Azure DevOps work item.

## Project
**Project:** {{ repo_id }}
**Work Item #{{ issue_number }}:** {{ issue_title }}

## Description
{{ issue_body }}

## Tags
{% if issue_labels %}{{ issue_labels | join(', ') }}{% else %}None{% endif %}

## Assigned To
{% if issue_assignees %}{{ issue_assignees | join(', ') }}{% else %}Unassigned{% endif %}

## Instructions
1. Analyze the work item and understand requirements.
2. Identify affected code files.
3. Implement the required changes.
4. Ensure existing tests pass.

Provide the resolution with detailed code changes.
""",

    f"azure_devops.{TemplateAction.PR_UPDATE_PROMPT.value}": """\
You are an expert software engineer updating an Azure DevOps Pull Request.

## Project
**Project:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## PR Description
{{ pr_body }}

## Branch Information
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}
- **Changed Files:** {{ changed_files }}

## Instructions
1. Review the PR changes.
2. Address any policy violations or review comments.
3. Ensure build pipelines pass.
4. Make targeted improvements.

Provide the required modifications.
""",

    f"azure_devops.{TemplateAction.MERGE_CONFLICT_PROMPT.value}": """\
You are an expert software engineer resolving merge conflicts in an Azure DevOps Pull Request.

## Project
**Project:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## Branches
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}

## Conflicting Files
{{ conflict_files }}

## Instructions
1. Analyze the merge conflicts.
2. Resolve by combining both branch changes intelligently.
3. Verify resolved code correctness.

Provide the resolved file contents.
""",

    f"azure_devops.{TemplateAction.ISSUE_COMMENT_PROMPT.value}": """\
You are an expert software engineer responding to an Azure DevOps work item comment.

## Project
**Project:** {{ repo_id }}
**Work Item #{{ issue_number }}:** {{ issue_title }}

## Comment by {{ comment_author }}
{{ comment_body }}

## Instructions
1. Understand the comment and its context.
2. Take appropriate action based on the comment.
3. Respond constructively.

Provide your response and any code changes needed.
""",

    # ── Bitbucket ───────────────────────────────────────────────────────────
    f"bitbucket.{TemplateAction.ISSUE_PROMPT.value}": """\
You are an expert software engineer tasked with resolving a Bitbucket issue.

## Repository
**Repo:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Issue Description
{{ issue_body }}

## Labels
{% if issue_labels %}{{ issue_labels | join(', ') }}{% else %}None{% endif %}

## Assignees
{% if issue_assignees %}{{ issue_assignees | join(', ') }}{% else %}Unassigned{% endif %}

## Instructions
1. Analyze the issue carefully.
2. Identify the root cause.
3. Implement a minimal, focused fix.
4. Ensure no regressions.

Provide the resolution with code changes.
""",

    f"bitbucket.{TemplateAction.PR_UPDATE_PROMPT.value}": """\
You are an expert software engineer updating a Bitbucket Pull Request.

## Repository
**Repo:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## PR Description
{{ pr_body }}

## Branch Information
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}
- **Changed Files:** {{ changed_files }}

## Instructions
1. Review the current PR changes.
2. Address review feedback.
3. Ensure pipelines pass.
4. Make targeted improvements.

Provide the required modifications.
""",

    f"bitbucket.{TemplateAction.MERGE_CONFLICT_PROMPT.value}": """\
You are an expert software engineer resolving merge conflicts in a Bitbucket Pull Request.

## Repository
**Repo:** {{ repo_id }}
**PR #{{ pr_number }}:** {{ pr_title }}

## Branches
- **Source:** {{ source_branch }}
- **Target:** {{ target_branch }}

## Conflicting Files
{{ conflict_files }}

## Instructions
1. Analyze the merge conflicts.
2. Resolve by intelligently combining changes.
3. Verify the resolution is correct.

Provide the resolved file contents.
""",

    f"bitbucket.{TemplateAction.ISSUE_COMMENT_PROMPT.value}": """\
You are an expert software engineer responding to a Bitbucket issue comment.

## Repository
**Repo:** {{ repo_id }}
**Issue #{{ issue_number }}:** {{ issue_title }}

## Comment by {{ comment_author }}
{{ comment_body }}

## Instructions
1. Understand the comment and its context.
2. Take appropriate action.
3. Respond constructively.

Provide your response and any code changes needed.
""",
}

# ──────────────────────────────────────────────────────────────────────────────
# Template cache entry
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    """Cached compiled template with metadata."""

    template: Any  # Jinja2 Template or _DummyTemplate
    source_hash: str
    compiled_at: float
    validation_errors: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# PromptTemplateManager
# ──────────────────────────────────────────────────────────────────────────────


class PromptTemplateManager:
    """
    Thread-safe manager for Jinja2 prompt templates.

    Features:
      - Built-in defaults for all provider/action combinations.
      - Custom template overlay from filesystem or direct registration.
      - Template validation against required variables.
      - In-memory caching with source-hash invalidation.

    Usage::

        mgr = PromptTemplateManager()

        # Render an issue prompt for GitHub
        prompt = mgr.render(
            provider="github",
            action=TemplateAction.ISSUE_PROMPT,
            variables={
                "repo_id": "owner/repo",
                "issue_title": "Bug in login",
                "issue_body": "Login fails when...",
                "issue_number": 42,
                "issue_labels": ["bug"],
                "issue_assignees": ["alice"],
            },
        )

        # Register a custom template
        mgr.register_template(
            provider="github",
            action=TemplateAction.ISSUE_PROMPT,
            template_string="Custom: {{ issue_title }} - {{ issue_body }}",
        )
    """

    def __init__(self, template_dir: Optional[Path] = None) -> None:
        self._template_dir = template_dir
        self._custom_templates: Dict[str, str] = {}
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()
        self._env = self._create_env()

    # ── Environment setup ──────────────────────────────────────────────────

    @staticmethod
    def _create_env() -> Any:
        """Create a Jinja2 Environment (or dummy)."""
        if _JINJA2_AVAILABLE:
            return Environment(
                loader=BaseLoader(),
                autoescape=False,
                keep_trailing_newline=True,
                trim_blocks=True,
                lstrip_blocks=True,
            )
        return _make_env()

    # ── Template key helper ────────────────────────────────────────────────

    @staticmethod
    def _key(provider: str, action: TemplateAction) -> str:
        return f"{provider}.{action.value}"

    # ── Template retrieval ─────────────────────────────────────────────────

    def _get_template_source(self, provider: str, action: TemplateAction) -> str:
        """Return the raw template string, checking custom then defaults."""
        key = self._key(provider, action)

        # 1. Check custom registered templates
        with self._lock:
            if key in self._custom_templates:
                return self._custom_templates[key]

        # 2. Check filesystem overlay
        if self._template_dir and self._template_dir.is_dir():
            file_path = self._template_dir / f"{key}.j2"
            if file_path.is_file():
                try:
                    return file_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "integrations.templates.fs_read_failed key=%s err=%s",
                        key, exc,
                    )

        # 3. Fall back to built-in defaults
        if key in _DEFAULT_TEMPLATES:
            return _DEFAULT_TEMPLATES[key]

        # 4. Generic fallback
        logger.warning(
            "integrations.templates.no_template key=%s using_generic", key
        )
        return "{{ issue_body or pr_body or comment_body or '' }}"

    # ── Compile with caching ───────────────────────────────────────────────

    def _compile(self, provider: str, action: TemplateAction) -> _CacheEntry:
        """Compile and cache a template, validating required variables."""
        key = self._key(provider, action)
        source = self._get_template_source(provider, action)
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()

        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.source_hash == source_hash:
                return cached

        # Compile the template
        validation_errors: List[str] = []
        try:
            compiled = self._env.from_string(source)
        except Exception as exc:
            validation_errors.append(f"Template compile error: {exc}")
            # Create a safe fallback template
            compiled = self._env.from_string("{{ issue_body or pr_body or '' }}")

        # Validate required variables
        required = _REQUIRED_VARS.get(action, set())
        if _JINJA2_AVAILABLE and required:
            try:
                from jinja2 import meta as jinja2_meta
                ast = self._env.parse(source)
                referenced = jinja2_meta.find_undeclared_variables(ast)
                missing = required - referenced
                if missing:
                    validation_errors.append(
                        f"Template may be missing variables: {sorted(missing)}"
                    )
            except Exception:
                pass  # Best-effort validation

        entry = _CacheEntry(
            template=compiled,
            source_hash=source_hash,
            compiled_at=time.time(),
            validation_errors=validation_errors,
        )

        with self._lock:
            self._cache[key] = entry

        if validation_errors:
            for err in validation_errors:
                logger.warning(
                    "integrations.templates.validation key=%s err=%s", key, err
                )

        return entry

    # ── Public API ─────────────────────────────────────────────────────────

    def render(
        self,
        provider: str,
        action: TemplateAction,
        variables: Dict[str, Any],
    ) -> str:
        """
        Render a prompt template for the given provider and action.

        Args:
            provider: Git provider name (github, gitlab, azure_devops, bitbucket).
            action: The template action to render.
            variables: Dictionary of template variables.

        Returns:
            Rendered prompt string.

        Raises:
            ValueError: If required variables are missing.
        """
        # Validate required variables
        required = _REQUIRED_VARS.get(action, set())
        missing = required - set(variables.keys())
        if missing:
            raise ValueError(
                f"Missing required template variables for {action.value}: "
                f"{sorted(missing)}"
            )

        entry = self._compile(provider, action)

        try:
            return entry.template.render(**variables)
        except Exception as exc:
            logger.error(
                "integrations.templates.render_failed provider=%s action=%s err=%s",
                provider, action.value, exc,
            )
            # Fallback: try to render a simple summary
            return self._fallback_render(action, variables, exc)

    async def render_async(
        self,
        provider: str,
        action: TemplateAction,
        variables: Dict[str, Any],
    ) -> str:
        """Async variant of :meth:`render`."""
        import asyncio
        return await asyncio.to_thread(
            self.render, provider, action, variables
        )

    def register_template(
        self,
        provider: str,
        action: TemplateAction,
        template_string: str,
    ) -> None:
        """
        Register a custom template string, overriding the default.

        Args:
            provider: Git provider name.
            action: Template action.
            template_string: Jinja2 template source.

        Raises:
            ValueError: If the template string is invalid.
        """
        # Validate by attempting to compile
        try:
            self._env.from_string(template_string)
        except Exception as exc:
            raise ValueError(f"Invalid template: {exc}") from exc

        key = self._key(provider, action)
        with self._lock:
            self._custom_templates[key] = template_string
            # Invalidate cache entry
            self._cache.pop(key, None)

        logger.info(
            "integrations.templates.registered provider=%s action=%s",
            provider, action.value,
        )

    def validate_template(
        self,
        provider: str,
        action: TemplateAction,
    ) -> List[str]:
        """
        Validate a template and return any errors.

        Returns:
            List of validation error strings (empty if valid).
        """
        entry = self._compile(provider, action)
        return list(entry.validation_errors)

    def get_available_actions(self, provider: str) -> List[TemplateAction]:
        """Return all actions that have templates for the given provider."""
        actions: List[TemplateAction] = []
        for action in TemplateAction:
            key = self._key(provider, action)
            if key in _DEFAULT_TEMPLATES or key in self._custom_templates:
                actions.append(action)
        return actions

    def clear_cache(self) -> None:
        """Clear the compiled template cache."""
        with self._lock:
            self._cache.clear()

    def reset_custom_templates(self) -> None:
        """Remove all custom template overrides and clear cache."""
        with self._lock:
            self._custom_templates.clear()
            self._cache.clear()

    # ── Fallback rendering ─────────────────────────────────────────────────

    @staticmethod
    def _fallback_render(
        action: TemplateAction,
        variables: Dict[str, Any],
        original_error: Exception,
    ) -> str:
        """Produce a minimal prompt when template rendering fails."""
        parts = [
            f"Error rendering template: {original_error}",
            "",
            f"Action: {action.value}",
        ]
        if "issue_title" in variables:
            parts.append(f"Issue: {variables['issue_title']}")
        if "issue_body" in variables:
            parts.append(f"Description: {variables['issue_body']}")
        if "pr_title" in variables:
            parts.append(f"PR: {variables['pr_title']}")
        if "pr_body" in variables:
            parts.append(f"PR Description: {variables['pr_body']}")
        if "comment_body" in variables:
            parts.append(f"Comment: {variables['comment_body']}")
        if "repo_id" in variables:
            parts.append(f"Repository: {variables['repo_id']}")
        parts.append("")
        parts.append("Please resolve the above based on the available context.")
        return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

template_manager = PromptTemplateManager()
