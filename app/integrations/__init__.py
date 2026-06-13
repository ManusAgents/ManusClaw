from __future__ import annotations

"""
ManusClaw Integrations Module
==============================
Enterprise-grade integrations for the ManusClaw platform.

Sub-modules:
  - **templates**: Jinja2 prompt template system for the resolver.
  - **resolver**: LLM-powered issue/PR resolver.
  - **webhook_handler**: Webhook handler for Git provider events.
  - **linear**: Linear issue tracking integration.
  - **jira**: Jira Cloud/DC issue tracking integration.
  - **slack**: Slack Bot integration for agent interaction.

Quick start::

    from app.integrations import IssueResolver, ResolutionRequest, ResolutionType

    resolver = IssueResolver()
    result = await resolver.resolve_issue(
        provider="github",
        repo_id="owner/repo",
        issue_number=42,
        auto_apply=True,
    )

    from app.integrations import webhook_handler, WebhookEventType

    webhook_handler.on(WebhookEventType.ISSUES, my_handler)
    await webhook_handler.start_processing()
"""

# ──────────────────────────────────────────────────────────────────────────────
# Templates — always available (Jinja2 optional)
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.templates import (
    GitProvider as TemplateGitProvider,
    PromptTemplateManager,
    TemplateAction,
    template_manager,
)

# ──────────────────────────────────────────────────────────────────────────────
# Resolver — always available (LLM loaded lazily)
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.resolver import (
    AuditEntry,
    IssueResolver,
    ResolutionRequest,
    ResolutionResult,
    ResolutionStatus,
    ResolutionType,
    ResolverCancelledError,
    ResolverError,
    ResolverTimeoutError,
    issue_resolver,
)

# ──────────────────────────────────────────────────────────────────────────────
# Webhook Handler — always available
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.webhook_handler import (
    DeduplicationStore,
    EventNormalizer,
    EventProcessingStatus,
    ProcessingResult,
    SignatureVerifier,
    WebhookEvent,
    WebhookEventType,
    WebhookHandler,
    WebhookProvider,
    resolver_webhook_handler,
    webhook_handler,
)

# ──────────────────────────────────────────────────────────────────────────────
# Linear Integration — always available (aiohttp loaded lazily)
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.linear import (
    LinearClient,
    LinearComment,
    LinearIssue,
    LinearIssueState,
    LinearPriority,
    LinearSuggestedTask,
    LinearTeam,
    LinearWebhookEvent,
)

# ──────────────────────────────────────────────────────────────────────────────
# Jira Integration — always available (aiohttp loaded lazily)
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.jira import (
    JiraAuthType,
    JiraClient,
    JiraComment,
    JiraIssue,
    JiraIssueType,
    JiraPriority,
    JiraProject,
    JiraSuggestedTask,
    JiraTransition,
    JiraTransitionItem,
    JiraWebhookEvent,
)

# ──────────────────────────────────────────────────────────────────────────────
# Slack Integration — always available (slack-bolt loaded lazily)
# ──────────────────────────────────────────────────────────────────────────────

from app.integrations.slack import (
    BlockKit,
    InteractionPayload,
    InteractionType,
    SlackConversation,
    SlackIntegration,
    SlackMessage,
    SlackEventType,
    SlashCommand,
    SlashCommandPayload,
    slack_integration,
)

# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Templates
    "TemplateAction",
    "TemplateGitProvider",
    "PromptTemplateManager",
    "template_manager",
    # Resolver
    "AuditEntry",
    "IssueResolver",
    "ResolutionRequest",
    "ResolutionResult",
    "ResolutionStatus",
    "ResolutionType",
    "ResolverCancelledError",
    "ResolverError",
    "ResolverTimeoutError",
    "issue_resolver",
    # Webhook Handler
    "DeduplicationStore",
    "EventNormalizer",
    "EventProcessingStatus",
    "ProcessingResult",
    "SignatureVerifier",
    "WebhookEvent",
    "WebhookEventType",
    "WebhookHandler",
    "WebhookProvider",
    "resolver_webhook_handler",
    "webhook_handler",
    # Linear
    "LinearClient",
    "LinearComment",
    "LinearIssue",
    "LinearIssueState",
    "LinearPriority",
    "LinearSuggestedTask",
    "LinearTeam",
    "LinearWebhookEvent",
    # Jira
    "JiraAuthType",
    "JiraClient",
    "JiraComment",
    "JiraIssue",
    "JiraIssueType",
    "JiraPriority",
    "JiraProject",
    "JiraSuggestedTask",
    "JiraTransition",
    "JiraTransitionItem",
    "JiraWebhookEvent",
    # Slack
    "BlockKit",
    "InteractionPayload",
    "InteractionType",
    "SlackConversation",
    "SlackEventType",
    "SlackIntegration",
    "SlackMessage",
    "SlashCommand",
    "SlashCommandPayload",
    "slack_integration",
]
