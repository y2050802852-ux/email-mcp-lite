"""命令行入口：
- 无子命令：启动 MCP 服务端（stdio）
- add / list / remove：管理账户凭证
"""

from __future__ import annotations

import argparse
import getpass
import sys

from .config import CONFIG_FILE, PRESETS, load_accounts, remove_account, save_account
from .models import Account


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{msg}{suffix}: ").strip()
    return value or default


def cmd_add(args) -> None:
    preset = args.preset
    if not preset:
        print("选择邮箱类型: 1) QQ  2) 163  3) Gmail  4) 其他（自定义 IMAP/SMTP）")
        choice = input("序号 [4]: ").strip() or "4"
        preset = {"1": "qq", "2": "163", "3": "gmail"}.get(choice, "custom")

    if preset == "custom":
        acc = Account(
            id=args.id or _prompt("账户短标识（如 suda）"),
            user=args.user or _prompt("登录用户名（邮箱地址）"),
            password=args.password or getpass.getpass("密码/授权码: "),
            imap_host=args.imap_host or _prompt("IMAP 服务器"),
            imap_port=args.imap_port or int(_prompt("IMAP 端口", "993")),
            smtp_host=args.smtp_host or _prompt("SMTP 服务器"),
            smtp_port=args.smtp_port or int(_prompt("SMTP 端口", "465")),
            from_name=args.from_name or _prompt("发件显示名（可空）"),
        )
    else:
        p = PRESETS[preset]
        print(f"提示: {p['note']}")
        acc = Account(
            id=args.id or _prompt(f"账户短标识（如 {preset}）", preset),
            user=args.user or _prompt("登录用户名（邮箱地址）"),
            password=args.password or getpass.getpass("密码/授权码: "),
            imap_host=args.imap_host or p["imap_host"],
            imap_port=args.imap_port or p["imap_port"],
            smtp_host=args.smtp_host or p["smtp_host"],
            smtp_port=args.smtp_port or p["smtp_port"],
            from_name=args.from_name or "",
        )

    if not acc.user or not acc.password:
        sys.exit("用户名和密码/授权码不能为空")
    save_account(acc)
    print(f"✓ 账户 {acc.id} 已保存（{CONFIG_FILE}，权限 600）")
    print("下一步: 把本服务端加进你的 MCP 客户端配置（见 README），然后即可使用。")


def cmd_list(_args) -> None:
    accs = load_accounts()
    if not accs:
        print("（无账户）运行 email-mcp add 添加")
        return
    for a in accs:
        print(f"{a.id:<12} {a.user:<32} imap={a.imap_host}:{a.imap_port} "
              f"smtp={(a.smtp_host or '-')}:{a.smtp_port}")


def cmd_remove(args) -> None:
    if remove_account(args.id):
        print(f"✓ 已删除账户 {args.id}")
    else:
        sys.exit(f"账户 {args.id} 不存在")


def cmd_web(args) -> None:
    from .web import serve
    serve(port=args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="email-mcp",
        description="轻量级多账户邮件 MCP Server（IMAP/SMTP）",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="添加/更新邮箱账户")
    p_add.add_argument("--id", help="账户短标识")
    p_add.add_argument("--user", help="登录用户名")
    p_add.add_argument("--password", help="密码/授权码（不传则交互输入）")
    p_add.add_argument("--preset", choices=list(PRESETS) + ["custom"],
                       help="服务商预设（默认交互选择）")
    p_add.add_argument("--imap-host")
    p_add.add_argument("--imap-port", type=int)
    p_add.add_argument("--smtp-host")
    p_add.add_argument("--smtp-port", type=int)
    p_add.add_argument("--from-name")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="列出已配置账户")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("remove", help="删除账户")
    p_rm.add_argument("id", help="账户短标识")
    p_rm.set_defaults(func=cmd_remove)

    p_web = sub.add_parser("web", help="打开本地网页管理账户（推荐）")
    p_web.add_argument("--port", type=int, default=8642, help="监听端口（默认 8642）")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    if args.cmd is None:
        from .server import run
        run()
        return
    args.func(args)


if __name__ == "__main__":
    main()
