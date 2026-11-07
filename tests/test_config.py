"""配置层测试：accounts.json 读写、权限、增删查。"""

import stat

import pytest

from email_mcp.config import (CONFIG_FILE, PRESETS, get_account, load_accounts,
                              remove_account, save_account)
from email_mcp.models import Account
from tests.conftest import TEST_ACCOUNT


def _acc(**kw) -> Account:
    base = dict(id="suda", user="me@stu.suda.edu.cn", password="pw123",
                imap_host="imap.suda.edu.cn", imap_port=993,
                smtp_host="smtp.suda.edu.cn", smtp_port=465)
    base.update(kw)
    return Account(**base)


class TestSaveLoad:
    def test_roundtrip(self):
        save_account(_acc())
        accs = load_accounts()
        assert len(accs) == 2
        suda = get_account("suda")
        assert suda.user == "me@stu.suda.edu.cn"
        assert suda.imap_host == "imap.suda.edu.cn"

    def test_same_id_updates(self):
        save_account(_acc(password="old"))
        save_account(_acc(password="new"))
        accs = load_accounts()
        assert len(accs) == 2
        assert get_account("suda").password == "new"

    def test_file_permissions_600(self):
        save_account(_acc())
        mode = stat.S_IMODE(CONFIG_FILE.stat().st_mode)
        assert mode == 0o600


class TestRemove:
    def test_remove_existing(self):
        save_account(_acc())
        assert remove_account("suda") is True
        assert get_account("suda") is None
        assert get_account("qq") is not None  # 其他账户不受影响

    def test_remove_missing(self):
        assert remove_account("nope") is False


class TestValidation:
    def test_missing_fields_raise(self):
        with pytest.raises(ValueError, match="缺少字段"):
            Account.from_dict({"id": "x", "user": "y"})

    def test_defaults(self):
        acc = Account.from_dict(
            {"id": "x", "user": "y@z.com", "password": "p", "imap_host": "h"})
        assert acc.imap_port == 993
        assert acc.smtp_port == 465
        assert acc.smtp_ssl is True


class TestPresets:
    @pytest.mark.parametrize("name", ["qq", "163", "gmail"])
    def test_preset_hosts(self, name):
        p = PRESETS[name]
        assert p["imap_host"] and p["smtp_host"]
        assert p["imap_port"] == 993 and p["smtp_port"] == 465


class TestDefaultAccount:
    def test_test_account_loaded(self):
        assert get_account("qq") is not None
        assert get_account("qq").user == TEST_ACCOUNT.user
