---
name: agent-collab
description: >
  跨 Agent 共享记忆与子 Agent 派工（CLI：agenthub）。

  Use when: 用户要求把本次工作的结论/偏好沉淀给其他 Agent 或"记住这件事"；
  查询其他 Agent 是否做过/知道某事、跨工具找回上下文；把子任务派给 codex/claude
  并行或借助其能力执行；跨 Agent 交接（handoff）。会话结束前有重要未沉淀结论时也应使用。
---

# AgentHub — 多 Agent 共享记忆 + 子 Agent 派工

所有能跑命令行的 Agent（Qoder / Codex / WorkBuddy / QwenWork / Claude Code / ZCode …）
共用一个记忆库（`~/.agenthub/memory.sqlite`），入口是 PATH 上的 `agenthub` 命令。

## 写共享记忆（完成有价值的工作后）

```bash
agenthub mem write "结论一句话" --agent 产品名 --kind note --topic 主题 --tags 标签1,标签2
```

- kind 可选 note（默认）/ decision（决定）/ fact（事实）/ handoff（交接）。
- 一条只写一件事，写结论和坑，不写流水账；目标是让其他 Agent 不必重新摸索。
- 内容为 `-` 时从 stdin 读，适合长文本。
- 典型时机：装好/修好某个工具、发现用户重要偏好、项目关键决定、可复用的命令或路径。

## 查共享记忆

```bash
agenthub mem search "关键词" [--agent qoder-cn] [--since 30d]   # 全文检索
agenthub mem context "查询" [-n 5]                              # 生成可粘进提示词的上下文块
agenthub mem list [-n 15] / stats / read <id>
```

- 中文 1–2 字也能查（自动退回子串匹配）。
- 各产品的原生记忆 md 在**每次查询前已自动增量同步**（45s 节流），不用也不该手动去改那些原生记忆文件；
  `agenthub mem sync` 只是手动补跑一次。同步哪些文件由 `~/.agenthub/sources.json` 决定，见下节。
- 查询结果末尾会附「过程原文」链接：把这条结论接回 AgentFind 里那场真实对话（点开可再一键跳回那个应用）。
  AgentFind 没在运行时这段会安静地不出现，不是出错。

## 派工给子 Agent

```bash
agenthub call claude "任务描述" --cwd D:\某项目 --mem 相关关键词
agenthub call codex  "任务描述" --cwd D:\某项目 --mem 相关关键词 --write
agenthub agents        # 看谁可派
agenthub log [-n 10] / log --show <id>   # 派工记录与完整输入输出
```

- `--mem 关键词`：派工前自动检索共享记忆注入子 Agent 提示词（子 Agent 因此知道
  其他 Agent 沉淀的背景），强烈建议带上。
- codex 默认只读沙箱，`--write` 才允许改文件；claude 默认拒绝工具调用，`--yolo`
  才跳过权限确认。只给确实需要的权限。
- 默认超时 900s，`--timeout` 可调；超时会强杀整棵进程树并记录退出码 124。
- 派工有成本和延迟：小任务、查资料、纯问答自己做完即可；需要另一种能力或真并行时再派。
- 每次派工自动写一条 dispatch 记忆进共享库，其他 Agent 能看到"谁派了什么、结果如何"。

## 接入新 Agent（本机装了个新产品之后）

共享库对各产品是**只读同步、绝不回写**，所以新 Agent 想吃别人的记忆必须主动查；三条路由省事到麻烦：

1. **命令行**（任何能跑 shell 的产品，零改动）：`agenthub mem search "关键词"` / `agenthub mem context "本次任务"`。
   在它的系统提示词里加一句"开工前先跑 `agenthub mem context`"就够了。
2. **共用 skill**：本 skill 在 `~/.agents/skills/agent-collab`。把新产品的 skills 目录 junction 到
   `~/.agents/skills` 即自动读到（`cmd /c mklink /J "<产品的skills目录>" "<用户目录>\.agents\skills"`；
   目录里已有内容要先合并再链，别覆盖）。
3. **指令文件**：往它的 `AGENTS.md` / `CLAUDE.md` 里加同样那一行。

反过来，把新 Agent 的原生记忆收进池子——**先跑一次扫描，它会列出没接入的源**：

```bash
agenthub mem source scan            # 只报告
agenthub mem source scan --fix      # 一次接入全部并同步
agenthub mem source list            # 现有源 + 各自命中文件数
agenthub mem source add <产品> "<相对home的glob>"   # 手动补一条（如记忆不在常见形状里）
agenthub mem source rm <产品或glob>  # 删掉；该产品的原生记忆同时清出共享库
```

- 扫描只认这些形状：`memory|memories|awareness/**/*.md`、`projects/*/memory/*.md`、
  `automations/*/memory.md`，以及产品目录根上的 `MEMORY/AGENTS/CLAUDE/USER/IDENTITY/SOUL.md`；
  判重按**实际命中的文件**，已有规则的超集不会被重复登记。
- 结果写进 `~/.agenthub/sources.json`，改完即生效，不需要改代码或重装。
- 新产品**对话**不用管：AgentFind 按形状自动发现（jsonl 目录结构 / sqlite 表集合），认不出会在首页点名。
- 卸载某产品后：它的源不再命中文件，下次 sync 自动把它的原生记忆清出共享库（自己写的 note 保留）。
- 记忆不是 md 的产品（sqlite / json / jsonl 形态）目前**同步不到**，`mem source scan` 也扫不出来；
  需要时得为它单独写解析器，别用 `source add` 硬塞非 md 文件（会被后缀白名单挡掉）。

