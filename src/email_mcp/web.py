"""本地网页账户管理（email-mcp web）。

- 仅绑定 127.0.0.1，标准库 http.server，零额外依赖
- 密码只进不出：GET 接口永远不返回 password；编辑时留空 = 保留原密码
- CSRF 防护：写操作必须带 X-Requested-With 头（跨站表单/简化 POST 因无法
  通过 CORS 预检而失效）
- 保存前后均可测连接（IMAP 登录 + SMTP 登录）
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .adapter import ImapSmtpAdapter
from .config import PRESETS, load_accounts, remove_account, save_account
from .models import Account

_DEFAULT_PORT = 8642
_HTML_PATH = Path(__file__).parent / "web.html"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_CSRF_HEADER = "X-Requested-With"


def _public(a: Account) -> dict:
    """账户的对外视图：绝不含密码。"""
    return {
        "id": a.id, "user": a.user,
        "imap_host": a.imap_host, "imap_port": a.imap_port,
        "smtp_host": a.smtp_host, "smtp_port": a.smtp_port,
        "smtp_ssl": a.smtp_ssl, "from_name": a.from_name,
    }


def _find(account_id: str) -> Account | None:
    return next((a for a in load_accounts() if a.id == account_id), None)


def _upsert(payload: dict) -> None:
    """新增/更新账户。编辑时密码留空 = 保留原密码（密码不出服务端）。"""
    missing = [k for k in ("id", "user", "imap_host") if not payload.get(k)]
    if missing:
        raise ValueError(f"账户配置缺少字段: {', '.join(missing)}")
    old = _find(str(payload["id"]))
    if not payload.get("password"):
        if old is None:
            raise ValueError("密码/授权码不能为空")
        payload = {**payload, "password": old.password}
    acc = Account.from_dict(payload)
    if not acc.smtp_host:
        acc.smtp_host = acc.imap_host
    save_account(acc)


def _test(payload: dict) -> dict:
    """对给定（或已保存的）账户参数做连通性自检，不落盘。"""
    acc = Account.from_dict(payload)
    return ImapSmtpAdapter(acc).test_connection()


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 工具方法 ----

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"请求体不是合法 JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("请求体应为 JSON 对象")
        return data

    def _check_csrf(self) -> bool:
        if self.headers.get(_CSRF_HEADER) == "email-mcp":
            return True
        self._json(403, {"error": "缺少 X-Requested-With: email-mcp 请求头（CSRF 防护）"})
        return False

    def _route_id(self) -> str:
        account_id = unquote(urlparse(self.path).path.split("/")[-1])
        if not _ID_RE.match(account_id):
            raise ValueError(f"非法账户标识: {account_id!r}")
        return account_id

    def log_message(self, fmt, *args):  # 安静模式：不往 stderr 刷访问日志
        pass

    # ---- 路由 ----

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = _HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            if path == "/api/accounts":
                self._json(200, [_public(a) for a in load_accounts()])
            elif path == "/api/presets":
                self._json(200, PRESETS)
            else:
                self._json(404, {"error": f"未知路径 {path}"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        if not self._check_csrf():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/accounts":
                payload = self._body()
                _upsert(payload)
                self._json(200, {"ok": True, "id": payload.get("id")})
            elif path == "/api/test":
                self._json(200, _test(self._body()))
            else:
                m = re.fullmatch(r"/api/accounts/([^/]+)/test", path)
                if m:
                    account_id = unquote(m.group(1))
                    acc = _find(account_id)
                    if acc is None:
                        self._json(404, {"error": f"账户 {account_id} 不存在"})
                        return
                    self._json(200, ImapSmtpAdapter(acc).test_connection())
                else:
                    self._json(404, {"error": f"未知路径 {path}"})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_DELETE(self):
        if not self._check_csrf():
            return
        try:
            account_id = self._route_id()
            if remove_account(account_id):
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": f"账户 {account_id} 不存在"})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": str(e)})


def serve(port: int = _DEFAULT_PORT, open_browser: bool = True) -> None:
    """启动本地管理页面（阻塞运行，Ctrl+C 退出）。"""
    url = f"http://127.0.0.1:{port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
    except OSError as e:
        raise SystemExit(
            f"端口 {port} 被占用（{e}）。换个端口试试: email-mcp web --port {port + 1}") from e
    server.daemon_threads = True
    print(f"email-mcp 账户管理页已启动: {url}  （Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()
