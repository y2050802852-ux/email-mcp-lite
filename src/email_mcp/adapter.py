"""IMAP/SMTP 通用适配器。

统一接口（MailAdapter Protocol）留给未来可能的 Gmail API / Graph API
适配器；当前唯一实现走标准 IMAP/SMTP，零第三方依赖。

imaplib 的参数会原样进入协议命令行（不自动加引号），所以：
- 文件夹名统一走 _mb()：修改版 UTF-7 编码 + 双引号包裹
- 中文搜索词统一走 conn.literal 机制（SEARCH ... CHARSET UTF-8 <literal>）
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import time
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Optional, Protocol

from . import imap_utf7
from .config import CONFIG_FILE
from .models import Account, EmailDetail, EmailMeta, parse_id
from .parsing import build_detail, build_meta_from_headers, sanitize_filename, to_imap_date

_SOCKET_TIMEOUT = 30

_TRASH_PATTERNS = ("trash", "deleted", "已删除", "回收站", "bin")
_DRAFT_PATTERNS = ("draft", "草稿")

# 搜索结果的头部字段（fetch 用，控制服务端返回量）
_LIST_HEADER_FIELDS = b"DATE FROM SUBJECT CONTENT-TYPE CONTENT-DISPOSITION"


class MailAdapter(Protocol):
    """统一邮件适配器接口。未来 GmailAdapter 等实现同一组方法。"""

    def list_folders(self) -> list[tuple[str, int, int]]: ...
    def search(self, folder: str, query: str = "", field: str = "subject",
               since: str = "", before: str = "", unread_only: bool = False,
               limit: int = 20) -> list[EmailMeta]: ...
    def get(self, email_id: str, body_max: int = 2000) -> EmailDetail: ...
    def mark(self, email_id: str, read: Optional[bool] = None,
             starred: Optional[bool] = None) -> str: ...
    def move(self, email_id: str, to_folder: str) -> str: ...
    def delete(self, email_id: str, permanent: bool = False) -> str: ...
    def test_connection(self) -> dict: ...
    def send(self, msg: EmailMessage) -> str: ...
    def create_draft(self, msg: EmailMessage, folder: str = "") -> str: ...


def _mb(name: str) -> bytes:
    """文件夹名 → 引号包裹的修改版 UTF-7 字节（imaplib 不会自动处理）。"""
    return b'"' + imap_utf7.encode(name) + b'"'


def normalize_addrs(addrs: list[str]) -> list[str]:
    """'Name <a@b>' / 'a@b' 混合输入 → 规范化地址串列表。"""
    out = []
    for raw in addrs:
        name, addr = parseaddr(raw.strip())
        if not addr:
            raise ValueError(f"无法解析的邮件地址: {raw!r}")
        out.append(formataddr((name, addr), charset="utf-8"))
    return out


def build_message(account: Account, to: list[str], cc: list[str],
                  subject: str, body: str,
                  attachment_paths: Optional[list[str]] = None,
                  in_reply_to: str = "", references: str = "") -> EmailMessage:
    """构建待发邮件（纯文本正文 + 可选本地文件附件）。"""
    msg = EmailMessage()
    from_header = (formataddr((account.from_name, account.user), charset="utf-8")
                   if account.from_name else account.user)
    msg["From"] = from_header
    msg["To"] = ", ".join(normalize_addrs(to))
    if cc:
        msg["Cc"] = ", ".join(normalize_addrs(cc))
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = account.user.split("@")[-1] if "@" in account.user else "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body, charset="utf-8")
    for path in attachment_paths or []:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ValueError(f"附件不存在: {path}")
        data = p.read_bytes()
        msg.add_attachment(data, maintype="application", subtype="octet-stream",
                           filename=p.name)
    return msg


class ImapSmtpAdapter:
    """标准协议适配器：连接复用，失败自动重连。"""

    def __init__(self, account: Account):
        self.account = account
        self._imap: Optional[imaplib.IMAP4] = None

    # ---------- 连接管理 ----------

    def _conn(self) -> imaplib.IMAP4:
        if self._imap is not None:
            try:
                self._imap.noop()
                return self._imap
            except Exception:
                self._imap = None
        acc = self.account
        try:
            if acc.imap_port == 143:
                conn = imaplib.IMAP4(acc.imap_host, acc.imap_port,
                                     timeout=_SOCKET_TIMEOUT)
                conn.starttls()
            else:
                conn = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port,
                                         timeout=_SOCKET_TIMEOUT)
            conn.login(acc.user, acc.password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(
                f"[{acc.id}] IMAP 登录失败：请检查用户名/授权码（{e}）。"
                "QQ/163/Gmail 需在邮箱设置里生成专用授权码或 App Password，不能是网页登录密码。"
            ) from e
        except (OSError, TimeoutError) as e:
            raise RuntimeError(
                f"[{acc.id}] 连不上 IMAP 服务器 {acc.imap_host}:{acc.imap_port}（{e}）"
            ) from e
        self._imap = conn
        return conn

    def _select(self, folder: str) -> imaplib.IMAP4:
        conn = self._conn()
        typ, data = conn.select(_mb(folder))
        if typ != "OK":
            raise RuntimeError(
                f"[{self.account.id}] 打开文件夹失败: {folder}（{data}）。"
                "可用 list_folders 查看实际文件夹名。"
            )
        return conn

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    # ---------- 文件夹 ----------

    def _folder_names(self) -> list[str]:
        conn = self._conn()
        typ, data = conn.list()
        if typ != "OK":
            return []
        names: list[str] = []
        for line in data:
            if not line:
                continue
            # 形如 b'(\\HasNoChildren) "/" "INBOX"'
            m = re.match(rb'\(([^)]*)\)\s+(?:"([^"]*)"|NIL)\s+(.+)$', line)
            if not m:
                continue
            attrs = m.group(1).decode("ascii", errors="replace")
            if b"Noselect" in m.group(1):
                continue
            raw_name = m.group(3)
            if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
                raw_name = raw_name[1:-1]
            name = imap_utf7.decode(raw_name)
            names.append(name)
        return names

    def _find_folder(self, patterns: tuple[str, ...], override: str = "") -> str:
        if override:
            return override
        low_match = None
        for name in self._folder_names():
            low = name.lower()
            for p in patterns:
                if p.lower() in low:
                    return name
                if low_match is None and p in name:
                    low_match = name
        raise RuntimeError(
            f"[{self.account.id}] 未找到目标文件夹（尝试匹配: {', '.join(patterns)}）。"
            "可用 list_folders 查看实际名称，并在 accounts.json 里配置。"
        )

    def list_folders(self) -> list[tuple[str, int, int]]:
        """→ [(名称, 总数, 未读数)]。"""
        conn = self._conn()
        names = self._folder_names()
        out: list[tuple[str, int, int]] = []
        for name in names:
            total, unseen = 0, 0
            try:
                typ, data = conn.status(_mb(name), b"(MESSAGES UNSEEN)")
                if typ == "OK" and data and data[0]:
                    m = re.search(rb"MESSAGES\s+(\d+)", data[0])
                    u = re.search(rb"UNSEEN\s+(\d+)", data[0])
                    total = int(m.group(1)) if m else 0
                    unseen = int(u.group(1)) if u else 0
            except Exception:
                pass  # 个别文件夹不可统计，跳过
            out.append((name, total, unseen))
        return out

    # ---------- 搜索 / 读取 ----------

    def search(self, folder: str, query: str = "", field: str = "subject",
               since: str = "", before: str = "", unread_only: bool = False,
               limit: int = 20) -> list[EmailMeta]:
        conn = self._select(folder)
        args: list[bytes] = []
        if since:
            args += [b"SINCE", to_imap_date(since).encode("ascii")]
        if before:
            args += [b"BEFORE", to_imap_date(before).encode("ascii")]
        key = {"from": b"FROM", "to": b"TO", "text": b"TEXT"}.get(field, b"SUBJECT")
        if query:
            args += [b"CHARSET", b"UTF-8", key]
            conn.literal = query.encode("utf-8")
        elif not args:
            args = [b"ALL"]
        if unread_only:
            args += [b"UNSEEN"]
        typ, data = conn.uid("SEARCH", *args)
        if typ != "OK":
            raise RuntimeError(f"[{self.account.id}] 搜索失败: {data}")
        uids = (data[0] or b"").split()
        if not uids:
            return []
        uids.reverse()  # UID 升序=时间升序，新邮件在前
        uids = uids[: max(1, min(int(limit), 100))]
        uid_set = b",".join(uids)
        typ, data = conn.uid(
            "FETCH", uid_set,
            b"(UID FLAGS BODY.PEEK[HEADER.FIELDS (%s)])" % _LIST_HEADER_FIELDS,
        )
        if typ != "OK":
            raise RuntimeError(f"[{self.account.id}] 读取邮件列表失败: {data}")
        metas: list[EmailMeta] = []
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            line = item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
            m_uid = re.search(rb"UID (\d+)", line)
            m_flags = re.search(rb"FLAGS \(([^)]*)\)", line)
            if not m_uid:
                continue
            metas.append(build_meta_from_headers(
                self.account.id, folder, int(m_uid.group(1)),
                m_flags.group(1).decode("ascii", errors="replace") if m_flags else "",
                item[1] or b"",
            ))
        # 按 fetch 响应可能出现乱序，按请求顺序（新→旧）恢复
        metas.sort(key=lambda m: -m.uid)
        return metas

    def get(self, email_id: str, body_max: int = 2000) -> EmailDetail:
        account, folder, uid = parse_id(email_id)
        conn = self._select(folder)
        typ, data = conn.uid("FETCH", str(uid).encode("ascii"),
                             b"(UID FLAGS BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"[{account}] 读取邮件失败 uid={uid}: {data}")
        raw, flags = None, ""
        for item in data or []:
            if isinstance(item, tuple):
                line = item[0] if isinstance(item[0], bytes) else str(item[0]).encode()
                m = re.search(rb"FLAGS \(([^)]*)\)", line)
                flags = m.group(1).decode("ascii", errors="replace") if m else ""
                raw = item[1]
                break
        if raw is None:
            raise RuntimeError(f"[{account}] 未找到邮件 {email_id}（可能已被移动或删除）")
        return build_detail(account, folder, uid, flags, raw, body_max)

    def save_attachments(self, email_id: str, save_dir: str = "",
                         attachment: str = "") -> list[str]:
        """附件落盘（绝不内联进响应，防 token 爆炸）→ 返回保存路径列表。"""
        detail = self.get(email_id, body_max=0)
        if not detail.attachments:
            raise ValueError(f"邮件 {email_id} 没有附件")
        if attachment:
            wanted = self._select_attachments(detail, attachment)
        else:
            wanted = detail.attachments
        base = (Path(save_dir).expanduser() if save_dir
                else Path.home() / "Downloads" / "email-mcp" / self.account.id)
        base.mkdir(parents=True, exist_ok=True)
        msg = _reparse(detail.raw)
        saved: list[str] = []
        for meta in wanted:
            payload = None
            for part in msg.walk():
                if (sanitize_filename(str(part.get_filename() or ""))
                        == meta.filename and not part.is_multipart()):
                    payload = part.get_payload(decode=True)
                    break
            if payload is None:
                continue
            path = _unique_path(base / meta.filename)
            path.write_bytes(payload)
            saved.append(str(path))
        if not saved:
            raise ValueError(f"没有匹配的附件: {attachment!r}")
        return saved

    @staticmethod
    def _select_attachments(detail: EmailDetail, attachment: str):
        a = attachment.strip()
        if a.isdigit():
            idx = int(a) - 1
            if not (0 <= idx < len(detail.attachments)):
                raise ValueError(
                    f"附件序号 {a} 超出范围（共 {len(detail.attachments)} 个）")
            return [detail.attachments[idx]]
        matched = [m for m in detail.attachments if a in m.filename]
        if not matched:
            raise ValueError(
                f"没有名称包含 {a!r} 的附件。现有: "
                + ", ".join(m.filename for m in detail.attachments))
        return matched

    # ---------- 管理操作 ----------

    def mark(self, email_id: str, read: Optional[bool] = None,
             starred: Optional[bool] = None) -> str:
        if read is None and starred is None:
            raise ValueError("read 和 starred 至少指定一个")
        account, folder, uid = parse_id(email_id)
        conn = self._select(folder)
        uid_b = str(uid).encode("ascii")
        if read is not None:
            op = b"+FLAGS" if read else b"-FLAGS"
            conn.uid("STORE", uid_b, op, b"(\\Seen)")
        if starred is not None:
            op = b"+FLAGS" if starred else b"-FLAGS"
            conn.uid("STORE", uid_b, op, b"(\\Flagged)")
        parts = []
        if read is not None:
            parts.append("已读" if read else "未读")
        if starred is not None:
            parts.append("加星" if starred else "去星")
        return f"{email_id}: {'、'.join(parts)}完成"

    def move(self, email_id: str, to_folder: str) -> str:
        account, folder, uid = parse_id(email_id)
        conn = self._select(folder)
        uid_b = str(uid).encode("ascii")
        try:
            typ, data = conn.uid("MOVE", uid_b, _mb(to_folder))
        except imaplib.IMAP4.error:
            # imaplib 对 BAD 响应直接抛异常（如服务器不支持 UID MOVE）
            typ, data = "BAD", [b"MOVE not supported"]
        if typ != "OK":
            # 服务器不支持 UID MOVE：退化为 COPY + 删除原件
            conn.uid("COPY", uid_b, _mb(to_folder))
            conn.uid("STORE", uid_b, b"+FLAGS.SILENT", b"(\\Deleted)")
            conn.uid("EXPUNGE", uid_b)
        return f"{email_id} → {to_folder} 完成"

    def delete(self, email_id: str, permanent: bool = False) -> str:
        account, folder, uid = parse_id(email_id)
        uid_b = str(uid).encode("ascii")
        if not permanent:
            trash = self._find_folder(_TRASH_PATTERNS, self.account.trash_folder)
            if trash == folder:
                # 已在回收站的邮件，删除即彻底删除
                permanent = True
            else:
                return self.move(email_id, trash) + "（已移入回收站）"
        conn = self._select(folder)
        conn.uid("STORE", uid_b, b"+FLAGS.SILENT", b"(\\Deleted)")
        typ, _ = conn.uid("EXPUNGE", uid_b)
        if typ != "OK":
            conn.expunge()  # 兜底：清空本文件夹所有已标记删除项
        return f"{email_id} 已永久删除"

    # ---------- 发送 / 草稿 ----------

    def _smtp(self) -> smtplib.SMTP:
        acc = self.account
        host = acc.smtp_host or acc.imap_host
        try:
            if acc.smtp_ssl:
                s = smtplib.SMTP_SSL(host, acc.smtp_port, timeout=_SOCKET_TIMEOUT)
            else:
                s = smtplib.SMTP(host, acc.smtp_port, timeout=_SOCKET_TIMEOUT)
                s.starttls()
            s.login(acc.user, acc.password)
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(
                f"[{acc.id}] SMTP 登录失败：请检查授权码（{e}）") from e
        except (OSError, TimeoutError) as e:
            raise RuntimeError(
                f"[{acc.id}] 连不上 SMTP 服务器 {host}:{acc.smtp_port}（{e}）") from e
        return s

    def test_connection(self) -> dict:
        """连通性自检：IMAP 登录+列文件夹、SMTP 登录。供 web UI 在保存前调用。"""
        result = {"imap_ok": False, "smtp_ok": False,
                  "folders": 0, "error": ""}
        try:
            result["folders"] = len(self.list_folders())
            result["imap_ok"] = True
        except Exception as e:
            result["error"] = f"IMAP: {e}"
            return result
        try:
            smtp = self._smtp()
            smtp.quit()
            result["smtp_ok"] = True
        except Exception as e:
            result["smtp_error"] = str(e)[:200]
        return result

    def send(self, msg: EmailMessage) -> str:
        smtp = self._smtp()
        try:
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return str(msg["Message-ID"])

    def create_draft(self, msg: EmailMessage, folder: str = "") -> str:
        drafts = folder or self._find_folder(_DRAFT_PATTERNS, self.account.drafts_folder)
        conn = self._conn()
        raw = msg.as_bytes(policy=policy.SMTP)  # SMTP policy 保证 CRLF 行尾
        conn.append(_mb(drafts), None, None, raw)
        return drafts


def _reparse(raw: bytes):
    from email.parser import BytesParser
    return BytesParser(policy=policy.default).parsebytes(raw)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{int(time.time())}{suffix}")


def get_adapter(account_id: str, accounts: Optional[list[Account]] = None):
    """按账户 ID 构建适配器。供 server 层与测试使用。"""
    from .config import load_accounts

    acc = None
    for a in (accounts if accounts is not None else load_accounts()):
        if a.id == account_id:
            acc = a
            break
    if acc is None:
        known = ", ".join(a.id for a in (accounts or [])) or "无"
        raise ValueError(f"账户 {account_id!r} 不存在（已配置: {known}）。"
                         "请引导用户运行 `uv run email-mcp web`（网页添加，推荐）"
                         f"或 `uv run email-mcp add`（命令行），或检查 {CONFIG_FILE}")
    return ImapSmtpAdapter(acc)
