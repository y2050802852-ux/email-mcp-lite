from datetime import datetime, timezone

import pytest

from email_mcp.scheduled_store import ScheduledEmail, ScheduledEmailStore


def task(send_at="2030-01-01T00:00:00+00:00"):
    return ScheduledEmail(
        account="qq", to=["you@example.com"], cc=[], subject="hello", body="body",
        attachments=[], send_at=datetime.fromisoformat(send_at), offset="+00:00", mode="auto",
    )


def test_store_persists_tasks_and_secures_database(tmp_path):
    path = tmp_path / "private" / "tasks.sqlite3"
    store = ScheduledEmailStore(path)
    created = store.create(task())
    reopened = ScheduledEmailStore(path)

    assert reopened.get(created.id).body == "body"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_update_and_cancel_only_before_sending(tmp_path):
    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    created = store.create(task())
    updated = store.update(created.id, {"subject": "updated"})
    assert updated.subject == "updated"
    store.transition(created.id, {"scheduled"}, "sending")

    with pytest.raises(ValueError, match="cannot be edited"):
        store.update(created.id, {"subject": "late"})
    with pytest.raises(ValueError, match="cannot be cancelled"):
        store.cancel(created.id)


def test_due_claim_is_atomic_and_claims_tasks_due_during_runtime(tmp_path):
    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    due = store.create(task("2030-01-01T00:00:00+00:00"))
    just_late = store.create(task("2029-12-31T23:59:00+00:00"))

    claimed = store.claim_due(datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert {item.id for item in claimed} == {due.id, just_late.id}
    assert store.get(due.id).status == "sending"
    assert store.get(just_late.id).status == "sending"


def test_invalid_transition_is_rejected(tmp_path):
    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    created = store.create(task())
    with pytest.raises(ValueError, match="invalid transition"):
        store.transition(created.id, {"scheduled"}, "sent")


def test_unclean_restart_marks_overdue_missed_and_sending_unknown(tmp_path):
    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    late = store.create(task("2029-12-31T23:59:00+00:00"))
    interrupted = store.create(task("2030-01-01T00:00:00+00:00"))
    store.transition(interrupted.id, {"scheduled"}, "sending")
    store.start_scheduler(datetime(2029, 12, 30, tzinfo=timezone.utc))
    store.claim_due(datetime(2029, 12, 30, tzinfo=timezone.utc))

    restarted = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    restarted.start_scheduler(datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert restarted.get(late.id).status == "missed"
    assert restarted.get(interrupted.id).status == "unknown"
