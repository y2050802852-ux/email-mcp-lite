#!/usr/bin/env python3
r"""真实邮箱联调脚本（只读检查，不影响邮箱内容）。

用法:
    uv run python scripts/smoke_test.py <账户id>          # 只读检查
    uv run python scripts/smoke_test.py <账户id> --send 收件地址   # 额外发一封自检邮件
    uv run python scripts/smoke_test.py <账户id> --folder "Sent Messages"  # 指定起始文件夹

依次验证：登录 → 列文件夹 → 搜索 → 读信 → 附件元数据。
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from email_mcp.adapter import get_adapter  # noqa: E402
from email_mcp.config import load_accounts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="email-mcp 真实邮箱联调")
    parser.add_argument("account", help="账户短标识（email-mcp list 可查）")
    parser.add_argument("--folder", default="INBOX", help="起始文件夹")
    parser.add_argument("--send", metavar="TO", help="给该地址发一封自检邮件")
    args = parser.parse_args()

    if args.account not in [a.id for a in load_accounts()]:
        print(f"✗ 账户 {args.account!r} 不存在。先运行: uv run email-mcp add")
        return 1

    ad = get_adapter(args.account)
    print(f"== 联调账户: {args.account} ==\n")

    print("[1/6] IMAP 登录...")
    folders = ad.list_folders()
    print(f"  ✓ 登录成功，共 {len(folders)} 个文件夹")

    print("[2/6] 文件夹列表（前 8 个）:")
    for name, total, unseen in folders[:8]:
        print(f"  {name} ({total}封/{unseen}未读)")

    print(f"[3/6] 搜索 {args.folder} 最近邮件...")
    metas = ad.search(args.folder, limit=5)
    print(f"  ✓ 取回 {len(metas)} 封")
    for m in metas:
        marks = "".join(
            s for s, on in (("[未读]", not m.read), ("[附件]", m.has_attachments)) if on)
        print(f"  {m.id} | {m.date} | {m.from_} | {m.subject} {marks}")
    if not metas:
        print("  （文件夹为空，跳过读信检查）")
        return 0

    print("[4/6] 读取第一封（正文截 500 字符）...")
    d = ad.get(metas[0].id, body_max=500)
    print(f"  ✓ Subject: {d.meta.subject}")
    print(f"  正文预览: {d.body[:120]!r}{'…' if d.truncated else ''}")
    print(f"  附件: {', '.join(a.filename for a in d.attachments) or '无'}")

    print("[5/6] 未读统计搜索...")
    unread = ad.search(args.folder, unread_only=True, limit=3)
    print(f"  ✓ 最近未读 {len(unread)} 封（示意）")

    if args.send:
        print(f"[6/6] SMTP 发信自检 → {args.send}")
        from email_mcp.adapter import build_message

        msg = build_message(
            ad.account, [args.send], [], "email-mcp 联调自检",
            "这是一封来自 email-mcp smoke_test 的自检邮件，收到即发送链路正常。")
        mid = ad.send(msg)
        print(f"  ✓ 已发送 Message-ID={mid}")
    else:
        print("[6/6] 跳过 SMTP 发信自检（加 --send 收件地址 可测发送链路）")

    print("\n✓ 全部通过。可以把本服务端接入 MCP 客户端了（见 README）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
