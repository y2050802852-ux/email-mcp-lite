"""进程内 Fake IMAP/SMTP 服务器。

模拟 tests 中适配器实际用到的 imaplib 接口语义与响应形状
（元组/字节混合的 FETCH 响应、字面量搜索、引号包裹的 UTF-7 文件夹名），
使适配器代码在测试中面对与真实 imaplib 一致的数据结构。
"""

from __future__ import annotations

import email.utils
import imaplib
import smtplib
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def parse_imap_date(b: bytes):
    """'06-Nov-2026' → date(2026, 11, 6)"""
    from datetime import date

    day, mon, year = b.decode("ascii").split("-")
    return date(int(year), _MONTHS[mon.capitalize()], int(day))


def make_raw(subject: str, from_: str, to: str, date_str: str, body: str,
             attach: str | None = None, html: bool = False,
             message_id: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date_str
    msg["Message-ID"] = message_id or f"<{abs(hash(subject)) % 99999}@example.com>"
    if html:
        msg.set_content(body, subtype="html", charset="utf-8")
    else:
        msg.set_content(body, charset="utf-8")
    if attach:
        msg.add_attachment(b"%PDF-1.4 fake pdf bytes here",
                           maintype="application", subtype="pdf", filename=attach)
    return msg.as_bytes()


class FakeWorld:
    """内存邮件世界：文件夹名 → {uid: [flags:set, raw_bytes]}。"""

    def __init__(self) -> None:
        self.folders: dict[str, dict[int, list]] = {
            "INBOX": {}, "Sent Messages": {}, "Trash": {}, "Drafts": {},
        }
        self.next_uid: dict[str, int] = {k: 1 for k in self.folders}
        self.smtp_sent: list[EmailMessage] = []
        self.imap_supports_move = True
        self.creds = ("me@qq.com", "authcode123")

    def add(self, folder: str, raw: bytes, flags: set[str] | None = None) -> int:
        uid = self.next_uid[folder]
        self.next_uid[folder] += 1
        self.folders[folder][uid] = [set(flags or ()), raw]
        return uid

    def seed(self) -> None:
        """标准测试数据。uid1 已读纯文本；uid2 未读带附件；uid3 未读 HTML。"""
        u1 = self.add("INBOX", make_raw(
            "会议通知", "张三 <zhangsan@example.com>", "me@qq.com",
            "Thu, 05 Nov 2026 02:30:00 +0000",
            "明天上午十点开组会，请准时参加。\n\n会议地点：301 会议室。"
        ), flags={"\\Seen"})
        assert u1 == 1
        u2 = self.add("INBOX", make_raw(
            "Quarterly Report", "Bob <bob@example.com>", "me@qq.com",
            "Tue, 03 Nov 2026 09:00:00 +0000",
            "Please find the quarterly report attached.", attach="report.pdf"
        ))
        assert u2 == 2
        u3 = self.add("INBOX", make_raw(
            "RAG 研究讨论", "张三 <zhangsan@example.com>", "me@qq.com",
            "Fri, 06 Nov 2026 08:00:00 +0000",
            "<html><body><p>我们讨论一下 RAG 的检索策略与重排模型。</p>"
            "<script>evil()</script></body></html>", html=True,
            message_id="<rag-thread-001@example.com>"
        ))
        assert u3 == 3
        self.add("Trash", make_raw(
            "old spam", "spam@example.com", "me@qq.com",
            "Mon, 01 Sep 2025 12:00:00 +0000", "buy things"))

    # ---- 辅助断言 ----

    def unseen_count(self, folder: str) -> int:
        return sum(1 for f, _ in self.folders[folder].values() if "\\Seen" not in f)

    def flags_line(self, folder: str, uid: int) -> bytes:
        flags = self.folders[folder][uid][0]
        inner = b" ".join(f.encode("ascii") for f in sorted(flags))
        return b"(" + inner + b")"

    def find_by_subject(self, folder: str, subject: str) -> int | None:
        for uid, (_, raw) in self.folders[folder].items():
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            if str(msg.get("Subject", "")) == subject:
                return uid
        return None


WORLD = FakeWorld()


def reset() -> FakeWorld:
    global WORLD
    WORLD = FakeWorld()
    WORLD.seed()
    return WORLD


class FakeIMAP4:
    """模拟 imaplib.IMAP4/IMAP4_SSL 中适配器用到的接口。"""

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.state = "AUTH"
        self.selected: str | None = None
        self.literal: bytes | None = None
        self.utf8_enabled = False
        self._logged_in = False

    # ---- 连接 ----

    def login(self, user, password):
        if (user, password) != WORLD.creds:
            raise imaplib.IMAP4.error(
                b"[AUTHENTICATIONFAILED] Invalid credentials".decode())
        self._logged_in = True
        self.state = "AUTH"
        return "OK", [b"Logged in"]

    def noop(self):
        return "OK", [b"NOOP completed"]

    def logout(self):
        self.state = "LOGOUT"
        return "BYE", [b"Logging out"]

    def starttls(self):
        return "OK", [b"TLS established"]

    # ---- 文件夹 ----

    def list(self):
        lines = [b'(\\HasNoChildren) "/" "INBOX"',
                 b'(\\HasNoChildren) "/" "Sent Messages"',
                 b'(\\HasNoChildren) "/" "Trash"',
                 b'(\\HasNoChildren) "/" "Drafts"',
                 b'(\\Noselect) "/" "foo"]']
        return "OK", lines

    def select(self, mailbox, readonly=False):
        from email_mcp import imap_utf7

        name = imap_utf7.decode(mailbox.strip(b'"'))
        if name not in WORLD.folders:
            return "NO", [b"Mailbox does not exist"]
        self.selected = name
        self.state = "SELECTED"
        return "OK", [str(len(WORLD.folders[name])).encode()]

    def status(self, mailbox, items):
        from email_mcp import imap_utf7

        name = imap_utf7.decode(mailbox.strip(b'"'))
        if name not in WORLD.folders:
            return "NO", [b"Mailbox does not exist"]
        box = WORLD.folders[name]
        total = len(box)
        unseen = WORLD.unseen_count(name)
        return "OK", [b'"%s" (MESSAGES %d UNSEEN %d)'
                      % (imap_utf7.encode(name), total, unseen)]

    # ---- 核心: UID 命令 ----

    def uid(self, command, *args):
        assert command in ("SEARCH", "FETCH", "STORE", "MOVE", "COPY", "EXPUNGE")
        pending_literal = self.literal
        self.literal = None
        if command == "SEARCH":
            return self._uid_search(args, pending_literal)
        if command == "FETCH":
            return self._uid_fetch(args)
        if command == "STORE":
            return self._uid_store(args)
        if command in ("MOVE", "COPY"):
            return self._uid_move_copy(command, args)
        if command == "EXPUNGE":
            return self._uid_expunge(args)
        raise AssertionError(command)

    def _uid_search(self, args, literal):
        if self.selected is None:
            return "NO", [b"Nothing selected"]
        box = WORLD.folders[self.selected]
        uids = []
        field = b"ALL"
        text = None
        since = before = None
        unseen_only = False
        i = 0
        while i < len(args):
            a = args[i]
            if a == b"CHARSET":
                i += 2
                continue
            if a in (b"SUBJECT", b"FROM", b"TO", b"TEXT"):
                field = a
                text = literal if literal is not None else b""
                i += 1
                continue
            if a == b"SINCE":
                since = parse_imap_date(args[i + 1])
                i += 2
                continue
            if a == b"BEFORE":
                before = parse_imap_date(args[i + 1])
                i += 2
                continue
            if a == b"UNSEEN":
                unseen_only = True
                i += 1
                continue
            if a == b"ALL":
                i += 1
                continue
            raise AssertionError(f"unhandled search arg {a!r}")
        for uid in sorted(box):
            flags, raw = box[uid]
            if unseen_only and "\\Seen" in flags:
                continue
            if since or before or field != b"ALL":
                msg = BytesParser(policy=policy.default).parsebytes(raw)
            if since or before:
                d = email.utils.parsedate_to_datetime(
                    str(msg.get("Date"))).date()
                if since and d < since:
                    continue
                if before and d >= before:
                    continue
            if field == b"ALL":
                uids.append(uid)
                continue
            q = (text or b"").decode("utf-8", errors="replace").lower()
            if field == b"SUBJECT":
                hit = q in str(msg.get("Subject", "")).lower()
            elif field == b"FROM":
                hit = q in str(msg.get("From", "")).lower()
            elif field == b"TO":
                hit = q in str(msg.get("To", "")).lower()
            else:  # TEXT：头部+正文粗粒度全文
                from email_mcp.parsing import extract_body

                body, _, _ = extract_body(msg, 100000)
                hit = (q in str(msg.get("Subject", "")).lower()
                       or q in str(msg.get("From", "")).lower()
                       or q in body.lower())
            if hit:
                uids.append(uid)
        return "OK", [b" ".join(str(u).encode() for u in uids)]

    def _uid_fetch(self, args):
        uid_set, spec = args
        box = WORLD.folders.get(self.selected, {})
        result = []
        want_uids = []
        for part in uid_set.split(b","):
            want_uids.append(int(part))
        fields = None
        full_body = False
        m = spec and __import__("re").search(
            rb"HEADER\.FIELDS \(([^)]*)\)", spec)
        if m:
            fields = {f.strip().upper() for f in m.group(1).split()}
        elif spec and b"BODY.PEEK[]" in spec:
            full_body = True
        for uid in want_uids:
            if uid not in box:
                continue
            flags, raw = box[uid]
            fl = WORLD.flags_line(self.selected, uid)
            if full_body:
                payload = raw
                head = b"%d (UID %d FLAGS %s BODY[] {%d}" % (
                    uid, uid, fl, len(payload))
            else:
                payload = _header_block(raw, fields or set())
                head = b"%d (UID %d FLAGS %s BODY[HEADER.FIELDS (%s)] {%d}" % (
                    uid, uid, fl, b" ".join(sorted(fields or [])), len(payload))
            result.append((head, payload))
            result.append(b")")
        return "OK", result

    def _uid_store(self, args):
        uid_b, op, flag_spec = args
        box = WORLD.folders[self.selected]
        uid = int(uid_b)
        flags, _ = box[uid]
        spec = flag_spec.decode().strip("()").split()
        for f in spec:
            if op == b"+FLAGS" or op == b"+FLAGS.SILENT":
                flags.add(f)
            else:
                flags.discard(f)
        return "OK", [b"%d (UID %d FLAGS %s)" % (uid, uid,
                                                 WORLD.flags_line(self.selected, uid))]

    def _uid_move_copy(self, command, args):
        uid_b, mailbox_b = args
        from email_mcp import imap_utf7

        dest = imap_utf7.decode(mailbox_b.strip(b'"'))
        if command == "MOVE" and not WORLD.imap_supports_move:
            raise imaplib.IMAP4.error("BAD MOVE not supported")
        if dest not in WORLD.folders:
            return "NO", [b"Mailbox does not exist"]
        uid = int(uid_b)
        src = WORLD.folders[self.selected]
        flags, raw = src[uid]
        WORLD.add(dest, raw, set(flags))
        if command == "MOVE":
            del src[uid]
            return "OK", [b"Move completed"]
        return "OK", [b"Copy completed"]

    def _uid_expunge(self, args):
        uid_b = args[0]
        box = WORLD.folders[self.selected]
        uid = int(uid_b)
        if uid in box and "\\Deleted" in box[uid][0]:
            del box[uid]
            return "OK", [b"%d Expunged" % uid]
        return "OK", [b"nothing expunged"]

    # ---- 其他 ----

    def append(self, mailbox, flags, date_time, message):
        from email_mcp import imap_utf7

        name = imap_utf7.decode(mailbox.strip(b'"'))
        if name not in WORLD.folders:
            return "NO", [b"Mailbox does not exist"]
        WORLD.add(name, bytes(message))
        return "OK", [b"Append completed"]

    def expunge(self):
        box = WORLD.folders[self.selected]
        removed = [u for u, (f, _) in box.items() if "\\Deleted" in f]
        for u in removed:
            del box[u]
        return "OK", [b"%d Expunged" % u for u in removed]


def _header_block(raw: bytes, fields: set[bytes]) -> bytes:
    """从原始消息截取指定头部的头部块（含折叠行），模拟 HEADER.FIELDS fetch。"""
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        sep = raw.find(b"\n\n")
        head = raw[:sep] if sep != -1 else raw
        eol = b"\n"
    else:
        head = raw[:sep]
        eol = b"\r\n"
    keep: list[bytes] = []
    for line in head.split(eol):
        if line[:1] in (b" ", b"\t"):
            if keep:
                keep.append(line)
            continue
        name = line.split(b":", 1)[0].strip().upper()
        if name in fields:
            keep.append(line)
    return eol.join(keep) + eol + eol


class FakeSMTP:
    """模拟 smtplib.SMTP/SMTP_SSL 中适配器用到的接口。"""

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self._logged_in = False

    def login(self, user, password):
        if (user, password) != WORLD.creds:
            raise smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        self._logged_in = True
        return (235, b"Authentication successful")

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if not self._logged_in:
            raise smtplib.SMTPSenderRefused(530, b"Auth required", from_addr)
        WORLD.smtp_sent.append(msg)
        return {}

    def quit(self):
        return (221, b"Bye")
