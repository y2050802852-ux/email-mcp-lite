"""web 管理页测试：起线程 + urllib 真实 HTTP 请求。"""

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from email_mcp.config import CONFIG_FILE, get_account, load_accounts
from email_mcp.web import WebHandler
from tests.conftest import TEST_ACCOUNT


@pytest.fixture
def server_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _req(url, method="GET", body=None, csrf=True):
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-Requested-With"] = "email-mcp"
    data = json.dumps(body).encode() if body is not None else None
    r = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


GOOD_PAYLOAD = {
    "id": "work", "user": "me@qq.com", "password": "authcode123",
    "imap_host": "imap.fake.test", "imap_port": 993,
    "smtp_host": "smtp.fake.test", "smtp_port": 465,
    "from_name": "打工人",
}


class TestPages:
    def test_index_html(self, server_url):
        with urlopen(server_url + "/", timeout=10) as resp:
            html = resp.read().decode()
        assert resp.status == 200
        assert "email-mcp" in html and "账户管理" in html
        assert "<script>" in html  # 前端逻辑完整下发

    def test_unknown_route_404(self, server_url):
        status, data = _req(server_url + "/api/nothing")
        assert status == 404 and "error" in data


class TestAccountsApi:
    def test_list_no_password_leak(self, server_url):
        status, accs = _req(server_url + "/api/accounts")
        assert status == 200
        assert any(a["id"] == TEST_ACCOUNT.id for a in accs)
        assert all("password" not in a for a in accs)  # 密码永不外泄

    def test_create_and_delete(self, server_url):
        status, data = _req(server_url + "/api/accounts", "POST", GOOD_PAYLOAD)
        assert status == 200 and data["ok"]
        acc = get_account("work")
        assert acc.password == "authcode123"  # 密码落盘
        assert acc.from_name == "打工人"

        status, _ = _req(server_url + "/api/accounts/work", "DELETE")
        assert status == 200
        assert get_account("work") is None

    def test_delete_missing_404(self, server_url):
        status, data = _req(server_url + "/api/accounts/nope", "DELETE")
        assert status == 404

    def test_csrf_blocked(self, server_url):
        status, data = _req(server_url + "/api/accounts", "POST", GOOD_PAYLOAD,
                            csrf=False)
        assert status == 403 and "CSRF" in data["error"]
        status, _ = _req(server_url + "/api/accounts/work", "DELETE", csrf=False)
        assert status == 403

    def test_invalid_payload_400(self, server_url):
        status, data = _req(server_url + "/api/accounts", "POST",
                            {"id": "bad"})  # 缺 user/password/host
        assert status == 400 and "缺少字段" in data["error"]

    def test_edit_empty_password_keeps_old(self, server_url):
        _req(server_url + "/api/accounts", "POST", GOOD_PAYLOAD)
        edited = {**GOOD_PAYLOAD, "password": "", "from_name": "新名字"}
        status, _ = _req(server_url + "/api/accounts", "POST", edited)
        assert status == 200
        acc = get_account("work")
        assert acc.password == "authcode123"  # 留空 = 保留原密码
        assert acc.from_name == "新名字"      # 其他字段正常更新

    def test_empty_password_new_account_400(self, server_url):
        status, data = _req(server_url + "/api/accounts", "POST",
                            {**GOOD_PAYLOAD, "id": "fresh", "password": ""})
        assert status == 400 and "密码" in data["error"]


class TestConnectionApi:
    def test_saved_account_test(self, server_url):
        status, r = _req(server_url + f"/api/accounts/{TEST_ACCOUNT.id}/test",
                         "POST")
        assert status == 200
        assert r["imap_ok"] is True and r["smtp_ok"] is True
        assert r["folders"] == 4             # fake 世界里的 4 个文件夹

    def test_unsaved_payload_test_no_writes(self, server_url):
        before = len(load_accounts())
        status, r = _req(server_url + "/api/test", "POST", GOOD_PAYLOAD)
        assert status == 200 and r["imap_ok"] and r["smtp_ok"]
        assert len(load_accounts()) == before  # 测试不落盘

    def test_wrong_password_reports_error(self, server_url):
        status, r = _req(server_url + "/api/test", "POST",
                         {**GOOD_PAYLOAD, "id": "x", "password": "wrong"})
        assert status == 200
        assert r["imap_ok"] is False and "登录失败" in r["error"]
