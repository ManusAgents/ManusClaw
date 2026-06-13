"""
Alembic Migration Environment
================================

Configures Alembic to use manusclaw's configuration system for database
connection.  Supports both online migrations (connected to the database)
and offline migrations (SQL script generation).

This file is referenced by ``alembic.ini`` as the ``script_location.env``
module.

Configuration sources (in priority order):
    1. ``MANUSCLAW_DB_URL`` environment variable
    2. manusclaw's :class:`app.config.Config` (reads from config files)
    3. Default SQLite path: ``workspace/.sessions/manusclaw.db``

Usage::

    # Online migration (connects to DB)
    alembic -c app/migrations/alembic.ini upgrade head

    # Offline migration (generates SQL)
    alembic -c app/migrations/alembic.ini upgrade head --sql
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so that ``app.*`` imports work
# ---------------------------------------------------------------------------

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to values in alembic.ini
# ---------------------------------------------------------------------------

config = context.config

# ---------------------------------------------------------------------------
# Interpret the config file for Python logging
# ---------------------------------------------------------------------------

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass  # Non-fatal: logging setup failure shouldn't block migrations

# ---------------------------------------------------------------------------
# Database URL resolution
# ---------------------------------------------------------------------------


def _get_database_url() -> str:
    """Resolve the database URL from environment, config, or default.

    Priority:
        1. ``MANUSCLAW_DB_URL`` environment variable
        2. ``sqlalchemy.url`` in alembic.ini
        3. manusclaw's config system
        4. Default SQLite path

    Returns:
        A SQLAlchemy-compatible database URL string.
    """
    # 1. Environment variable (highest priority)
    env_url = os.getenv("MANUSCLAW_DB_URL", "")
    if env_url:
        return env_url

    # 2. alembic.ini value
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url and not ini_url.startswith("%"):
        return ini_url

    # 3. manusclaw config
    try:
        from app.config import Config

        cfg = Config.get()
        # Try to get a DB URL from config — currently manusclaw uses SQLite
        db_path = os.path.join(cfg.workspace_dir, ".sessions", "manusclaw.db")
        return f"sqlite:///{db_path}"
    except Exception:
        pass

    # 4. Default
    default_path = os.path.join("workspace", ".sessions", "manusclaw.db")
    return f"sqlite:///{default_path}"


# Override sqlalchemy.url in the config
config.set_main_option("sqlalchemy.url", _get_database_url())

# ---------------------------------------------------------------------------
# Target metadata for autogenerate
# ---------------------------------------------------------------------------

target_metadata = None

# Try to import the app's models to get metadata for autogenerate
try:
    # If using SQLAlchemy models in the future, import the Base here:
    # from app.models.base import Base
    # target_metadata = Base.metadata
    pass
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Offline migration — generate SQL scripts
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.  Calls to
    ``context.execute()`` emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite-specific: render as transactional DDL
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration — connect to the database
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    from sqlalchemy import create_engine

    url = config.get_main_option("sqlalchemy.url")

    # SQLite-specific connect args
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite requires batch mode for ALTER TABLE operations
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
