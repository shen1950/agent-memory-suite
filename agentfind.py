#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentFind —— 本机多 Agent 对话全局检索。

线索进（关键词 / 产物文件名 / 项目名 / 日期），出「什么时候 · 哪个产品 · 哪个项目 · 哪个对话」。

用法:
    agentfind                     刷新索引，起本地网页并打开浏览器
    agentfind 锂电池 RUL           同上，搜索框预填关键词
    agentfind --status            打印各数据源会话数与索引状态
    agentfind --rebuild           全量重建索引
    agentfind --cli 关键词         终端里直接搜，不起服务
    agentfind --port 9000 --no-open

只读扫描各 Agent 的本地记录，不修改任何原始文件。索引落在 ~/.agentfind/index.sqlite。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

HOME = Path.home()
APP_DIR = Path(os.environ.get("AGENTFIND_HOME") or (HOME / ".agentfind"))
DB_PATH = APP_DIR / "index.sqlite"
LOG_PATH = APP_DIR / "agentfind.log"

MAX_MSG_CHARS = 20000   # 单条消息入库上限
TRIGRAM_MIN = 3         # fts5 trigram 分词器要求的最短子串长度
IDX_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 运行环境
# --------------------------------------------------------------------------

def attach_log():
    """pythonw / 快捷方式启动时没有控制台，sys.stdout 是 None，print 会直接崩。"""
    if getattr(sys, "frozen", False) or sys.stdout is None or sys.stderr is None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr = stream


def already_running(port):
    """本机端口上已有 AgentFind 在跑就返回 True（避免重复启动去抢同一个端口）。"""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1.2) as r:
            return "total" in json.loads(r.read().decode("utf-8"))
    except Exception:
        return False


# --------------------------------------------------------------------------
# 时间 / 文本
# --------------------------------------------------------------------------

def to_epoch(v):
    """各种时间戳表示统一成 epoch 秒。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def day_of(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def fmt_time(ts):
    if not ts:
        return "时间未知"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


BLOCK_TAG = re.compile(
    r"<(system-reminder|command-name|command-message|command-args|local-command-stdout|"
    r"local-command-stderr|user-prompt-submit-hook|task-notification|environment_context"
    r"|available-skills)(?:\s[^>]*)?>"
    r".*?</\1>", re.S)

PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'`<>|]{2,200}"
    r"|/(?:Users|home|mnt|opt|var|srv|workspace|Claude|data|D)/[^\s\"'`<>|]{2,200}")

PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|/(?:Users|home|mnt|opt|var|srv|workspace|Claude|data)/)")


KEEP_INNER = re.compile(r"</?(user_query|local-command-caveat|user_prompt)(?:\s[^>]*)?>")


def clean_text(s):
    if not s:
        return ""
    s = BLOCK_TAG.sub(" ", str(s))
    s = KEEP_INNER.sub("", s)
    s = s.replace("\r\n", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def norm_path(p):
    """规整成可信路径，不像路径或太短就丢弃。"""
    if not isinstance(p, str):
        return None
    p = p.strip().strip("\"'<>(),;，。；、").replace("\\\\", "\\")
    p = re.sub(r"[\\/]$", "", p)
    if len(p) < 5 or "\n" in p or not PATH_START.match(p):
        return None
    return p


def collect_paths(obj, sink):
    """从工具调用参数里递归抓文件路径。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if re.search(r"path|file|filename|directory|target|output", k, re.I):
                    np = norm_path(v)
                    if np:
                        sink.add(np)
            elif isinstance(v, (dict, list)):
                collect_paths(v, sink)
    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, str):
                np = norm_path(it)
                if np:
                    sink.add(np)
            else:
                collect_paths(it, sink)


def paths_in_text(text, sink):
    for m in PATH_RE.finditer(text or ""):
        np = norm_path(m.group(0))
        if np:
            sink.add(np)


NOISE_PREFIX = ("Caveat:", "This request must be fulfilled using",
                "Target file this round:",              # QwenWork CN 每轮注入的记忆上下文
                "The following is the Codex agent history",  # Codex 审批提示
                "User explicitly selected MCP server")

# 取标题时要跳过的包装行：Codex 转写边界（>>> TRANSCRIPT START）、分隔线、成对标签行。
TITLE_SKIP = re.compile(r"^\s*(>{3}|<\s*/?\w|[-=*_~]{3,}\s*$)")


def is_system_prompt(raw):
    """整段产品自带 system prompt，不是用户内容。"""
    return len(raw) > 4000 and raw.lstrip().startswith(("You are", "Your task",
                                                        "You will", "## Rules", "# Personality"))


# --------------------------------------------------------------------------
# 解析结果
# --------------------------------------------------------------------------

@dataclass
class Parsed:
    sid: str
    source: str
    label: str
    src_file: str
    project: str = ""
    title: str = ""
    started: float | None = None
    ended: float | None = None
    branch: str = ""
    models: set = field(default_factory=set)
    messages: list = field(default_factory=list)   # (ts, role, text)
    artifacts: set = field(default_factory=set)

    def add(self, ts, role, text):
        text = clean_text(text)
        if not text:
            return
        if ts:
            if self.started is None or ts < self.started:
                self.started = ts
            if self.ended is None or ts > self.ended:
                self.ended = ts
        self.messages.append((ts, role, text[:MAX_MSG_CHARS]))

    def finish(self):
        if not self.title:
            for _ts, role, text in self.messages:
                if role != "user":
                    continue
                for line in text.split("\n"):
                    line = re.sub(r"^#+\s*", "", line.strip())
                    if line and not TITLE_SKIP.match(line):
                        self.title = line[:160]
                        break
                if self.title:
                    break
        return self


def read_lines(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.strip():
                    yield line
    except OSError:
        return


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------

TRAE_SOURCE = "trae-cn"
TRAE_LABEL = "TraeWork CN"

NAMED_CLAUDE = [
    (".qoder-cn", "Qoder CN"),
    (".qoder", "Qoder 国际版"),
    (".claude", "Claude Code"),
    (".catpaw", "CatPaw"),
    (".meituan-catpaw", "美团 CatPaw"),
    (".openclaw", "OpenClaw"),
    (".workbuddy", "WorkBuddy"),
    (".qwenworkcn", "QwenWork CN"),
]

SKIP_DIRS = {"node_modules", ".git", "vendor_imports", "backups_state", "skills", "plugins"}
SCAN_SKIP = {".agents", ".cache", ".config", ".local", ".npm", ".cargo", ".conda", ".android"}

# 本机装了但记录格式还没解析的：页面上要如实告诉用户，免得以为没聊过
UNCOVERED = ["Cursor", "VS Code Copilot Chat", "Qoder IDE 侧边栏对话"]


def pretty(dirname: str) -> str:
    name = dirname.lstrip(".") or "unknown"
    return re.sub(r"[-_]+", " ", name).strip().title() or name


def discover_sources():
    """[(source_id, label, kind, [root, ...])]，kind ∈ claude_jsonl | codex_jsonl | opencode_db | trae_memory"""
    out, known = [], set()

    def add(sid, label, kind, roots):
        roots = [r for r in roots if Path(r).exists()]
        if roots and sid not in known:
            known.add(sid)
            out.append((sid, label, kind, roots))

    for dname, label in NAMED_CLAUDE:
        root = HOME / dname
        add(dname, label, "claude_jsonl", [root / "projects", *sorted(root.glob("*/projects"))])

    # 新装 Agent 只要沿用 ~/.xxx/projects/<项目>/<会话>.jsonl 就自动纳入
    try:
        children = sorted(c for c in HOME.iterdir()
                          if c.is_dir() and c.name.startswith(".") and c.name not in SCAN_SKIP)
    except OSError:
        children = []
    for child in children:
        if child.name in known:
            continue
        prj = child / "projects"
        if prj.is_dir() and any(prj.glob("*/*.jsonl")):
            add(child.name, pretty(child.name), "claude_jsonl", [prj])

    codex = HOME / ".codex"
    add("codex", "Codex", "codex_jsonl", [codex / "sessions", codex / "archived_sessions"])
    add("opencode", "opencode", "opencode_db", [HOME / ".local/share/opencode/opencode.db"])
    add("zcode", "ZCode", "opencode_db", [HOME / ".zcode/cli/db/db.sqlite"])
    add(TRAE_SOURCE, TRAE_LABEL, "trae_memory", [HOME / ".trae-cn/memory/projects"])
    return out


KIND_PATTERN = {"opencode_db": "*.db", "trae_memory": "*"}


def iter_files(roots, pattern):
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root
            continue
        try:
            for p in root.rglob(pattern):
                if p.is_file() and not (SKIP_DIRS & set(p.parts)):
                    yield p
        except OSError:
            continue


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------

SKIP_TYPES = {
    "reasoning", "thinking", "tool_result", "function_call_result", "local_command_output",
    "file-history-snapshot", "snapshot", "last-prompt", "active-leaf", "attachment",
    "token_count", "step-start", "step-finish", "compact_boundary", "progress", "summary",
}


def ingest(s: Parsed, rec: dict, ts):
    """把一条记录并入会话。按字段形状分派，兼容 Qoder/Claude 嵌套式、
    Codex payload 包裹式、WorkBuddy 扁平式三种记录布局。"""
    typ = rec.get("type")
    payload = rec.get("payload")
    if ts is None:
        ts = to_epoch(rec.get("timestamp"))
    if isinstance(payload, dict) and typ in ("session_meta", "turn_context",
                                             "response_item", "event_msg"):
        ingest(s, payload, ts or to_epoch(rec.get("timestamp")))
        return
    if typ in SKIP_TYPES:
        return
    if rec.get("cwd"):
        s.project = rec["cwd"]
    if rec.get("gitBranch"):
        s.branch = rec["gitBranch"]
    if rec.get("model"):
        s.models.add(str(rec["model"]))

    if typ in ("session_meta", "turn_context"):
        if rec.get("cwd"):
            s.project = rec["cwd"]
        return
    if typ == "workspace-directories":
        dirs = rec.get("directories") or []
        if dirs and not s.project:
            s.project = dirs[0]
        return
    if typ == "runtime-config":
        if rec.get("model"):
            s.models.add(str(rec["model"]))
        return
    if typ in ("user_message", "agent_message"):
        return      # Codex 会把同一轮对话再记一份事件，response_item 已收录
    if typ == "function_call":
        name = rec.get("name") or "tool"
        args = rec.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"cmd": args}
        collect_paths(args, s.artifacts)
        cmd = ""
        if isinstance(args, dict):
            cmd = str(args.get("cmd") or args.get("command") or args.get("file_path")
                      or args.get("path") or args.get("pattern") or "")[:500]
        s.add(ts, "tool", f"[调用 {name}] {cmd}".strip())
        return

    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    role = msg.get("role") or rec.get("role") or (typ if typ in ("user", "assistant") else None)
    content = rec.get("content") if rec.get("content") is not None else msg.get("content")
    if role not in ("user", "assistant") or content is None or rec.get("isMeta"):
        return
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt in ("text", "input_text", "output_text"):
            text = b.get("text") or ""
            if is_system_prompt(text) or text.lstrip().startswith(NOISE_PREFIX):
                continue
            paths_in_text(text, s.artifacts)
            s.add(ts, role, text)
        elif bt == "tool_use":
            name = b.get("name") or "tool"
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            collect_paths(inp, s.artifacts)
            bits = [str(inp[k]).strip()[:500] for k in
                    ("command", "cmd", "pattern", "query", "prompt", "description", "url")
                    if isinstance(inp.get(k), str) and inp.get(k, "").strip()]
            s.add(ts, "tool", f"[调用 {name}] " + " | ".join(bits))


def parse_transcript_file(path: Path, source: str, label: str):
    """一个 .jsonl 会话记录文件 → 若干会话（同一文件可能含 sidechain）。"""
    sessions: dict[str, Parsed] = {}

    def get(sid):
        if sid not in sessions:
            sessions[sid] = Parsed(sid=sid, source=source, label=label, src_file=str(path))
        return sessions[sid]

    records = []
    for raw in read_lines(path):
        try:
            records.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    # Codex 把会话 id 写在 session_meta 里，优先用它，否则退回文件名
    real_id = next((str(r["payload"].get("id")) for r in records
                    if r.get("type") == "session_meta"
                    and isinstance(r.get("payload"), dict) and r["payload"].get("id")), None)
    fallback = real_id or path.stem
    for d in records:
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        sid = str(d.get("sessionId") or payload.get("sessionId") or "") or fallback
        ingest(get(sid), d, to_epoch(d.get("timestamp")))
    return [s.finish() for s in sessions.values() if s.messages]


def parse_opencode_db(path: Path, sid="opencode", label="opencode"):
    """opencode / ZCode：session / message / part 三张表，正文在 part.data。"""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    try:
        sess = list(con.execute("select id,title,directory,time_created,time_updated "
                                "from session order by time_created"))
        parts, msgs = {}, {}
        for r in con.execute("select message_id,data from part"):
            parts.setdefault(r["message_id"], []).append(r["data"])
        for r in con.execute("select id,session_id,time_created,data from message"):
            msgs.setdefault(r["session_id"], []).append(r)
    except sqlite3.Error:
        con.close()
        return out
    con.close()

    for sr in sess:
        sid_ = str(sr["id"])
        p = Parsed(sid=sid_, source=sid, label=label, src_file=str(path))
        p.project = sr["directory"] or ""
        p.title = (sr["title"] or "").strip()
        p.started = to_epoch(sr["time_created"])
        p.ended = to_epoch(sr["time_updated"]) or p.started
        for mr in msgs.get(sid_, []):
            try:
                md = json.loads(mr["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            role = md.get("role")
            if role not in ("user", "assistant"):
                continue
            mm = md.get("model")
            model = md.get("modelID") or (mm.get("modelID") if isinstance(mm, dict) else mm)
            if model:
                p.models.add(str(model))
            ts = to_epoch(mr["time_created"])
            for raw in parts.get(mr["id"], []):
                try:
                    b = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    paths_in_text(b["text"], p.artifacts)
                    p.add(ts, role, b["text"])
                elif bt == "file":
                    np = norm_path(b.get("url") or b.get("filename") or "")
                    if np:
                        p.artifacts.add(np)
                elif bt == "tool":
                    st = b.get("state") if isinstance(b.get("state"), dict) else {}
                    inp = st.get("input") if isinstance(st.get("input"), dict) else {}
                    collect_paths(inp, p.artifacts)
                    cmd = str(inp.get("command") or inp.get("filePath") or inp.get("path") or "")[:500]
                    p.add(ts, "tool", f"[调用 {b.get('tool') or 'tool'}] {cmd}".strip())
        if p.messages:
            out.append(p.finish())
    return out


TRAE_SUM_FILE = re.compile(r"^session_memory_(.+)\.jsonl$")
TRAE_TOPIC = re.compile(r"^##\s+\[session:\s*([^\]]+)\]\s*(.+)$")
_TRAE_PROJECT = {}


def _trae_project_path(mem_root: Path, slug: str):
    """slug 里的中文被逐字替换成了 '-'，从同目录 project_memory.md 头部还原真实路径。"""
    if slug not in _TRAE_PROJECT:
        real = ""
        try:
            head = (mem_root / slug / "project_memory.md").read_text(
                encoding="utf-8", errors="ignore")[:2000]
            m = re.search(r"项目路径[：:]\s*(\S.*\S)", head)
            if m:
                real = m.group(1).strip().strip('`"')
        except OSError:
            pass
        _TRAE_PROJECT[slug] = real
    return _TRAE_PROJECT[slug]


def _trae_parts(path: Path):
    """~/.trae-cn/memory/projects/<slug>/<YYYYMMDD>/file → (项目路径, day, day_epoch)"""
    day = path.parent.name
    slug = path.parent.parent.name if path.parent.parent else ""
    mem_root = path.parent.parent.parent if path.parent.parent else path.parent
    project = _trae_project_path(mem_root, slug) or slug
    try:
        return project, day, datetime.strptime(day, "%Y%m%d").timestamp()
    except ValueError:
        return project, "", None


def _trae_new(path: Path, sid: str, project: str, title: str):
    p = Parsed(sid="trae:" + sid, source=TRAE_SOURCE, label=TRAE_LABEL, src_file=str(path))
    p.project = project
    p.title = title[:160]
    return p


def parse_trae_memory(path: Path):
    """TraeWork CN：正文库加密，只能索引它自己落盘的会话摘要与每日 topics.md。"""
    project, day, day_ts = _trae_parts(path)
    out = []

    if TRAE_SUM_FILE.match(path.name):
        for line in read_lines(path):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            intent = str(rec.get("intent") or "").strip()
            if not intent:
                continue
            ts = to_epoch(rec.get("message_summary_time")) or day_ts
            p = _trae_new(path, str(rec.get("message_id") or f"{day}-{intent[:12]}"), project, intent)
            bits = [f"【意图】{intent}"]
            for key, tag in (("actions", "【动作】"), ("outcome", "【结论】"), ("learned", "【学到】")):
                val = rec.get(key)
                if val:
                    bits.append(tag + ("；".join(str(x) for x in val) if isinstance(val, list) else str(val)))
            body = "\n".join(bits)
            paths_in_text(body, p.artifacts)
            p.add(ts, "summary", body)
            if p.messages:
                out.append(p.finish())
        return out

    if path.name == "topics.md":
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return out
        m = re.search(r"^#\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        ts = to_epoch(m.group(1) + " 12:00:00") if m else day_ts
        blocks, cur = [], None
        for ln in text.splitlines():
            b = TRAE_TOPIC.match(ln.strip())
            if b:
                cur = {"key": b.group(1).strip(), "title": b.group(2).strip(), "body": []}
                blocks.append(cur)
            elif cur is not None and ln.strip():
                cur["body"].append(ln.strip().lstrip("- ").strip())
        for i, b in enumerate(blocks):
            body = "\n".join(b["body"])
            p = _trae_new(path, f"topic-{day}-{b['key']}-{i}", project, b["title"])
            paths_in_text(body, p.artifacts)
            p.add(ts, "summary", f"【{b['title']}】\n{body}" if body else b["title"])
            if p.messages:
                out.append(p.finish())
        return out

    return []


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------

SCHEMA = """
create table if not exists meta(k text primary key, v text);
create table if not exists file(path text primary key, sig text, source text);
create table if not exists session(
  sid text primary key, source text, label text, title text, project text,
  project_name text, started real, ended real, day text, n_msg integer,
  models text, branch text, src_file text, artifacts text);
create index if not exists session_day on session(day);
create virtual table if not exists msg_fts using fts5(
  body, sid unindexed, role unindexed, ts unindexed, day unindexed, seq unindexed,
  tokenize='trigram');
"""


def connect():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # 所有读写都在 IDX_LOCK 内串行，因此可以让工作线程共用这一个连接
    con = sqlite3.connect(str(DB_PATH), timeout=60, isolation_level=None,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("pragma journal_mode=WAL")
    con.execute("pragma synchronous=NORMAL")
    return con


def file_sig(path: Path):
    st = path.stat()
    return f"{int(st.st_mtime)}:{st.st_size}"


def project_name(p):
    if not p:
        return "(未知项目)"
    return str(Path(str(p).rstrip("/\\")).name) or p


def drop_session(con, sid):
    con.execute("delete from msg_fts where sid=?", (sid,))
    con.execute("delete from session where sid=?", (sid,))


def store_session(con, p: Parsed):
    sid = f"{p.source}|{p.sid}"
    drop_session(con, sid)
    con.execute(
        "insert into session(sid,source,label,title,project,project_name,started,ended,"
        "day,n_msg,models,branch,src_file,artifacts) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, p.source, p.label, p.title or "(无标题)", p.project, project_name(p.project),
         p.started, p.ended, day_of(p.ended or p.started), len(p.messages),
         ",".join(sorted(p.models))[:200], p.branch, p.src_file,
         "\n".join(sorted(p.artifacts))))
    con.executemany(
        "insert into msg_fts(body,sid,role,ts,day,seq) values(?,?,?,?,?,?)",
        [(text, sid, role, ts, day_of(ts), seq)
         for seq, (ts, role, text) in enumerate(p.messages, 1)])


def refresh(con, sources, force=False, log=None):
    """增量刷新：按 mtime+size 只重读变过的会话文件。"""
    say = log or (lambda *_: None)
    t0 = time.time()
    with IDX_LOCK:
        seen, n_new, n_upd, n_skip = set(), 0, 0, 0
        existing = {} if force else {r["path"]: r["sig"]
                                    for r in con.execute("select path,sig from file")}
        if force:
            con.execute("delete from file")

        def handle(path, parsed_list, source):
            nonlocal n_new, n_upd, n_skip
            sp = str(path)
            sig = file_sig(path)
            if not force and existing.get(sp) == sig:
                n_skip += 1
                return
            if sp in existing:
                n_upd += 1
            else:
                n_new += 1
            keep = {f"{source}|{p.sid}" for p in parsed_list}
            old = {r["sid"] for r in con.execute(
                "select sid from session where src_file=?", (sp,))}
            for gone in old - keep:
                drop_session(con, gone)
            for p in parsed_list:
                store_session(con, p)
            con.execute("insert or replace into file(path,sig,source) values(?,?,?)",
                        (sp, sig, source))

        con.execute("begin")
        try:
            for sid, label, kind, roots in sources:
                b_new, b_upd = n_new, n_upd
                for f in iter_files(roots, KIND_PATTERN.get(kind, "*.jsonl")):
                    seen.add(str(f))
                    try:
                        if kind == "opencode_db":
                            handle(f, parse_opencode_db(f, sid, label), sid)
                        elif kind == "trae_memory":
                            handle(f, parse_trae_memory(f), sid)
                        else:
                            handle(f, parse_transcript_file(f, sid, label), sid)
                    except Exception as e:
                        say(f"  ! {f.name}: {type(e).__name__}: {e}")
                touched = (n_new - b_new) + (n_upd - b_upd)
                say(f"  {label}: " + (f"索引 {touched} 个记录文件" if touched else "无变化"))

            for r in list(con.execute("select path from file")):
                if r["path"] not in seen and not Path(r["path"]).exists():
                    for s in list(con.execute("select sid from session where src_file=?",
                                              (r["path"],))):
                        drop_session(con, s["sid"])
                    con.execute("delete from file where path=?", (r["path"],))
            con.execute("insert or replace into meta(k,v) values('last_refresh',?)",
                        (str(time.time()),))
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
        stats = {"new": n_new, "updated": n_upd, "skipped": n_skip, "secs": time.time() - t0}
    say(f"刷新完成：新增 {n_new}、更新 {n_upd}、未变 {n_skip}，耗时 {stats['secs']:.1f}s")
    return stats


def rebuild(con, sources, log=None):
    with IDX_LOCK:
        con.execute("delete from session")
        con.execute("delete from msg_fts")
        con.execute("delete from file")
    return refresh(con, sources, force=True, log=log)


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------

def split_terms(q):
    return [t for t in re.split(r"\s+", (q or "").strip()) if t][:8]


def scope_sql(sources, day_from, day_to, alias):
    conds, params = [], []
    if sources:
        conds.append(f"{alias}.source in ({','.join('?' * len(sources))})")
        params += list(sources)
    if day_from:
        conds.append(f"{alias}.day >= ?")
        params.append(day_from)
    if day_to:
        conds.append(f"{alias}.day <= ?")
        params.append(day_to)
    return (" and " + " and ".join(conds)) if conds else "", params


def fts_match(term):
    return '"' + term.replace('"', '""') + '"'


def term_hits(con, term, sources, day_from, day_to):
    """单个线索 → ({sid: 命中消息数}, 只在标题/项目/产物里命中的 sid 集合)。"""
    cond, params = scope_sql(sources, day_from, day_to, "s")
    counts = {}
    if len(term) >= TRIGRAM_MIN:
        sql = ("select msg_fts.sid sid, count(*) c from msg_fts "
               "join session s on s.sid = msg_fts.sid "
               f"where msg_fts match ?{cond} group by msg_fts.sid")
        try:
            rows = con.execute(sql, (fts_match(term), *params))
        except sqlite3.OperationalError:
            rows = []
    else:
        sql = ("select msg_fts.sid sid, count(*) c from msg_fts "
               "join session s on s.sid = msg_fts.sid "
               f"where instr(lower(msg_fts.body),?)>0{cond} group by msg_fts.sid")
        rows = con.execute(sql, (term.lower(), *params))
    for r in rows:
        counts[r["sid"]] = r["c"]

    like = f"%{term.lower()}%"
    meta = {r["sid"] for r in con.execute(
        "select sid from session where lower(title) like ? or lower(project) like ? "
        "or lower(project_name) like ? or lower(artifacts) like ? or lower(src_file) like ?",
        (like,) * 5)}
    for sid in meta:
        counts.setdefault(sid, 0)
    return counts, meta


def search(con, q, sources=None, day_from=None, day_to=None, limit=60, sort="relevance"):
    terms = split_terms(q)
    if not terms:
        return []
    limit = max(1, min(limit, 300))
    with IDX_LOCK:
        per_term = [term_hits(con, t, sources, day_from, day_to) for t in terms]
        cand = set(per_term[0][0])
        for counts, _ in per_term[1:]:
            cand &= set(counts)
        if not cand:
            return []

        scored = []
        for sid in cand:
            score = hits = 0
            for counts, meta_only in per_term:
                n = counts.get(sid, 0)
                hits += n
                score += min(n, 20) + (6 if sid in meta_only else 0)
            scored.append((score, hits, sid))
        scored.sort(key=lambda t: -t[0])
        top = scored[:limit]
        ids = [t[2] for t in top]

        infos = {r["sid"]: dict(r) for r in con.execute(
            f"select * from session where sid in ({','.join('?' * len(ids))})", ids)}
        snips = fetch_snippets(con, terms, ids)
        results = []
        for score, hits, sid in top:
            meta = infos.get(sid)
            if not meta:
                continue
            snips_ = snips.get(sid, [])
            if not snips_:
                html = meta_snippet(meta, terms)
                if html:
                    snips_ = [{"role": "meta", "ts": meta["ended"] or meta["started"],
                               "day": meta["day"], "html": html}]
            results.append({
                "sid": sid, "label": meta["label"], "source": meta["source"],
                "title": meta["title"], "project": meta["project"],
                "project_name": meta["project_name"], "started": meta["started"],
                "ended": meta["ended"], "day": meta["day"], "n_msg": meta["n_msg"],
                "models": [m for m in (meta["models"] or "").split(",") if m],
                "branch": meta["branch"], "src_file": meta["src_file"],
                "artifacts": [a for a in (meta["artifacts"] or "").split("\n") if a],
                "hits": hits, "score": score, "snippets": snips_,
                "open_level": opener_for(meta["source"])["level"],
            })
    if sort == "time":
        results.sort(key=lambda r: -(r["ended"] or r["started"] or 0))
    elif sort == "time_asc":
        results.sort(key=lambda r: (r["ended"] or r["started"] or 0))
    return results


def fetch_snippets(con, terms, sids):
    """给最靠前的若干结果取带高亮的上下文片段。

    每个会话单独取最近 6 条（row_number 分区），只回传命中位置附近的小窗口。
    不能用一个全局 limit：SQLite 按 rowid 截断，消息量最大的数据源会把后面的
    数据源全部挤光，表现就是它们明明命中几十次却一条片段都没有。
    """
    out = {s: [] for s in sids}
    if not sids:
        return out
    marks = ",".join("?" * len(sids))
    rn = "row_number() over (partition by msg_fts.sid order by msg_fts.ts desc) rn from msg_fts "
    cols = "select msg_fts.sid sid, msg_fts.role role, msg_fts.ts ts, msg_fts.day day, "
    for term in terms:
        picked = []
        if len(term) >= TRIGRAM_MIN:
            # snippet() 不能和窗口函数同层使用，也不允许在 join 之后再取（会退化成文档开头且无高亮），
            # 所以先按会话挑出 rowid，再对这批 rowid 单独取高亮片段。
            sel = (cols + "msg_fts.rowid rid, " + rn
                   + f"where msg_fts match ? and msg_fts.sid in ({marks})")
            try:
                picked = [(r["sid"], r["role"], r["ts"], r["day"], r["rid"])
                          for r in con.execute(f"select * from ({sel}) where rn <= 6",
                                               (fts_match(term), *sids))]
            except sqlite3.OperationalError:
                picked = []
            rids = [p[4] for p in picked]
            snips = {}
            if rids:
                sql = ("select msg_fts.rowid rid, snippet(msg_fts,0,'<b>','</b>',' … ',20) txt "
                       f"from msg_fts where msg_fts match ? and msg_fts.rowid in "
                       f"({','.join('?' * len(rids))})")
                try:
                    snips = {r["rid"]: r["txt"] for r in con.execute(sql, (fts_match(term), *rids))}
                except sqlite3.OperationalError:
                    snips = {}
            for sid, role, ts, day, rid in picked:
                if rid in snips:
                    out[sid].append({"role": role, "ts": ts, "day": day, "html": snips[rid]})
        else:
            low = term.lower()
            sel = (cols + "substr(msg_fts.body, max(1, instr(lower(msg_fts.body), ?) - 70), 280) txt, "
                   + rn + f"where instr(lower(msg_fts.body),?)>0 and msg_fts.sid in ({marks})")
            try:
                for r in con.execute(f"select sid,role,ts,day,txt from ({sel}) where rn <= 6",
                                     (low, low, *sids)):
                    out[r["sid"]].append({"role": r["role"], "ts": r["ts"], "day": r["day"],
                                          "html": make_snippet(r["txt"], [term])})
            except sqlite3.OperationalError:
                pass
    for sid, lst in out.items():
        seen, dedup = set(), []
        for s in sorted(lst, key=lambda x: -(x["ts"] or 0)):
            key = re.sub(r"<[^>]+>", "", s["html"])[:70]
            if key not in seen:
                seen.add(key)
                dedup.append(s)
        out[sid] = dedup[:4]
    return out


def meta_snippet(meta, terms):
    """正文里没有片段时，指出线索命中的其实是标题 / 项目 / 产物名。"""
    low = [t.lower() for t in terms]
    cands = [meta.get("title"), meta.get("project")]
    cands += [a for a in (meta.get("artifacts") or "").split("\n") if a]
    for val in cands:
        v = (val or "").split("\n")[0]
        if v and any(t in v.lower() for t in low):
            return make_snippet(v, terms)
    return ""


def make_snippet(text, terms):
    text = text or ""
    low = text.lower()
    for t in terms:
        i = low.find(t.lower())
        if i >= 0:
            a = max(0, i - 70)
            return ("…" if a else "") + esc(text[a:i]) + "<b>" + esc(text[i:i + len(t)]) \
                + "</b>" + esc(text[i + len(t):i + len(t) + 150])
    return esc(text[:180]) + ("…" if len(text) > 180 else "")


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def session_detail(con, sid):
    with IDX_LOCK:
        meta = con.execute("select * from session where sid=?", (sid,)).fetchone()
        if not meta:
            return None
        msgs = con.execute(
            "select seq,role,ts,day,body as text from msg_fts where sid=? order by seq",
            (sid,)).fetchall()
    out = dict(meta)
    out["open_level"] = opener_for(meta["source"])["level"]
    return {"session": out, "messages": [dict(m) for m in msgs]}


# --------------------------------------------------------------------------
# 跳回原对话
# --------------------------------------------------------------------------

# level：chat = 直接进那场对话；app = 只能把应用唤到前台（顺带复制会话 ID 供应用内搜索）；
#         file = 这个产品没有对外跳转入口，退而在资源管理器里定位原始记录。
# 只有实测或包内代码确认过的才标 chat，其余宁可低报，别让按钮骗人。
# 2026-09-12 实测：workbuddy://chat/<id> 真能打开那场对话（截图确认）；codex resume <id> 能定位到
# 对应 rollout 文件；qoder-cn://chat/<id> 只会把应用唤到前台、不切会话，所以停在 app 档。
# 同族的 qwenwork-cn / qoder 同理未验证，谁实测通了再把 level 改成 chat。
OPENERS = {
    ".workbuddy": {"level": "chat", "kind": "url", "tpl": "workbuddy://chat/{sid}"},
    ".qoder-cn": {"level": "app", "kind": "url", "tpl": "qoder-cn://chat/{sid}"},
    ".qwenworkcn": {"level": "app", "kind": "url", "tpl": "qwenwork-cn://chat/{sid}"},
    ".qoder": {"level": "app", "kind": "url", "tpl": "qoder://chat/{sid}"},
    "codex": {"level": "chat", "kind": "console", "argv": ("codex", "resume", "{sid}")},
    "zcode": {"level": "app", "kind": "url", "tpl": "zcode://"},
    "opencode": {"level": "app", "kind": "url", "tpl": "opencode://open-project?directory={dir}"},
    TRAE_SOURCE: {"level": "file", "kind": "file"},
}
DEFAULT_OPENER = {"level": "file", "kind": "file"}
OPEN_TEXT = {"chat": "已打开那场对话", "app": "已唤起应用", "file": "已定位原始记录"}


def opener_for(source):
    return OPENERS.get(source, DEFAULT_OPENER)


def reveal_file(path):
    """在资源管理器里选中这个文件；文件已删则退回打开它所在目录。"""
    f = Path(path or "")
    if f.exists():
        subprocess.Popen(["explorer", "/select," + str(f)])
        return True
    if f.parent.is_dir():
        subprocess.Popen(["explorer", str(f.parent)])
        return True
    return False


def open_session(con, sid):
    """跳回原对话。只接受索引里已存在的 sid，参数一律按列表交给 CreateProcess，不拼 shell。"""
    with IDX_LOCK:
        row = con.execute("select source, project, src_file from session where sid=?",
                          (sid,)).fetchone()
    if row is None:
        return {"error": "这场会话不在索引里，刷新一次再试"}
    op = opener_for(row["source"])
    raw = sid.split("|", 1)[1] if "|" in sid else sid
    err = ""
    try:
        if op["kind"] == "url":
            os.startfile(op["tpl"].format(sid=quote(raw, safe=""),
                                          dir=quote(row["project"] or "", safe="")))
        elif op["kind"] == "console":
            if not re.fullmatch(r"[\w.:\-]{1,80}", raw):
                return {"error": "会话 ID 格式异常，不启动外部命令"}
            argv = [a.replace("{sid}", raw) for a in op["argv"]]
            proj = (row["project"] or "").strip()
            cwd = Path(proj) if proj else None
            # .cmd 包装不能直接 CreateProcess，只能借 cmd 走一遍 PATH + PATHEXT；
            # 用 /k 而不是 /c，这样 resume 失败时窗口还留着能看到报错。
            subprocess.Popen(["cmd", "/d", "/k"] + argv,
                             cwd=str(cwd) if cwd and cwd.is_dir() else None,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        else:
            if not reveal_file(row["src_file"]):
                return {"error": "原始记录文件已经不在了"}
            return {"ok": True, "level": "file", "text": OPEN_TEXT["file"], "copy": ""}
        return {"ok": True, "level": op["level"], "text": OPEN_TEXT[op["level"]],
                "copy": raw if op["level"] == "app" else ""}
    except (OSError, ValueError) as e:
        err = f"{type(e).__name__}: {e}"
    if reveal_file(row["src_file"]):
        return {"ok": True, "level": "file", "text": f"跳不过去（{err}），已改为定位原始记录",
                "copy": ""}
    return {"error": err}


def example_terms(con):
    """首页示例线索：从索引里挑真实存在的项目名与产物文件名。

    不能把话题写死在代码里——那等于把使用者的私人上下文印在源码上。
    """
    with IDX_LOCK:
        projs = [r["p"].strip() for r in con.execute(
            "select project_name p from session where project_name<>'' "
            "and project_name<>'(未知项目)' group by p "
            "order by count(*) desc, max(ended) desc limit 6").fetchall()]
        arts = [r["a"] for r in con.execute(
            "select artifacts a from session where artifacts<>'' "
            "order by ended desc limit 60").fetchall()]
    out, seen = [], set()

    def push(t):
        t = (t or "").strip().split("\n")[0]
        key = t.lower()
        if 2 <= len(t) <= 24 and key not in seen:
            seen.add(key)
            out.append(t)

    for p in projs:
        if len(out) < 3:
            push(p)
    for blob in arts:
        for line in blob.split("\n"):
            base = line.strip().replace("\\", "/").split("/")[-1]
            if base.split(".")[-1].lower() in ("docx", "pptx", "xlsx", "md", "py", "pdf"):
                push(base)
        if len(out) >= 6:
            break
    while len(out) < 6:
        for fb in ("报错", "总结", "README"):
            if fb not in seen:
                push(fb)
    return out[:6]


def overview(con):
    with IDX_LOCK:
        srcs = [dict(r) for r in con.execute(
            "select source,label,count(*) n,max(ended) b from session "
            "group by source order by n desc")]
        tot = con.execute("select count(*) c from session").fetchone()["c"]
        days = con.execute("select count(distinct day) c from session where day<>''").fetchone()["c"]
        msgs = con.execute("select count(*) c from msg_fts").fetchone()["c"]
        last = con.execute("select v from meta where k='last_refresh'").fetchone()
    return {"sources": srcs, "total": tot, "days": days, "messages": msgs,
            "uncovered": UNCOVERED, "examples": example_terms(con),
            "last_refresh": float(last["v"]) if last else None}


def browse(con):
    with IDX_LOCK:
        rows = con.execute(
            "select sid,label,source,title,project_name,n_msg,started,day from session "
            "order by coalesce(ended,started) desc limit 400").fetchall()
    groups = []
    for r in rows:
        d = r["day"] or "未知日期"
        if not groups or groups[-1]["day"] != d:
            groups.append({"day": d, "items": []})
        groups[-1]["items"].append(dict(r))
    return groups


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------

STATE = {"progress": "正在扫描本机 Agent 记录…", "ready": False, "next_scan": 0.0}
REFRESH_MIN_INTERVAL = 45     # 秒；全量扫描一次约 3s，避免每次检索都重扫


def serve(port, open_browser, initial_query):
    url = f"http://127.0.0.1:{port}/"
    if initial_query:
        url += "?q=" + quote(initial_query)
    if already_running(port):
        print(f"已有 AgentFind 服务在运行 → {url}")
        if open_browser:
            webbrowser.open(url)
        return

    sources = discover_sources()
    con = connect()

    def worker():
        try:
            stats = refresh(con, sources, log=print)
            STATE["progress"] = (f"索引已就绪：新增 {stats['new']}、更新 {stats['updated']} 个记录文件")
            STATE["next_scan"] = time.time() + REFRESH_MIN_INTERVAL
        except Exception as e:
            STATE["progress"] = f"索引失败：{type(e).__name__}: {e}"
            print(STATE["progress"])
        finally:
            STATE["ready"] = True

    threading.Thread(target=worker, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, raw, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif u.path == "/api/meta":
                    ov = overview(con)
                    ov.update(progress=STATE["progress"], ready=STATE["ready"])
                    self._json(ov)
                elif u.path == "/api/search":
                    forced = q.get("fresh") == "1"
                    scanned = forced or time.time() >= STATE["next_scan"]
                    if scanned:
                        refresh(con, sources, force=False)
                        STATE["next_scan"] = time.time() + REFRESH_MIN_INTERVAL
                    srcs = [s for s in q.get("sources", "").split(",") if s] or None
                    self._json({"results": search(con, q.get("q", ""), srcs,
                                                  q.get("from") or None, q.get("to") or None,
                                                  min(int(q.get("limit", 60)), 300),
                                                  q.get("sort", "relevance")),
                                "progress": STATE["progress"], "scanned": scanned})
                elif u.path == "/api/session":
                    self._json(session_detail(con, q.get("id", "")) or {"error": "会话不在索引中"})
                elif u.path == "/api/browse":
                    self._json({"days": browse(con)})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            """只有 /api/open：会启动本机应用，所以要求自定义头 + 同源，挡掉别的网页跨站调用。"""
            u = urlparse(self.path)
            if u.path != "/api/open":
                return self._json({"error": "not found"}, 404)
            if self.headers.get("X-Requested-With") != "agentfind":
                return self._json({"error": "forbidden"}, 403)
            origin = (urlparse(self.headers.get("Origin") or "").netloc).lower()
            if origin and origin not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return self._json({"error": "forbidden"}, 403)
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                sid = str(json.loads(self.rfile.read(n).decode("utf-8") or "{}").get("sid", ""))[:200]
                self._json(open_session(con, sid))
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"端口 {port} 起不来：{e}\n稍等几秒再试，或换个端口：agentfind --port 9000")
        con.close()
        return
    print(f"AgentFind → {url}")
    print("只读索引，不改任何原始记录。Ctrl+C 停止服务。")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------
# 前端
# --------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentFind · 本机 Agent 对话检索</title>
<style>
:root{--ink:#0F172A;--mid:#334155;--mute:#64748B;--line:#E2E8F0;--bg:#F8FAFC;--red:#B91C1C}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.65 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:15px 0}
.inner{max-width:1180px;margin:0 auto;padding:0 26px}
h1{margin:0;font-size:16px;letter-spacing:.4px}
h1 small{font-weight:400;color:var(--mute)}
.sub{color:var(--mute);font-size:12px;margin-top:3px}
.wrap{max-width:1180px;margin:0 auto;padding:0 26px 70px}
.bar{background:#fff;border:1px solid var(--line);border-radius:8px;padding:13px;margin:16px 0;
 position:sticky;top:0;z-index:20;box-shadow:0 2px 10px rgba(15,23,42,.05)}
.q{display:flex;gap:8px;flex-wrap:wrap}
input[type=text]{flex:1;min-width:240px;border:1px solid var(--line);border-radius:6px;
 padding:9px 12px;font-size:15px;color:var(--ink);background:#fff}
input[type=text]:focus{outline:2px solid var(--ink);outline-offset:-1px}
button{border:1px solid var(--ink);background:var(--ink);color:#fff;border-radius:6px;
 padding:8px 15px;font-size:13px;cursor:pointer;white-space:nowrap}
button.ghost{background:#fff;color:var(--ink)}
button.go{background:var(--red);border-color:var(--red)}
button.go:disabled{opacity:.45;cursor:wait}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px;font-size:12px}
.chip{border:1px solid var(--line);border-radius:999px;padding:2px 10px;cursor:pointer;
 user-select:none;background:#fff;color:var(--mid);display:inline-block}
.chip.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.chip .n{opacity:.55;margin-left:4px;font-size:11px}
input[type=date]{border:1px solid var(--line);border-radius:5px;padding:2px 5px;font-size:12px}
select{border:1px solid var(--line);border-radius:5px;padding:4px 6px;font-size:12px;background:#fff}
.status{color:var(--mute);font-size:12px;margin-left:auto}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:13px 15px}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.tag{background:var(--ink);color:#fff;font-size:11px;padding:2px 8px;border-radius:4px}
.when{font-size:12px;color:var(--red);font-weight:600;font-variant-numeric:tabular-nums}
.hits{font-size:11px;color:var(--mute)}
.ttl{font-size:15px;font-weight:600;margin:5px 0 1px;word-break:break-word}
.proj{font-size:12px;color:var(--mid);word-break:break-all}
.proj code{background:var(--bg);padding:1px 5px;border-radius:3px;font-size:11px;font-weight:600}
.snips{margin:9px 0 0;padding:0;list-style:none}
.snips li{border-left:2px solid var(--line);padding:3px 0 3px 10px;margin:5px 0;font-size:13px;
 color:var(--mid);word-break:break-word}
.snips b{color:var(--red);font-weight:700;background:#FEE2E2;padding:0 2px}
.who{font-size:11px;color:var(--mute);margin-right:6px;border:1px solid var(--line);
 border-radius:3px;padding:0 4px}
.arts{margin-top:9px;font-size:12px;color:var(--mute)}
.arts span{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:4px;
 padding:1px 7px;margin:2px 4px 2px 0;font-family:Consolas,monospace;font-size:11px;cursor:pointer;
 color:var(--mid);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 vertical-align:bottom}
.arts span:hover{border-color:var(--red);color:var(--red)}
.row{display:flex;gap:12px;margin-top:11px;align-items:center;flex-wrap:wrap}
.lnk{font-size:12px;color:var(--mute);cursor:pointer;text-decoration:underline}
.lnk:hover{color:var(--red)}
#mask{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;z-index:50}
#drawer{position:fixed;top:0;right:-72%;width:72%;height:100%;background:#fff;z-index:51;
 transition:right .18s;overflow:auto;border-left:1px solid var(--line)}
#drawer.open{right:0}
.dh{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:13px 20px;z-index:2}
.db{padding:6px 20px 70px}
.m{border-bottom:1px dotted var(--line);padding:8px 0}
.mh{font-size:11px;color:var(--mute);margin-bottom:2px}
.mh .r{font-weight:700;color:var(--mid)}
.mh .r.user{color:var(--red)}
.mh .r.tool{color:#7C3AED}
.mh .r.summary{color:var(--mute)}
.mt{white-space:pre-wrap;word-break:break-word;font-size:13.5px}
.meta{font-size:12px;color:var(--mute);line-height:1.85;margin-top:4px}
.meta b{color:var(--mid)}
.empty{padding:46px 0;text-align:center;color:var(--mute)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}
.day{margin:18px 0 6px;font-size:13px;font-weight:700;color:var(--mid);
 border-bottom:1px solid var(--line);padding-bottom:4px}
.guide{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px 22px;margin:6px 0}
.guide h3{margin:0 0 8px;font-size:15px}
.guide ol{margin:0;padding-left:20px;color:var(--mid)}
.guide li{margin:4px 0}
.ex{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.ex b{border:1px solid var(--line);background:var(--bg);border-radius:14px;padding:5px 13px;
 font-weight:400;font-size:13px;cursor:pointer;color:var(--mid)}
.ex b:hover{border-color:var(--ink);color:var(--ink);background:#fff}
.note{font-size:12.5px;color:var(--mute);margin-top:14px;border-top:1px dashed var(--line);padding-top:11px}
.note em{font-style:normal;color:var(--red);font-weight:700}
.toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:var(--ink);
 color:#fff;padding:8px 16px;border-radius:6px;font-size:12px;opacity:0;transition:.2s;z-index:60}
.toast.on{opacity:1}
</style></head><body>
<header><div class="inner">
<h1>AgentFind <small>· 帮你找回和 AI 聊过的那段话</small></h1>
<div class="sub" id="sub">加载中…</div>
</div></header>
<div class="wrap">
<div class="bar">
 <div class="q">
  <input id="q" type="text" placeholder="想起什么就输什么：一个词、一个文件名、一个项目名都行">
  <select id="sort"><option value="relevance">按相关度</option><option value="time">最新在前</option>
   <option value="time_asc">最早在前</option></select>
  <button onclick="go()">查找</button>
  <button class="ghost" onclick="go(1)">刷新索引</button>
  <button class="ghost" onclick="browse()">时间线</button>
 </div>
 <div class="filters">
  <span style="color:#64748B">产品</span><span id="chips"></span>
  <span style="color:#64748B;margin-left:6px">日期</span>
  <input type="date" id="from"><span>→</span><input type="date" id="to">
  <span class="status" id="status"></span>
 </div>
</div>
<div id="out"></div>
</div>
<div id="mask" onclick="closeD()"></div><div id="drawer"></div>
<div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
 .replace(/"/g,'&quot;');
let sel=new Set(), CHIPS_DONE=false, MSGS=[], META=null;
function EX(){return (META&&META.examples&&META.examples.length)?META.examples:['报错','总结','README']}
function unc(){const u=((META&&META.uncovered)||[]).join(' / ');
 return u?`注意：暂时读不到 <em>${esc(u)}</em> 里的记录，所以「没查到」不等于「没聊过」。`:''}
function home(){
 $('out').innerHTML=`<div class="guide"><h3>四步回到那段对话</h3>
 <ol><li>在上面输入框里写<b>任何你还记得的线索</b>：一个词、一个产物文件名、一个项目名都行。</li>
  <li>按回车，或点「查找」。</li>
  <li>每条结果的第一行直接写着 <b>哪个产品 · 什么时候 · 哪个项目</b>；点「阅读这场对话」看完整原文。</li>
  <li>点红色的<b>跳回</b>按钮就能回到那个应用里接着聊。按钮上的字会告诉你这次能跳到哪一步：
   直接进那场对话 / 只把应用唤到前台（会话 ID 顺手复制好）/ 在资源管理器里定位原始记录。</li></ol>
 <div class="ex">${EX().map(t=>`<b data-q="${esc(t)}" onclick="ex(this.dataset.q)">${esc(t)}</b>`).join('')}</div>
 <div class="note">上面几个词是你本机真聊过的话题，点一下就知道是在哪个产品里聊的。<br>${unc()}</div></div>`}
function ex(q){$('q').value=q;go()}
function clearF(){sel.clear();[...document.querySelectorAll('.chip.on')].forEach(c=>c.classList.remove('on'));
 $('from').value='';$('to').value='';go()}
function toast(t){const x=$('toast');x.textContent=t;x.classList.add('on');
 setTimeout(()=>x.classList.remove('on'),t.length>36?4200:1700)}
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()}
// 三档跳转能力：能进对话的就写「对话」，进不去的如实说明会做什么
const LVL={chat:['跳回那场对话','在对应应用里直接打开这场会话'],
 app:['唤起应用','这个应用不认会话级链接，只能把它带到前台，同时把会话 ID 复制好，进应用后粘贴搜索'],
 file:['定位原始记录','这个应用没有对外跳转入口，改为在资源管理器里选中它的记录文件']};
async function jpost(u,body){
 const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json',
  'X-Requested-With':'agentfind'},body:JSON.stringify(body)});
 if(!r.ok)throw new Error(r.status);return r.json()}
async function jump(b){
 const t=b.dataset.sid, lv=b.dataset.lv||'file';
 b.disabled=true;
 try{
  const r=await jpost('/api/open',{sid:t});
  if(r.error){toast('跳不过去：'+r.error);return}
  if(r.copy){try{await navigator.clipboard.writeText(r.copy)}catch(_){prompt('手动复制会话 ID：',r.copy)}}
  toast(r.text+(r.copy?'，会话 ID 已复制':'')+(lv==='chat'?'':'（这个应用只能跳到这里）'))
 }catch(e){toast('跳转请求失败了')}
 finally{setTimeout(()=>{b.disabled=false},1200)}
}
function fmt(ts){if(!ts)return '时间未知';const d=new Date(ts*1000),p=n=>String(n).padStart(2,'0');
 return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`}
function tgl(el){const s=el.dataset.s;
 if(sel.has(s)){sel.delete(s);el.classList.remove('on')}else{sel.add(s);el.classList.add('on')}
 if($('q').value.trim())go()}
async function meta(){
 const m=await j('/api/meta'); META=m;
 $('sub').innerHTML=`本机已收录 <b>${m.total}</b> 场对话、<b>${m.messages.toLocaleString()}</b> 条消息`
  +`（${m.sources.length} 个产品 · 跨 ${m.days} 天）· 最近扫描 ${m.last_refresh?fmt(m.last_refresh):'—'}`;
 if(!CHIPS_DONE&&m.sources.length){
  $('chips').innerHTML=m.sources.map(s=>`<span class="chip" data-s="${esc(s.source)}" `
   +`onclick="tgl(this)">${esc(s.label)}<span class="n">${s.n}</span></span>`).join(' ');
  CHIPS_DONE=true}
 if(!$('q').value&&m.progress)$('status').textContent=m.progress;
}
async function go(fresh){
 const q=$('q').value.trim(); if(!q){toast('请输入线索');return}
 history.replaceState(null,'','?q='+encodeURIComponent(q));
 $('status').textContent=fresh?'重建索引中，稍等…':'检索中…';
 const p=new URLSearchParams({q,sort:$('sort').value,fresh:fresh?'1':'0'});
 if(sel.size)p.set('sources',[...sel].join(','));
 if($('from').value)p.set('from',$('from').value);
 if($('to').value)p.set('to',$('to').value);
 let r; try{r=await j('/api/search?'+p)}catch(e){$('status').textContent='检索失败：服务可能已被关闭，重新双击桌面图标即可';return}
 if(r.error){$('status').textContent='出错了：'+r.error;return}
 $('status').textContent=`找到 ${r.results.length} 场对话${r.scanned?' · 已扫描过新记录':''}`;
 if(!r.results.length){$('out').innerHTML=`<div class="guide"><h3>这场没查到，多半是线索太长</h3>
  <ol><li>词缩短：「季度报告整理」→ 只写「报告」。</li>
   <li>换一类线索：产物文件名（如 答辩稿.docx）、项目名片段、或你记得的一句原话。</li>
   <li>去掉上面的产品 / 日期筛选再查一次（筛选还留着时会一直限定范围）。</li></ol>
  <div class="row" style="margin-top:13px"><button onclick="clearF()">去掉筛选，重查「${esc(q)}」</button>
   <button class="ghost" onclick="home()">回首页</button></div>
  <div class="note">${unc()}</div></div>`;return}
 $('out').innerHTML=r.results.map(card).join('');
}
function card(x){
 const arts=(x.artifacts||[]).slice(0,14);
 const when=fmt(x.started)+(x.ended&&x.ended!==x.started?' → '+fmt(x.ended):'');
 return `<div class="card"><div class="top">
  <span class="tag">${esc(x.label)}</span><span class="when">${when}</span>
  <span class="hits">${x.hits?`提到 ${x.hits} 次 · `:'线索在标题/项目/产物 · '}这场对话 ${x.n_msg} 条</span></div>
 <div class="ttl">${esc(x.title)}</div>
 <div class="proj">项目 <code>${esc(x.project_name)}</code> ${esc(x.project||'')}
  ${x.branch?'· '+esc(x.branch):''}${x.models.length?'· '+esc(x.models.join('/')):''}</div>
 ${x.snippets.length?`<ul class="snips">${x.snippets.map(s=>`<li><span class="who">${s.role==='user'?'我问':s.role==='tool'?'工具':s.role==='summary'?'摘要':s.role==='meta'?'线索':'它答'}</span>${s.html}</li>`).join('')}</ul>`:''}
 ${arts.length?`<div class="arts">产物 / 提及文件：${arts.map(a=>`<span title="${esc(a)}" data-p="${esc(a)}" `
   +`onclick="cp(this.dataset.p)">${esc(a.split(/[\\/]/).pop())}</span>`).join('')}</div>`:''}
 <div class="row"><button class="go" data-sid="${esc(x.sid)}" data-lv="${x.open_level||'file'}"
   title="${LVL[x.open_level||'file'][1]}" onclick="jump(this)">${LVL[x.open_level||'file'][0]}</button>
  <button data-sid="${esc(x.sid)}" onclick="openS(this.dataset.sid)">阅读这场对话</button>
  <span class="lnk" data-p="${esc(x.src_file)}" onclick="cp(this.dataset.p)">复制原始记录路径</span>
  ${x.project_name?`<span class="lnk" data-p="${esc(x.project_name)}" onclick="q2(this.dataset.p)">只看这个项目的对话</span>`:''}
 </div></div>`}
function q2(p){$('q').value=p;go()}
async function openS(id){
 const d=await j('/api/session?id='+encodeURIComponent(id));
 if(d.error){toast(d.error);return}
 const s=d.session;
 $('drawer').innerHTML=`<div class="dh"><button class="ghost" style="float:right" `
  +`onclick="closeD()">关闭</button><div class="ttl">${esc(s.title)}</div>
  <div class="meta"><b>${esc(s.label)}</b> · ${fmt(s.started)} → ${fmt(s.ended)} · ${s.n_msg} 条<br>
   项目 ${esc(s.project||'—')}${s.branch?' · '+esc(s.branch):''}${s.models?' · '+esc(s.models):''}<br>
   原始记录 ${esc(s.src_file)}</div>
  <div class="row"><button class="go" data-sid="${esc(s.sid)}" data-lv="${s.open_level||'file'}"
   title="${LVL[s.open_level||'file'][1]}" onclick="jump(this)">${LVL[s.open_level||'file'][0]}</button>
   <input type="text" id="filt" placeholder="在这场对话内过滤…" `
  +`style="font-size:13px" oninput="renderM()"></div></div><div class="db" id="dbody"></div>`;
 MSGS=d.messages;renderM();
 $('mask').style.display='block';$('drawer').classList.add('open');
}
function renderM(){
 const f=($('filt')?$('filt').value:'').toLowerCase();
 const list=MSGS.filter(m=>!f||m.text.toLowerCase().includes(f));
 $('dbody').innerHTML=list.map(m=>`<div class="m"><div class="mh"><span class="r ${esc(m.role)}">${
   m.role==='user'?'用户':m.role==='assistant'?'Agent':m.role==='summary'?'摘要':'工具调用'}</span> ${fmt(m.ts)}</div>
  <div class="mt">${esc(m.text)}</div></div>`).join('')||'<div class="empty">这场对话里没有匹配内容。</div>';
}
function closeD(){$('mask').style.display='none';$('drawer').classList.remove('open')}
async function browse(){
 $('status').textContent='载入时间线…';
 const r=await j('/api/browse');
 $('status').textContent='时间线（最近 400 场）';
 $('out').innerHTML=r.days.map(g=>`<div class="day">${esc(g.day)} · ${g.items.length} 场</div>
  <div class="grid">${g.items.map(x=>`<div class="card" style="margin:0">
   <div class="top"><span class="tag">${esc(x.label)}</span></div>
   <div class="ttl" style="font-size:13.5px">${esc(x.title)}</div>
   <div class="proj"><code>${esc(x.project_name)}</code> · ${x.n_msg} 条 · ${fmt(x.started)}</div>
   <div class="row"><button class="ghost" data-sid="${esc(x.sid)}" `
    +`onclick="openS(this.dataset.sid)">阅读</button></div>
  </div>`).join('')}</div>`).join('');
}
async function cp(t){try{await navigator.clipboard.writeText(t);toast('已复制到剪贴板')}
 catch(_){prompt('手动复制：',t)}}
$('q').addEventListener('keydown',ev=>{if(ev.key==='Enter')go()});
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')closeD()});
setInterval(()=>{meta().catch(()=>{})},20000);
(async()=>{await meta();
 const u=new URLSearchParams(location.search);
 if(u.get('q')){$('q').value=u.get('q');go()}
 else if(u.get('sid')){home();openS(u.get('sid'))}
 else{home()}
})();
</script></body></html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_status():
    con = connect()
    ov = overview(con)
    counts = {s["source"]: s for s in ov["sources"]}
    print(f"索引文件: {DB_PATH}")
    print(f"{'产品':<22}{'会话数':>8}   最近活跃")
    print("-" * 58)
    for sid, label, _kind, _roots in discover_sources():
        c = counts.get(sid)
        print(f"{label:<20}{(c['n'] if c else 0):>8}   {fmt_time(c['b']) if c else '—'}")
    print("-" * 58)
    print(f"合计 {ov['total']} 场对话、{ov['messages']} 条消息，跨 {ov['days']} 天")
    if ov["last_refresh"]:
        print(f"索引更新于 {fmt_time(ov['last_refresh'])}")
    con.close()


def cmd_cli(q):
    con = connect()
    refresh(con, discover_sources(), log=lambda *_: None)
    res = search(con, q, limit=25)
    if not res:
        print("没有命中。试试更短的关键词，或只搜产物文件名。")
        return
    for r in res:
        print(f"\n[{r['label']}] {fmt_time(r['started'])}  {r['title']}")
        print(f"    项目   {r['project']}")
        print(f"    记录   {r['src_file']}")
        print(f"    命中   {r['hits']} 处" + (f"，产物 {len(r['artifacts'])} 个" if r["artifacts"] else ""))
        for s in r["snippets"][:2]:
            print(f"      · {re.sub('<[^>]+>', '', s['html'])[:180]}")
    con.close()


def cmd_stop(port):
    """停掉本机所有 AgentFind 服务进程（索引文件保留）。

    Windows 上 allow_reuse_address 允许多个进程绑同一端口，改完代码后旧实例可能仍在服务，
    所以这里按命令行匹配全部清掉，而不是只杀端口上那一个。
    """
    was = already_running(port)
    ps = ("Get-CimInstance Win32_Process | "
          "Where-Object { $_.Name -match '^python' -and $_.CommandLine -like '*agentfind.py*' "
          "-and $_.ProcessId -ne " + str(os.getpid()) + " } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True,
                             encoding="gbk", errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    pids = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    if pids:
        print(f"已停止 {len(pids)} 个 AgentFind 服务进程（{', '.join(pids)}）。"
              f"索引保留在 {DB_PATH}，双击桌面图标即可再起。")
    elif was:
        print(f"端口 {port} 上有服务但没找到可结束的进程，请用任务管理器确认。")
    else:
        print(f"没有正在运行的 AgentFind 服务，无需关闭。")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentfind", description="本机多 Agent 对话全局检索")
    ap.add_argument("query", nargs="*", help="搜索线索，自动填入网页搜索框")
    ap.add_argument("--status", action="store_true", help="打印索引状态后退出")
    ap.add_argument("--rebuild", action="store_true", help="全量重建索引后退出")
    ap.add_argument("--cli", metavar="Q", help="终端里直接搜索，不起服务")
    ap.add_argument("--stop", action="store_true", help="关闭本机 AgentFind 服务后退出")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTFIND_PORT", 8765)))
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args(argv)
    attach_log()

    if a.status:
        cmd_status()
    elif a.cli:
        cmd_cli(a.cli)
    elif a.stop:
        cmd_stop(a.port)
    elif a.rebuild:
        con = connect()
        print("全量重建索引…")
        rebuild(con, discover_sources(), log=print)
        con.close()
        cmd_status()
    else:
        serve(a.port, not a.no_open, " ".join(a.query))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
