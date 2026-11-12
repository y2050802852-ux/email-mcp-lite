"""Install and inspect the macOS per-user launchd scheduler agent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.email-mcp-lite.scheduler"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _executable() -> str:
    return shutil.which("uvx") or "uvx"


def _plist_path(label: str = LABEL) -> Path:
    if label != LABEL:
        raise ValueError("only the email-mcp-lite scheduler service can be managed")
    return _launch_agents_dir() / f"{label}.plist"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("launchd service management is only available on macOS")


def _launchctl(action: str, path: Path) -> subprocess.CompletedProcess:
    _require_macos()
    uid = os.getuid()
    target = f"gui/{uid}/{LABEL}"
    if action == "bootstrap":
        return subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                              check=False, capture_output=True, text=True)
    if action == "bootout":
        return subprocess.run(["launchctl", "bootout", target], check=False,
                              capture_output=True, text=True)
    return subprocess.run(["launchctl", "print", target], check=False,
                          capture_output=True, text=True)


def install_service(label: str = LABEL, load: bool = True) -> Path:
    if load:
        _require_macos()
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir = Path.home() / ".email-mcp" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {
        "Label": LABEL,
        "ProgramArguments": [_executable(), "--from", "email-mcp-lite", "email-mcp", "scheduler", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "scheduler.log"),
        "StandardErrorPath": str(log_dir / "scheduler-error.log"),
        "WorkingDirectory": str(Path.home()),
    }
    temporary = path.with_suffix(".plist.tmp")
    temporary.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=True))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    if load:
        _launchctl("bootstrap", path)
    return path


def uninstall_service(label: str = LABEL, unload: bool = True) -> bool:
    if unload:
        _require_macos()
    path = _plist_path(label)
    if not path.exists():
        return False
    if unload:
        _launchctl("bootout", path)
    path.unlink()
    return True


def service_status(label: str = LABEL) -> str:
    _require_macos()
    path = _plist_path(label)
    if not path.exists():
        return "not installed"
    result = _launchctl("print", path)
    return "running" if result.returncode == 0 else "installed, not running"
