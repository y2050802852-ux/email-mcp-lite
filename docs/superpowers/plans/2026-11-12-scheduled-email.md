# Scheduled Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent scheduled email delivery managed through MCP and executed by an installable macOS launchd service.

**Architecture:** Store scheduled messages and state transitions in a permission-restricted SQLite database. Keep persistence/state-machine logic, worker delivery, MCP management tools, and launchd/CLI lifecycle in focused modules; the worker sends through the existing `build_message` and adapter `send` path.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, `datetime`, `plistlib`, existing FastMCP and IMAP/SMTP adapters, pytest.

## Global Constraints

- Python support remains `>=3.10`.
- Schedule input must be ISO 8601 with an explicit UTC offset.
- Persist task data under the configured `~/.email-mcp` directory with directory mode `0700` and database mode `0600`.
- The scheduler must not auto-send overdue scheduled tasks; mark them `missed`.
- Uncertain SMTP outcomes must become `unknown`, never auto-retry.
- No real mailbox or launchd registration in tests.
- Bump package version to `0.2.0` and synchronize `src/email_mcp/__init__.py` and `CHANGELOG.md`.

---

## File map

- Create `src/email_mcp/scheduled_store.py`: SQLite schema, secure setup, CRUD, state transitions and atomic task claiming.
- Create `src/email_mcp/scheduler.py`: due-task processing, recovery, injected clock and mail-delivery orchestration.
- Create `src/email_mcp/service.py`: macOS launchd plist installation/removal/status and safe path handling.
- Modify `src/email_mcp/config.py`: expose shared config directory/database path.
- Modify `src/email_mcp/adapter.py`: classify SMTP send-stage uncertainty without changing immediate-send behavior.
- Modify `src/email_mcp/server.py`: six MCP tools for schedule/list/edit/cancel/confirm/retry.
- Modify `src/email_mcp/cli.py`: scheduler foreground and service lifecycle commands.
- Create `tests/test_scheduled_store.py`, `tests/test_scheduler.py`, `tests/test_service.py`; extend `tests/test_server.py`.
- Modify `README.zh-CN.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `src/email_mcp/__init__.py`.
- Keep the accepted spec at `docs/superpowers/specs/2026-11-12-scheduled-email-design.md`.

## Task 1: Secure SQLite task store and lifecycle

**Files:** Create `src/email_mcp/scheduled_store.py`, `tests/test_scheduled_store.py`; modify `src/email_mcp/config.py`.

**Interfaces:**
- `ScheduledEmailStore(path: Path | None = None)` creates a private SQLite database in `CONFIG_DIR / "scheduled-emails.sqlite3"` by default.
- `create(task: ScheduledEmail) -> ScheduledEmail`, `get(task_id: str) -> ScheduledEmail`, `list(status: str = "", limit: int = 50) -> list[ScheduledEmail]`.
- `update(task_id: str, changes: dict) -> ScheduledEmail` only for `scheduled` tasks.
- `cancel(task_id: str) -> ScheduledEmail` only before `sending`.
- `claim_due(now: datetime) -> list[ScheduledEmail]` atomically claims only eligible, on-time automatic tasks.
- `transition(task_id: str, expected: set[str], new_status: str, **fields) -> ScheduledEmail` enforces allowed transitions.
- Task statuses: `scheduled`, `awaiting_confirmation`, `sending`, `sent`, `failed`, `unknown`, `missed`, `cancelled`.

- [ ] Write failing tests for database path, directory/file modes, create/get/list and persisted restart state.
- [ ] Run `uv run pytest tests/test_scheduled_store.py -q`; confirm missing module/API failures.
- [ ] Implement dataclass serialization and schema with fields: id, account, to, cc, subject, body, attachment paths JSON, scheduled UTC timestamp, original offset string, mode, status, created/updated timestamps, sent timestamp, last error.
- [ ] Add status transition validation and transactional atomic claim; mark due-past scheduled tasks `missed` during recovery instead of claiming them.
- [ ] Add tests for rejected edits/cancels after claim, invalid transitions, transaction rollback, and missed-task handling.
- [ ] Run `uv run pytest tests/test_scheduled_store.py -q`; expect all store tests pass.

## Task 2: Delivery worker, recovery and uncertain outcomes

**Files:** Create `src/email_mcp/scheduler.py`, `tests/test_scheduler.py`; modify `src/email_mcp/adapter.py` if a typed uncertainty exception is needed.

**Interfaces:**
- `Scheduler(store, adapter_factory=get_adapter, clock=utc_now)`.
- `run_once() -> int` recovers stale tasks, claims eligible tasks, delivers them, and returns count processed.
- `run_forever(poll_interval: float = 15.0) -> None` repeatedly calls `run_once` until interrupted.
- `send_task(task, adapter_factory) -> str` uses `build_message` and the existing adapter `send` API.
- `UncertainDeliveryError` distinguishes failure during SMTP message transmission from connection/login failure before send begins.

- [ ] Write fake-clock/fake-adapter tests for on-time delivery, not-yet-due tasks, auto mode, confirmation mode, and missing account/attachment failures.
- [ ] Run `uv run pytest tests/test_scheduler.py -q`; confirm failures before implementation.
- [ ] Implement validation and message construction through `build_message(account, to, cc, subject, body, attachment_paths=...)`.
- [ ] Ensure an overdue scheduled task transitions to `missed`, while a due confirmation-mode task becomes `awaiting_confirmation`.
- [ ] On deterministic pre-send error transition to `failed`; on send-stage uncertainty transition to `unknown`; on success transition to `sent` and record completion time.
- [ ] On startup, transition abandoned `sending` tasks to `unknown`; do not auto retry.
- [ ] Add manual confirm and retry methods; retry sets status to `scheduled` and scheduled time to current UTC, requiring an explicit caller action for `unknown`.
- [ ] Run `uv run pytest tests/test_scheduler.py tests/test_adapter.py -q`; expect pass without network.

## Task 3: MCP management tools and input safety

**Files:** Modify `src/email_mcp/server.py`, `tests/test_server.py`; add server-facing store tests if needed.

**Interfaces:**
- Add tools `schedule_email`, `list_scheduled_emails`, `edit_scheduled_email`, `cancel_scheduled_email`, `confirm_scheduled_email`, `retry_scheduled_email`.
- `schedule_email(account: str, to: str | list[str], send_at: str, subject: str, body: str, cc: str | list[str] = "", attachments: list[str] | None = None, mode: str = "auto") -> str`.
- Parse `send_at` with `datetime.fromisoformat`; require explicit offset and future instant; preserve offset string and store UTC.
- Use the existing `_writable()` guard for all mutations.

- [ ] Add failing tests for ISO timestamp with offset, missing offset, past times, invalid mode, account validation, list body redaction, mutation read-only mode, and each lifecycle operation.
- [ ] Run `uv run pytest tests/test_server.py -q`; confirm failures.
- [ ] Implement tools with concise tool docstrings and status summaries; list output must omit body and credentials.
- [ ] Confirm `unknown` retry responses explicitly state duplicate-send possibility.
- [ ] Run `uv run pytest tests/test_server.py -q`; expect pass.

## Task 4: macOS launchd service and CLI lifecycle

**Files:** Create `src/email_mcp/service.py`, `tests/test_service.py`; modify `src/email_mcp/cli.py`.

**Interfaces:**
- `install_service(label: str = "com.email-mcp-lite.scheduler") -> Path` writes a user LaunchAgent plist under `~/Library/LaunchAgents`.
- `uninstall_service(label: str = "com.email-mcp-lite.scheduler") -> bool` removes only this app's plist and unloads it if supported.
- `service_status(label: str = "com.email-mcp-lite.scheduler") -> str` reports installed/running state.
- CLI subcommands: `scheduler run`, `service install`, `service uninstall`, `service status`.
- LaunchAgent uses the resolved current executable and absolute paths; tests must not invoke `launchctl` against the host.

- [ ] Write tests using temporary home/config paths for plist program/arguments, label, logs, escaping, idempotent install, and safe uninstall.
- [ ] Run `uv run pytest tests/test_service.py -q`; confirm failures.
- [ ] Implement plist generation and filesystem-only test hooks; call `launchctl` only in explicit CLI execution paths on macOS.
- [ ] Implement CLI argument routing and clear non-macOS error messages for service operations; keep `scheduler run` usable in foreground on supported platforms.
- [ ] Run `uv run pytest tests/test_service.py -q`; expect pass without modifying the real LaunchAgents directory.

## Task 5: Documentation, version, and full verification

**Files:** Modify `README.zh-CN.md`, `README.md`, `CHANGELOG.md`, `pyproject.toml`, `src/email_mcp/__init__.py`.

- [ ] Document service install/status/uninstall, the need for the user login session to be active, MCP tools and parameter examples, all statuses, missed and unknown handling, and private local storage.
- [ ] Bump project and module version to `0.2.0`; add changelog entry.
- [ ] Run `uv run pytest -q`; expect all prior and new tests pass.
- [ ] Run `uv build`; expect wheel and sdist creation.
- [ ] Run `git diff --check` and inspect `git status --short` and complete diff.
- [ ] Verify release instructions and content can be prepared locally; do not push or publish GitHub Release until the user reviews the final release contents.

## Plan self-review

- Store permissions, CRUD, state transitions, and missed recovery are covered by Task 1.
- Scheduling execution, confirmation-mode handling, deterministic errors, uncertain SMTP outcomes, and stale `sending` recovery are covered by Task 2.
- All six MCP operations, ISO offset validation, redaction, and read-only behavior are covered by Task 3.
- launchd installation, removal, status, CLI, and safe tests are covered by Task 4.
- Docs, version, test suite, build, and release preparation are covered by Task 5.
- No external GitHub writes are performed without final review of concrete release contents.
