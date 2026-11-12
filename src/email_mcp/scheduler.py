"""Background delivery worker for scheduled email tasks."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .adapter import UncertainDeliveryError, build_message, get_adapter
from .config import get_account
from .scheduled_store import ScheduledEmail, ScheduledEmailStore


class SchedulerAlreadyRunning(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, store: ScheduledEmailStore | None = None,
                 adapter_factory: Callable = get_adapter,
                 clock: Callable[[], datetime] = utc_now):
        self.store = store or ScheduledEmailStore()
        self.adapter_factory = adapter_factory
        self.clock = clock
        self._started = False
        self._lock_file = None

    def _acquire_lock(self) -> None:
        lock_path = self.store.path.with_suffix(self.store.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = lock_path.open("a+")
        os.chmod(lock_path, 0o600)
        try:
            import fcntl
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError) as e:
            self._lock_file.close()
            self._lock_file = None
            raise SchedulerAlreadyRunning("another email scheduler is already running") from e

    def _release_lock(self) -> None:
        if self._lock_file is not None:
            try:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def run_once(self) -> int:
        if not self._started:
            self._acquire_lock()
            self.store.start_scheduler(self.clock())
            self._started = True
        tasks = self.store.claim_due(self.clock())
        for task in tasks:
            self._deliver(task)
        return len(tasks)

    def run_forever(self, poll_interval: float = 15.0) -> None:
        try:
            while True:
                self.run_once()
                time.sleep(poll_interval)
        finally:
            if self._started:
                self.store.stop_scheduler()
                self._started = False
            self._release_lock()

    def _deliver(self, task: ScheduledEmail) -> ScheduledEmail:
        try:
            account = get_account(task.account)
            if account is None:
                raise ValueError(f"account not found: {task.account}")
            msg = build_message(account, task.to, task.cc, task.subject, task.body,
                                attachment_paths=task.attachments)
            adapter = self.adapter_factory(task.account)
            adapter.send(msg)
        except UncertainDeliveryError as e:
            return self.store.transition(task.id, {"sending"}, "unknown", last_error=str(e)[:1000])
        except Exception as e:
            return self.store.transition(task.id, {"sending"}, "failed", last_error=str(e)[:1000])
        return self.store.transition(task.id, {"sending"}, "sent", sent_at=self.clock(), last_error="")

    def _run_explicit(self, task_id: str, action: str) -> ScheduledEmail:
        self._acquire_lock()
        try:
            previous_status = ""
            if action == "confirm":
                task = self.store.transition(task_id, {"awaiting_confirmation"}, "sending")
            else:
                task = self.store.get(task_id)
                if task.status not in {"failed", "unknown"}:
                    raise ValueError("only failed or unknown tasks can be retried")
                previous_status = task.status
                self.store.transition(task_id, {previous_status}, "scheduled", send_at=self.clock(), last_error="")
                task = self.store.transition(task_id, {"scheduled"}, "sending")
            result = self._deliver(task)
            if action == "retry" and previous_status == "unknown":
                result.last_error = "previous delivery outcome was unknown; retry may have sent a duplicate"
            return result
        finally:
            self._release_lock()

    def confirm(self, task_id: str) -> ScheduledEmail:
        return self._run_explicit(task_id, "confirm")

    def retry(self, task_id: str) -> ScheduledEmail:
        return self._run_explicit(task_id, "retry")
