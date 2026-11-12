"""FastMCP 工具层：9 个工具，全部返回紧凑文本以控制 token 体积。

约定：
- 返回值是面向 Agent 的纯文本（非 JSON），字段顺序固定，见各工具 docstring
- 所有会改动邮箱的工具先过 _writable()（EMAIL_MCP_READONLY=1 时整体只读）
- 邮件 ID 形如 'account|folder|uid'，由 search_emails 返回、原样回传
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from mcp.server.mcpserver import MCPServer

from .adapter import get_adapter
from .config import CONFIG_FILE, get_account, load_accounts
from .scheduled_store import ScheduledEmail, ScheduledEmailStore
from .models import parse_id

mcp = MCPServer(
    "email",
    instructions=(
        "多账户邮件工具集（IMAP/SMTP）。首次使用：先调用 list_accounts；"
        "若显示尚未配置账户，引导用户在 email-mcp 项目目录运行 "
        "`uv run email-mcp web` 打开本地网页添加邮箱（或 `uv run email-mcp add` 交互录入），"
        "完成后立即生效、无需重启服务端。"
        "邮件 ID 形如 account|folder|uid，一律从 search_emails 的结果原样回传。"
        "send_email / delete_email 属于危险操作，建议用户在客户端开启人工确认。"
    ),
)


def _readonly_mode() -> bool:
    return os.environ.get("EMAIL_MCP_READONLY", "").lower() not in ("", "0", "false", "no", "off")


def _writable() -> None:
    if _readonly_mode():
        raise RuntimeError(
            "服务器处于只读模式（EMAIL_MCP_READONLY=1），发送/删除/移动等操作被禁止")


_adapter_cache: dict[str, tuple[float, object]] = {}


def _get_adapter(account_id: str):
    """按账户取适配器；配置文件 mtime 变化时自动重建（CLI 加账户后无需重启）。"""
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _adapter_cache.get(account_id)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    ad = get_adapter(account_id)
    _adapter_cache[account_id] = (mtime, ad)
    return ad


def _resolve_account(account: str) -> str:
    """账户参数留空且只配置了一个账户时自动选择。"""
    if account:
        return account
    ids = [a.id for a in load_accounts()]
    if len(ids) == 1:
        return ids[0]
    hint = ", ".join(ids) or "无"
    raise ValueError(
        f"未指定 account 参数，且当前账户不止一个（{hint}），请显式指定 account")


# ---------------- 账户 / 文件夹 ----------------


@mcp.tool()
def list_accounts() -> str:
    """列出已配置的邮件账户（id、登录用户、IMAP 服务器）。返回为空时说明尚未初始化，见返回值中的指引。"""
    accs = load_accounts()
    if not accs:
        return (
            "尚未配置账户。请引导用户二选一完成初始化：\n"
            "① 在 email-mcp 项目目录运行 `uv run email-mcp web`（推荐，打开本地网页管理）；\n"
            "② 运行 `uv run email-mcp add` 交互式录入。\n"
            "完成后自动生效，无需重启服务端；密码等凭证由用户亲自输入，不经过 Agent。"
        )
    rows = [f"{a.id}: {a.user} (imap {a.imap_host})" for a in accs]
    if _readonly_mode():
        rows.append("[当前只读模式 EMAIL_MCP_READONLY=1]")
    return "\n".join(rows)


@mcp.tool()
def list_folders(account: str = "") -> str:
    """列出账户的 IMAP 文件夹及邮件数/未读数，每行: 名称 (N封/M未读)。account 留空且仅一个账户时自动选择。"""
    ad = _get_adapter(_resolve_account(account))
    rows = [f"{name} ({total}封/{unseen}未读)"
            for name, total, unseen in ad.list_folders()]
    return "\n".join(rows) or "（无文件夹）"


# ---------------- 搜索 / 读取 ----------------


@mcp.tool()
def search_emails(account: str = "", folder: str = "INBOX", query: str = "",
                  field: str = "subject", since: str = "", before: str = "",
                  unread_only: bool = False, limit: int = 20) -> str:
    """搜索邮件，返回紧凑元数据（不含正文，省 token），每行: id | 日期 | 发件人 | 主题 [标记]。
    field: subject/from/to/text(text=含正文全文检索，较慢); since/before: 'YYYY-MM-DD';
    空 query 且无日期 = 列出全部（新邮件在前）。
    返回的 id 直接回传给 get_email/mark_email/move_email/delete_email/send_email。"""
    ad = _get_adapter(_resolve_account(account))
    metas = ad.search(folder, query=query, field=field, since=since,
                      before=before, unread_only=unread_only, limit=limit)
    if not metas:
        return "未找到匹配邮件"
    lines = []
    for m in metas:
        flags = ""
        if not m.read:
            flags += " [未读]"
        if m.starred:
            flags += " [星标]"
        if m.has_attachments:
            flags += " [附件]"
        lines.append(f"{m.id} | {m.date} | {m.from_} | {m.subject}{flags}")
    return f"共 {len(metas)} 封:\n" + "\n".join(lines)


@mcp.tool()
def get_email(id: str, body_max: int = 2000) -> str:
    """读取邮件详情：头部 + 纯文本正文（默认截断 2000 字符）+ 附件清单（仅元数据）。
    body_max=0 不取正文；想读全文可加大该值。id 来自 search_emails。"""
    account_id, _, _ = parse_id(id)
    d = _get_adapter(account_id).get(id, body_max=body_max)
    m = d.meta
    lines = [f"From: {m.from_}", f"To: {d.to}"]
    if d.cc:
        lines.append(f"Cc: {d.cc}")
    lines += [f"Date: {m.date}", f"Subject: {m.subject}"]
    if d.attachments:
        lines.append("Attachments: " + ", ".join(
            f"{a.filename}({a.size_text})" for a in d.attachments))
    lines.append("")
    if body_max > 0:
        lines.append(d.body or "（此邮件无文本正文）")
        if d.truncated:
            lines.append(f"（正文已截断，全文 {d.total_body_len} 字符；"
                         "可用 get_email 加大 body_max 获取更多）")
    return "\n".join(lines)


@mcp.tool()
def get_attachments(id: str, attachment: str = "", save_dir: str = "") -> str:
    """把邮件附件保存到本地并返回路径列表（附件内容绝不内联进响应）。
    attachment 留空=全部；可传序号（1 起）或文件名关键词。save_dir 留空默认 ~/Downloads/email-mcp/<账户>/。"""
    account_id, _, _ = parse_id(id)
    saved = _get_adapter(account_id).save_attachments(id, save_dir, attachment)
    return "已保存:\n" + "\n".join(saved)


# ---------------- 发送 ----------------


@mcp.tool()
def send_email(account: str = "", to: str | list[str] = "", cc: str | list[str] = "",
               subject: str = "", body: str = "",
               attachments: Optional[list[str]] = None,
               in_reply_to: str = "", forward_of: str = "",
               reply_all: bool = False, draft: bool = False) -> str:
    """发送邮件；draft=True 时改为存草稿不发信。
    to/cc: 地址或 '名字 <a@b>'，多个用列表或逗号分隔。
    回复: 传 in_reply_to=邮件id（自动设置引用头、收件人为原发件人，reply_all=True 时带上原抄送）。
    转发: 传 forward_of=邮件id（自动引用原文；原附件不自动带上，可先 get_attachments 再经 attachments 发出）。
    in_reply_to 与 forward_of 互斥。subject/body 留空时回复/转发场景自动补全。"""
    _writable()
    from .adapter import build_message

    account_id = _resolve_account(account)
    ad = _get_adapter(account_id)
    to_list = _addr_list(to)
    cc_list = _addr_list(cc)
    subject_final, body_final = subject.strip(), body
    irt, refs = "", ""

    if in_reply_to and forward_of:
        raise ValueError("in_reply_to 与 forward_of 只能二选一")

    if in_reply_to:
        orig = ad.get(in_reply_to, body_max=2000)
        own = ad.account.user.lower()
        if reply_all:
            to_list = to_list or _reply_all_to(orig, own)
            cc_list = cc_list or _reply_all_cc(orig, own)
        else:
            to_list = to_list or ([orig.meta.from_] if orig.meta.from_ else [])
        irt = orig.message_id
        refs = (orig.references + " " + orig.message_id).strip()
        subject_final = subject_final or _re_subject(orig.meta.subject)
    elif forward_of:
        orig = ad.get(forward_of, body_max=2000)
        subject_final = subject_final or ("Fwd: " + orig.meta.subject
                                          if orig.meta.subject else "Fwd")
        body_final = ((body.strip() + "\n\n") if body.strip() else "") + \
            _forward_block(orig)
    if not to_list and not draft:
        raise ValueError("缺少收件人 to（回复场景应传 in_reply_to）")
    if not subject_final:
        raise ValueError("缺少主题 subject")
    if not body_final and not draft:
        raise ValueError("缺少正文 body")

    msg = build_message(ad.account, to_list, cc_list, subject_final,
                        body_final or "（空）",
                        attachment_paths=attachments,
                        in_reply_to=irt, references=refs)
    if draft:
        folder = ad.create_draft(msg)
        return f"草稿已保存到 {account_id} 的 {folder} 文件夹"
    mid = ad.send(msg)
    return f"已发送: To={msg['To']}; Message-ID={mid}"


def _addr_list(value: str | list[str]) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    return [str(v).strip() for v in value if str(v).strip()]


def _re_subject(subject: str) -> str:
    s = subject.strip()
    return s if s.lower().startswith("re:") else "Re: " + s


def _reply_all_to(orig, own: str) -> list[str]:
    import email.utils

    addrs = []
    if orig.meta.from_:
        addrs.append(orig.meta.from_)
    for _, addr in email.utils.getaddresses([orig.to]):
        if addr and addr.lower() != own and addr not in addrs:
            addrs.append(addr)
    return addrs


def _reply_all_cc(orig, own: str) -> list[str]:
    import email.utils

    addrs = []
    for _, addr in email.utils.getaddresses([orig.cc]):
        if addr and addr.lower() != own and addr not in addrs:
            addrs.append(addr)
    return addrs


def _forward_block(d) -> str:
    lines = ["---------- 转发邮件 ----------",
             f"From: {d.meta.from_}",
             f"Date: {d.meta.date}",
             f"Subject: {d.meta.subject}"]
    if d.to:
        lines.append(f"To: {d.to}")
    lines += ["", d.body]
    if d.truncated:
        lines.append(f"（原文较长已截断，全文 {d.total_body_len} 字符）")
    return "\n".join(lines)


# ---------------- 管理操作 ----------------


@mcp.tool()
def mark_email(id: str, read: Optional[bool] = None,
               starred: Optional[bool] = None) -> str:
    """标记邮件：read=True 已读 / False 未读；starred=True 加星 / False 去星。至少传一个。"""
    _writable()
    account_id, _, _ = parse_id(id)
    return _get_adapter(account_id).mark(id, read=read, starred=starred)


@mcp.tool()
def move_email(id: str, to_folder: str) -> str:
    """移动邮件到指定文件夹（归档=移到 Archive 类文件夹）。to_folder 名称来自 list_folders。"""
    _writable()
    account_id, _, _ = parse_id(id)
    return _get_adapter(account_id).move(id, to_folder)


@mcp.tool()
def delete_email(id: str, permanent: bool = False) -> str:
    """删除邮件。默认软删除=移入回收站；permanent=True 彻底删除（不可恢复，建议客户端配置人工确认）。
    注意：删除已在回收站里的邮件会直接彻底删除。"""
    _writable()
    account_id, _, _ = parse_id(id)
    return _get_adapter(account_id).delete(id, permanent=permanent)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _scheduled_store() -> ScheduledEmailStore:
    return ScheduledEmailStore()


def _addresses(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return _addr_list(value)
    return _addr_list(value)


@mcp.tool()
def schedule_email(account: str, to: str | list[str], send_at: str, subject: str,
                   body: str, cc: str | list[str] = "",
                   attachments: Optional[list[str]] = None, mode: str = "auto") -> str:
    """安排邮件定时发送。send_at 必须是带时区偏移的 ISO 8601 时间；mode 为 auto 或 confirm。"""
    _writable()
    try:
        scheduled_local = datetime.fromisoformat(send_at)
    except ValueError as e:
        raise ValueError("send_at 必须是带 UTC offset 的 ISO 8601 时间") from e
    if scheduled_local.tzinfo is None or scheduled_local.utcoffset() is None:
        raise ValueError("send_at 必须包含 UTC offset")
    now = _utc_now()
    scheduled_utc = scheduled_local.astimezone(timezone.utc)
    if scheduled_utc <= now:
        raise ValueError("send_at 必须是未来时间")
    if mode not in {"auto", "confirm"}:
        raise ValueError("mode 必须是 auto 或 confirm")
    if not get_account(account):
        raise ValueError(f"邮箱账户不存在: {account}")
    recipients = _addresses(to)
    if not recipients:
        raise ValueError("缺少收件人 to")
    if not subject.strip() or not body:
        raise ValueError("主题和正文不能为空")
    task = _scheduled_store().create(ScheduledEmail(
        account=account, to=recipients, cc=_addresses(cc), subject=subject.strip(), body=body,
        attachments=[os.path.abspath(os.path.expanduser(p)) for p in (attachments or [])],
        send_at=scheduled_utc, offset=scheduled_local.strftime("%z"), mode=mode,
    ))
    return f"任务 {task.id} 已安排: To={', '.join(task.to)}; Subject={task.subject}; SendAt={send_at}; Mode={mode}; Status={task.status}"


@mcp.tool()
def list_scheduled_emails(status: str = "", limit: int = 50) -> str:
    """列出定时邮件任务摘要；不会显示邮件正文或凭证。"""
    tasks = _scheduled_store().list(status=status, limit=limit)
    if not tasks:
        return "没有定时邮件任务"
    return "\n".join(
        f"{t.id} | {t.status} | {t.account} | {', '.join(t.to)} | {t.subject} | {_iso_local(t.send_at, t.offset)}"
        for t in tasks
    )


def _iso_local(value: datetime, offset: str) -> str:
    sign = 1 if offset.startswith("+") else -1
    digits = offset.lstrip("+-")
    hours, minutes = int(digits[:2]), int(digits[2:] or "0")
    from datetime import timedelta
    local = value.astimezone(timezone(timedelta(minutes=sign * (hours * 60 + minutes))))
    return local.isoformat()


@mcp.tool()
def edit_scheduled_email(task_id: str, send_at: str = "", to: str | list[str] | None = None,
                         cc: str | list[str] | None = None, subject: str = "", body: str = "",
                         attachments: Optional[list[str]] = None, mode: str = "") -> str:
    """编辑尚未到期的定时任务；只传需要更改的字段。"""
    _writable()
    changes = {}
    if send_at:
        parsed = datetime.fromisoformat(send_at)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("send_at 必须包含 UTC offset")
        if parsed.astimezone(timezone.utc) <= _utc_now():
            raise ValueError("send_at 必须是未来时间")
        changes.update(send_at=parsed.astimezone(timezone.utc), offset=parsed.strftime("%z"))
    if to is not None:
        recipients = _addresses(to)
        if not recipients:
            raise ValueError("缺少收件人 to")
        changes["to"] = recipients
    if cc is not None:
        changes["cc"] = _addresses(cc)
    if subject:
        if not subject.strip():
            raise ValueError("主题不能为空")
        changes["subject"] = subject.strip()
    if body:
        changes["body"] = body
    if attachments is not None:
        changes["attachments"] = [os.path.abspath(os.path.expanduser(p)) for p in attachments]
    if mode:
        if mode not in {"auto", "confirm"}:
            raise ValueError("mode 必须是 auto 或 confirm")
        changes["mode"] = mode
    task = _scheduled_store().update(task_id, changes)
    return f"任务 {task.id} 已更新，状态={task.status}"


@mcp.tool()
def cancel_scheduled_email(task_id: str) -> str:
    """取消尚未开始发送的定时任务。"""
    _writable()
    task = _scheduled_store().cancel(task_id)
    return f"任务 {task.id} 已取消"


@mcp.tool()
def confirm_scheduled_email(task_id: str) -> str:
    """立即发送已到期且等待确认的任务。"""
    _writable()
    from .scheduler import Scheduler
    task = Scheduler(_scheduled_store(), clock=_utc_now).confirm(task_id)
    return f"任务 {task.id} 状态={task.status}" + ("，已发送" if task.status == "sent" else "")


@mcp.tool()
def retry_scheduled_email(task_id: str) -> str:
    """明确重试失败或结果未知的定时邮件；结果未知时可能造成重复发送。"""
    _writable()
    from .scheduler import Scheduler
    task = Scheduler(_scheduled_store(), clock=_utc_now).retry(task_id)
    warning = "；原发送结果未知，本次重试可能导致重复发送" if "previous delivery outcome was unknown" in task.last_error else ""
    return f"任务 {task.id} 状态={task.status}{warning}"


def run() -> None:
    """启动 MCP 服务端（stdio 传输）。"""
    mcp.run(transport="stdio")
