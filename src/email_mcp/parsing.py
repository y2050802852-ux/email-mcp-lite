"""MIME 解析、HTML→纯文本、正文截断。

设计原则：正文优先取 text/plain；HTML 转纯文本后再截断，
保证返回给 Agent 的永远是可控体积的纯文本。
"""

from __future__ import annotations

import re
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .models import AttachmentMeta, EmailDetail, EmailMeta

# IMAP 搜索日期用英文月份（%b 受 locale 影响，不能直接用）
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def to_imap_date(date_text: str) -> str:
    """'2026-11-06' → '06-Nov-2026'。"""
    d = datetime.strptime(date_text.strip(), "%Y-%m-%d").date()
    return f"{d.day:02d}-{_MONTHS_EN[d.month - 1]}-{d.year}"


def header_str(msg: Message, name: str, default: str = "") -> str:
    """解码后的头部值（policy.default 自动处理 RFC 2047 中文编码）。"""
    value = msg.get(name)
    if value is None:
        return default
    try:
        return str(value).replace("\r", " ").replace("\n", " ").strip()
    except Exception:
        return default


def format_date(msg: Message) -> str:
    raw = msg.get("Date", "")
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(str(raw))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw)


class _HTMLTextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "ul", "ol", "blockquote", "pre", "section", "article", "hr"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "br" or tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip > 0:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)


def html_to_text(html: str) -> str:
    ex = _HTMLTextExtractor()
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        return html
    text = "".join(ex._parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def sanitize_filename(name: str) -> str:
    """附件文件名是不可信外部输入：去路径分隔符，防目录穿越。"""
    name = name.replace("\\", "_").replace("/", "_").replace("\x00", "")
    name = name.strip().strip(".").strip()
    if len(name) > 200:
        name = name[:200]
    return name or "attachment"


def flags_to_state(flags: str) -> tuple[bool, bool]:
    """IMAP FLAGS 字符串 → (是否已读, 是否加星)。"""
    return ("\\Seen" in flags), ("\\Flagged" in flags)


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def extract_body(msg: Message, body_max: int) -> tuple[str, bool, int]:
    """→ (正文, 是否截断, 全文长度)。body_max<=0 返回空正文。"""
    if body_max <= 0:
        return "", False, 0
    plain_part: Message | None = None
    html_part: Message | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain_part is None:
            plain_part = part
        elif ctype == "text/html" and html_part is None:
            html_part = part
    part = plain_part or html_part
    if part is None:
        return "", False, 0
    text = _part_text(part)
    if part is html_part:
        text = html_to_text(text)
    total = len(text)
    if len(text) > body_max:
        return text[:body_max], True, total
    return text, False, total


def extract_attachments(msg: Message) -> list[AttachmentMeta]:
    out: list[AttachmentMeta] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disp = part.get_content_disposition() or ""
        if not filename and disp != "attachment":
            continue
        payload = part.get_payload(decode=True)
        out.append(AttachmentMeta(
            filename=sanitize_filename(str(filename) or "attachment"),
            size=len(payload) if payload else 0,
            content_type=part.get_content_type(),
        ))
    return out


def build_detail(account: str, folder: str, uid: int, flags: str,
                 raw: bytes, body_max: int) -> EmailDetail:
    """从原始 MIME 构建完整 EmailDetail（一次解析，各字段共用）。"""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    read, starred = flags_to_state(flags)
    meta = EmailMeta(
        account=account, folder=folder, uid=uid,
        date=format_date(msg),
        from_=header_str(msg, "From"),
        subject=header_str(msg, "Subject"),
        read=read, starred=starred,
        has_attachments="multipart/mixed" in str(msg.get("Content-Type", "")).lower(),
    )
    body, truncated, total_len = extract_body(msg, body_max)
    return EmailDetail(
        meta=meta,
        to=header_str(msg, "To"),
        cc=header_str(msg, "Cc"),
        body=body,
        truncated=truncated,
        total_body_len=total_len,
        attachments=extract_attachments(msg),
        message_id=header_str(msg, "Message-ID"),
        references=header_str(msg, "References"),
        raw=raw,
    )


def build_meta_from_headers(account: str, folder: str, uid: int,
                            flags: str, header_bytes: bytes) -> EmailMeta:
    """搜索列表用：只解析头部块，不碰正文。"""
    msg = BytesParser(policy=policy.default).parsebytes(header_bytes)
    read, starred = flags_to_state(flags)
    return EmailMeta(
        account=account, folder=folder, uid=uid,
        date=format_date(msg),
        from_=header_str(msg, "From"),
        subject=header_str(msg, "Subject"),
        read=read, starred=starred,
        has_attachments="multipart/mixed" in str(msg.get("Content-Type", "")).lower(),
    )
