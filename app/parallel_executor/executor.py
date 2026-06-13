"""
Parallel Tool Executor
======================
Execute multiple tool calls concurrently when their declared resources do not
conflict, and serialise them when they do.

The executor uses a *resource-aware scheduling* strategy:

1. Each tool call declares its required resources via :class:`DeclaredResources`.
2. Calls whose resource sets are **mutually non-conflicting** run in parallel.
3. Calls that **conflict** on at least one resource are placed in the same
   *scheduling group* and executed serially within that group.
4. Scheduling groups themselves run in parallel.

Both synchronous (thread-pool) and asynchronous (``asyncio.gather``) variants
are provided.

Progress & metrics
------------------
A callback can be supplied to receive progress events (start, complete,
error) for each tool call.  After execution finishes, a
:class:`ExecutionMetrics` object is available with timing, concurrency, and
conflict statistics.
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    runtime_checkable,
)

from app.parallel_executor.declared_resources import (
    AccessMode,
    DeclaredResources,
    ResourceDeclaration,
)
from app.parallel_executor.resource_lock import (
    DeadlockDetectedError,
    ResourceLockHandle,
    ResourceLockManager,
)


# ──────────────────────────────────────────────────────────────────────────────
# Public types & protocols
# ──────────────────────────────────────────────────────────────────────────────

T = TypeVar("T")


@runtime_checkable
class ToolCallable(Protocol):
    """Minimal protocol for a synchronous tool function."""

    def __call__(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class AsyncToolCallable(Protocol):
    """Minimal protocol for an asynchronous tool function."""

    async def __call__(self, **kwargs: Any) -> Any: ...


class ToolCallStatus(enum.Enum):
    """Lifecycle status of a single tool call."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class ProgressEventType(enum.Enum):
    """Types of progress events emitted during execution."""

    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    GROUP_STARTED = "GROUP_STARTED"
    GROUP_COMPLETED = "GROUP_COMPLETED"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """An event emitted by the executor during a run."""

    event_type: ProgressEventType
    call_id: str
    tool_name: str
    timestamp: float
    detail: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Tool call descriptor
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ToolCall:
    """Descriptor for a single tool invocation submitted to the executor.

    Attributes
    ----------
    call_id:
        Unique identifier assigned automatically if not provided.
    tool_name:
        Human-readable name for logging and progress.
    fn:
        The callable to execute (sync or async depending on executor variant).
    kwargs:
        Keyword arguments forwarded to *fn*.
    declared_resources:
        Resources this call needs, used for scheduling.
    timeout:
        Per-call wall-clock timeout in seconds.  ``None`` means no timeout.
    """

    call_id: str
    tool_name: str
    fn: Any  # Union[ToolCallable, AsyncToolCallable] — we accept any callable
    kwargs: Dict[str, Any]
    declared_resources: DeclaredResources
    timeout: Optional[float] = None

    def __init__(
        self,
        tool_name: str,
        fn: Any,
        kwargs: Optional[Dict[str, Any]] = None,
        declared_resources: Optional[DeclaredResources] = None,
        timeout: Optional[float] = None,
        call_id: Optional[str] = None,
    ) -> None:
        self.call_id = call_id or uuid.uuid4().hex[:12]
        self.tool_name = tool_name
        self.fn = fn
        self.kwargs = kwargs or {}
        self.declared_resources = declared_resources or DeclaredResources.empty()
        self.timeout = timeout


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ToolResult:
    """Outcome of a single tool call execution.

    Attributes
    ----------
    call_id:
        Matches the :attr:`ToolCall.call_id`.
    tool_name:
        Name of the tool that was executed.
    status:
        Final status (COMPLETED, FAILED, TIMED_OUT).
    result:
        Return value of the tool on success.
    error:
        Exception instance on failure.
    execution_time:
        Wall-clock seconds spent executing (excluding scheduling wait).
    """

    call_id: str
    tool_name: str
    status: ToolCallStatus
    result: Any = None
    error: Optional[BaseException] = None
    execution_time: float = 0.0

    @property
    def success(self) -> bool:
        return self.status is ToolCallStatus.COMPLETED


# ──────────────────────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ExecutionMetrics:
    """Aggregate metrics for a batch execution.

    All times are in seconds.
    """

    total_wall_time: float = 0.0
    total_tool_time: float = 0.0
    max_concurrency: int = 0
    num_tool_calls: int = 0
    num_succeeded: int = 0
    num_failed: int = 0
    num_timed_out: int = 0
    num_scheduling_groups: int = 0
    num_resource_conflicts: int = 0

    @property
    def efficiency_ratio(self) -> float:
        """Ratio of total tool time to wall time.  Values > 1 indicate
        effective parallelism."""
        if self.total_wall_time <= 0:
            return 0.0
        return self.total_tool_time / self.total_wall_time

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ExecutionMetrics(wall={self.total_wall_time:.3f}s, "
            f"tool={self.total_tool_time:.3f}s, "
            f"concurrency={self.max_concurrency}, "
            f"ok={self.num_succeeded}/{self.num_tool_calls}, "
            f"conflicts={self.num_resource_conflicts}, "
            f"efficiency={self.efficiency_ratio:.2f}x)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Progress callback type
# ──────────────────────────────────────────────────────────────────────────────

ProgressCallback = Callable[[ProgressEvent], None]
AsyncProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


# ──────────────────────────────────────────────────────────────────────────────
# Scheduling group builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_scheduling_groups(
    calls: Sequence[ToolCall],
) -> Tuple[List[List[ToolCall]], int]:
    """Partition tool calls into groups that can execute in parallel.

    Two calls are placed in the **same** group if they conflict on any
    declared resource.  Groups are built using a greedy graph-colouring
    approach (not optimal but fast and deterministic).

    Returns
    -------
    groups :
        List of groups.  Each group is a list of calls that must be
        serialised.
    conflict_count :
        Number of pairwise resource conflicts detected.
    """
    if not calls:
        return [], 0

    # Each call is assigned to a group index.  We use a greedy approach:
    # for each call, find the first existing group with no conflict, or
    # create a new group.
    groups: List[List[ToolCall]] = []
    group_resources: List[DeclaredResources] = []
    conflict_count = 0

    for call in calls:
        placed = False
        for idx, group_res in enumerate(group_resources):
            if group_res.conflicts_with(call.declared_resources):
                conflict_count += 1
            else:
                # No conflict — add to this group
                groups[idx].append(call)
                group_resources[idx] = group_res.union(call.declared_resources)
                placed = True
                break

        if not placed:
            groups.append([call])
            group_resources.append(call.declared_resources)

    return groups, conflict_count


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous Parallel Tool Executor
# ──────────────────────────────────────────────────────────────────────────────

class ParallelToolExecutor:
    """Execute multiple :class:`ToolCall` objects concurrently with
    resource-aware scheduling.

    Parameters
    ----------
    max_workers:
        Maximum number of threads in the underlying pool.  Defaults to 4.
    default_timeout:
        Per-call wall-clock timeout.  ``None`` means no timeout.
    lock_manager:
        Optional shared :class:`ResourceLockManager`.  If not supplied a
        fresh one is created.
    on_progress:
        Optional callback invoked for :class:`ProgressEvent` instances.
    """

    def __init__(
        self,
        max_workers: int = 4,
        default_timeout: Optional[float] = None,
        lock_manager: Optional[ResourceLockManager] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        self._max_workers = max_workers
        self._default_timeout = default_timeout
        self._lock_manager = lock_manager or ResourceLockManager()
        self._on_progress = on_progress

        # Concurrency tracking (guarded by _concurrency_lock)
        self._concurrency_lock = threading.Lock()
        self._active_count = 0
        self._max_active_seen = 0

    # ── public API ───────────────────────────────────────────────────────

    def execute(
        self,
        calls: Sequence[ToolCall],
    ) -> Tuple[List[ToolResult], ExecutionMetrics]:
        """Execute a batch of tool calls with resource-aware parallelism.

        Returns
        -------
        results :
            One :class:`ToolResult` per input call, in the **same order**
            as the input *calls*.
        metrics :
            Aggregate :class:`ExecutionMetrics`.
        """
        if not calls:
            return [], ExecutionMetrics()

        wall_start = time.monotonic()
        groups, conflict_count = _build_scheduling_groups(calls)

        # Map call_id → result for ordering
        results_by_id: Dict[str, ToolResult] = {}

        # Reset concurrency tracking
        with self._concurrency_lock:
            self._active_count = 0
            self._max_active_seen = 0

        # Submit each call as its own task.  The ResourceLockManager handles
        # serialisation of conflicting operations via acquire_resources(), so
        # non-conflicting calls naturally run in parallel while conflicting
        # calls block on the same lock(s).
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            # Submit one future per call
            call_futures: Dict[str, Future[ToolResult]] = {}
            for call in calls:
                future = pool.submit(self._execute_single, call)
                call_futures[call.call_id] = future

            # Collect results as they complete
            for future in as_completed(call_futures.values()):
                try:
                    result = future.result()
                except Exception:
                    # Defensive: _execute_single catches all errors, but be safe.
                    continue
                results_by_id[result.call_id] = result

        wall_end = time.monotonic()

        # Build ordered result list
        ordered_results = [
            results_by_id.get(
                c.call_id,
                ToolResult(
                    call_id=c.call_id,
                    tool_name=c.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=RuntimeError("Result missing for call"),
                ),
            )
            for c in calls
        ]

        # Build metrics
        metrics = ExecutionMetrics(
            total_wall_time=wall_end - wall_start,
            total_tool_time=sum(r.execution_time for r in ordered_results),
            max_concurrency=self._max_active_seen,
            num_tool_calls=len(calls),
            num_succeeded=sum(1 for r in ordered_results if r.success),
            num_failed=sum(1 for r in ordered_results if r.status is ToolCallStatus.FAILED),
            num_timed_out=sum(1 for r in ordered_results if r.status is ToolCallStatus.TIMED_OUT),
            num_scheduling_groups=len(groups),
            num_resource_conflicts=conflict_count,
        )

        return ordered_results, metrics

    def _execute_single(self, call: ToolCall) -> ToolResult:
        """Execute one tool call with resource locking, timeout, and
        isolation."""
        # Track concurrency
        with self._concurrency_lock:
            self._active_count += 1
            if self._active_count > self._max_active_seen:
                self._max_active_seen = self._active_count

        self._emit(ProgressEventType.STARTED, call.call_id, call.tool_name)

        start = time.monotonic()
        status = ToolCallStatus.COMPLETED
        result_val: Any = None
        error_val: Optional[BaseException] = None

        try:
            effective_timeout = call.timeout or self._default_timeout

            if effective_timeout is not None:
                result_val = self._run_with_timeout(call, effective_timeout)
            else:
                result_val = self._run_with_locks(call)

        except TimeoutError:
            status = ToolCallStatus.TIMED_OUT
            error_val = TimeoutError(
                f"Tool '{call.tool_name}' timed out after "
                f"{effective_timeout}s"
            )
            self._emit(ProgressEventType.TIMED_OUT, call.call_id, call.tool_name)
        except Exception as exc:
            status = ToolCallStatus.FAILED
            error_val = exc
            self._emit(
                ProgressEventType.FAILED,
                call.call_id,
                call.tool_name,
                detail=str(exc),
            )
        finally:
            elapsed = time.monotonic() - start
            with self._concurrency_lock:
                self._active_count -= 1

        if status is ToolCallStatus.COMPLETED:
            self._emit(ProgressEventType.COMPLETED, call.call_id, call.tool_name)

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status=status,
            result=result_val,
            error=error_val,
            execution_time=elapsed,
        )

    # ── lock-aware execution helpers ─────────────────────────────────────

    def _run_with_locks(self, call: ToolCall) -> Any:
        """Acquire declared resources and invoke the tool."""
        with self._lock_manager.acquire_resources(call.declared_resources):
            return call.fn(**call.kwargs)

    def _run_with_timeout(self, call: ToolCall, timeout: float) -> Any:
        """Run the tool with a wall-clock timeout.

        Uses a separate thread for the actual call so we can enforce
        the timeout without relying on the callable being cancellation-aware.
        """
        result_holder: list[Any] = []
        error_holder: list[BaseException] = []

        def _target() -> None:
            try:
                result_holder.append(self._run_with_locks(call))
            except BaseException as exc:
                error_holder.append(exc)

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout=timeout)

        if worker.is_alive():
            # Thread is still running — it's a daemon so it won't prevent
            # exit, but we can't actually kill it.  Mark as timed out.
            raise TimeoutError(
                f"Tool '{call.tool_name}' exceeded {timeout}s timeout"
            )

        if error_holder:
            raise error_holder[0]

        if not result_holder:
            raise RuntimeError(
                f"Tool '{call.tool_name}' returned without a result"
            )

        return result_holder[0]

    # ── progress emission ────────────────────────────────────────────────

    def _emit(
        self,
        event_type: ProgressEventType,
        call_id: str,
        tool_name: str,
        detail: str = "",
    ) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(
                    ProgressEvent(
                        event_type=event_type,
                        call_id=call_id,
                        tool_name=tool_name,
                        timestamp=time.monotonic(),
                        detail=detail,
                    )
                )
            except Exception:
                # Progress callbacks must never break execution.
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Asynchronous Parallel Tool Executor
# ──────────────────────────────────────────────────────────────────────────────

class AsyncParallelToolExecutor:
    """Async variant of the parallel executor using ``asyncio.gather``.

    Instead of a thread pool, this executor runs each tool call as an
    ``asyncio`` task.  It is designed for use inside an existing event loop
    and avoids blocking the loop while waiting for locks.

    Parameters
    ----------
    max_concurrency:
        Maximum number of tasks to run simultaneously via a semaphore.
    default_timeout:
        Per-call timeout in seconds.  ``None`` means no timeout.
    on_progress:
        Optional async callback for progress events.
    """

    def __init__(
        self,
        max_concurrency: int = 4,
        default_timeout: Optional[float] = None,
        on_progress: Optional[AsyncProgressCallback] = None,
    ) -> None:
        self._max_concurrency = max_concurrency
        self._default_timeout = default_timeout
        self._on_progress = on_progress
        self._lock_manager = ResourceLockManager()

    # ── public API ───────────────────────────────────────────────────────

    async def execute(
        self,
        calls: Sequence[ToolCall],
    ) -> Tuple[List[ToolResult], ExecutionMetrics]:
        """Execute a batch of async tool calls with resource-aware scheduling.

        Returns results in the **same order** as the input *calls*, plus
        aggregate metrics.
        """
        if not calls:
            return [], ExecutionMetrics()

        wall_start = time.monotonic()
        groups, conflict_count = _build_scheduling_groups(calls)

        call_lookup = {c.call_id: c for c in calls}

        # Semaphore to cap concurrency
        semaphore = asyncio.Semaphore(self._max_concurrency)

        # Track max concurrency
        active_count = 0
        max_active_seen = 0
        track_lock = asyncio.Lock()

        async def _tracked_task(call: ToolCall) -> ToolResult:
            nonlocal active_count, max_active_seen
            async with semaphore:
                async with track_lock:
                    active_count += 1
                    if active_count > max_active_seen:
                        max_active_seen = active_count
                try:
                    return await self._execute_single(call)
                finally:
                    async with track_lock:
                        active_count -= 1

        # Launch all calls as tasks.  The semaphore and resource locking
        # ensure that conflicting calls are not simultaneously active.
        tasks = {c.call_id: asyncio.create_task(_tracked_task(c)) for c in calls}

        # Await all tasks (exception isolation: each task handles its own)
        raw_results = await asyncio.gather(
            *tasks.values(), return_exceptions=True
        )

        # Map call_id → result
        results_by_id: Dict[str, ToolResult] = {}
        for call, raw in zip(calls, raw_results):
            if isinstance(raw, BaseException):
                results_by_id[call.call_id] = ToolResult(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=raw,
                )
            else:
                results_by_id[call.call_id] = raw

        wall_end = time.monotonic()

        ordered_results = [
            results_by_id.get(
                c.call_id,
                ToolResult(
                    call_id=c.call_id,
                    tool_name=c.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=RuntimeError("Result missing for call"),
                ),
            )
            for c in calls
        ]

        metrics = ExecutionMetrics(
            total_wall_time=wall_end - wall_start,
            total_tool_time=sum(r.execution_time for r in ordered_results),
            max_concurrency=max_active_seen,
            num_tool_calls=len(calls),
            num_succeeded=sum(1 for r in ordered_results if r.success),
            num_failed=sum(1 for r in ordered_results if r.status is ToolCallStatus.FAILED),
            num_timed_out=sum(1 for r in ordered_results if r.status is ToolCallStatus.TIMED_OUT),
            num_scheduling_groups=len(groups),
            num_resource_conflicts=conflict_count,
        )

        return ordered_results, metrics

    # ── single-call execution ────────────────────────────────────────────

    async def _execute_single(self, call: ToolCall) -> ToolResult:
        """Execute one async tool call with resource locking and timeout."""
        await self._emit_async(
            ProgressEventType.STARTED, call.call_id, call.tool_name
        )

        start = time.monotonic()
        status = ToolCallStatus.COMPLETED
        result_val: Any = None
        error_val: Optional[BaseException] = None

        effective_timeout = call.timeout or self._default_timeout

        try:
            if effective_timeout is not None:
                result_val = await asyncio.wait_for(
                    self._run_async_with_locks(call),
                    timeout=effective_timeout,
                )
            else:
                result_val = await self._run_async_with_locks(call)

        except asyncio.TimeoutError:
            status = ToolCallStatus.TIMED_OUT
            error_val = TimeoutError(
                f"Tool '{call.tool_name}' timed out after {effective_timeout}s"
            )
            await self._emit_async(
                ProgressEventType.TIMED_OUT, call.call_id, call.tool_name
            )
        except Exception as exc:
            status = ToolCallStatus.FAILED
            error_val = exc
            await self._emit_async(
                ProgressEventType.FAILED,
                call.call_id,
                call.tool_name,
                detail=str(exc),
            )

        elapsed = time.monotonic() - start

        if status is ToolCallStatus.COMPLETED:
            await self._emit_async(
                ProgressEventType.COMPLETED, call.call_id, call.tool_name
            )

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status=status,
            result=result_val,
            error=error_val,
            execution_time=elapsed,
        )

    async def _run_async_with_locks(self, call: ToolCall) -> Any:
        """Acquire locks (offloaded to a thread) and then invoke the
        callable.

        Uses :meth:`ResourceLockManager.acquire_resources_explicit` so we
        can split acquire and release across ``await`` boundaries without
        relying on a context manager.
        """
        loop = asyncio.get_running_loop()

        # Acquire locks in a thread so we don't block the event loop.
        handle: ResourceLockHandle = await loop.run_in_executor(
            None,
            self._lock_manager.acquire_resources_explicit,
            call.declared_resources,
        )

        try:
            fn = call.fn
            if asyncio.iscoroutinefunction(fn):
                return await fn(**call.kwargs)
            else:
                # Allow sync callables in the async executor
                return await loop.run_in_executor(
                    None, lambda: fn(**call.kwargs)
                )
        finally:
            # Release locks in a thread
            await loop.run_in_executor(
                None,
                self._lock_manager.release_resources,
                handle,
            )

    # ── async progress emission ──────────────────────────────────────────

    async def _emit_async(
        self,
        event_type: ProgressEventType,
        call_id: str,
        tool_name: str,
        detail: str = "",
    ) -> None:
        if self._on_progress is not None:
            try:
                await self._on_progress(
                    ProgressEvent(
                        event_type=event_type,
                        call_id=call_id,
                        tool_name=tool_name,
                        timestamp=time.monotonic(),
                        detail=detail,
                    )
                )
            except Exception:
                # Progress callbacks must never break execution.
                pass
