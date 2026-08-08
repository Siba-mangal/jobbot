"""Running pipeline commands from the browser.

The web UI shells out to the same `jobbot` CLI it documents rather than
calling the runners in-process. Three reasons, in order of importance:

1. Playwright's sync API cannot run inside FastAPI's event loop. A subprocess
   sidesteps that entirely instead of maintaining a parallel async codepath.
2. The browser and the terminal then exercise identical, tested code.
3. A wedged scrape can be killed without taking the dashboard down with it.

**One task at a time.** Two commands touching the same browser profile would
corrupt the session, so starting a task while one is running is refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import ROOT

MAX_LINES = 2_000


@dataclass
class Task:
    id: str
    label: str
    command: list[str]
    started_at: datetime
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    finished_at: datetime | None = None
    returncode: int | None = None
    process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def duration(self) -> str:
        end = self.finished_at or datetime.now(UTC)
        seconds = int((end - self.started_at).total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60}s"

    def snapshot(self, since: int = 0) -> tuple[list[str], int]:
        """Lines from index `since` onward, plus the new index."""
        current = list(self.lines)
        return current[since:], len(current)


class TaskBusy(RuntimeError):
    """Another command is already running."""


class TaskManager:
    """Owns the single running task and the recent history."""

    def __init__(self, max_history: int = 20):
        self._lock = threading.Lock()
        self._current: Task | None = None
        self._history: deque[Task] = deque(maxlen=max_history)

    # ------------------------------------------------------------------

    @property
    def current(self) -> Task | None:
        with self._lock:
            return self._current if self._current and self._current.running else None

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            if self._current and self._current.id == task_id:
                return self._current
            return next((t for t in self._history if t.id == task_id), None)

    def history(self) -> list[Task]:
        with self._lock:
            return list(reversed(self._history))

    # ------------------------------------------------------------------

    def start(self, args: list[str], label: str) -> Task:
        """Spawn `jobbot <args>`. Raises TaskBusy if one is already running."""
        with self._lock:
            if self._current and self._current.running:
                raise TaskBusy(
                    f"{self._current.label!r} is still running. "
                    "Wait for it to finish, or stop it first."
                )

            task = Task(
                id=uuid.uuid4().hex[:12],
                label=label,
                command=args,
                started_at=datetime.now(UTC),
            )
            self._current = task
            self._history.append(task)

        thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        thread.start()
        return task

    def _run(self, task: Task) -> None:
        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            # Rich would otherwise emit ANSI colour and wrap to a guessed
            # width; we want clean lines for the browser.
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "100",
        }
        command = [sys.executable, "-m", "jobbot.cli", *task.command]
        task.lines.append(f"$ jobbot {' '.join(task.command)}")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            task.lines.append(f"failed to start: {exc}")
            task.returncode = -1
            task.finished_at = datetime.now(UTC)
            return

        task.process = process
        try:
            assert process.stdout is not None
            for line in process.stdout:
                task.lines.append(line.rstrip("\n"))
            process.wait()
            task.returncode = process.returncode
        except Exception as exc:
            task.lines.append(f"error while running: {exc}")
            task.returncode = -1
        finally:
            task.finished_at = datetime.now(UTC)

    def stop(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or not task.running or task.process is None:
            return False
        task.lines.append("— stopping —")
        try:
            task.process.terminate()
            try:
                task.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                task.process.kill()
        except Exception:
            return False
        return True


TASKS = TaskManager()


def stream_lines(task: Task):
    """Server-sent events for a task's output, until it finishes."""
    cursor = 0
    while True:
        lines, cursor = task.snapshot(cursor)
        for line in lines:
            yield f"data: {line}\n\n"
        if not task.running:
            status = "ok" if task.ok else f"exit {task.returncode}"
            yield f"event: done\ndata: {status}\n\n"
            return
        time.sleep(0.4)
