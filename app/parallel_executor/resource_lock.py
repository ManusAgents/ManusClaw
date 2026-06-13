"""
Resource Lock Manager
=====================
Fine-grained, readers-writer locking based on declared resources.

How it works
------------
Each unique resource (identified by ``ResourceDeclaration.lock_key``) gets its
own ``_ResourceRWLock``.  The manager enforces:

* **Multiple READ locks** may be held concurrently on the same resource.
* **A WRITE lock** is exclusive — no other reader or writer may hold the
  resource while a write lock is active.

Deadlock prevention
-------------------
The manager uses a **global acquisition ordering** strategy combined with a
timeout.  When ``acquire_resources`` is called, the caller must acquire *all*
required locks within the timeout.  If it cannot, it releases any partial
acquisitions and retries, preventing indefinite deadlock.

Thread safety
-------------
All public methods are thread-safe.  Internally a single ``threading.Lock``
guards the lock registry, and each ``_ResourceRWLock`` uses its own
``threading.Condition`` for waiting / notification.

Context-manager interface
-------------------------
::

    with lock_manager.acquire_resources(declarations):
        # ... safe to use the resources ...
    # locks automatically released
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Generator, List, Optional, Sequence, Type

from app.parallel_executor.declared_resources import (
    AccessMode,
    DeclaredResources,
    ResourceDeclaration,
)


# ──────────────────────────────────────────────────────────────────────────────
# Per-resource readers-writer lock
# ──────────────────────────────────────────────────────────────────────────────

class _ResourceRWLock:
    """Non-reentrant readers-writer lock for a single resource key.

    This is **not** intended for direct use by callers.  Use
    :class:`ResourceLockManager` instead.

    Invariant: ``_writer is not None`` implies ``_readers == 0``.
    """

    __slots__ = ("_key", "_cond", "_readers", "_writer", "_reader_threads")

    def __init__(self, key: str) -> None:
        self._key = key
        self._cond = threading.Condition(threading.Lock())
        self._readers: int = 0
        self._writer: Optional[threading.Thread] = None
        # Track which threads hold read locks for debugging
        self._reader_threads: dict[int, int] = {}  # thread ident -> count

    # ── acquisition ──────────────────────────────────────────────────────

    def acquire_read(self, timeout: float) -> bool:
        """Block until a READ lock can be granted or *timeout* elapses.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._writer is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not self._cond.wait(timeout=remaining):
                    return False
            self._readers += 1
            tid = threading.current_thread().ident
            self._reader_threads[tid] = self._reader_threads.get(tid, 0) + 1
            return True

    def acquire_write(self, timeout: float) -> bool:
        """Block until a WRITE lock can be granted or *timeout* elapses.

        Returns ``True`` if the lock was acquired, ``False`` on timeout.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._readers > 0 or self._writer is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if not self._cond.wait(timeout=remaining):
                    return False
            self._writer = threading.current_thread()
            return True

    # ── release ──────────────────────────────────────────────────────────

    def release_read(self) -> None:
        """Release one READ lock.  Must be called by the same thread."""
        with self._cond:
            tid = threading.current_thread().ident
            if self._readers <= 0:
                raise RuntimeError(
                    f"Cannot release READ lock on {self._key!r}: "
                    f"no readers held (thread={tid})"
                )
            self._readers -= 1
            count = self._reader_threads.get(tid, 0)
            if count <= 1:
                self._reader_threads.pop(tid, None)
            else:
                self._reader_threads[tid] = count - 1
            if self._readers == 0:
                self._cond.notify_all()

    def release_write(self) -> None:
        """Release the WRITE lock.  Must be called by the owning thread."""
        with self._cond:
            if self._writer is None:
                raise RuntimeError(
                    f"Cannot release WRITE lock on {self._key!r}: "
                    f"no writer held (thread={threading.current_thread().ident})"
                )
            if self._writer is not threading.current_thread():
                raise RuntimeError(
                    f"Cannot release WRITE lock on {self._key!r}: "
                    f"owned by {self._writer.ident}, "
                    f"released by {threading.current_thread().ident}"
                )
            self._writer = None
            self._cond.notify_all()

    # ── introspection ────────────────────────────────────────────────────

    @property
    def is_write_locked(self) -> bool:
        """``True`` if a writer currently holds the lock."""
        with self._cond:
            return self._writer is not None

    @property
    def reader_count(self) -> int:
        """Number of active READ lock holders."""
        with self._cond:
            return self._readers

    def __repr__(self) -> str:  # pragma: no cover
        state = "W" if self._writer else f"R:{self._readers}"
        return f"_ResourceRWLock({self._key!r}, {state})"


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class ResourceLockError(Exception):
    """Base exception for resource-lock errors."""


class DeadlockDetectedError(ResourceLockError):
    """Raised when lock acquisition times out, indicating a likely deadlock."""


class LockAcquisitionError(ResourceLockError):
    """Raised when a lock cannot be acquired within the timeout."""


# ──────────────────────────────────────────────────────────────────────────────
# Resource lock manager
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# Lock handle (for explicit acquire / release without context managers)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ResourceLockHandle:
    """Opaque handle returned by :meth:`ResourceLockManager.acquire_resources_explicit`.

    Pass this handle to :meth:`ResourceLockManager.release_resources` to
    free the acquired locks.  **Do not** construct this object directly.
    """

    _acquired: List[tuple[_ResourceRWLock, AccessMode]] = field(
        default_factory=list, repr=False
    )
    _released: bool = False

    @property
    def is_released(self) -> bool:
        """``True`` after :meth:`ResourceLockManager.release_resources` has
        been called on this handle."""
        return self._released


# ──────────────────────────────────────────────────────────────────────────────
# Resource lock manager
# ──────────────────────────────────────────────────────────────────────────────

class ResourceLockManager:
    """Fine-grained lock manager that maps :class:`ResourceDeclaration` objects
    to per-resource readers-writer locks.

    Typical usage::

        manager = ResourceLockManager()
        decls = DeclaredResources.file_resource("/tmp/data.csv", AccessMode.READ)
        with manager.acquire_resources(decls):
            ...  # safe to read the file

    Thread-safety
    -------------
    All public methods are safe to call from multiple threads.

    Parameters
    ----------
    default_timeout:
        Maximum seconds to wait for each lock-acquisition attempt before
        raising :class:`DeadlockDetectedError`.  Defaults to 30 seconds.
    retry_count:
        Number of times to retry the full acquisition if a partial
        acquisition fails (to break potential deadlocks).  Defaults to 3.
    """

    def __init__(
        self,
        default_timeout: float = 30.0,
        retry_count: int = 3,
    ) -> None:
        self._default_timeout = default_timeout
        self._retry_count = retry_count
        self._registry_lock = threading.Lock()
        self._locks: dict[str, _ResourceRWLock] = {}

    # ── internal lock registry ───────────────────────────────────────────

    def _get_or_create_lock(self, key: str) -> _ResourceRWLock:
        """Return the lock for *key*, creating it if necessary.

        Must be called while ``self._registry_lock`` is held or from a
        thread-safe context.
        """
        with self._registry_lock:
            rw_lock = self._locks.get(key)
            if rw_lock is None:
                rw_lock = _ResourceRWLock(key)
                self._locks[key] = rw_lock
            return rw_lock

    # ── public API ───────────────────────────────────────────────────────

    @contextmanager
    def acquire_resources(
        self,
        declarations: DeclaredResources,
        timeout: Optional[float] = None,
    ) -> Generator[None, None, None]:
        """Context manager that acquires all declared resource locks and
        releases them on exit (even on exception).

        Parameters
        ----------
        declarations:
            The set of resources the caller needs.
        timeout:
            Per-attempt timeout in seconds.  Falls back to
            ``default_timeout`` if not supplied.

        Raises
        ------
        DeadlockDetectedError
            If the locks cannot be acquired within the retry budget,
            suggesting a deadlock.
        """
        if declarations.is_empty:
            yield
            return

        effective_timeout = timeout if timeout is not None else self._default_timeout

        # Sort declarations by lock_key for consistent acquisition order
        # to reduce the chance of deadlocks.
        sorted_decls = sorted(declarations.declarations, key=lambda d: d.lock_key)

        acquired: List[tuple[_ResourceRWLock, AccessMode]] = []

        try:
            self._acquire_all(sorted_decls, effective_timeout, acquired)
            yield
        finally:
            # Always release in reverse acquisition order (LIFO)
            self._release_all(acquired)

    def try_acquire_resources(
        self,
        declarations: DeclaredResources,
        timeout: float = 0.1,
    ) -> bool:
        """Non-blocking attempt to acquire all declared resources.

        Returns ``True`` if all locks were acquired (and immediately released),
        ``False`` if any lock could not be obtained within *timeout*.

        This is useful for pre-checking whether a set of resources is
        available without actually holding them.
        """
        if declarations.is_empty:
            return True

        sorted_decls = sorted(declarations.declarations, key=lambda d: d.lock_key)
        acquired: List[tuple[_ResourceRWLock, AccessMode]] = []
        try:
            return self._try_acquire_all(sorted_decls, timeout, acquired)
        finally:
            self._release_all(acquired)

    # ── explicit acquire / release (for async usage) ────────────────────

    def acquire_resources_explicit(
        self,
        declarations: DeclaredResources,
        timeout: Optional[float] = None,
    ) -> ResourceLockHandle:
        """Acquire all declared resource locks and return an opaque
        :class:`ResourceLockHandle`.

        Use :meth:`release_resources` to free the locks when done.
        This is the non-context-manager variant, suitable for async code
        that cannot use ``with`` across ``await`` boundaries.

        Raises
        ------
        DeadlockDetectedError
            If the locks cannot be acquired within the retry budget.
        """
        if declarations.is_empty:
            return ResourceLockHandle()

        effective_timeout = timeout if timeout is not None else self._default_timeout
        sorted_decls = sorted(declarations.declarations, key=lambda d: d.lock_key)

        handle = ResourceLockHandle()
        self._acquire_all(sorted_decls, effective_timeout, handle._acquired)
        return handle

    def release_resources(self, handle: ResourceLockHandle) -> None:
        """Release all locks held by *handle*.

        Safe to call multiple times — subsequent calls are no-ops.

        Parameters
        ----------
        handle:
            A handle previously returned by
            :meth:`acquire_resources_explicit`.
        """
        if handle._released:
            return
        handle._released = True
        self._release_all(handle._acquired)

    # ── conflict query ───────────────────────────────────────────────────

    def is_resource_locked(self, resource_key: str) -> bool:
        """Return ``True`` if any thread currently holds a lock on the
        resource identified by *resource_key*."""
        with self._registry_lock:
            rw_lock = self._locks.get(resource_key)
        if rw_lock is None:
            return False
        return rw_lock.is_write_locked or rw_lock.reader_count > 0

    def get_lock_info(self) -> dict[str, dict[str, int | bool]]:
        """Snapshot of all registered locks and their current state.

        Returns a mapping of ``lock_key → {is_write_locked, reader_count}``.
        Useful for debugging and monitoring.
        """
        with self._registry_lock:
            keys = list(self._locks.keys())
        result: dict[str, dict[str, int | bool]] = {}
        for key in keys:
            with self._registry_lock:
                rw_lock = self._locks.get(key)
            if rw_lock is not None:
                result[key] = {
                    "is_write_locked": rw_lock.is_write_locked,
                    "reader_count": rw_lock.reader_count,
                }
        return result

    # ── internal acquisition helpers ─────────────────────────────────────

    def _acquire_all(
        self,
        sorted_decls: Sequence[ResourceDeclaration],
        timeout: float,
        acquired: List[tuple[_ResourceRWLock, AccessMode]],
    ) -> None:
        """Attempt to acquire all locks with retries.

        On each retry, any partially-acquired locks are released first to
        avoid holding partial state while waiting.
        """
        last_error: Optional[Exception] = None

        for attempt in range(self._retry_count):
            # Release any partial acquisitions from a previous attempt
            self._release_all(acquired)
            acquired.clear()

            success = True
            for decl in sorted_decls:
                rw_lock = self._get_or_create_lock(decl.lock_key)
                got: bool
                if decl.mode is AccessMode.READ:
                    got = rw_lock.acquire_read(timeout)
                else:
                    got = rw_lock.acquire_write(timeout)

                if not got:
                    last_error = LockAcquisitionError(
                        f"Failed to acquire {decl.mode.value} lock on "
                        f"{decl.resource_type}:{decl.resource_id} "
                        f"(key={decl.lock_key!r}) within {timeout}s "
                        f"(attempt {attempt + 1}/{self._retry_count})"
                    )
                    success = False
                    break
                acquired.append((rw_lock, decl.mode))

            if success:
                return

        # All retries exhausted
        raise DeadlockDetectedError(
            f"Could not acquire all resource locks after {self._retry_count} "
            f"attempts. Last error: {last_error}"
        )

    def _try_acquire_all(
        self,
        sorted_decls: Sequence[ResourceDeclaration],
        timeout: float,
        acquired: List[tuple[_ResourceRWLock, AccessMode]],
    ) -> bool:
        """Single-attempt acquisition for ``try_acquire_resources``."""
        for decl in sorted_decls:
            rw_lock = self._get_or_create_lock(decl.lock_key)
            got: bool
            if decl.mode is AccessMode.READ:
                got = rw_lock.acquire_read(timeout)
            else:
                got = rw_lock.acquire_write(timeout)

            if not got:
                return False
            acquired.append((rw_lock, decl.mode))

        return True

    @staticmethod
    def _release_all(
        acquired: List[tuple[_ResourceRWLock, AccessMode]],
    ) -> None:
        """Release all held locks in reverse order.  Safe to call multiple
        times — each lock is released exactly once."""
        while acquired:
            rw_lock, mode = acquired.pop()
            try:
                if mode is AccessMode.READ:
                    rw_lock.release_read()
                else:
                    rw_lock.release_write()
            except RuntimeError:
                # Already released or ownership mismatch — swallow silently
                # to ensure remaining locks still get cleaned up.
                pass

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ResourceLockManager(timeout={self._default_timeout}, "
            f"retry_count={self._retry_count}, "
            f"registered_resources={len(self._locks)})"
        )
