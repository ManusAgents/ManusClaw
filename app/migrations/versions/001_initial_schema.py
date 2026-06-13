"""Initial schema — core manusclaw tables

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000

Creates the core tables used by manusclaw:
    - conversations: Agent conversation sessions
    - events: Event log for conversation turns
    - sessions: User/agent session tracking
    - tasks: Task execution records
    - credentials: LLM API credential storage
    - secrets: Encrypted secret storage
    - audit_log: Audit trail for security events
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# Revision identifiers
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create initial schema tables."""

    # ------------------------------------------------------------------
    # conversations — Agent conversation sessions
    # ------------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=True, index=True),
        sa.Column("agent_name", sa.String(100), nullable=False, server_default="manus"),
        sa.Column("mode", sa.String(20), nullable=False, server_default="build"),
        sa.Column("goal", sa.Text, nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="running"),
        sa.Column("parent_conversation_id", sa.String(36), nullable=True),
        sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("compressed", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime, nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_conversations_parent",
        "conversations",
        ["parent_conversation_id"],
    )
    op.create_index(
        "ix_conversations_state",
        "conversations",
        ["state"],
    )
    op.create_index(
        "ix_conversations_created_at",
        "conversations",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # events — Event log for conversation turns
    # ------------------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(36), nullable=False, index=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("role", sa.String(20), nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("tool_name", sa.String(100), nullable=True),
        sa.Column("tool_args", sa.Text, nullable=True),
        sa.Column("tool_output", sa.Text, nullable=True),
        sa.Column("tool_error", sa.Text, nullable=True),
        sa.Column("tool_success", sa.Boolean, nullable=True),
        sa.Column("step", sa.Integer, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("metadata_json", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_events_conversation_step",
        "events",
        ["conversation_id", "step"],
    )
    op.create_index(
        "ix_events_type",
        "events",
        ["event_type"],
    )
    op.create_index(
        "ix_events_created_at",
        "events",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # sessions — User/agent session tracking
    # ------------------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=True, index=True),
        sa.Column("conversation_id", sa.String(36), nullable=True, index=True),
        sa.Column("agent_name", sa.String(100), nullable=False, server_default="manus"),
        sa.Column("mode", sa.String(20), nullable=False, server_default="build"),
        sa.Column("state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("parent_session_id", sa.String(36), nullable=True),
        sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("compressed", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime, nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_sessions_parent",
        "sessions",
        ["parent_session_id"],
    )
    op.create_index(
        "ix_sessions_state",
        "sessions",
        ["state"],
    )
    op.create_index(
        "ix_sessions_created_at",
        "sessions",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # tasks — Task execution records
    # ------------------------------------------------------------------
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=False, index=True),
        sa.Column("session_id", sa.String(36), nullable=True, index=True),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("assigned_agent", sa.String(100), nullable=True),
        sa.Column("parent_task_id", sa.String(36), nullable=True),
        sa.Column("step_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_steps", sa.Integer, nullable=False, server_default="30"),
        sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="3600"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("success_score", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("deadline_at", sa.DateTime, nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_tasks_status",
        "tasks",
        ["status"],
    )
    op.create_index(
        "ix_tasks_parent",
        "tasks",
        ["parent_task_id"],
    )
    op.create_index(
        "ix_tasks_priority",
        "tasks",
        ["priority"],
    )
    op.create_index(
        "ix_tasks_created_at",
        "tasks",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # credentials — LLM API credential storage
    # ------------------------------------------------------------------
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("encrypted_api_key", sa.Text, nullable=True),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rate_limit_rpm", sa.Integer, nullable=True),
        sa.Column("rate_limit_tpm", sa.Integer, nullable=True),
        sa.Column("total_requests", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_error_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_credentials_provider",
        "credentials",
        ["provider"],
    )
    op.create_index(
        "ix_credentials_active",
        "credentials",
        ["is_active"],
    )
    op.create_index(
        "ix_credentials_name",
        "credentials",
        ["name"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # secrets — Encrypted secret storage
    # ------------------------------------------------------------------
    op.create_table(
        "secrets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("encrypted_value", sa.Text, nullable=False),
        sa.Column("scope", sa.String(50), nullable=False, server_default="global"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("last_accessed_at", sa.DateTime, nullable=True),
        sa.Column("access_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_secrets_key_scope",
        "secrets",
        ["key", "scope"],
        unique=True,
    )
    op.create_index(
        "ix_secrets_scope",
        "secrets",
        ["scope"],
    )

    # ------------------------------------------------------------------
    # audit_log — Audit trail for security events
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(50), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="success"),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("details_json", sa.Text, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_audit_event_type",
        "audit_log",
        ["event_type"],
    )
    op.create_index(
        "ix_audit_actor",
        "audit_log",
        ["actor"],
    )
    op.create_index(
        "ix_audit_resource",
        "audit_log",
        ["resource_type", "resource_id"],
    )
    op.create_index(
        "ix_audit_severity",
        "audit_log",
        ["severity"],
    )


def downgrade() -> None:
    """Drop all tables created in the initial schema."""
    op.drop_table("audit_log")
    op.drop_table("secrets")
    op.drop_table("credentials")
    op.drop_table("tasks")
    op.drop_table("sessions")
    op.drop_table("events")
    op.drop_table("conversations")
