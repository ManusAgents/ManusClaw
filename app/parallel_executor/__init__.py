"""
Parallel Executor Package
=========================
Enterprise-grade parallel tool execution with resource-aware scheduling.

This module provides the components needed to execute multiple tool calls
concurrently while ensuring that operations on shared resources are
properly serialised:

* :class:`DeclaredResources` / :class:`ResourceDeclaration` — tools declare
  what resources they need and how (READ vs WRITE).
* :class:`ResourceLockManager` — fine-grained readers-writer locking with
  deadlock detection.
* :class:`ParallelToolExecutor` — thread-pool–based concurrent executor.
* :class:`AsyncParallelToolExecutor` — ``asyncio``-based concurrent executor.

Quick start (synchronous)::

    from app.parallel_executor import (
        ParallelToolExecutor, ToolCall, DeclaredResources, AccessMode,
    )

    executor = ParallelToolExecutor(max_workers=4)

    calls = [
        ToolCall("read_config", read_config_fn,
                 declared_resources=DeclaredResources.file_resource("/etc/app.cfg")),
        ToolCall("fetch_data", fetch_data_fn,
                 declared_resources=DeclaredResources.network_resource("api.example.com")),
    ]

    results, metrics = executor.execute(calls)
    for r in results:
        print(r.tool_name, r.status, r.result)

Quick start (asynchronous)::

    from app.parallel_executor import AsyncParallelToolExecutor, ToolCall

    executor = AsyncParallelToolExecutor(max_concurrency=4)
    results, metrics = await executor.execute(calls)
"""

from app.parallel_executor.declared_resources import (
    AccessMode,
    DeclaredResources,
    ResourceDeclaration,
)
from app.parallel_executor.executor import (
    AsyncParallelToolExecutor,
    AsyncProgressCallback,
    ExecutionMetrics,
    ParallelToolExecutor,
    ProgressCallback,
    ProgressEvent,
    ProgressEventType,
    ToolCall,
    ToolCallStatus,
    ToolResult,
)
from app.parallel_executor.resource_lock import (
    DeadlockDetectedError,
    LockAcquisitionError,
    ResourceLockError,
    ResourceLockHandle,
    ResourceLockManager,
)

__all__ = [
    # Declared resources
    "AccessMode",
    "DeclaredResources",
    "ResourceDeclaration",
    # Resource locking
    "DeadlockDetectedError",
    "LockAcquisitionError",
    "ResourceLockError",
    "ResourceLockHandle",
    "ResourceLockManager",
    # Execution
    "AsyncParallelToolExecutor",
    "AsyncProgressCallback",
    "ExecutionMetrics",
    "ParallelToolExecutor",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressEventType",
    "ToolCall",
    "ToolCallStatus",
    "ToolResult",
]
