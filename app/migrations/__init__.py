"""
ManusClaw Database Migrations
================================

Alembic-based database migration infrastructure for manusclaw.

This package provides:
    - Standard Alembic setup with auto-generation support
    - Environment that uses manusclaw's config for DB connection
    - Support for both online and offline migrations
    - Initial migration creating core tables

Usage::

    # Generate a new migration
    cd /path/to/manusclaw
    alembic -c app/migrations/alembic.ini revision --autogenerate -m "description"

    # Apply migrations
    alembic -c app/migrations/alembic.ini upgrade head

    # Rollback one step
    alembic -c app/migrations/alembic.ini downgrade -1

    # Check current version
    alembic -c app/migrations/alembic.ini current
"""
