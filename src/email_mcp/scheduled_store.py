"""Persistent, permission-restricted storage for scheduled email tasks."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

STATUSES = {
    "scheduled", "awaiting_confirmation", "sending", "sent", "failed",
    "unknown", "missed", "cancelled",
}
_ALLOWED = {
    "scheduled": {"sending", "awaiting_confirmation", "missed", "cancelled"},
    "awaiting_confirmation": {"sending", "cancelled"},
    "sending": {"sent", "failed", "unknown"},
    "failed": {"scheduled"},
    "unknown": {"scheduled"},
    "sent": set(), "missed": set(), "cancelled": set(),
}


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@dataclass
class ScheduledEmail:
    account: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    attachments: list[str]
    send_at: datetime
    offset: str
    mode: str = "auto"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "scheduled"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    last_error: str = ""

    def __post_init__(self):
        if self.mode not in {"auto", "confirm"}:
            raise ValueError("mode must be 'auto' or 'confirm'")
        if self.status not in STATUSES:
            raise ValueError(f"invalid status: {self.status}")
        self.send_at = self.send_at.astimezone(timezone.utc)


class ScheduledEmailStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else CONFIG_DIR / "scheduled-emails.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        self._connect().close()
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_emails (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, to_json TEXT NOT NULL,
            cc_json TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
            attachments_json TEXT NOT NULL, send_at TEXT NOT NULL, offset TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, sent_at TEXT, last_error TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS scheduler_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        return conn

    @staticmethod
    def _values(task: ScheduledEmail) -> tuple[Any, ...]:
        return (
            task.id, task.account, json.dumps(task.to), json.dumps(task.cc), task.subject,
            task.body, json.dumps(task.attachments), _iso(task.send_at), task.offset,
            task.mode, task.status, _iso(task.created_at), _iso(task.updated_at),
            _iso(task.sent_at) if task.sent_at else None, task.last_error,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ScheduledEmail:
        return ScheduledEmail(
            id=row["id"], account=row["account"], to=json.loads(row["to_json"]),
            cc=json.loads(row["cc_json"]), subject=row["subject"], body=row["body"],
            attachments=json.loads(row["attachments_json"]), send_at=_dt(row["send_at"]),
            offset=row["offset"], mode=row["mode"], status=row["status"],
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
            sent_at=_dt(row["sent_at"]) if row["sent_at"] else None,
            last_error=row["last_error"],
        )

    def create(self, task: ScheduledEmail) -> ScheduledEmail:
        task.updated_at = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO scheduled_emails VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         self._values(task))
            conn.commit()
            return replace(task)
        finally:
            conn.close()

    def get(self, task_id: str) -> ScheduledEmail:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM scheduled_emails WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"scheduled task not found: {task_id}")
            return self._from_row(row)
        finally:
            conn.close()

    def list(self, status: str = "", limit: int = 50) -> list[ScheduledEmail]:
        if status and status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM scheduled_emails WHERE status=? ORDER BY send_at LIMIT ?",
                                    (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM scheduled_emails ORDER BY send_at LIMIT ?",
                                    (limit,)).fetchall()
            return [self._from_row(row) for row in rows]
        finally:
            conn.close()

    def update(self, task_id: str, changes: dict[str, Any]) -> ScheduledEmail:
        allowed = {"account", "to", "cc", "subject", "body", "attachments", "send_at", "offset", "mode"}
        if not changes or set(changes) - allowed:
            raise ValueError("invalid or empty task changes")
        task = self.get(task_id)
        if task.status != "scheduled":
            raise ValueError("scheduled task cannot be edited unless status is scheduled")
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = datetime.now(timezone.utc)
        values = self._values(task)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("""UPDATE scheduled_emails SET account=?,to_json=?,cc_json=?,subject=?,body=?,
                attachments_json=?,send_at=?,offset=?,mode=?,status=?,created_at=?,updated_at=?,sent_at=?,last_error=?
                WHERE id=? AND status='scheduled'""", values[1:] + (task_id,))
            if cur.rowcount != 1:
                conn.rollback()
                raise ValueError("scheduled task cannot be edited unless status is scheduled")
            conn.commit()
        finally:
            conn.close()
        return self.get(task_id)

    def cancel(self, task_id: str) -> ScheduledEmail:
        task = self.get(task_id)
        if task.status not in {"scheduled", "awaiting_confirmation"}:
            raise ValueError("scheduled task cannot be cancelled after sending starts")
        return self.transition(task_id, {"scheduled", "awaiting_confirmation"}, "cancelled")

    def transition(self, task_id: str, expected: set[str], new_status: str, **fields) -> ScheduledEmail:
        if new_status not in STATUSES:
            raise ValueError(f"invalid status: {new_status}")
        if not expected or any(new_status not in _ALLOWED.get(s, set()) for s in expected):
            raise ValueError(f"invalid transition to {new_status} from {sorted(expected)}")
        task = self.get(task_id)
        if task.status not in expected:
            raise ValueError(f"task status is {task.status}, expected {sorted(expected)}")
        for name, value in fields.items():
            if name not in {"last_error", "sent_at", "send_at"}:
                raise ValueError(f"invalid transition field: {name}")
            setattr(task, name, value)
        task.status = new_status
        task.updated_at = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in expected)
            cur = conn.execute(
                f"UPDATE scheduled_emails SET status=?,updated_at=?,sent_at=?,send_at=?,last_error=? WHERE id=? AND status IN ({placeholders})",
                (new_status, _iso(task.updated_at), _iso(task.sent_at) if task.sent_at else None,
                 _iso(task.send_at), task.last_error, task_id, *expected),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise ValueError("task status changed concurrently")
            conn.commit()
        finally:
            conn.close()
        return self.get(task_id)

    def claim_due(self, now: datetime) -> list[ScheduledEmail]:
        now_utc = now.astimezone(timezone.utc)
        conn = self._connect()
        claimed = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO scheduler_meta(key,value) VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_iso(now_utc),))
            rows = conn.execute(
                "SELECT * FROM scheduled_emails WHERE status='scheduled' AND mode='auto' AND send_at<=? ORDER BY send_at",
                (_iso(now_utc),),
            ).fetchall()
            for row in rows:
                cur = conn.execute("UPDATE scheduled_emails SET status='sending',updated_at=? WHERE id=? AND status='scheduled'",
                                   (_iso(now_utc), row["id"]))
                if cur.rowcount:
                    claimed.append(self._from_row(conn.execute("SELECT * FROM scheduled_emails WHERE id=?", (row["id"],)).fetchone()))
            confirm_rows = conn.execute("SELECT id FROM scheduled_emails WHERE status='scheduled' AND mode='confirm' AND send_at<=?",
                                        (_iso(now_utc),)).fetchall()
            for row in confirm_rows:
                conn.execute("UPDATE scheduled_emails SET status='awaiting_confirmation',updated_at=? WHERE id=? AND status='scheduled'",
                             (_iso(now_utc), row["id"]))
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_scheduler(self, now: datetime) -> int:
        """Recover tasks after an unclean stop and mark missed scheduled work."""
        now = now.astimezone(timezone.utc)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            was_running = conn.execute("SELECT value FROM scheduler_meta WHERE key='running'").fetchone()
            if was_running and was_running[0] == "1":
                heartbeat = conn.execute("SELECT value FROM scheduler_meta WHERE key='heartbeat' ").fetchone()
                last_heartbeat = heartbeat[0] if heartbeat else ""
                conn.execute("UPDATE scheduled_emails SET status='unknown',last_error=?,updated_at=? WHERE status='sending'",
                             ("Scheduler stopped while delivery outcome was unknown", _iso(now)))
                if last_heartbeat:
                    conn.execute("UPDATE scheduled_emails SET status='missed',updated_at=? WHERE status='scheduled' AND send_at<? AND send_at>?",
                                 (_iso(now), _iso(now), last_heartbeat))
            conn.execute("INSERT INTO scheduler_meta(key,value) VALUES('running','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            conn.execute("INSERT INTO scheduler_meta(key,value) VALUES('started_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (_iso(now),))
            conn.commit()
            return 1 if was_running and was_running[0] == "1" else 0
        finally:
            conn.close()

    def stop_scheduler(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO scheduler_meta(key,value) VALUES('running','0') ON CONFLICT(key) DO UPDATE SET value='0'")
            conn.commit()
        finally:
            conn.close()
