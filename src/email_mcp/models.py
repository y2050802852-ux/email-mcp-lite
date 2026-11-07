"""数据模型：账户、邮件元数据、邮件详情。

设计原则：所有对外输出都经过 server 层压缩为紧凑文本，
模型只承担内部类型安全，不直接暴露给 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Account:
    """一个已配置的邮箱账户。"""

    id: str  # 短标识，如 "qq"、"suda"，用于邮件 ID 与工具参数
    user: str  # 登录用户名（通常是邮箱地址）
    password: str  # 密码 / 授权码
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_ssl: bool = True
    from_name: str = ""  # 发件时显示名
    # 可选的文件夹名覆盖（留空则自动探测）
    trash_folder: str = ""
    drafts_folder: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user": self.user,
            "password": self.password,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_ssl": self.smtp_ssl,
            "from_name": self.from_name,
            "trash_folder": self.trash_folder,
            "drafts_folder": self.drafts_folder,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Account:
        missing = [k for k in ("id", "user", "password", "imap_host") if not d.get(k)]
        if missing:
            raise ValueError(f"账户配置缺少字段: {', '.join(missing)}")
        return cls(
            id=str(d["id"]),
            user=str(d["user"]),
            password=str(d["password"]),
            imap_host=str(d["imap_host"]),
            imap_port=int(d.get("imap_port") or 993),
            smtp_host=str(d.get("smtp_host") or ""),
            smtp_port=int(d.get("smtp_port") or 465),
            smtp_ssl=bool(d.get("smtp_ssl", True)),
            from_name=str(d.get("from_name") or ""),
            trash_folder=str(d.get("trash_folder") or ""),
            drafts_folder=str(d.get("drafts_folder") or ""),
        )


@dataclass
class EmailMeta:
    """搜索结果里的邮件摘要（无正文，控制 token）。"""

    account: str
    folder: str
    uid: int
    date: str = ""  # 已格式化的本地时间字符串 "YYYY-MM-DD HH:MM"
    from_: str = ""
    subject: str = ""
    read: bool = False
    starred: bool = False
    has_attachments: bool = False

    @property
    def id(self) -> str:
        """无状态短 ID：account|folder|uid，可直接回传给工具。"""
        return format_id(self.account, self.folder, self.uid)


@dataclass
class AttachmentMeta:
    filename: str
    size: int  # 字节数
    content_type: str = ""

    @property
    def size_text(self) -> str:
        return human_size(self.size)


@dataclass
class EmailDetail:
    """get_email 的完整结果。"""

    meta: EmailMeta
    to: str = ""
    cc: str = ""
    body: str = ""  # 已转纯文本并按 body_max 截断
    truncated: bool = False
    total_body_len: int = 0
    attachments: list[AttachmentMeta] = field(default_factory=list)
    message_id: str = ""
    references: str = ""
    raw: bytes = b""  # 原始 MIME（供转发/回复引用头信息，不输出给 Agent）


def format_id(account: str, folder: str, uid: int) -> str:
    return f"{account}|{folder}|{uid}"


def parse_id(email_id: str) -> tuple[str, str, int]:
    """解析 'account|folder|uid'。folder 含 '|' 时无法解析，报清晰错误。"""
    parts = email_id.split("|")
    if len(parts) != 3:
        raise ValueError(
            f"邮件 ID 格式错误: {email_id!r}（应为 account|folder|uid，"
            "直接使用 search_emails 返回的 id 即可）"
        )
    account, folder, uid_s = parts
    try:
        uid = int(uid_s)
    except ValueError as e:
        raise ValueError(f"邮件 ID 的 uid 不是数字: {email_id!r}") from e
    return account, folder, uid


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"
