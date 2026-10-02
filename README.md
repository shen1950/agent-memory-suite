# agent-memory-suite

两个**单文件、零第三方依赖**的 Windows 工具，解决同一件事：你和多个 AI Agent 聊过的内容，散在各自产品的私有记录里，互相看不见。

| 工具 | 干什么 | 入口 |
|---|---|---|
| **AgentFind** | 跨 Agent 对话全文检索：把各产品的会话记录统一索引成一份本地 FTS5 库，按关键词 / 项目 / 时间 / 产物文件名反查「何时 · 哪个产品 · 哪个项目 · 哪场对话」，网页里还能一键跳回原应用的那场对话 | `agentfind`（网页）/ `agentfind 关键词` / `agentfind --cli 关键词` |
| **AgentHub** | 跨 Agent 共享记忆 + 子 Agent 派工：一份中心 SQLite 库，各产品的原生记忆只读同步进来；任何 Agent 都能写结论、查别人的结论；也能把任务派给 `codex` / `claude` 无头执行 | `agenthub mem search "关键词"` / `agenthub mem write ...` / `agenthub call codex "任务"` |

两者是闭环的：AgentHub 的检索结果末尾会附上 AgentFind 的深链，从「别的 Agent 的结论」直接跳回「那场对话的原文」。

## 安装（Windows 10/11，Python ≥ 3.10）

```powershell
git clone https://github.com/shen1950/agent-memory-suite.git
cd agent-memory-suite
powershell -ExecutionPolicy Bypass -File install.ps1                     # 装到 ~/.agentfind 与 ~/.agenthub，并加入 PATH
powershell -ExecutionPolicy Bypass -File install.ps1 -DesktopShortcuts   # 额外在桌面建「找 AI 对话 / 关闭找 AI 对话」双击入口
```

装完新开一个终端：

```bash
agentfind                 # 起本地网页（127.0.0.1:8765）并打开浏览器
agentfind 锂电池          # 预填关键词
agentfind --status        # 各产品会话数
agentfind --rebuild       # 全量重建索引（约 30s / 700 场）
agenthub mem search "关键词"
agenthub mem context "查询"   # 生成可直接粘进提示词的记忆上下文块
```

桌面入口是给不想碰终端的人的：双击「找 AI 对话」即开网页，重复双击不会起第二个服务；「关闭找 AI 对话」只停服务、不删索引。

## 已验证能索引的产品

自动发现规则：任何把会话存成 `~/.<产品>/projects/<项目>/<会话>.jsonl` 的 Agent 都会被纳入，无需改代码。
v1.3 起发现逻辑改成**按形状嗅探**（不再靠产品名单）：`projects/**.jsonl`、`sessions/**.jsonl[.zst|.zstd]`、
以及 sqlite 里的表集合（`session/message/part` 或 `sessions/turns`），扫描 home 与 `%APPDATA%` 下像 Agent 的目录，
结果缓存 10 分钟。**新装的 Agent 要么被自动收录，要么以目录名出现在首页"还没解析器"清单里**，不会静默漏掉。

| 产品 | 记录位置 | 粒度 |
|---|---|---|
| Claude Code / Qoder CN / Qoder / WorkBuddy / QwenWork CN / CatPaw 等 | `~/.<产品>/projects/**/*.jsonl` | 逐条消息 |
| Codex | `~/.codex/sessions/**/*.jsonl` | 逐条消息 |
| DeepSeek DSH | `~/.dsh/sessions/<工作区>/session-*/session.vN.jsonl.zstd` | 逐条消息（需 Python 3.14+ 的标准库 zstd，低版本自动跳过该源） |
| opencode / ZCode | `~/.local/share/opencode/opencode.db`、`~/.zcode/cli/db/db.sqlite`（同构） | 逐条消息 |
| VS Code Copilot | `%APPDATA%\Code\User\globalStorage\github.copilot-chat\session-store.db` | 逐条消息 |
| TraeWork CN | `~/.trae-cn/memory/projects/**`（正文库加密，只索引它自己落盘的会话摘要） | 摘要级 |

**索引不到 ≠ 没聊过**：Cursor、VS Code Copilot Chat（记录在 `workspaceStorage/*/state.vscdb`）、Qoder IDE 侧边栏、以及任何加密正文库，页面首页会如实列出来。

## 跳回原对话（三档，按钮文案如实标注）

| 档 | 行为 | 已实测 |
|---|---|---|
| `chat` | 直接打开那场会话 | WorkBuddy（`workbuddy://chat/<sessionId>`）、Codex（`codex resume <id>`） |
| `app` | 把应用唤到前台，同时把会话 ID 复制进剪贴板 | Qoder / QwenWork / ZCode / opencode（协议只到应用级） |
| `file` | 在资源管理器里选中原始记录文件 | 兜底，所有源都保证有反应 |

## 隐私

- **所有数据留在本机**：索引在 `~/.agentfind/index.sqlite`，共享记忆在 `~/.agenthub/memory.sqlite`；网页服务只绑定 `127.0.0.1`。
- 仓库里不含任何索引、记忆、日志或派工留痕（见 `.gitignore`）。
- 只读原则：两个工具都不改写任何产品的原始记录；AgentHub 同步原生记忆时也只读抓取、从不回写。
- 会启动本机应用的接口（`POST /api/open`）有三道闸：自定义请求头、Origin 校验、sid 必须存在于索引；命令一律按参数列表执行，不拼 shell。

## 卸载

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1              # 移除命令与快捷方式，保留数据
powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData # 连索引和记忆库一起删
```

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 索引结构、FTS5 在本机 SQLite 上的坑、片段抓取的两步法、跳转层与同步机制的设计理由。
- [skills/agent-collab/SKILL.md](skills/agent-collab/SKILL.md) —— 给 Agent 自己读的协作规则（装到 `~/.agents/skills/` 后各产品共用）。

## 更新记录

- **v1.3.3**
  - 两个桌面图标现在都保证"点了就有框"：`WshShell.Exec(...).StdIn.ReadAll()` 在装了国产安全软件的机器上
    会抛"错误的文件模式"（连 `cmd /c echo` 都读不出来），关闭图标的弹框那一行根本执行不到，
    于是用户只看到页面、以为"关闭"按钮开了标签页。改为 `stop.cmd` 把 `--stop` 输出重定向到临时文件，
    VBS 用 `ADODB.Stream`(utf-8) 读回来弹框，实测显示"已停止 1 个 AgentFind 服务进程（PID）…"。
  - 打开图标不再盲等 2.5 秒：改成最多 12 秒轮询服务端口，就绪才开页面；起不来就明确弹框说明，
    不再开一个连不上的标签页。桌面快捷方式参数补上引号（原先 `$q` 未定义）。
- **v1.3.1**
  - 桌面快捷方式不再用 `pythonw.exe`：部分机器上安全软件只放行 `python.exe`，`pythonw.exe` 能监听但
    收不到任何回环连接，双击图标看起来就像程序坏了。改为 `wscript` 隐藏窗口启动 `python.exe`，效果相同。
  - 自调用（启动探测、`--cli`/`--status`、AgentHub 的过程原文链接）统一绕过系统代理：
    Windows 上 Python 不认 `ProxyOverride` 的 `127.*`/`<local>`，开着 Clash 时会被丢给代理并挂住。
  - 自动发现加了时间预算（6 秒）与嗅探上限，且不再等待被别的进程锁住的库；最坏情况不再拖住启动。
- **v1.3.0**
  - 新装 Agent 自动收录：发现逻辑从"写死产品名单"改成按形状嗅探（jsonl 目录结构 + sqlite 表集合），
    扫 home 与 `%APPDATA%`，结果缓存 10 分钟；识别不出来的目录会在首页点名，不会静默漏。
  - 新增 DeepSeek DSH 源：`~/.dsh/sessions/**/session.vN.jsonl.zstd`，zstd 压缩的 JSONL，
    项目路径取自会话头的 `cwd`。
- **v1.2.0**
  - TraeWork CN 的项目路径全部可还原：逆向出其 slug 算法（规范化路径逐字符替换 + `-p2-` + `sha256(规范化路径)[:20]`），
    候选路径来自 Trae 自己的记录 + `state.vscdb` 里的 `file://` URI + 有界目录遍历，sha256 逐条校验（本机 12/12）。
  - 新增 VS Code Copilot 源：`globalStorage/github.copilot-chat/session-store.db` 的 `sessions`+`turns` 两表，
    只读打开不带 `immutable`（否则读不到 WAL）。本机 Copilot 暂无历史正文，解析器以 fixture 验证。
  - 未覆盖清单更新：Cursor 不在列是因为它的对话正文存在服务器端、本机只有空壳元数据，不是没做。
- **v1.1.0**
  - 查询切词在 ASCII↔中文交界自动切开（`ChatGPT打不开` → 两个线索）；单个长中文串整串查不到时退化为 3 字块 OR（要求至少两块共现），粘在一起的关键词不再查空。
  - 产品自己的自动任务会话（一场里连一条你的提问都没有，如记忆整理）打 `auto` 标记：默认降权排在真人对话后面，卡片带「自动任务」角标，筛选栏可一键隐藏。
  - `--cli` / `--status` 在服务运行时改走本地 HTTP，不再开第二个写者和在役服务抢 SQLite 锁。
- **v1.0.0** 首版：AgentFind 检索 + 跳回原对话，AgentHub 共享记忆与派工。

## License

MIT
