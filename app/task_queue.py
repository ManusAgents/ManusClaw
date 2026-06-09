"""
Persistent Autonomous Task Queue — survives terminal close, restart, and disconnection.

Features:
  - Task persistence to SQLite (survives process restart)
  - Background execution with async workers
  - Queue management with priority ordering
  - Progress recovery and resume from last checkpoint
  - Automatic restart handling
  - Offline continuation support
  - Graceful shutdown with task state preservation

Architecture:
  Task → Queued → Running → [Paused → Running] → Completed/Failed
                   ↓
               Checkpointed (periodic state save for resume)
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from app.logger import logger


# ---------------------------------------------------------------------------
# Task state machine
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class TaskCheckpoint:
    """Snapshot of task state for resume after restart."""
    task_id: str
    step_count: int
    last_tool_call: Optional[dict] = None
    memory_snapshot: Optional[list] = None  # Serialized memory messages
    created_at: float = field(default_factory=time.time)


@dataclass
class TaskEntry:
    """A persistent task entry."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    prompt: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    priority: TaskPriority = TaskPriority.NORMAL
    result: Optional[str] = None
    error: Optional[str] = None
    checkpoint: Optional[TaskCheckpoint] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    retry_count: int = 0
    max_retries: int = 3
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        if self.checkpoint:
            d["checkpoint"] = asdict(self.checkpoint)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TaskEntry:
        d = dict(d)
        d["status"] = TaskStatus(d["status"])
        d["priority"] = TaskPriority(d["priority"])
        if d.get("checkpoint") and isinstance(d["checkpoint"], dict):
            d["checkpoint"] = TaskCheckpoint(**d["checkpoint"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Persistent task queue backed by SQLite
# ---------------------------------------------------------------------------

_WORKSPACE = Path(os.getenv("MANUSCLAW_WORKSPACE", "workspace"))
_DB_PATH = _WORKSPACE / ".task_queue" / "tasks.db"


class TaskQueue:
    """
    Persistent task queue with SQLite backend.

    - Tasks survive process restarts (stored in SQLite)
    - Workers pull tasks from queue in priority order
    - Checkpoints allow resuming interrupted tasks
    - Background workers run independently
    """

    def __init__(self, db_path: Optional[str] = None, max_workers: int = 2) -> None:
        self._db_path = Path(db_path or _DB_PATH)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_workers = max_workers
        self._workers: list[asyncio.Task] = []
        self._running = False
        self._task_executor: Optional[Callable] = None
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite tables."""
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def submit(self, prompt: str, priority: TaskPriority = TaskPriority.NORMAL,
                     metadata: Optional[dict] = None) -> TaskEntry:
        """Submit a new task to the queue."""
        task = TaskEntry(
            prompt=prompt,
            status=TaskStatus.QUEUED,
            priority=priority,
            metadata=metadata or {},
        )
        await self._save(task)
        logger.info(f"[TaskQueue] Submitted task {task.id}: {prompt[:80]}...")
        return task

    async def _save(self, task: TaskEntry) -> None:
        """Persist task to SQLite."""
        def _write():
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO tasks (id, data, updated_at) VALUES (?, ?, ?)",
                    (task.id, json.dumps(task.to_dict()), time.time()),
                )
                conn.commit()
        await asyncio.to_thread(_write)

    async def _load(self, task_id: str) -> Optional[TaskEntry]:
        """Load a task from SQLite."""
        def _read():
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT data FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                return row
        row = await asyncio.to_thread(_read)
        if not row:
            return None
        return TaskEntry.from_dict(json.loads(row[0]))

    async def list_tasks(self, status: Optional[TaskStatus] = None) -> list[TaskEntry]:
        """List all tasks, optionally filtered by status."""
        def _read():
            with sqlite3.connect(str(self._db_path)) as conn:
                if status:
                    rows = conn.execute(
                        "SELECT data FROM tasks WHERE json_extract(data, '$.status') = ? ORDER BY updated_at DESC",
                        (status.value,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT data FROM tasks ORDER BY updated_at DESC"
                    ).fetchall()
                return rows
        rows = await asyncio.to_thread(_read)
        return [TaskEntry.from_dict(json.loads(r[0])) for r in rows]

    async def get_task(self, task_id: str) -> Optional[TaskEntry]:
        """Get a specific task by ID."""
        return await self._load(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task."""
        task = await self._load(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED):
            task.status = TaskStatus.CANCELLED
            task.completed_at = time.time()
            await self._save(task)
            # Cancel the asyncio task if running
            if task_id in self._active_tasks:
                self._active_tasks[task_id].cancel()
            return True
        return False

    async def retry_task(self, task_id: str) -> bool:
        """Re-queue a failed or cancelled task."""
        task = await self._load(task_id)
        if not task or task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        task.status = TaskStatus.QUEUED
        task.retry_count += 1
        task.error = None
        task.result = None
        await self._save(task)
        return True

    async def save_checkpoint(self, task_id: str, step_count: int,
                              last_tool_call: Optional[dict] = None,
                              memory_snapshot: Optional[list] = None) -> None:
        """Save a checkpoint for task resume."""
        task = await self._load(task_id)
        if not task:
            return
        task.checkpoint = TaskCheckpoint(
            task_id=task_id,
            step_count=step_count,
            last_tool_call=last_tool_call,
            memory_snapshot=memory_snapshot,
        )
        await self._save(task)
        logger.debug(f"[TaskQueue] Checkpoint saved for task {task_id} at step {step_count}")

    async def cleanup_completed(self, older_than_hours: int = 24) -> int:
        """Remove completed/failed tasks older than N hours."""
        cutoff = time.time() - (older_than_hours * 3600)
        def _clean():
            with sqlite3.connect(str(self._db_path)) as conn:
                cursor = conn.execute(
                    "DELETE FROM tasks WHERE json_extract(data, '$.status') IN ('completed', 'failed', 'cancelled') AND updated_at < ?",
                    (cutoff,),
                )
                conn.commit()
                return cursor.rowcount
        return await asyncio.to_thread(_clean)

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    def set_executor(self, executor: Callable) -> None:
        """Set the async function that executes tasks.

        Signature: async def executor(task: TaskEntry) -> str
        """
        self._task_executor = executor

    async def start_workers(self) -> None:
        """Start background workers that pull and execute tasks from the queue."""
        if self._running:
            return
        self._running = True
        for i in range(self._max_workers):
            worker = asyncio.create_task(self._worker_loop(i), name=f"task-worker-{i}")
            self._workers.append(worker)
        logger.info(f"[TaskQueue] Started {self._max_workers} workers")

    async def stop_workers(self) -> None:
        """Gracefully stop all workers, saving task state."""
        self._running = False
        for w in self._workers:
            w.cancel()
        # Wait for workers to finish current tasks
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # Mark running tasks as paused for resume
        for task_id, atask in self._active_tasks.items():
            if not atask.done():
                atask.cancel()
                task = await self._load(task_id)
                if task and task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PAUSED
                    await self._save(task)
        self._active_tasks.clear()
        logger.info("[TaskQueue] Workers stopped, running tasks paused for resume")

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker coroutine that pulls tasks from the queue."""
        while self._running:
            try:
                task = await self._next_queued_task()
                if not task:
                    await asyncio.sleep(2)  # No tasks, wait
                    continue

                logger.info(f"[Worker-{worker_id}] Picked up task {task.id}: {task.prompt[:60]}...")
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()
                await self._save(task)

                if self._task_executor:
                    atask = asyncio.create_task(self._task_executor(task))
                    self._active_tasks[task.id] = atask
                    try:
                        result = await atask
                        task.status = TaskStatus.COMPLETED
                        task.result = result
                        task.completed_at = time.time()
                    except asyncio.CancelledError:
                        task.status = TaskStatus.PAUSED
                        logger.info(f"[Worker-{worker_id}] Task {task.id} paused (worker shutdown)")
                    except Exception as e:
                        task.status = TaskStatus.FAILED
                        task.error = str(e)
                        task.completed_at = time.time()
                        logger.error(f"[Worker-{worker_id}] Task {task.id} failed: {e}")
                    finally:
                        self._active_tasks.pop(task.id, None)
                        await self._save(task)
                else:
                    logger.warning("[TaskQueue] No executor set, task will remain in RUNNING state")
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Worker-{worker_id}] Error: {e}")
                await asyncio.sleep(5)

    async def _next_queued_task(self) -> Optional[TaskEntry]:
        """Get the highest-priority queued task (FIFO within same priority)."""
        def _read():
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute(
                    "SELECT data FROM tasks WHERE json_extract(data, '$.status') = 'queued' "
                    "ORDER BY json_extract(data, '$.priority') DESC, updated_at ASC LIMIT 1"
                ).fetchone()
                return row
        row = await asyncio.to_thread(_read)
        if not row:
            return None
        return TaskEntry.from_dict(json.loads(row[0]))

    # ------------------------------------------------------------------
    # Resume interrupted tasks (called on startup)
    # ------------------------------------------------------------------

    async def resume_interrupted(self) -> int:
        """Resume tasks that were RUNNING or PAUSED when the process last exited.
        Returns the number of tasks resumed."""
        resumed = 0
        tasks = await self.list_tasks()
        for task in tasks:
            if task.status == TaskStatus.RUNNING:
                # Was running when process exited — re-queue for retry
                task.status = TaskStatus.QUEUED
                task.retry_count += 1
                if task.retry_count <= task.max_retries:
                    await self._save(task)
                    resumed += 1
                    logger.info(f"[TaskQueue] Resumed interrupted task {task.id}")
                else:
                    task.status = TaskStatus.FAILED
                    task.error = "Max retries exceeded after multiple interruptions"
                    await self._save(task)
            elif task.status == TaskStatus.PAUSED:
                # Was paused — re-queue with checkpoint for resume
                task.status = TaskStatus.QUEUED
                await self._save(task)
                resumed += 1
                logger.info(
                    f"[TaskQueue] Resumed paused task {task.id} "
                    f"(checkpoint at step {task.checkpoint.step_count if task.checkpoint else '?'})"
                )
        return resumed

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    async def status_summary(self) -> dict:
        """Return a summary of all tasks."""
        tasks = await self.list_tasks()
        summary: dict[str, Any] = {"total": len(tasks)}
        for status in TaskStatus:
            summary[status.value] = sum(1 for t in tasks if t.status == status)
        return summary
