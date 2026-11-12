import plistlib

import pytest

from email_mcp import service
from email_mcp import cli


def test_install_writes_launch_agent_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_launch_agents_dir", lambda: tmp_path / "Library" / "LaunchAgents")
    monkeypatch.setattr(service, "_executable", lambda: "/usr/local/bin/uvx")

    path = service.install_service(load=False)

    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == service.LABEL
    assert data["ProgramArguments"] == [
        "/usr/local/bin/uvx", "--from", "email-mcp-lite", "email-mcp", "scheduler", "run"
    ]
    assert data["RunAtLoad"] is True
    assert path.stat().st_mode & 0o777 == 0o600


def test_uninstall_only_removes_our_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_launch_agents_dir", lambda: tmp_path)
    path = tmp_path / f"{service.LABEL}.plist"
    path.write_text("test")

    assert service.uninstall_service(unload=False) is True
    assert not path.exists()
    assert service.uninstall_service(unload=False) is False


def test_cli_routes_service_status(monkeypatch, capsys):
    monkeypatch.setattr(service, "service_status", lambda: "running")
    cli.main(["service", "status"])
    assert "running" in capsys.readouterr().out
