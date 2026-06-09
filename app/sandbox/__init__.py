"""Sandbox backends for isolated code execution.

Available backends:
  - DockerSandbox: Docker container isolation (default)
  - SshSandbox: Remote execution via SSH
  - OpenShellSandbox: Linux namespace isolation (Linux only)

Use the factory to create the appropriate backend:

    from app.sandbox.factory import create_sandbox
    sandbox = create_sandbox()
"""

from app.sandbox.docker import DockerSandbox, DaytonaSandbox
from app.sandbox.factory import create_sandbox, list_available_backends
from app.sandbox.ssh import SshSandbox
from app.sandbox.openshell import OpenShellSandbox

__all__ = [
    "DockerSandbox",
    "DaytonaSandbox",
    "SshSandbox",
    "OpenShellSandbox",
    "create_sandbox",
    "list_available_backends",
]
