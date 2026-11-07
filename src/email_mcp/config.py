"""账户凭证管理：~/.email-mcp/accounts.json，权限 600。

明文存储的安全取舍与风险说明见 README「安全说明」一节。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

from .models import Account

CONFIG_DIR = Path(os.environ.get("EMAIL_MCP_CONFIG_DIR", "~/.email-mcp")).expanduser()
CONFIG_FILE = CONFIG_DIR / "accounts.json"

# 常见服务商预设（IMAP 993 / SMTP 465 SSL）
PRESETS: dict[str, dict] = {
    "qq": {
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "note": "密码请用 QQ 邮箱设置→账户→开启 IMAP/SMTP 后生成的授权码",
    },
    "163": {
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "note": "密码请用 163 邮箱设置→POP3/IMAP/SMTP→开启并生成的客户端授权码",
    },
    "gmail": {
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "note": "需开启两步验证后生成 App Password；国内网络可能需要代理",
    },
}


def load_accounts() -> list[Account]:
    if not CONFIG_FILE.exists():
        return []
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"账户配置文件解析失败: {CONFIG_FILE}\n{e}\n"
            "可手动修复，或删除该文件后用 email-mcp add 重新录入。"
        ) from e
    items = data.get("accounts", data if isinstance(data, list) else [])
    accounts, seen = [], set()
    for item in items:
        acc = Account.from_dict(item)
        if acc.id in seen:
            raise ValueError(f"账户 ID 重复: {acc.id}（{CONFIG_FILE}）")
        seen.add(acc.id)
        accounts.append(acc)
    return accounts


def save_account(account: Account) -> None:
    accounts = load_accounts()
    accounts = [a for a in accounts if a.id != account.id]
    accounts.append(account)
    accounts.sort(key=lambda a: a.id)
    _write(accounts)


def remove_account(account_id: str) -> bool:
    accounts = load_accounts()
    kept = [a for a in accounts if a.id != account_id]
    if len(kept) == len(accounts):
        return False
    _write(kept)
    return True


def get_account(account_id: str) -> Optional[Account]:
    for a in load_accounts():
        if a.id == account_id:
            return a
    return None


def _write(accounts: list[Account]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"accounts": [a.to_dict() for a in accounts]}, ensure_ascii=False, indent=2
    )
    # 以 600 权限原子写入；已存在的文件也强制收紧权限
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, CONFIG_FILE)
