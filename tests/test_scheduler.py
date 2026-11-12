from datetime import datetime, timezone

from email_mcp.scheduler import Scheduler, UncertainDeliveryError
from email_mcp.scheduled_store import ScheduledEmail, ScheduledEmailStore


class FakeAdapter:
    def __init__(self, outcome="ok"):
        self.outcome = outcome
        self.sent = []

    def send(self, msg):
        if self.outcome == "uncertain":
            raise UncertainDeliveryError("connection lost after DATA")
        if self.outcome == "failed":
            raise RuntimeError("SMTP login failed")
        self.sent.append(msg)
        return "<sent@example.com>"


def make_task(store, send_at, mode="auto"):
    return store.create(ScheduledEmail(
        account="qq", to=["you@example.com"], cc=[], subject="hello", body="body",
        attachments=[], send_at=datetime.fromisoformat(send_at), offset="+00:00", mode=mode,
    ))


def test_run_once_sends_due_task_and_marks_sent(tmp_path, monkeypatch):
    import email_mcp.scheduler as module
    from email_mcp.models import Account

    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    task = make_task(store, "2030-01-01T00:00:00+00:00")
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "get_account", lambda account_id: Account(
        id="qq", user="me@example.com", password="pw", imap_host="imap", imap_port=993,
        smtp_host="smtp", smtp_port=465))

    scheduler = Scheduler(store, adapter_factory=lambda _: adapter,
                          clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert scheduler.run_once() == 1
    assert len(adapter.sent) == 1
    assert store.get(task.id).status == "sent"


def test_confirm_mode_waits_until_explicit_confirmation(tmp_path, monkeypatch):
    import email_mcp.scheduler as module
    from email_mcp.models import Account

    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    task = make_task(store, "2030-01-01T00:00:00+00:00", mode="confirm")
    adapter = FakeAdapter()
    monkeypatch.setattr(module, "get_account", lambda _: Account(
        id="qq", user="me@example.com", password="pw", imap_host="imap", imap_port=993,
        smtp_host="smtp", smtp_port=465))
    scheduler = Scheduler(store, adapter_factory=lambda _: adapter,
                          clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert scheduler.run_once() == 0
    assert store.get(task.id).status == "awaiting_confirmation"
    assert adapter.sent == []
    assert scheduler.confirm(task.id).status == "sent"
    assert len(adapter.sent) == 1


def test_uncertain_send_is_not_automatically_retried(tmp_path, monkeypatch):
    import email_mcp.scheduler as module
    from email_mcp.models import Account

    store = ScheduledEmailStore(tmp_path / "tasks.sqlite3")
    task = make_task(store, "2030-01-01T00:00:00+00:00")
    monkeypatch.setattr(module, "get_account", lambda _: Account(
        id="qq", user="me@example.com", password="pw", imap_host="imap", imap_port=993,
        smtp_host="smtp", smtp_port=465))
    scheduler = Scheduler(store, adapter_factory=lambda _: FakeAdapter("uncertain"),
                          clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert scheduler.run_once() == 1
    assert store.get(task.id).status == "unknown"
    assert scheduler.run_once() == 0
