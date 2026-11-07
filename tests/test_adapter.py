"""适配器层测试：对着 FakeIMAP4/FakeSMTP 验证完整行为。"""

import pytest

from email_mcp.adapter import ImapSmtpAdapter, build_message
from email_mcp.config import get_account
from email_mcp.models import parse_id


@pytest.fixture
def ad():
    return ImapSmtpAdapter(get_account("qq"))


def _eid(uid, folder="INBOX"):
    return f"qq|{folder}|{uid}"


class TestSearch:
    def test_list_all_newest_first(self, ad, fake_mail_world):
        metas = ad.search("INBOX")
        assert [m.uid for m in metas] == [3, 2, 1]
        assert metas[0].subject == "RAG 研究讨论"
        assert metas[2].subject == "会议通知"

    def test_flags(self, ad, fake_mail_world):
        metas = {m.uid: m for m in ad.search("INBOX")}
        assert metas[1].read is True        # 会议通知已读
        assert metas[2].read is False       # Quarterly Report 未读
        assert metas[3].read is False       # RAG 未读

    def test_attachment_flag(self, ad, fake_mail_world):
        metas = {m.uid: m for m in ad.search("INBOX")}
        # uid2 是 multipart/mixed 带附件，其余没有
        assert metas[2].has_attachments is True
        assert metas[1].has_attachments is False
        assert metas[3].has_attachments is False

    def test_subject_query(self, ad, fake_mail_world):
        metas = ad.search("INBOX", query="RAG", field="subject")
        assert [m.uid for m in metas] == [3]

    def test_text_query_hits_body(self, ad, fake_mail_world):
        metas = ad.search("INBOX", query="检索策略", field="text")
        assert [m.uid for m in metas] == [3]

    def test_unread_only(self, ad, fake_mail_world):
        metas = ad.search("INBOX", unread_only=True)
        assert sorted(m.uid for m in metas) == [2, 3]

    def test_since_before(self, ad, fake_mail_world):
        metas = ad.search("INBOX", since="2026-11-04")
        assert sorted(m.uid for m in metas) == [1, 3]
        metas = ad.search("INBOX", before="2026-11-04")
        assert sorted(m.uid for m in metas) == [2]

    def test_limit(self, ad, fake_mail_world):
        metas = ad.search("INBOX", limit=2)
        assert len(metas) == 2
        assert [m.uid for m in metas] == [3, 2]

    def test_empty_result(self, ad, fake_mail_world):
        assert ad.search("INBOX", query="不存在的关键词xyz") == []

    def test_unknown_folder(self, ad, fake_mail_world):
        with pytest.raises(RuntimeError, match="list_folders"):
            ad.search("Nope")


class TestFolders:
    def test_list_folders(self, ad, fake_mail_world):
        rows = dict((n, (t, u)) for n, t, u in ad.list_folders())
        assert rows["INBOX"] == (3, 2)
        assert rows["Trash"] == (1, 1)

    def test_noselect_excluded(self, ad, fake_mail_world):
        names = [n for n, _, _ in ad.list_folders()]
        assert "INBOX" in names
        assert not any("foo" in n for n in names)


class TestGet:
    @staticmethod
    def _local_date(rfc2822: str) -> str:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(rfc2822).astimezone().strftime("%Y-%m-%d %H:%M")

    def test_full_email(self, ad, fake_mail_world):
        d = ad.get(_eid(1))
        assert d.meta.subject == "会议通知"
        assert d.meta.from_ == "张三 <zhangsan@example.com>"
        assert d.meta.date == self._local_date("Thu, 05 Nov 2026 02:30:00 +0000")
        assert "组会" in d.body
        assert d.to == "me@qq.com"
        assert d.attachments == []

    def test_html_email_body(self, ad, fake_mail_world):
        d = ad.get(_eid(3))
        assert "<p>" not in d.body
        assert "检索策略" in d.body and "重排模型" in d.body
        assert "evil()" not in d.body

    def test_attachment_metadata(self, ad, fake_mail_world):
        d = ad.get(_eid(2))
        assert len(d.attachments) == 1
        assert d.attachments[0].filename == "report.pdf"
        assert d.attachments[0].size > 0

    def test_truncate(self, ad, fake_mail_world):
        d = ad.get(_eid(1), body_max=5)
        assert d.truncated and len(d.body) == 5

    def test_missing_uid(self, ad, fake_mail_world):
        with pytest.raises(RuntimeError, match="未找到"):
            ad.get(_eid(999))


class TestMark:
    def test_mark_read(self, ad, fake_mail_world):
        ad.mark(_eid(2), read=True)
        assert "\\Seen" in fake_mail_world.folders["INBOX"][2][0]

    def test_mark_unread(self, ad, fake_mail_world):
        ad.mark(_eid(1), read=False)
        assert "\\Seen" not in fake_mail_world.folders["INBOX"][1][0]

    def test_mark_starred(self, ad, fake_mail_world):
        ad.mark(_eid(1), starred=True)
        assert "\\Flagged" in fake_mail_world.folders["INBOX"][1][0]

    def test_mark_nothing(self, ad, fake_mail_world):
        with pytest.raises(ValueError):
            ad.mark(_eid(1))


class TestMoveDelete:
    def test_move_to_trash(self, ad, fake_mail_world):
        ad.move(_eid(2), "Trash")
        assert 2 not in fake_mail_world.folders["INBOX"]
        assert fake_mail_world.find_by_subject("Trash", "Quarterly Report")

    def test_move_fallback_without_move_support(self, ad, fake_mail_world):
        fake_mail_world.imap_supports_move = False
        ad.move(_eid(2), "Trash")
        assert 2 not in fake_mail_world.folders["INBOX"]
        assert fake_mail_world.find_by_subject("Trash", "Quarterly Report")

    def test_delete_soft_moves_to_trash(self, ad, fake_mail_world):
        msg = ad.delete(_eid(1))
        assert "回收站" in msg
        assert fake_mail_world.find_by_subject("Trash", "会议通知")
        assert 1 not in fake_mail_world.folders["INBOX"]

    def test_delete_in_trash_is_permanent(self, ad, fake_mail_world):
        trash_uid = fake_mail_world.find_by_subject("Trash", "old spam")
        ad.delete(f"qq|Trash|{trash_uid}")
        assert fake_mail_world.find_by_subject("Trash", "old spam") is None
        assert "old spam" not in str(fake_mail_world.folders["Trash"])

    def test_delete_permanent(self, ad, fake_mail_world):
        ad.delete(_eid(1), permanent=True)
        assert 1 not in fake_mail_world.folders["INBOX"]


class TestAttachments:
    def test_save_all(self, ad, fake_mail_world, tmp_path):
        saved = ad.save_attachments(_eid(2), save_dir=str(tmp_path))
        assert len(saved) == 1
        path = saved[0]
        assert (path.find("report.pdf"))
        import pathlib
        assert pathlib.Path(path).read_bytes().startswith(b"%PDF-1.4")

    def test_save_by_index(self, ad, fake_mail_world, tmp_path):
        saved = ad.save_attachments(_eid(2), save_dir=str(tmp_path),
                                    attachment="1")
        assert len(saved) == 1

    def test_save_by_name(self, ad, fake_mail_world, tmp_path):
        saved = ad.save_attachments(_eid(2), save_dir=str(tmp_path),
                                    attachment="report")
        assert len(saved) == 1

    def test_save_no_attachment(self, ad, fake_mail_world, tmp_path):
        with pytest.raises(ValueError, match="没有附件"):
            ad.save_attachments(_eid(1), save_dir=str(tmp_path))

    def test_save_bad_name(self, ad, fake_mail_world, tmp_path):
        with pytest.raises(ValueError, match="没有名称包含"):
            ad.save_attachments(_eid(2), save_dir=str(tmp_path),
                                attachment="nonexistent")


class TestSendDraft:
    def test_send_captures(self, ad, fake_mail_world, tmp_path):
        att = tmp_path / "hello.txt"
        att.write_bytes(b"attachment content")
        msg = build_message(ad.account, ["peer@example.com"], [],
                            "测试主题", "测试正文",
                            attachment_paths=[str(att)])
        mid = ad.send(msg)
        assert mid.startswith("<")
        assert len(fake_mail_world.smtp_sent) == 1
        sent = fake_mail_world.smtp_sent[0]
        assert "peer@example.com" in sent["To"]
        assert sent["Subject"] == "测试主题"
        parts = [p.get_content_type() for p in sent.walk()]
        assert "application/octet-stream" in parts

    def test_draft_appended(self, ad, fake_mail_world):
        msg = build_message(ad.account, ["a@b.com"], [], "草稿主题", "草稿正文")
        folder = ad.create_draft(msg)
        assert folder == "Drafts"
        assert fake_mail_world.find_by_subject("Drafts", "草稿主题")

    def test_auth_failure_message(self, fake_mail_world):
        from email_mcp.models import Account
        bad = Account(id="bad", user="me@qq.com", password="wrong",
                      imap_host="imap.fake.test", imap_port=993,
                      smtp_host="smtp.fake.test", smtp_port=465)
        with pytest.raises(RuntimeError, match="登录失败"):
            ImapSmtpAdapter(bad).list_folders()


class TestMessageBuilder:
    def test_reply_headers(self):
        acc = get_account("qq")
        msg = build_message(acc, ["a@b.com"], ["c@d.com"], "Re: x", "y",
                            in_reply_to="<orig@example.com>",
                            references="<r1@e.com> <orig@example.com>")
        assert msg["In-Reply-To"] == "<orig@example.com>"
        assert "<orig@example.com>" in msg["References"]

    def test_normalize_addrs_named(self):
        from email_mcp.adapter import normalize_addrs
        out = normalize_addrs(["张三 <zs@x.com>", "ls@x.com"])
        assert len(out) == 2
        assert any("zs@x.com" in o for o in out)

    def test_missing_attachment_file(self):
        acc = get_account("qq")
        with pytest.raises(ValueError, match="附件不存在"):
            build_message(acc, ["a@b.com"], [], "s", "b",
                          attachment_paths=["/nonexistent/file.bin"])
