"""MCP 工具层测试：直接调用 server 里的工具函数。"""

import email.utils
import pytest

from email_mcp import server
from email_mcp.config import save_account
from email_mcp.models import Account
from tests.fakes import make_raw


def _eid(uid, folder="INBOX"):
    return f"qq|{folder}|{uid}"


class TestAccountsFolders:
    def test_list_accounts(self):
        out = server.list_accounts()
        assert "qq: me@qq.com" in out

    def test_list_folders(self):
        out = server.list_folders()
        assert "INBOX (3封/2未读)" in out

    def test_single_account_auto_selected(self):
        # 只有一个账户时 account 留空应可用
        out = server.list_folders(account="")
        assert "INBOX" in out

    def test_multi_account_requires_explicit(self):
        save_account(Account(id="suda", user="b@stu.suda.edu.cn",
                             password="pw", imap_host="h", imap_port=993,
                             smtp_host="h", smtp_port=465))
        with pytest.raises(ValueError, match="请显式指定 account"):
            server.list_folders(account="")


class TestSearchTool:
    def test_compact_format(self):
        out = server.search_emails()
        lines = out.splitlines()
        assert lines[0].startswith("共 3 封")
        assert "qq|INBOX|3" in out
        assert "RAG 研究讨论" in out
        # 标记紧凑
        assert "[未读]" in out and "[附件]" in out
        # 已读邮件没有未读标记
        inbox1 = next(l for l in lines if "qq|INBOX|1 " in l)
        assert "[未读]" not in inbox1

    def test_empty(self):
        out = server.search_emails(query="zzz-no-match")
        assert out == "未找到匹配邮件"


class TestGetEmailTool:
    def test_structure(self):
        from email.utils import parsedate_to_datetime

        expected_date = parsedate_to_datetime(
            "Thu, 05 Nov 2026 02:30:00 +0000").astimezone().strftime("%Y-%m-%d %H:%M")
        out = server.get_email(_eid(1))
        assert out.startswith("From: 张三 <zhangsan@example.com>")
        assert "To: me@qq.com" in out
        assert f"Date: {expected_date}" in out
        assert "Subject: 会议通知" in out
        assert "组会" in out

    def test_attachment_list_inline(self):
        out = server.get_email(_eid(2))
        assert "Attachments: report.pdf" in out

    def test_truncation_note(self):
        out = server.get_email(_eid(1), body_max=5)
        assert "正文已截断" in out

    def test_body_max_zero(self):
        out = server.get_email(_eid(1), body_max=0)
        assert "组会" not in out


class TestGetAttachmentsTool:
    def test_save(self, tmp_path):
        out = server.get_attachments(_eid(2), save_dir=str(tmp_path))
        assert "已保存" in out and "report.pdf" in out


class TestSendTool:
    def test_basic_send(self, fake_mail_world):
        out = server.send_email(to="peer@example.com",
                                subject="工具层发送", body="你好")
        assert "已发送" in out
        sent = fake_mail_world.smtp_sent[0]
        assert "peer@example.com" in sent["To"]
        assert sent["Subject"] == "工具层发送"
        assert sent["From"] == "测试员 <me@qq.com>"

    def test_reply(self, fake_mail_world):
        orig = server._get_adapter("qq").get(_eid(1), body_max=0)
        out = server.send_email(in_reply_to=_eid(1), body="收到，准时参加。")
        assert "已发送" in out
        sent = fake_mail_world.smtp_sent[0]
        assert "zhangsan@example.com" in sent["To"]
        assert sent["Subject"] == "Re: 会议通知"
        assert sent["In-Reply-To"] == orig.message_id
        assert orig.message_id in sent["References"]

    def test_reply_all(self, fake_mail_world):
        # 构造带 Cc 的原始邮件
        raw = make_raw("多人群发", "张三 <zhangsan@example.com>", "me@qq.com",
                       "Thu, 05 Nov 2026 02:30:00 +0000", "讨论",
                       message_id="<all-1@example.com>")
        # 给原始消息加 Cc 头（policy.default 序列化用 \n 行尾）
        raw = raw.replace(b"To: me@qq.com\n",
                          b"To: me@qq.com, Alice <alice@x.com>\n"
                          b"Cc: Bob <bob@x.com>\n", 1)
        uid = fake_mail_world.add("INBOX", raw)
        server.send_email(in_reply_to=f"qq|INBOX|{uid}", reply_all=True,
                          body="ok")
        sent = fake_mail_world.smtp_sent[0]
        assert "zhangsan@example.com" in sent["To"]
        assert "alice@x.com" in sent["To"]
        assert "bob@x.com" in sent["Cc"]

    def test_forward(self, fake_mail_world):
        out = server.send_email(to="friend@example.com",
                                forward_of=_eid(1))
        assert "已发送" in out
        sent = fake_mail_world.smtp_sent[0]
        assert sent["Subject"] == "Fwd: 会议通知"
        body = sent.get_content()
        assert "转发邮件" in body and "组会" in body

    def test_draft(self, fake_mail_world):
        out = server.send_email(to="a@b.com", subject="草稿信", body="内容",
                                draft=True)
        assert "草稿已保存" in out and "Drafts" in out
        assert fake_mail_world.smtp_sent == []
        assert fake_mail_world.find_by_subject("Drafts", "草稿信")

    def test_reply_and_forward_exclusive(self):
        with pytest.raises(ValueError, match="二选一"):
            server.send_email(in_reply_to=_eid(1), forward_of=_eid(2),
                              to="a@b.com", subject="s", body="b")

    def test_missing_recipient(self):
        with pytest.raises(ValueError, match="收件人"):
            server.send_email(subject="s", body="b")


class TestScheduledEmailTools:
    def test_schedule_requires_offset_and_returns_id(self, monkeypatch):
        from datetime import datetime, timezone
        import email_mcp.server as module
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(ValueError, match="UTC offset"):
            server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00",
                                  subject="s", body="b")
        result = server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+08:00",
                                       subject="s", body="private body")
        assert "scheduled" in result and "a@b.com" in result
        assert "private body" not in server.list_scheduled_emails()

    def test_readonly_blocks_scheduling(self, monkeypatch):
        monkeypatch.setenv("EMAIL_MCP_READONLY", "1")
        with pytest.raises(RuntimeError, match="只读"):
            server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+00:00",
                                  subject="s", body="b")

    def test_schedule_edit_cancel_and_confirm(self, monkeypatch):
        from datetime import datetime, timezone
        import email_mcp.server as module
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        created = server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+00:00",
                                        subject="s", body="b", mode="confirm")
        task_id = created.split()[1]
        assert "已更新" in server.edit_scheduled_email(task_id, subject="new")
        # Put task into confirmation state using the store's due processing.
        from email_mcp.scheduled_store import ScheduledEmailStore
        store = ScheduledEmailStore()
        store.claim_due(datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc))
        assert "已发送" in server.confirm_scheduled_email(task_id)

    def test_edit_can_clear_cc_and_attachments(self, monkeypatch):
        from datetime import datetime, timezone
        import email_mcp.server as module
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        created = server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+00:00",
                                        subject="s", body="b", cc="c@d.com", attachments=["/tmp/a"])
        task_id = created.split()[1]
        server.edit_scheduled_email(task_id, cc=[], attachments=[])
        task = module._scheduled_store().get(task_id)
        assert task.cc == [] and task.attachments == []

    def test_edit_rejects_invalid_mode(self, monkeypatch):
        from datetime import datetime, timezone
        import email_mcp.server as module
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        created = server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+00:00",
                                        subject="s", body="b")
        with pytest.raises(ValueError, match="mode"):
            server.edit_scheduled_email(created.split()[1], mode="later")

    def test_retry_unknown_requires_explicit_call(self, monkeypatch):
        from datetime import datetime, timezone
        import email_mcp.server as module
        from email_mcp.scheduled_store import ScheduledEmailStore
        monkeypatch.setattr(module, "_utc_now", lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        created = server.schedule_email(account="qq", to="a@b.com", send_at="2030-01-01T12:00:00+00:00",
                                        subject="s", body="b")
        task_id = created.split()[1]
        store = ScheduledEmailStore()
        store.transition(task_id, {"scheduled"}, "sending")
        store.transition(task_id, {"sending"}, "unknown", last_error="timeout")
        result = server.retry_scheduled_email(task_id)
        assert "状态=sent" in result
        assert "重复" in result


class TestMutatingTools:
    def test_mark(self, fake_mail_world):
        out = server.mark_email(_eid(2), read=True)
        assert "已读" in out
        assert "\\Seen" in fake_mail_world.folders["INBOX"][2][0]

    def test_move(self, fake_mail_world):
        out = server.move_email(_eid(2), "Trash")
        assert "完成" in out
        assert 2 not in fake_mail_world.folders["INBOX"]

    def test_delete_soft(self, fake_mail_world):
        out = server.delete_email(_eid(1))
        assert "回收站" in out
        assert fake_mail_world.find_by_subject("Trash", "会议通知")

    def test_readonly_blocks_mutations(self, monkeypatch):
        monkeypatch.setenv("EMAIL_MCP_READONLY", "1")
        for fn, kwargs in (
            (server.delete_email, {"id": _eid(1)}),
            (server.move_email, {"id": _eid(1), "to_folder": "Trash"}),
            (server.mark_email, {"id": _eid(1), "read": True}),
            (server.send_email, {"to": "a@b.com", "subject": "s", "body": "b"}),
        ):
            with pytest.raises(RuntimeError, match="只读"):
                fn(**kwargs)

    def test_readonly_allows_reading(self, monkeypatch):
        monkeypatch.setenv("EMAIL_MCP_READONLY", "1")
        assert "会议通知" in server.get_email(_eid(1))
        assert "共 3 封" in server.search_emails()
