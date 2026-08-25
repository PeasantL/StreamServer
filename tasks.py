"""In-process registry for background jobs (downloads, library scans).

Replaces a bare module-level dict that was mutated from threadpool workers,
never pruned, and lost on restart with no way for the client to tell.
Completed entries expire so the registry cannot grow without bound.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

DEFAULT_TTL_SECONDS = 900
DEFAULT_MAX_TASKS = 200

TERMINAL_STATES = {"completed", "failed"}


class TaskRegistry:
    def __init__(self, ttl: int = DEFAULT_TTL_SECONDS, max_tasks: int = DEFAULT_MAX_TASKS):
        self._ttl = ttl
        self._max_tasks = max_tasks
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def create(self, kind: str) -> str:
        task_id = str(uuid.uuid4())
        with self._lock:
            self._prune_locked()
            self._tasks[task_id] = {
                "kind": kind,
                "status": "in_progress",
                "progress": 0,
                "error": None,
                "created_at": time.monotonic(),
                "finished_at": None,
            }
        return task_id

    def update(self, task_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.update(fields)
            if task.get("status") in TERMINAL_STATES and task["finished_at"] is None:
                task["finished_at"] = time.monotonic()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune_locked()
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return {
                "status": task["status"],
                "progress": task["progress"],
                "error": task["error"],
            }

    def _prune_locked(self) -> None:
        now = time.monotonic()
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task["finished_at"] is not None and now - task["finished_at"] > self._ttl
        ]
        for task_id in expired:
            del self._tasks[task_id]

        # Hard ceiling in case something creates jobs faster than they expire.
        overflow = len(self._tasks) - self._max_tasks
        if overflow > 0:
            oldest = sorted(self._tasks.items(), key=lambda item: item[1]["created_at"])
            for task_id, _ in oldest[:overflow]:
                del self._tasks[task_id]


registry = TaskRegistry()
