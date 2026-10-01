# 架构与踩过的坑

两个工具都是**单文件、只用标准库**的 Python 脚本（3.10+ 实测 3.14）。这份文档记录的不是"代码在哪"，而是"为什么只能这么写"——每一条都对应一次真实故障。

## 1. 存储：FTS5 trigram 独立表

`msg_fts` 是一张**独立**的 FTS5 虚拟表（`tokenize='trigram'`），元数据列（sid/role/ts/day/seq）标 `UNINDEXED`。

- 不用 external-content 表：部分 SQLite 构建（含 3.50）**不支持**对外部内容表执行 `insert`/`delete` 特殊命令，报 `SQL logic error`。独立表把正文和元数据一起存，代价是索引体积翻倍，换来写入路径简单可靠。
- `MATCH` 与 `snippet()` 里**必须写表名 `msg_fts`，不能写别名**，否则报 "unable to use function snippet in the requested context" 一类错误。
- 中文检索靠 trigram：≥3 字走索引；1–2 字退回 `instr()` 扫表（几万行的量级下毫秒级，可接受）。trigram 对 ASCII 默认忽略大小写。

## 2. 片段抓取：两条会静默骗人的坑

症状都是"明明命中 N 次，卡片却一条片段都没有"。

1. **不能用一个全局 `LIMIT` 截断候选行。** SQLite 按 rowid 返回，消息量最大的数据源（比如某个产品 1 万条消息）会把后面入库的源全部挤光。必须 `row_number() over (partition by sid order by ts desc)` 每个会话单独取前几条。
2. **`snippet()` 不能和窗口函数同层使用**，也**不能先用窗口挑出 rowid 再 join 回 FTS 表取**——后者不报错，但会退化成"文档开头 20 个 token"且丢掉高亮，比报错更难查。正确做法是两步：窗口查询只拿 `rowid` + 元数据，再对这批 rowid 跑一次带 `MATCH` 的 `snippet()`。
3. 短词分支在服务端开窗：`substr(body, max(1, instr(lower(body), ?) - 70), 280)`，别把整条 body 传回 HTTP 层。
4. 只有标题/项目/产物名命中、正文没命中的会话，补一条 `role="meta"` 的"线索"片段，计数文案写"线索在标题/项目/产物"而不是"提到 0 次"。

## 3. 增量与节流

- 每个源文件记 `file(path, sig="mtime:size")`，sig 不变就跳过；源文件被删则连带清掉它的会话。
- 全量扫一遍源目录约 3s，所以检索默认只在**距上次扫描 ≥45s** 时才重扫；AgentHub 的 `ensure_fresh()` 用同一套节流（`.last_sync` 的 mtime）。
- AgentHub 的同步挂在**读路径**上而不是定时器：需要新鲜的时刻就是有人查的时刻。同步失败一律吞掉——宁可查到稍旧的，也不把一次查询变成报错。

## 4. 无控制台运行

`pythonw.exe` 下 `sys.stdout is None`，任何 `print` 都会崩。`attach_log()` 在无控制台时把 stdout/stderr 重定向到 `~/.agentfind/agentfind.log`。排查桌面快捷方式"点了没反应"先看这个文件。

## 5. Windows 端口与进程

- `allow_reuse_address=True` 在 Windows 上**允许两个进程绑同一端口**。改完代码重启时，旧实例可能赖在 8765 上继续用旧代码服务，表现是"改了没生效"。
- 所以 `--stop` 不是杀端口上那一个，而是按命令行匹配杀掉**所有** `python*` + `agentfind.py` 的进程（排除自己）。写这类 PowerShell 过滤时**务必带进程名限制**（`$_.Name -match '^python'`），否则会把执行命令的 shell 自己杀掉。

## 6. 跳转层（跳回原对话）

- 每个 source 在 `OPENERS` 里登记一档：`chat` / `app` / `file`。**只有实测或包内代码确认过的才标 `chat`**，其余宁可低报——按钮文案必须诚实。
- 依据是"索引里本来就有的东西"：sid 竖线后半段就是各产品自己的 sessionId，所以零新增存储、不动表结构。
- `workbuddy://chat/<sessionId>` 是从 WorkBuddy 自己的 asar 里读出来的（它点系统通知时用的就是这句），截图确认有效。
- **CreateProcess 跑不了 npm 的 `.cmd` 包装**（`codex` 在 PATH 上是 `codex.cmd`），直接 `Popen(["codex", ...])` 会 `FileNotFoundError`；必须借 `cmd /d /k` 走一遍 PATH + PATHEXT。用 `/k` 而不是 `/c`，这样 resume 失败时窗口还留着能看到报错。
- url 档失败（协议没注册、应用被卸载）自动回落到 `reveal_file()`：资源管理器选中原始记录；文件没了就打开它所在目录。
- `POST /api/open` 会启动本机应用，所以有三道闸：`X-Requested-With: agentfind` 自定义头（跨站表单发不出来）、Origin 必须是 `127.0.0.1:<port>`、sid 必须能在 `session` 表里查到。参数一律 argv 列表或 `os.startfile`，不拼 shell 字符串。

## 7. AgentHub 的共享记忆模型

- 中心库 `~/.agenthub/memory.sqlite`：`notes`（含 agent/kind/topic/tags/src/sig）+ `notes_fts`（同款 trigram）+ `files`（增量签名）+ `dispatches`（派工留痕）。
- 三条通路：主动写（`mem write`）、只读搬（`mem sync` 按 `SYNC_SOURCES` 的 glob 抓各产品原生记忆 md，`kind='native'`，**永不回写产品目录**）、主动读（`mem search` / `mem context -n N`）。
- `mem context` 只注入 top-N，**不全量塞上下文**。这是与"项目内共享账本"式设计的根本区别：账本要求 agent 先读完全部历史才拿到当前状态，长度一涨必然互相触发循环；按查询注入 top-N 在结构上免疫这个问题。
- 派工只接有无头模式的产品（`codex exec`、`claude -p`）；桌面产品无无头 CLI，只参与记忆共享，`agenthub agents` 会如实说明。每次派工自动写一条 dispatch 记忆并在 `~/.agenthub/dispatches/` 留全文。
- 与 AgentFind 的闭环：`mem search` 结尾调 AgentFind 的 `/api/search`，输出 `http://127.0.0.1:8765/?sid=<source>|<id>` 形式的"过程原文"链接；AgentFind 页面支持 `?sid=` 直接弹出那场对话的阅读抽屉。AgentFind 没在服务时这段安静地不出现（失败冷却 60s，因为 Windows 连没监听的 127.0.0.1 端口不会立刻被拒，会干等到超时）。

## 8. 标题清洗

产品会往"用户消息"里塞自己的注入文本（记忆上下文、审批提示、转写边界 `>>> TRANSCRIPT START` 等）。`NOISE_PREFIX` 整条丢弃伪用户消息；取标题时用 `TITLE_SKIP` 跳过包装行/分隔线/标签行，取第一个"像样的行"。改这两个规则后必须 `--rebuild`：标题是存进 `session` 表的，增量扫描不重写。

## 9. 查询切词与召回回退（v1.1.0）

- 默认按空白切词、词间 AND。v1.1.0 起在 **ASCII↔中文交界**再切一刀：`ChatGPT打不开` → `ChatGPT` + `打不开`。
  只有当切出的每块都 ≥2 字、且拼回去恰好等于原串时才切——文件名（`三段式套磁信.docx`）、带标点的串、含单字的串都保持原样，否则 AND 语义会把它们查死。
- 单个 ≥6 字纯中文串整串 0 命中时，退化为 **3 字块 OR**，但要求**至少两块同时命中**才进候选。
  两块共现是精度闸门：单块（如「业介绍」）太容易捞到无关会话。实测 `数字孪生微专业介绍` 从 0 场变 39 场，且前几名就是那场微专业对话。

## 10. 自动任务会话（v1.1.0）

有些"会话"是产品自己跑的自动任务（典型：QwenWork CN 的记忆整理，一整场只有注入的记忆 dump + assistant 输出，没有一条 user）。
判定规则：**一场里既没有 `user` 也没有 `summary` 消息** → `session.auto=1`（TraeWork 的摘要级会话有 `summary`，不会被误判）。
处理是**降权而不是删除**：relevance 分 ×0.35，让它们排在真人对话后面但仍可被找到；卡片带「自动任务」角标，筛选栏可一键隐藏（`auto=0`）。
直接删掉会更"干净"，但那些会话里确实含有产品整理后的记忆正文，偶尔正是用户要找的东西。

## 11. 命令行复用在役服务（v1.1.0）

`--cli` / `--status` 在检测到 8765 已有服务时**改走本地 HTTP**，不再自己开库扫描。
原因：SQLite 同一文件上两个写者会 `database is locked`；而在役服务的增量扫描本来就会做这件事，CLI 再扫一遍既是锁冲突源也是纯浪费。
服务没在跑时才回落到本地开库的老路径。
