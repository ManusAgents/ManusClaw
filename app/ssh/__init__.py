"""ManusClaw SSH Module.

Provides SSH remote gateway control with a restricted shell interface.
Allows authenticated SSH clients to run a limited set of ManusClaw management
commands (status, restart, logs, agent, channels list, cron list).

Classes:
    RestrictedShell — parses, validates, and maps SSH commands to internal APIs
"""
from __future__ import annotations

from app.ssh.shell import RestrictedShell

__all__ = ["RestrictedShell"]
