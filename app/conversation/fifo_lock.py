"""
ManusClaw Conversation System — FIFO Lock
============================================

Fair, starvation-free locks that grant access in first-in-first-out order.

Two variants are provided:

  - :class:`FIFOLock` — synchronous (threading-based) FIFO lock.
  - :class:`AsyncFIFOLock` — asynchronous (asyncio-based) FIFO lock.

Both guarantee that waiters acquire the lock in the order they requested
it, preventing starvation even under high contention.

Design:
  The lock maintains an internal queue of waiters.  When the lock is
  released, the waiter at the head of the queue is granted ownership.
  This is more fair than Python's built-in ``threading.Lock``, which
  makes no ordering guarantees.

Usage (sync)::

    lock = FIFOLock()
    with lock:
        # critical section
        ...

Usage (async)::

    lock = AsyncFIFOLock()
    async with lock:
        # critical section
        ...
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Synchronous FIFO Lock
# ──────────────────────────────────────────────────────────────────────────────

class FIFOLock:
    """
    A fair, reentrant-safe lock that grants access in FIFO order.

    Unlike Python's built-in ``threading.Lock``, this lock guarantees
    that waiters acquire the lock in the exact order they called
    ``acquire()``.  This prevents starvation under high contention.

    The lock is **not reentrant** — a thread that holds the lock must
    not call ``acquire()`` again without releasing it first, or it
    will deadlock.

    Thread Safety:
        All public methods are thread-safe.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._waiters: deque[threading.Event] = deque()
        self._held: bool = False
        self._owner: Optional[int] = None
        self._hold_count: int = 0

    # ── Core API ──────────────────────────────────────────────────────────────

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """
        Acquire the lock, blocking until granted or *timeout* elapses.

        If the lock is not held, it is granted immediately.  Otherwise,
        the calling thread is placed at the tail of the FIFO queue and
        blocks until it reaches the head and the lock is released.

        Args:
            timeout: Maximum seconds to wait.  ``None`` means wait forever.

        Returns:
            ``True`` if the lock was acquired, ``False`` if the timeout
            elapsed first.
        """
        with self._lock:
            if not self._held:
                # Fast path: lock is free
                self._held = True
                self._owner = threading.current_thread().ident
                self._hold_count = 1
                return True
            # Slow path: enqueue a waiter
            event = threading.Event()
            self._waiters.append(event)

        # Wait for our turn (outside the internal lock)
        acquired = event.wait(timeout=timeout)

        if not acquired:
            # Timeout — remove ourselves from the queue if still there
            with self._lock:
                try:
                    self._waiters.remove(event)
                except ValueError:
                    # We were already dequeued and signalled between
                    # the timeout check and acquiring _lock
                    acquired = True
            if not acquired:
                return False

        with self._lock:
            self._held = True
            self._owner = threading.current_thread().ident
            self._hold_count = 1
        return True

    def release(self) -> None:
        """
        Release the lock, granting it to the next waiter in the queue.

        Raises:
            RuntimeError: If the calling thread does not hold the lock.
        """
        with self._lock:
            if not self._held:
                raise RuntimeError("Cannot release unheld FIFOLock")
            self._held = False
            self._owner = None
            self._hold_count = 0

            # Grant to next waiter
            if self._waiters:
                next_event = self._waiters.popleft()
                next_event.set()

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "FIFOLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def locked(self) -> bool:
        """Return ``True`` if the lock is currently held."""
        return self._held

    @property
    def waiting_count(self) -> int:
        """Return the number of threads waiting for the lock."""
        with self._lock:
            return len(self._waiters)

    def __repr__(self) -> str:
        status = "held" if self._held else "free"
        waiters = len(self._waiters)
        return f"<FIFOLock {status} waiters={waiters}>"


# ──────────────────────────────────────────────────────────────────────────────
# Asynchronous FIFO Lock
# ──────────────────────────────────────────────────────────────────────────────

class AsyncFIFOLock:
    """
    An async fair lock that grants access in FIFO order.

    This is the asyncio equivalent of :class:`FIFOLock`.  Waiters
    acquire the lock in the exact order they called ``acquire()``.

    The lock is **not reentrant**.

    Thread Safety:
        This lock is designed for use within a single asyncio event
        loop.  Do not share across threads.
    """

    def __init__(self) -> None:
        self._waiters: deque[asyncio.Future] = deque()
        self._held: bool = False

    # ── Core API ──────────────────────────────────────────────────────────────

    async def acquire(self) -> None:
        """
        Acquire the lock asynchronously.

        If the lock is not held, it is granted immediately.  Otherwise,
        the calling coroutine is placed at the tail of the FIFO queue
        and awaits its turn.
        """
        if not self._held:
            # Fast path
            self._held = True
            return

        # Slow path: enqueue a future
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters.append(future)

        try:
            await future
        except asyncio.CancelledError:
            # If we were cancelled while waiting, remove ourselves
            # from the queue and pass the lock to the next waiter
            # if we were at the head.
            self._remove_from_queue(future)
            raise

    def release(self) -> None:
        """
        Release the lock, granting it to the next waiter in the queue.

        Raises:
            RuntimeError: If the lock is not currently held.
        """
        if not self._held:
            raise RuntimeError("Cannot release unheld AsyncFIFOLock")

        self._held = False

        # Grant to next waiter
        while self._waiters:
            next_future = self._waiters.popleft()
            if not next_future.done():
                self._held = True
                next_future.set_result(None)
                break

    def _remove_from_queue(self, future: asyncio.Future) -> None:
        """
        Remove a cancelled future from the waiters queue.

        If the removed future was at the head of the queue and the lock
        is not held, grant the lock to the next waiter.
        """
        try:
            self._waiters.remove(future)
        except ValueError:
            # Already dequeued
            pass

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncFIFOLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def locked(self) -> bool:
        """Return ``True`` if the lock is currently held."""
        return self._held

    @property
    def waiting_count(self) -> int:
        """Return the number of coroutines waiting for the lock."""
        return len(self._waiters)

    def __repr__(self) -> str:
        status = "held" if self._held else "free"
        waiters = len(self._waiters)
        return f"<AsyncFIFOLock {status} waiters={waiters}>"
