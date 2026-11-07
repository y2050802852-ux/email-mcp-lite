"""测试公共设施。

关键点：必须在 email_mcp 模块导入前设置 EMAIL_MCP_CONFIG_DIR，
因为 config.py 在 import 时读取环境变量确定配置路径。
conftest 在所有测试模块之前被 pytest 导入，满足该时序。
"""

import os
import tempfile

os.environ.setdefault(
    "EMAIL_MCP_CONFIG_DIR",
    os.path.join(tempfile.mkdtemp(prefix="email-mcp-test-"), "cfg"),
)

import imaplib  # noqa: E402
import smtplib  # noqa: E402

import pytest  # noqa: E402

from email_mcp import server  # noqa: E402
from tests import fakes  # noqa: E402
from email_mcp.config import CONFIG_FILE, save_account  # noqa: E402
from email_mcp.models import Account  # noqa: E402

TEST_ACCOUNT = Account(
    id="qq", user="me@qq.com", password="authcode123",
    imap_host="imap.fake.test", imap_port=993,
    smtp_host="smtp.fake.test", smtp_port=465,
    from_name="测试员",
)


@pytest.fixture(autouse=True)
def fake_mail_world(monkeypatch):
    """每个测试：隔离配置文件、重置假邮件世界、monkeypatch 网络层。"""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    world = fakes.reset()
    save_account(TEST_ACCOUNT)
    server._adapter_cache.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fakes.FakeIMAP4)
    monkeypatch.setattr(smtplib, "SMTP_SSL", fakes.FakeSMTP)
    monkeypatch.delenv("EMAIL_MCP_READONLY", raising=False)
    yield world
