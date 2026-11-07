# Changelog

## 0.1.1（2026-11-07）

- 新增与包名同名的命令别名 `email-mcp-lite`，`uvx email-mcp-lite web` 开箱即用

## 0.1.0（2026-11-07）

首个公开版本。

- IMAP/SMTP 通用适配器（标准库实现，运行时仅依赖 `mcp`）
- 9 个 MCP 工具：list_accounts / list_folders / search_emails / get_email /
  get_attachments / send_email（回复·转发·草稿）/ mark_email / move_email / delete_email
- 多账户管理与账户隔离；本地网页管理页（`email-mcp web`，含保存前连通性测试）
- Token 经济设计：搜索只回元数据、正文默认截断 2000 字符、附件只落盘不内联、
  工具 schema 全量约 1.9K tokens
- 安全机制：软删除默认、`EMAIL_MCP_READONLY` 只读开关、凭证 600 权限、
  网页仅监听 127.0.0.1 且带 CSRF 防护
- 101 个测试（进程内 fake IMAP/SMTP），支持 Python 3.10-3.13
