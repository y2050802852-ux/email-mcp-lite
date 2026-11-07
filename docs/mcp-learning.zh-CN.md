# MCP 是怎么跑起来的 —— 用 email-mcp-lite 把它拆开看

> 修订记录：2026-11-07 v1（首次整理；实验与行号基于当天的代码）

## 1 这份文档适合谁

你写（或维护）过一个 MCP 服务端，但当时是照着别人的思路搭的，现在想把原理补上——这份文档就是为你写的。读完之后，你应该能回答这三个问题：

1. 一个 MCP 服务端是怎么把「9 个工具」暴露给 Agent 的？
2. Agent 说「查一下我的新邮件」之后，中间到底发生了什么？
3. 如果要自己加第 10 个工具，该改哪里？

已经替你划掉的部分：每个模块的逐行源码、MIME 解析细节、IMAP 协议细节。想钻的时候，第 11 节给了入口。

## 2 一句话理解 MCP

MCP（Model Context Protocol）是一套约定：外部程序（我们叫**服务端**）把自己能做的事情包装成一份机器可读的「菜单」，Agent 宿主（ZCode、Claude Desktop，叫**客户端**）拿到菜单，转告大模型，模型点菜，客户端再把做好的菜端回去。

开胃的类比只有这一个，后文尽量用真实报文说话。记住三个角色就够：

- **模型**（GLM/GPT……）：只会输出文字和「我想调用某工具」的意图
- **客户端**（ZCode）：管连接、管菜单转发、真正去调服务端、把结果塞回对话
- **服务端**（email-mcp-lite）：一个普通进程，收 JSON-RPC 请求，干活，返回文本

一个容易搞反的点先说清楚：**服务端从来不主动找 Agent**。它像一个只会应答的窗口，收到请求才回话。牵线搭桥的活全部由客户端承担。

## 3 需要哪些基础知识

| 知识 | 到什么程度 | 在哪补 |
|---|---|---|
| JSON | 能看懂嵌套的对象和数组 | 不用补，边看边懂 |
| 命令行 | 会跑 `uv run`、认识管道 `\|` | 已具备 |
| Python | 懂函数、装饰器的基本概念（`@xxx` 是什么） | 装饰器看第 7.1 节的现场解释 |
| MCP 协议 | 零基础就行 | 这份文档本身 |

## 4 全局地图：一次「看看我的新邮件」背后发生了什么

这是全文最重要的一节。后面所有内容都是在放大这张图的不同角落。

你（`20265258053@stu.suda.edu.cn` 的主人）在 ZCode 输入框里敲下「看看我苏大邮箱今天有什么新邮件」，然后回车。接下来按顺序发生 8 件事：

```
① ZCode 启动会话时，就已经按配置拉起了 email-mcp 子进程，
   并完成了握手（拿到 instructions 和 9 个工具的 schema）

② ZCode 把「你的这句话」+「9 个工具的定义」一起发给模型

③ 模型读完，输出结构化意图：我要调 search_emails，
   参数是 {folder: "INBOX", since: "2026-11-07"}

④ ZCode（客户端）把它翻译成 JSON-RPC 请求，
   写进 email-mcp 子进程的 stdin：
   {"method":"tools/call","params":{"name":"search_emails","arguments":{...}}}

⑤ 子进程里，server.py 的 search_emails 函数被真正执行
   → 走 adapter 连 imap.stu.suda.edu.cn → 拿回邮件列表 → 拼成紧凑文本

⑥ 结果以 JSON-RPC 响应写回 stdout：
   {"result":{"content":[{"type":"text","text":"共 1 封:\nsuda|INBOX|..."}],"isError":false}}

⑦ ZCode 把这段 text 包装成「工具结果」塞回对话上下文

⑧ 模型看到结果，组织成人话回答你
```

注意两件事。第一，②和⑦发生在**模型的上下文窗口**里，这就是 token 消耗的来源——之前实测过：9 个工具的 schema 常驻约 1900 tokens，一次搜索返回 85 tokens，读一封邮件约 290 tokens。第二，⑤里服务端可以失败，失败的标志是响应里的 `isError: true`，模型看到后通常会说「调用出错了」，然后可能换参数重试。

## 5 学习路线图：从哪里开始看

按这个顺序走，每一步都踩在上一步上：

| 阶段 | 做什么 | 对应章节 | 产出 |
|---|---|---|---|
| S0 | 亲手跑一次握手和调用，看到真实报文 | §8 实验 1、2、3 | 「原来就是几行 JSON」的体感 |
| S1 | 读 `server.py`：工具层的全貌 | §6.2、§7.1 | 明白「暴露接口」是怎么发生的 |
| S2 | 回头精读一次完整报文 | §7.3 | 能逐字段说出每个部分是谁写给谁的 |
| S3 | 读入口和干活层 | §6.1、§6.3 | 明白一次调用从 stdin 到 IMAP 的路径 |
| S4 | 钻进 mcp SDK 看 MCPServer 怎么实现协议 | §11 | 明白哪些是协议要求、哪些是库的封装 |
| S5 | 加第 10 个工具练手 | §11 | 检验掌握程度的最好方式 |

时间感：S0 到 S3 一个晚上足够；S4、S5 看你愿意挖多深。

## 6 四个模块各管什么

项目结构（`src/email_mcp/`，共 8 个文件约 1600 行）：

```
cli.py        123 行   入口：命令行分发
server.py     304 行   工具层：9 个 @mcp.tool()，MCP 的「门面」
adapter.py    477 行   干活层：IMAP/SMTP 的实际操作
config.py     104 行   凭证：accounts.json 读写
parsing.py    209 行   MIME 解析、HTML 转纯文本、截断
models.py     136 行   数据类定义
imap_utf7.py   71 行   中文文件夹名编码
web.py/html   186 行   本地网页管理页（和 MCP 主链路无关）
```

### 6.1 cli.py —— 入口

主函数是 `main()`（`src/email_mcp/cli.py:83`）。逻辑很短：解析命令行参数；没有子命令就调 `run()` 启动 MCP 服务端（`:116-117`），有子命令（`add`/`list`/`remove`/`web`）就走账户管理。

```python
# src/email_mcp/cli.py:116-117
from .server import run
run()
```

MCP 服务端和账户管理共用一个包，这是刻意的：你在网页里加完账户，配置文件变了，运行中的服务端靠检查文件 mtime 感知到变化（`server.py:46` 的 `_get_adapter`），自动重新加载，不用重启。

### 6.2 server.py —— 工具层，MCP 的门面

这是学 MCP 最该精读的文件。开头两件事（`src/email_mcp/server.py:20`）：

```python
mcp = MCPServer(
    "email",
    instructions="多账户邮件工具集（IMAP/SMTP）。首次使用：先调用 list_accounts……",
)
```

`instructions` 是协议提供的字段：握手时随响应发给客户端，让 Agent 在正式干活前就拿到一份使用说明——比如「先调 list_accounts」「邮件 ID 要原样回传」。

然后是 9 个长得都一样的函数（`:75、92、104、130、153、165……`），以最简单的为例：

```python
# src/email_mcp/server.py:75-76
@mcp.tool()
def list_accounts() -> str:
    """列出已配置的邮件账户（id、登录用户、IMAP 服务器）。"""
```

`@mcp.tool()` 做了三件事：把函数注册进工具表；**从类型注解自动生成参数 schema**（`id: str` 就变成 `{"type":"string"}`）；把 docstring 当作工具描述。也就是说，你写的类型注解和 docstring 不只是给 Python 看的——它们原样变成了模型眼里的工具说明书，写得越准，模型调用越少出错。

末尾的 `run()`（`:302`）就一行：`mcp.run(transport="stdio")`，启动 stdio 传输，之后这个进程的 stdin/stdout 就是协议管道。

### 6.3 adapter.py —— 干活层

`ImapSmtpAdapter`（`:101`）包办所有真实邮件操作。它的 `_conn()`（`:110`）值得看一眼：维护一条可复用的 IMAP 连接，每次用 `noop()` 探活，断了就重连——这就是为什么连续调用不用反复登录。搜索、读信、发信分别在 `:219`、`:269`、`:428`。

分层的原因：adapter 不 import 任何 mcp 的东西，server 也不碰 imaplib。将来要支持 Gmail API，写一个 `GmailAdapter` 实现同一组方法（`MailAdapter` Protocol，`:37`），server 层一行不用改。

### 6.4 支撑层

`config.py` 管 `~/.email-mcp/accounts.json`（`CONFIG_FILE`，`:17`；写入时 chmod 600），`PRESETS`（`:20`）存了 QQ/163/Gmail 的服务器预设。`parsing.py` 把 MIME 邮件变成干净纯文本——这是「返回值省 token」的执行者。`models.py`、`imap_utf7.py` 顾名思义。

## 7 三个关键机制

### 7.1 工具是怎么「暴露」出去的

不用注册表配置文件，不用写接口文档。整个「暴露」过程就两步：

1. 每个公开能力写成一个普通 Python 函数，加上 `@mcp.tool()`
2. 客户端连上来发 `tools/list` 请求，SDK 把注册的 9 个函数各自变成 `{name, description, inputSchema}` 返回

实验 1 会让你亲眼看到这 9 个名字。装饰器在这里就是「把函数塞进一个列表」的语法糖，没有更多魔法。

### 7.2 Agent 是怎么决定调用工具的

这一段是新手最容易模糊的地方，值得说得刻板一些。

模型本身**不会执行任何东西**。它只是根据对话里的工具定义，输出一个结构化意图（行业里叫 tool call / tool_use），内容是「工具名 + 参数」。真正去调服务端的是客户端——ZCode 拿到意图，封一个 `tools/call` 请求发给子进程，拿到结果，再以「工具结果」的身份放回对话。模型这才看到结果，继续组织回答；如果它觉得还没完，可以接着输出下一个工具调用。

所以调用链是：**模型（出主意）→ 客户端（跑腿）→ 服务端（干活）→ 客户端（回传）→ 模型（继续）**。服务端视角更简单：stdin 来一行 JSON，stdout 回一行 JSON，仅此而已。

「Agent 知道该怎么用工具吗」这个问题也在这里得到回答：它知道的全部信息就是每个工具的 description + 参数 schema + 服务端的 instructions + 历次调用的结果。所以这些文本的质量直接决定调用质量——我们在 `instructions` 里写「邮件 ID 一律原样回传」，就是为了防模型自己编 ID。

### 7.3 一次调用的完整报文（真实数据）

以下是真实抓取的，不是构造的（抓取方法见实验）。

**握手**——客户端发 `initialize`，服务端回答自己是谁、支持什么：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2024-11-05",
  "capabilities":{"tools":{"listChanged":false}, ...},
  "serverInfo":{"name":"email","version":""},
  "instructions":"多账户邮件工具集（IMAP/SMTP）。首次使用：先调用 list_accounts……"}}
```

**发现工具**——客户端发 `tools/list`，服务端回 9 个工具的完整 schema（每个几百字符，全量约 5.9K 字符）。

**调用**——客户端发 `tools/call`：

```json
{"jsonrpc":"2.0","id":3,
 "method":"tools/call",
 "params":{"name":"list_accounts","arguments":{}}}
```

服务端执行 `list_accounts()`，把返回的字符串包进响应：

```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"qq: 2050802852@qq.com (imap imap.qq.com)\nsuda: ..."}],
  "isError":false}}
```

如果函数抛了异常，`isError` 变 `true`。实测发现一个值得注意的行为：mcp 2.x SDK 在工具抛异常时，回给 Agent 的文本是通用的 `"Error executing tool delete_email"`，具体的报错原因（比如「服务器处于只读模式」）只出现在服务端自己的日志里。这个设计有好有坏——模型知道失败了但不知道为什么。第 11 节把它列为一个练手改进项。

## 8 动手实验

以下命令都在本项目根目录验证过可以直接跑。建议真的敲一遍——看过报文之后，第 4 节那张图会从「文字」变成「事实」。

**实验 1：手动完成握手 + 发现工具**

```bash
(printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"me","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'; sleep 2) | uv run email-mcp
```

你会看到两行 JSON：id=1 的握手响应（含 instructions）和 id=2 的 9 个工具定义。**`sleep 2` 不能省**——printf 发完就关闭 stdin，服务端可能在处理完请求之前就退出了。这是 stdio 传输「对话结束即进程结束」的天性，正式客户端会一直保持管道打开，所以遇不到这个问题。

**实验 2：真实调用一个工具**

把第三行请求换成：

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_accounts","arguments":{}}}
```

响应里 `content[0].text` 就是工具函数的返回字符串——和你平时在对话里看到的一模一样，只是少了客户端的包装。

**实验 3：看错误怎么回到 Agent**

给服务端加上只读开关再调一个危险工具：

```bash
(printf '%s\n' \
'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"me","version":"0"}}}' \
'{"jsonrpc":"2.0","method":"notifications/initialized"}' \
'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"delete_email","arguments":{"id":"qq|INBOX|1"}}}'; sleep 2) \
| EMAIL_MCP_READONLY=1 uv run email-mcp
```

观察响应里的 `"isError": true`。模型看到的就是这个标志加一句通用错误——它知道失败了，然后大概率会告诉你「删除失败了」并停下来问你。

## 9 常见坑

| 坑 | 现象 | 解法 |
|---|---|---|
| 手动发报文时 stdin 立刻关闭 | 请求发了但没有响应 | 像实验那样 `(printf ...; sleep 2)` 保持管道两秒 |
| 中文文件夹名直接传 | IMAP 报错或找不到文件夹 | 走修改版 UTF-7 编码 + 引号包裹（`imap_utf7.py`，adapter 的 `_mb()`） |
| 中文搜索词放进命令行 | 部分服务器拒绝 | 用 IMAP 字面量机制（`conn.literal`）+ `CHARSET UTF-8`（`adapter.py` 的 search） |
| 工具抛异常只有通用错误文本 | Agent 不知道失败原因 | 这是 mcp 2.x 的默认行为；想给模型更多信息，见第 11 节改进项 |
| 想当然认为服务端能弹确认框 | 安全机制落空 | 确认 UI 属于客户端；服务端能做的是软删除、只读开关 |

## 10 术语表

| 术语 | 含义 |
|---|---|
| MCP | Model Context Protocol，模型与外部工具之间的标准协议 |
| 服务端 / Server | 提供工具的一方（本项目 `email-mcp-lite`），stdio 下是个子进程 |
| 客户端 / Host | Agent 宿主（ZCode、Claude Desktop），负责连接、转发、把结果回填对话 |
| JSON-RPC | 走在 stdin/stdout 上的消息格式，`initialize`/`tools/list`/`tools/call` 都是它的 method |
| 工具 / Tool | 一个可被模型调用的函数，由名称、描述、参数 schema 三部分描述 |
| schema | 参数的机器可读说明，由函数的类型注解自动生成 |
| stdio | 最常用的传输方式：stdin 收请求、stdout 发响应 |
| tool_use / tools/call | 模型输出的调用意图 / 客户端据此发出的真实请求 |

## 11 下一步可以往哪里钻

**改进项（拿真实问题练手）**：mcp 2.x 下工具异常只回通用文本。改法是在 `server.py` 的工具函数里捕获异常、把友好错误文本作为正常返回值交出去（配合 `isError` 语义需要一点取舍），改完用实验 3 验证 Agent 能看到具体原因。

**加第 10 个工具**：比如 `list_unread_count(account, folder)`。走一遍这个流程你就掌握全链路了：在 `server.py` 写函数 + docstring → `uv run pytest` 加一条用例 → 手动 `tools/list` 确认它出现 → 让 ZCode 真调一次。

**读 SDK 源码**：`.venv/lib/python3.13/site-packages/mcp/server/` 下的 `mcpserver.py`（工具注册与 run）和 `session.py`（JSON-RPC 收发循环）。建议带着问题读：「sync 工具函数被谁、在哪个线程里调用？」答案会让你对第 4 节的第⑤步有实感。
