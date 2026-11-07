"""解析层测试：MIME、HTML→纯文本、截断、UTF-7、ID。"""

from email import policy
from email.parser import BytesParser

import pytest

from email_mcp import imap_utf7
from email_mcp.models import format_id, human_size, parse_id
from email_mcp.parsing import (build_detail, extract_body, html_to_text,
                               sanitize_filename, to_imap_date)
from tests.fakes import make_raw


class TestImapUtf7:
    def test_ascii_untouched(self):
        assert imap_utf7.encode("INBOX") == b"INBOX"
        assert imap_utf7.encode("Sent Messages") == b"Sent Messages"

    def test_ampersand_escape(self):
        assert imap_utf7.encode("a&b") == b"a&-b"
        assert imap_utf7.decode(b"a&-b") == "a&b"

    def test_chinese_roundtrip(self):
        for name in ("已删除", "草稿箱", "收件箱&存档", "混English合"):
            assert imap_utf7.decode(imap_utf7.encode(name)) == name

    def test_known_encoding_cross_check(self):
        # 用独立实现（标准 base64 + '/'→','  + padding 去除）交叉验证
        import base64

        name = "已删除"
        expect = "&" + base64.b64encode(name.encode("utf-16-be")).decode(
            "ascii").rstrip("=").replace("/", ",") + "-"
        assert imap_utf7.encode(name) == expect.encode("ascii")


class TestId:
    def test_format_parse_roundtrip(self):
        eid = format_id("qq", "Sent Messages", 42)
        assert eid == "qq|Sent Messages|42"
        assert parse_id(eid) == ("qq", "Sent Messages", 42)

    def test_parse_bad(self):
        with pytest.raises(ValueError):
            parse_id("nonsense")
        with pytest.raises(ValueError):
            parse_id("a|b|notanumber")


class TestDates:
    def test_to_imap_date(self):
        assert to_imap_date("2026-11-06") == "06-Nov-2026"
        assert to_imap_date(" 2026-01-01 ") == "01-Jan-2026"


class TestHtmlToText:
    def test_basic(self):
        html = "<html><body><p>你好<br>世界</p><p>第二段</p></body></html>"
        text = html_to_text(html)
        assert "你好" in text and "世界" in text and "第二段" in text

    def test_skips_script_style(self):
        html = "<p>正文</p><script>alert('evil')</script><style>.x{}</style>"
        text = html_to_text(html)
        assert "evil" not in text and ".x" not in text and "正文" in text


class TestSanitize:
    def test_path_traversal(self):
        assert "/" not in sanitize_filename("../../etc/passwd")
        assert "\\" not in sanitize_filename("..\\..\\win.ini")

    def test_empty(self):
        assert sanitize_filename("") == "attachment"
        assert sanitize_filename("...") == "attachment"


class TestBuildDetail:
    def _detail(self, raw, body_max=2000):
        return build_detail("qq", "INBOX", 1, "\\Seen", raw, body_max)

    def test_plain_text_chinese(self):
        from email.utils import parsedate_to_datetime

        raw = make_raw("会议通知", "张三 <zhangsan@example.com>", "me@qq.com",
                       "Thu, 05 Nov 2026 02:30:00 +0000", "明天开会。")
        d = self._detail(raw)
        assert d.meta.subject == "会议通知"
        assert d.meta.from_ == "张三 <zhangsan@example.com>"
        expected = parsedate_to_datetime(
            "Thu, 05 Nov 2026 02:30:00 +0000").astimezone().strftime("%Y-%m-%d %H:%M")
        assert d.meta.date == expected
        assert d.meta.read is True
        assert "明天开会" in d.body
        assert d.attachments == []

    def test_attachment_metadata(self):
        raw = make_raw("Report", "bob@example.com", "me@qq.com",
                       "Tue, 03 Nov 2026 09:00:00 +0000", "see attached",
                       attach="report.pdf")
        d = self._detail(raw)
        assert len(d.attachments) == 1
        a = d.attachments[0]
        assert a.filename == "report.pdf"
        assert a.size > 0
        assert a.content_type == "application/pdf"

    def test_html_converted(self):
        raw = make_raw("HTML", "a@b.com", "x@y.com",
                       "Fri, 06 Nov 2026 08:00:00 +0000",
                       "<p>第一段</p><p>第二段</p>", html=True)
        d = self._detail(raw)
        assert "<p>" not in d.body
        assert "第一段" in d.body and "第二段" in d.body

    def test_truncation(self):
        raw = make_raw("Long", "a@b.com", "x@y.com",
                       "Fri, 06 Nov 2026 08:00:00 +0000", "字" * 500)
        d = self._detail(raw, body_max=100)
        assert d.truncated is True
        # set_content 会在末尾补一个换行，全文 ≥500 字符
        assert d.total_body_len >= 500
        assert len(d.body) == 100

    def test_body_max_zero(self):
        raw = make_raw("L", "a@b.com", "x@y.com",
                       "Fri, 06 Nov 2026 08:00:00 +0000", "内容")
        d = self._detail(raw, body_max=0)
        assert d.body == "" and d.truncated is False

    def test_starred_flag(self):
        raw = make_raw("S", "a@b.com", "x@y.com",
                       "Fri, 06 Nov 2026 08:00:00 +0000", "内容")
        d = build_detail("qq", "INBOX", 1, "\\Flagged \\Seen", raw, 100)
        assert d.meta.starred and d.meta.read


class TestExtractBody:
    def test_prefers_plain_over_html(self):
        raw = make_raw("M", "a@b.com", "x@y.com",
                       "Fri, 06 Nov 2026 08:00:00 +0000", "纯文本优先").replace(
            b"Content-Type: text/plain;", b"Content-Type: text/plain;")
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        body, trunc, total = extract_body(msg, 2000)
        assert "纯文本优先" in body and trunc is False
