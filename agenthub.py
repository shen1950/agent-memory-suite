#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentHub — 多 Agent 共享记忆 + 子 Agent 派工（单文件零依赖）

每个能跑命令行的 Agent（Qoder/Codex/WorkBuddy/QwenWork/Claude Code/ZCode...）都可以：
  agenthub mem write "结论..."  --agent codex --tags rul,数据     把结论沉淀给所有 Agent
  agenthub mem search "关键词"  查其他 Agent 的记忆与工作记录
  agenthub mem context "查询"   生成可直接粘进提示词的记忆上下文块
  agenthub call codex "任务"    把任务派给另一个 Agent 无头执行，自动带上相关记忆
  agenthub log                  查看派工记录

mem search / context / list 在查之前会自动增量同步各产品原生记忆（45s 节流），不用手动 sync。
索引放 ~/.agenthub/，与 AgentFind 互不干扰。
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler

HOME = Path.home()
APP_DIR = HOME / ".agenthub"
DB_PATH = APP_DIR / "memory.sqlite"
DISPATCH_DIR = APP_DIR / "dispatches"
SYNC_STAMP = APP_DIR / ".last_sync"
SYNC_MIN_INTERVAL = 45     # 秒。读路径节流：查的时候才同步，保证「查到时不滞后」
AGENTFIND = "http://127.0.0.1:8765"   # 本机 Agent 对话检索服务，用来把结论接回原始对话
DOWN_STAMP = APP_DIR / ".agentfind_down"
DOWN_COOLDOWN = 60         # 秒。服务探到没起就冷却，别每次查询都干等连接超时

TRIGRAM_MIN = 3          # fts5 trigram 分词器要求的最短子串长度
CHUNK_LIMIT = 5000       # 原生记忆入库分段上限（字符）
NOTE_CAP = 20000         # 单条记忆硬上限
DEFAULT_TIMEOUT = 900    # 子 Agent 默认超时（秒）

# 内置清单只当"第一次运行时该同步哪些"的种子；生效的是 ~/.agenthub/sources.json，
# 接入新 Agent 改那个文件就行，不用碰代码（agenthub mem source add/scan --fix 会写它）。
BUILTIN_SOURCES = [
    ("qoder-cn",   ".qoder-cn/memory/**/*.md"),
    ("qoder-cn",   ".qoder-cn/memories/**/*.md"),
    ("qoder",      ".qoder/memory/**/*.md"),
    ("qoder",      ".qoder/projects/*/memory/*.md"),
    ("codex",      ".codex/memories/*.md"),
    ("codex",      ".codex/AGENTS.md"),
    ("workbuddy",  ".workbuddy/memory/*.md"),
    ("qwenworkcn", ".qwenworkcn/awareness/main/MEMORY.md"),
    ("qwenworkcn", ".qwenworkcn/awareness/main/memory/*.md"),
    ("claude",     ".claude/CLAUDE.md"),
]
SOURCES_FILE = APP_DIR / "sources.json"
_src_cache = (0, [])


def _seed_rows():
    return [{"product": p, "glob": g, "enabled": True} for p, g in BUILTIN_SOURCES]


def source_rows():
    """sources.json 的原始条目；文件不存在时先按内置清单落一份，让配置成为唯一事实源。"""
    if not SOURCES_FILE.exists():
        save_source_rows(_seed_rows())
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _seed_rows()
    rows = data.get("sources") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else _seed_rows()


def save_source_rows(rows):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(
        json.dumps({"sources": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_sources():
    """[(产品名, 相对 home 的 glob)]，跳过 disabled；按 mtime 缓存，改完文件下次查询就生效。"""
    global _src_cache
    try:
        mt = SOURCES_FILE.stat().st_mtime_ns
    except OSError:
        mt = 0
    if mt and _src_cache[0] == mt:
        return _src_cache[1]
    out = []
    for r in source_rows():
        if not isinstance(r, dict) or r.get("enabled", True) is False:
            continue
        p = str(r.get("product") or "").strip()
        g = str(r.get("glob") or "").strip()
        if p and g:
            out.append((p, g))
    _src_cache = (mt, out)
    return out


def count_glob(pattern):
    """glob 现在能命中几个文件——加源的时候用来当场验证，别写个查不到的规则进去。"""
    return len(_glob_files(pattern))


# 自动发现新 Agent 的记忆目录：只认这几种形状，避免把 skills/文档/缓存当记忆。
# 前三个是"产品把记忆放在自己目录下一层"，中间三个是"按项目分文件夹"的嵌套形状。
MEM_SHAPES = (
    "memory/**/*.md", "memories/**/*.md", "awareness/**/*.md",
    "projects/*/memory/*.md", "projects/*/memories/*.md", "automations/*/memory.md",
)
MEM_FILES = ("MEMORY.md", "AGENTS.md", "CLAUDE.md", "USER.md", "IDENTITY.md", "SOUL.md")
MD_SUFFIXES = (".md", ".markdown", ".txt")
SCAN_BUDGET = 12.0         # 秒。宁可下次再报，也不让一次查询卡住终端


def _glob_files(pattern):
    root = HOME / pattern.split("/")[0]
    if not root.exists():
        return set()
    return {str(f) for f in HOME.glob(pattern) if f.is_file()}


def scan_sources():
    """扫 home 的点开头的目录 + %APPDATA%，返回还没进 sources.json 的候选记忆源。
    判重看实际命中的文件而不是 glob 字符串——否则 .codex/memories/**/*.md 这种
    "已有规则的超集"会被当成新源，加进去只是重复劳动。"""
    have = {g for _, g in load_sources()}
    covered = set()
    for _, g in load_sources():
        covered |= _glob_files(g)
    roots = [HOME]
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]))
    out, deadline = [], time.time() + SCAN_BUDGET
    for root in roots:
        if not root.is_dir():
            continue
        try:
            top = sorted(root.iterdir())
        except OSError:
            continue
        for d in top:
            if time.time() > deadline:
                break
            if not d.is_dir():
                continue
            if root == HOME and not d.name.startswith("."):
                continue          # home 下只认 .产品 目录，别把用户自己的文件夹当 Agent
            name = d.name.lstrip(".").strip().lower().replace(" ", "-")
            if not name or name in ("agents", "agenthub", "agentfind"):
                continue          # agents 是共享 skill 目录，后两个是工具自己的家
            try:
                rel = d.relative_to(HOME).as_posix()
            except ValueError:
                continue
            cands = [f"{rel}/{f}" for f in MEM_FILES if (d / f).is_file()]
            cands += [f"{rel}/{s}" for s in MEM_SHAPES if _glob_files(f"{rel}/{s}")]
            for g in cands:
                if g in have:
                    continue
                files = _glob_files(g)
                new = files - covered
                if not new:
                    continue
                covered |= files
                out.append({"product": name, "glob": g, "files": len(new)})
    return sorted(out, key=lambda r: (-r["files"], r["product"]))

SCHEMA = """
create table if not exists notes(
  id integer primary key autoincrement,
  ts text not null, day text not null,
  agent text not null default 'user',
  kind text not null default 'note',
  topic text default '', tags text default '',
  content text not null,
  src text default '', sig text default '');
create index if not exists notes_day on notes(day);
create index if not exists notes_src on notes(src);
create virtual table if not exists notes_fts using fts5(
  body, nid unindexed, tokenize='trigram');
create table if not exists files(path text primary key, sig text);
create table if not exists dispatches(
  id integer primary key autoincrement,
  ts text not null, product text not null, cwd text, prompt text,
  mem_query text, exit_code int, dur real, transcript text, output text);
"""


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def day_of(ts=None):
    return (dt.datetime.now() if ts is None
            else dt.datetime.fromisoformat(ts)).strftime("%Y-%m-%d")


def connect():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("pragma journal_mode=WAL")
    con.execute("pragma synchronous=NORMAL")
    return con


# ---------------------------------------------------------------- 检索层 ---

def split_terms(q):
    return [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "") if (a or b)]


def fts_match(term):
    return '"' + term.replace('"', '""') + '"'


def term_nids(con, term, agent=None, kind=None, since_day=None):
    """单个关键词 → {nid: 命中数}。≥3 字走 trigram 索引，1–2 字退回子串。"""
    cond, params = [], []
    if agent:
        cond.append("n.agent = ?"); params.append(agent)
    if kind:
        cond.append("n.kind = ?"); params.append(kind)
    if since_day:
        cond.append("n.day >= ?"); params.append(since_day)
    where = (" and " + " and ".join(cond)) if cond else ""
    if len(term) >= TRIGRAM_MIN:
        sql = (f"select f.nid nid, count(*) c from notes_fts f "
               f"join notes n on n.id = f.nid where f.body match ?{where} group by f.nid")
        try:
            rows = con.execute(sql, (fts_match(term), *params)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    else:
        sql = (f"select f.nid nid, count(*) c from notes_fts f "
               f"join notes n on n.id = f.nid "
               f"where instr(lower(f.body),?)>0{where} group by f.nid")
        rows = con.execute(sql, (term.lower(), *params)).fetchall()
    return {r["nid"]: r["c"] for r in rows}


def search_notes(con, q, agent=None, kind=None, since=None, limit=15, sort="relevance"):
    terms = split_terms(q)
    if not terms:
        return []
    since_day = since_to_day(since)
    per_term = [term_nids(con, t, agent, kind, since_day) for t in terms]
    cand = set(per_term[0])
    for counts in per_term[1:]:
        cand &= set(counts)
    if not cand:
        return []
    scored = sorted(cand, key=lambda nid: -sum(c[nid] for c in per_term)) \
        if sort == "relevance" else sorted(cand, reverse=True)
    scored = scored[:max(1, min(limit, 100))]
    marks = ",".join("?" * len(scored))
    rows = con.execute(f"select * from notes where id in ({marks})", scored).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[nid] for nid in scored if nid in by_id]


def since_to_day(since):
    if not since:
        return None
    m = re.fullmatch(r"(\d+)\s*d", since.strip(), re.I)
    if m:
        d = dt.date.today() - dt.timedelta(days=int(m.group(1)))
        return d.isoformat()
    return since.strip()


def fmt_note(r, with_body=True):
    head = (f"#{r['id']} [{r['agent']}/{r['kind']}] {r['day']}"
            + (f" · {r['topic']}" if r["topic"] else "")
            + (f" · #{','.join(t for t in r['tags'].split(',') if t)}" if r["tags"] else ""))
    body = r["content"] if with_body else ""
    return head + (("\n" + body) if body else "")


def build_context(con, query, n=5, agent=None):
    """给子 Agent / 其他 Agent 用的记忆上下文块。"""
    rows = search_notes(con, query, agent=agent, limit=n)
    if not rows:
        return ""
    lines = ["以下摘自本机多 Agent 共享记忆库（AgentHub），来自你以外的 Agent 或其历史记录，"
             "供背景参考；与当前任务冲突时以任务为准。"]
    for r in rows:
        body = r["content"]
        if len(body) > 1200:
            body = body[:1200] + " …（截断）"
        src = f" · 源:{Path(r['src']).name}" if r["src"] else ""
        lines.append(f"### [{r['agent']}/{r['kind']}] {r['day']}{src}\n{body}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- 写入层 ---

def insert_note(con, agent, kind, topic, tags, content, src="", sig=""):
    content = content.strip()[:NOTE_CAP]
    if not content:
        return None
    ts = now_iso()
    cur = con.execute(
        "insert into notes(ts,day,agent,kind,topic,tags,content,src,sig) "
        "values(?,?,?,?,?,?,?,?,?)",
        (ts, day_of(), agent, kind, topic, tags, content, src, sig))
    con.execute("insert into notes_fts(body,nid) values(?,?)", (content, cur.lastrowid))
    return cur.lastrowid


def drop_src(con, src):
    for r in con.execute("select id from notes where src=?", (src,)).fetchall():
        con.execute("delete from notes_fts where nid=?", (r["id"],))
    con.execute("delete from notes where src=?", (src,))


def chunk_text(text, limit=CHUNK_LIMIT):
    chunks, cur, size = [], [], 0
    for para in text.split("\n\n"):
        if cur and size + len(para) > limit:
            chunks.append("\n\n".join(cur)); cur, size = [], 0
        cur.append(para); size += len(para)
    if cur:
        chunks.append("\n\n".join(cur))
    return [c for c in chunks if c.strip()] or [""]


def run_sync(con):
    t0 = time.time()
    seen = {}
    for product, pattern in load_sources():
        root = HOME / pattern.split("/")[0]
        if not root.exists():
            continue
        for f in HOME.glob(pattern):
            if not f.is_file() or f.stat().st_size > 500_000:
                continue
            if f.suffix.lower() not in MD_SUFFIXES:
                continue      # glob 写宽了也不至于把 config.toml / json 当记忆搬进来
            p = str(f)
            sig = f"{int(f.stat().st_mtime)}:{f.stat().st_size}"
            seen[p] = (product, sig)
    changed = removed = 0
    for p, (product, sig) in sorted(seen.items()):
        row = con.execute("select sig from files where path=?", (p,)).fetchone()
        if row and row["sig"] == sig:
            continue
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        drop_src(con, p)
        chunks = chunk_text(text)
        multi = len(chunks) > 1
        for i, chunk in enumerate(chunks):
            topic = f"{Path(p).stem}#{i + 1}" if multi else Path(p).stem
            insert_note(con, product, "native", topic, "", chunk, src=p, sig=sig)
        con.execute("insert or replace into files(path,sig) values(?,?)", (p, sig))
        changed += 1
    for (p,) in con.execute("select path from files").fetchall():
        if p not in seen:
            drop_src(con, p)
            con.execute("delete from files where path=?", (p,))
            removed += 1
    total = con.execute("select count(*) c from notes where kind='native'").fetchone()["c"]
    return len(seen), changed, removed, total, time.time() - t0


def cmd_mem_sync(con, args):
    seen, changed, removed, total, dur = run_sync(con)
    touch_stamp()
    print(f"同步完成：扫描 {seen} 个原生记忆文件，更新 {changed}、清理 {removed}，"
          f"当前原生记忆 {total} 条（{dur:.1f}s）")


def _sync_now(con, quiet=False):
    seen, changed, removed, total, dur = run_sync(con)
    touch_stamp()
    if not quiet:
        print(f"  同步：扫描 {seen} 个文件，更新 {changed}、清理 {removed}，"
              f"原生记忆 {total} 条（{dur:.1f}s）")


def cmd_mem_source(con, args):
    act, rows = args.source_act, source_rows()

    if act == "list":
        for r in rows:
            g = str(r.get("glob") or "")
            on = r.get("enabled", True) is not False
            print(f"  {'✓' if on else '✗'} {str(r.get('product', '')):<13}{g:<48} 命中 {count_glob(g)}")
        print(f"\n配置文件：{SOURCES_FILE}（改完即生效，不用重装修改代码）")
        return

    if act == "add":
        g = args.glob.strip().strip('"')
        if any(str(r.get("glob")) == g for r in rows):
            print(f"这条规则已经在里了：{g}")
            return
        p = (args.product or g.split("/")[0].lstrip(".")) .strip().lower()
        rows.append({"product": p, "glob": g, "enabled": True})
        save_source_rows(rows)
        n = count_glob(g)
        print(f"已登记 {p} ← {g}（当前命中 {n} 个文件）")
        if n:
            _sync_now(con)
        else:
            print("  暂时一个都没命中：路径是相对 home 的 glob，等那个产品装好后跑 agenthub mem sync 即可。")
        return

    if act == "rm":
        t = args.target.strip().strip('"').lower()
        keep = [r for r in rows
                if str(r.get("glob", "")).lower() != t and str(r.get("product", "")).lower() != t]
        if len(keep) == len(rows):
            print(f"没找到这条规则：{t}（先看 agenthub mem source list）")
            return
        save_source_rows(keep)
        print(f"已删除 {len(rows) - len(keep)} 条规则，正在按新清单重算（该产品的原生记忆会一并清出共享库）")
        _sync_now(con)
        return

    cands = scan_sources()
    if not cands:
        print("没发现未接入的记忆目录。已接入的看 agenthub mem source list")
        return
    print(f"发现 {len(cands)} 条还没接入的原生记忆源：\n")
    for r in cands:
        print(f"  {r['product']:<14}{r['glob']:<50} {r['files']} 个文件")
    if args.source_act == "scan" and getattr(args, "fix", False):
        have = {str(r.get("glob")) for r in rows}
        add = [r for r in cands if r["glob"] not in have]
        for r in add:
            rows.append({"product": r["product"], "glob": r["glob"], "enabled": True})
        save_source_rows(rows)
        print(f"\n已自动接入 {len(add)} 条 → {SOURCES_FILE}")
        _sync_now(con)
    else:
        print("\n一次接入全部：agenthub mem source scan --fix")
        print("只接其中一条：  agenthub mem source add <产品名> \"<glob>\"")


def touch_stamp():
    try:
        SYNC_STAMP.touch()
    except OSError:
        pass


def ensure_fresh(con):
    """读之前顺手增量同步一次。

    挂在读路径而不是定时器上：需要新鲜的时刻就是有人查的时刻，这样只要查了就不可能滞后。
    同步失败一律吞掉——宁可查到稍旧的记忆，也不要把一次查询弄成报错。
    """
    try:
        if time.time() - SYNC_STAMP.stat().st_mtime < SYNC_MIN_INTERVAL:
            return
    except OSError:
        pass
    try:
        run_sync(con)
        touch_stamp()
    except Exception:
        pass


def agentfind_trail(query, limit=2):
    """问 AgentFind：这条结论出自哪场对话。服务没开就安静地什么都不加。

    Windows 上连一个没监听的 127.0.0.1 端口不会立刻被拒，会干等到超时，所以失败要冷却 60s，
    免得服务没起时每次查询都白等一秒。
    """
    try:
        if time.time() - DOWN_STAMP.stat().st_mtime < DOWN_COOLDOWN:
            return []
    except OSError:
        pass
    try:
        url = f"{AGENTFIND}/api/search?q={quote(query)}&limit={limit}"
        opener = build_opener(ProxyHandler({}))   # 走系统代理会把 localhost 请求丢给代理并挂住
        with opener.open(url, timeout=1.2) as r:
            res = (json.loads(r.read().decode("utf-8")).get("results") or [])[:limit]
    except Exception:
        try:
            DOWN_STAMP.touch()
        except OSError:
            pass
        return []
    return [f'  · {x["label"]} {x["day"]} · {str(x["title"])[:30]} '
            f'→ {AGENTFIND}/?sid={quote(x["sid"], safe="")}' for x in res]


# ---------------------------------------------------------------- 命令 ---

def cmd_mem_write(con, args):
    content = sys.stdin.read() if args.content in ("-", "") else args.content
    nid = insert_note(con, args.agent, args.kind, args.topic, args.tags, content)
    print(f"已写入共享记忆 #{nid}（agent={args.agent} kind={args.kind}）")


def cmd_mem_list(con, args):
    ensure_fresh(con)
    cond, params = ["1=1"], []
    if args.agent:
        cond.append("agent=?"); params.append(args.agent)
    if args.kind:
        cond.append("kind=?"); params.append(args.kind)
    rows = con.execute(
        f"select * from notes where {' and '.join(cond)} "
        f"order by id desc limit ?", (*params, args.n)).fetchall()
    if not rows:
        print("（还没有记忆。让各 Agent 用 agenthub mem write 写入，或跑 agenthub mem sync 同步原生记忆）")
        return
    for r in rows:
        head = fmt_note(r, with_body=False)
        first = r["content"].strip().splitlines()[0][:80] if r["content"].strip() else ""
        print(f"{head}\n    {first}")


def cmd_mem_read(con, args):
    r = con.execute("select * from notes where id=?", (args.id,)).fetchone()
    print(fmt_note(r) if r else f"没有 #{args.id} 这条记忆")


def cmd_mem_forget(con, args):
    r = con.execute("select id from notes where id=?", (args.id,)).fetchone()
    if not r:
        print(f"没有 #{args.id} 这条记忆"); return
    con.execute("delete from notes_fts where nid=?", (args.id,))
    con.execute("delete from notes where id=?", (args.id,))
    print(f"已删除 #{args.id}")


def cmd_mem_search(con, args):
    ensure_fresh(con)
    rows = search_notes(con, args.query, agent=args.agent, kind=args.kind,
                        since=args.since, limit=args.n, sort=args.sort)
    if not rows:
        print("没有命中。试试更短的关键词（1–2 字也可）——各产品的原生记忆在查之前已经自动同步进来了")
        return
    for r in rows:
        print("-" * 62)
        print(fmt_note(r, with_body=False))
        try:
            snip = con.execute(
                "select snippet(notes_fts,0,'»','«',' … ',64) s from notes_fts "
                "where nid=?", (r["id"],)).fetchone()["s"]
            print(f"    {snip[:400]}")
        except sqlite3.OperationalError:
            print("    " + r["content"][:200].replace("\n", " "))
    trail = agentfind_trail(args.query)
    if trail:
        print("-" * 62)
        print("过程原文（这场对话的完整记录，点开可跳回那个应用）:")
        print("\n".join(trail))


def cmd_mem_context(con, args):
    ensure_fresh(con)
    block = build_context(con, args.query, n=args.n, agent=args.agent)
    print(block if block else f"（共享记忆里没有与「{args.query}」相关的内容）")
    if block:
        trail = agentfind_trail(args.query, limit=1)
        if trail:
            print(f"\n<!-- 过程原文：{trail[0].strip()}（这条是给人点的，粘进提示词时可删） -->")


def cmd_mem_stats(con, args):
    print("按来源：")
    for r in con.execute("select agent, count(*) c from notes group by agent order by c desc"):
        print(f"  {r['agent']:<12} {r['c']:>5}")
    print("按类型：")
    for r in con.execute("select kind, count(*) c from notes group by kind order by c desc"):
        print(f"  {r['kind']:<12} {r['c']:>5}")


# ---------------------------------------------------------------- 派工层 ---

AGENTS = {
    "codex": {
        "bin": "codex",
        "desc": "Codex CLI（OpenAI）— codex exec，支持 -C 工作目录、-s 沙箱",
        "example": 'agenthub call codex "把 README 里的错别字改掉" --cwd D:\\某项目 --write',
    },
    "claude": {
        "bin": "claude",
        "desc": "Claude Code CLI（Anthropic）— claude -p，纯问答默认够用",
        "example": 'agenthub call claude "审一下这段摘要的逻辑" --cwd D:\\某项目 --yolo',
    },
}
# 探测列表：装了但没接无头模式的产品，仅提示存在
EXTRA_PROBES = ["opencode", "qoder", "qwen", "gemini", "grok", "cursor-agent", "droid", "kimi"]


def resolve_exe(name):
    """把命令名解析成可直接启动的完整路径（Windows 下 npm shim 是 .cmd，裸名字起不来）。"""
    hits = shutil.which(name)
    if not hits:
        return None
    p = Path(hits)
    if p.suffix.lower() == ".cmd":
        exe = p.with_suffix(".exe")
        if exe.exists():
            return str(exe)
    return hits


def proxy_env():
    """子 Agent 继承不到终端里的代理设置（如 clash 7897/7890），探测本机常见代理端口并注入。"""
    env = dict(os.environ)
    if any(k in env for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return env, None
    for port in (7897, 7890, 10809, 1080):
        s = socket.socket()
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"
        env["NO_PROXY"] = "localhost,127.0.0.1"
        return env, f"已注入本机代理 127.0.0.1:{port}"
    return env, None


def run_dispatch(argv, cwd, full_prompt, timeout, child_env):
    """跑子 Agent。Windows 下 .cmd shim 会派生孙进程握住输出管道，超时必须按进程树强杀。"""
    t0 = time.time()
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(cwd), env=child_env)
    except FileNotFoundError:
        raise SystemExit(f"无法启动：{argv[0]}")
    timed_out = False
    try:
        out_b, err_b = proc.communicate(input=full_prompt.encode("utf-8"),
                                        timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
        try:
            out_b, err_b = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_b, err_b = proc.communicate()
        code = 124
    dur = time.time() - t0
    out = (out_b or b"").decode("utf-8", errors="replace")
    err = (err_b or b"").decode("utf-8", errors="replace")
    if timed_out:
        err += f"\n[agenthub] 超时（>{timeout}s），已强制终止整棵进程树"
    return out, err, code, dur


def build_cmd(product, prompt, cwd, write, yolo, model):
    """返回 (argv, 说明)。提示词统一走 stdin，避免 Windows 命令行长度/转义问题。"""
    exe = resolve_exe(AGENTS[product]["bin"])
    if not exe:
        raise SystemExit(f"本机没找到 {AGENTS[product]['bin']} 命令")
    if product == "codex":
        argv = [exe, "exec", "--skip-git-repo-check", "--color", "never",
                "-s", "workspace-write" if write else "read-only", "-C", str(cwd), "-"]
        if model:
            argv += ["-m", model]
        note = "沙箱=" + ("workspace-write" if write else "read-only")
    elif product == "claude":
        argv = [exe, "-p", "--output-format", "text"]
        if yolo:
            argv += ["--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        note = "权限=" + ("bypass(--yolo)" if yolo else "默认(工具调用会被拒，适合问答)")
    else:
        raise SystemExit(f"不支持的子 Agent：{product}（agenthub agents 看可用列表）")
    return argv, note


def cmd_agents(con, args):
    print("可派工（无头模式已接通）：")
    for name, a in AGENTS.items():
        ok = shutil.which(a["bin"])
        print(f"  {'✓' if ok else '✗'} {name:<8} {a['desc']}")
        if ok:
            print(f"      {a['example']}")
    extra = [b for b in EXTRA_PROBES if shutil.which(b)]
    if extra:
        print(f"检测到但暂未接派工：{', '.join(extra)}（Qoder/WorkBuddy/QwenWork 等桌面产品无无头 CLI，"
              f"只参与记忆共享）")


def cmd_call(con, args):
    product = args.product
    if product not in AGENTS:
        raise SystemExit(f"不支持的子 Agent：{product}（agenthub agents 看可用列表）")
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.exists():
        raise SystemExit(f"工作目录不存在：{cwd}")

    prompt = sys.stdin.read() if args.task in ("-", "") else args.task
    if not prompt.strip():
        raise SystemExit("任务内容为空")

    mem_block = ""
    if args.mem:
        ensure_fresh(con)
        mem_block = build_context(con, args.mem, n=args.mem_n)
        if not mem_block:
            print(f"[agenthub] 共享记忆里没有与「{args.mem}」相关的内容，照常派工")

    full_prompt = prompt
    if mem_block:
        full_prompt = (f"<shared-memory>\n{mem_block}\n</shared-memory>\n\n"
                       f"<task>\n{prompt}\n</task>")
    else:
        full_prompt = f"<task>\n{prompt}\n</task>"

    argv, note = build_cmd(product, prompt, cwd, args.write, args.yolo, args.model)
    child_env, proxy_note = (dict(os.environ), None) if args.no_proxy else proxy_env()
    if proxy_note:
        note += f" · {proxy_note}"
    print(f"[agenthub] 派工 → {product}（{note}）cwd={cwd}"
          + (f" 记忆查询=「{args.mem}」" if args.mem else ""))
    out, err, code, dur = run_dispatch(argv, cwd, full_prompt, args.timeout, child_env)

    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tpath = DISPATCH_DIR / f"{stamp}-{product}.md"
    tpath.write_text(
        f"# agenthub 派工 · {product}\n\n- 时间: {now_iso()}\n- cwd: {cwd}\n"
        f"- 沙箱/权限: {note}\n- 记忆查询: {args.mem or '-'}\n- 退出码: {code} · 用时 {dur:.0f}s\n\n"
        f"## 任务\n\n{prompt}\n\n## 共享记忆上下文\n\n{mem_block or '（未使用）'}\n\n"
        f"## stdout\n\n```\n{out.strip() or '（空）'}\n```\n\n"
        f"## stderr\n\n```\n{err.strip()[-3000:] or '（空）'}\n```\n",
        encoding="utf-8")

    con.execute(
        "insert into dispatches(ts,product,cwd,prompt,mem_query,exit_code,dur,"
        "transcript,output) values(?,?,?,?,?,?,?,?,?)",
        (now_iso(), product, str(cwd), prompt, args.mem or "", code, dur,
         str(tpath), out.strip()[:8000]))
    insert_note(con, product, "dispatch",
                f"派工: {prompt.strip().splitlines()[0][:60]}",
                f"agenthub,{product}",
                f"主 Agent 派工给 {product}（cwd={cwd}）。\n任务: {prompt}\n"
                f"结果摘要: {out.strip()[:1500] or '(无输出)'}")
    print(f"[agenthub] 完成：退出码 {code}，用时 {dur:.0f}s，完整记录 → {tpath}\n")
    out_clean = out.strip()
    if len(out_clean) > 8000:
        print(out_clean[:8000] + "\n…（输出过长，已截断，完整内容看上面的记录文件）")
    else:
        print(out_clean or "（子 Agent 没有文本输出，详见记录文件）")


def cmd_log(con, args):
    if args.show:
        r = con.execute("select * from dispatches where id=?", (args.show,)).fetchone()
        if not r:
            print(f"没有 #{args.show} 这条派工"); return
        p = Path(r["transcript"])
        print(p.read_text(encoding="utf-8", errors="replace") if p.exists()
              else r["output"])
        return
    rows = con.execute("select * from dispatches order by id desc limit ?",
                       (args.n,)).fetchall()
    if not rows:
        print("（还没有派工记录）"); return
    for r in rows:
        head = r["prompt"].strip().splitlines()[0][:56] if r["prompt"].strip() else ""
        print(f"#{r['id']} {r['ts']} {r['product']:<7} exit={r['exit_code']} "
              f"{r['dur']:.0f}s  {head}")


# ---------------------------------------------------------------- 入口 ---

CATALOG_PATH = HOME / ".agents" / "catalog.json"

# 预存的各家 Agent 索引信息：安装位置候选、配置目录、原生记忆形状、会话记录形状、
# 本机实测过的多开结论。三个工具共用（驾驶舱识别产品、mem source 接入记忆、AgentFind 找解析器），
# 所以只写"本机验证过的事实"，不写猜出来的路径；额度接口留空由 agenthub catalog 之后补。
CATALOG_SEED = [
    {"id": "workbuddy", "name": "WorkBuddy", "vendor": "Tencent", "kind": "electron",
     "config": ".workbuddy", "exe": ["D:/LenovoSoftstore/Install/WorkBuddy/WorkBuddy.exe"],
     "memory": [".workbuddy/memory/*.md", ".workbuddy/MEMORY.md", ".workbuddy/USER.md",
                ".workbuddy/IDENTITY.md", ".workbuddy/SOUL.md", ".workbuddy/automations/*/memory.md"],
     "sessions": [], "multi_open": False,
     "note": "第二次启动被已有窗口接管（参数被丢弃），只能整目录换号"},
    {"id": "qoder-cn", "name": "Qoder CN", "vendor": "Alibaba", "kind": "electron",
     "config": ".qoder-cn", "exe": ["D:/Artificial_Intelligence/Qoder CN/Qoder CN.exe"],
     "memory": [".qoder-cn/memory/**/*.md", ".qoder-cn/memories/**/*.md",
                ".qoder-cn/projects/*/memory/*.md"],
     "sessions": [], "multi_open": False, "note": "单实例接管"},
    {"id": "qoder", "name": "Qoder 国际版", "vendor": "Alibaba", "kind": "electron",
     "config": ".qoder", "exe": ["{LOCALAPPDATA}/Programs/Qoder/Qoder.exe"],
     "memory": [".qoder/memory/**/*.md", ".qoder/projects/*/memory/*.md"],
     "sessions": [], "multi_open": False, "note": "与 CN 版是两个独立产品，配置目录不共用"},
    {"id": "qoder-cn-ide", "name": "Qoder CN IDE", "vendor": "Alibaba", "kind": "electron",
     "config": ".qoder-cn-ide", "exe": ["D:/Artificial_Intelligence/Qoder CN IDE/Qoder CN IDE.exe"],
     "memory": [], "sessions": [], "multi_open": True, "note": "实测可 --user-data-dir 多开"},
    {"id": "trae-cn", "name": "TraeWork CN", "vendor": "ByteDance", "kind": "electron",
     "config": ".trae-cn", "exe": ["D:/Artificial_Intelligence/TRAE SOLO CN/TRAE SOLO CN.exe"],
     "memory": [".trae-cn/memory/**/*.md"],
     "sessions": [".trae-cn/memory/projects/**/session_memory_*.jsonl"],
     "multi_open": True,
     "note": "正文库加密，AgentFind 只能索引会话摘要；session_memory_*.jsonl 尚未接解析器"},
    {"id": "zcode", "name": "ZCode", "vendor": "Zhipu", "kind": "electron",
     "config": ".zcode", "exe": ["D:/Artificial_Intelligence/ZCode/ZCode.exe"],
     "memory": [], "sessions": ["~/.zcode/cli/db/db.sqlite"], "multi_open": True,
     "note": "memoryEnabled:false，对共享记忆是纯读者"},
    {"id": "codex", "name": "Codex CLI", "vendor": "OpenAI", "kind": "cli",
     "config": ".codex", "exe": ["D:/npm-global/codex.cmd"], "env": "CODEX_HOME",
     "memory": [".codex/memories/*.md", ".codex/memories/**/*.md", ".codex/AGENTS.md"],
     "sessions": [".codex/sessions/**/*.jsonl"], "multi_open": True,
     "note": "⚠ 原生记忆已迁到 .codex/memories_1.sqlite，md 侧是僵尸数据"},
    {"id": "claude", "name": "Claude Code CLI", "vendor": "Anthropic", "kind": "cli",
     "config": ".claude", "exe": ["D:/npm-global/claude.cmd"], "env": "CLAUDE_CONFIG_DIR",
     "memory": [".claude/CLAUDE.md"], "sessions": [".claude/projects/*/*.jsonl"],
     "multi_open": True, "note": ""},
    {"id": "dsh", "name": "DeepSeek DSH", "vendor": "DeepSeek", "kind": "electron",
     "config": ".dsh", "exe": [], "memory": [],
     "sessions": [".dsh/sessions/**/session.*.jsonl.zst*"], "multi_open": None,
     "note": "zstd 压缩 jsonl，需 Python 3.14+ 标准库；会把注入型通知当用户消息当标题"},
    {"id": "qwenworkcn", "name": "QwenWork CN", "vendor": "Alibaba", "kind": "electron",
     "config": ".qwenworkcn", "exe": [],
     "memory": [".qwenworkcn/awareness/main/MEMORY.md", ".qwenworkcn/awareness/main/memory/*.md",
                ".qwenworkcn/awareness/**/*.md"],
     "sessions": [], "multi_open": None, "note": "自动任务会话多，AgentFind 已按 session.auto 降权"},
    {"id": "opencode", "name": "opencode", "vendor": "SST", "kind": "cli",
     "config": ".config/opencode", "exe": [], "memory": [],
     "sessions": ["~/.local/share/opencode/opencode.db"], "multi_open": None,
     "note": "sqlite 表集合 {session,message,part}，与 ZCode 同构"},
    {"id": "copilot", "name": "VS Code Copilot Chat", "vendor": "GitHub", "kind": "extension",
     "config": "Code/User/globalStorage/github.copilot-chat", "exe": [], "memory": [],
     "sessions": ["%APPDATA%/Code/User/globalStorage/github.copilot-chat/session-store.db"],
     "multi_open": None, "note": "一行一个来回；只读打开不能带 immutable（WAL 可见性）"},
]
# 余额/额度接口：只写**本机日志或官方文档里确证存在**的端点，状态分四档，避免把猜的当成品。
#   ready         有 API Key 就能直接查（官方公开）
#   need_capture  端点确凿，但要客户端自己的登录态，得先抓包导出 token
#   need_key      端点确凿，缺一个能用的 API Key
#   none          本轮没找到任何证据
QUOTA = {
    "dsh": ("need_key", "https://api.deepseek.com/user/balance", "balance_infos",
                     "Bearer <sk- API Key>", "官方文档；实测端点活着，用登录态 token 会 401"),
    "codex": ("ready", "https://api.openai.com/v1/organization/costs", None,
              "Bearer <Admin Key>", "驾驶舱已有 openai 适配器；本地只有占位 key"),
    "claude": ("need_key", None, None, "Bearer",
              "走 aiwelink 中转，settings.json 里有明文 sk-，用 newapi/oneapi 适配器实测 /api/user/self"),
    "zcode": ("need_capture", "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.14.4",
              "data.balances", "Bearer <客户端凭据>",
              "日志确证客户端自己调得到 code:0；但 leveldb 里那份 JWT 打过去 400，真凭据在 credentials.json(enc:v1)"),
    "qoder-cn": ("local_read", "https://openapi.qoder.com.cn/api/v2/user/plan", None, "本地 DPAPI",
                 "已打通：agenthub quota 直接解 state.vscdb 的 secret://aicoding.auth.creditUsage（v10/AES-GCM），不联网"),
    "qoder": ("local_read", "https://openapi.qoder.sh/api/v2/user/plan", None, "本地 DPAPI",
              "与 CN 版同一套代码路径，同一份实现直接读出多桶积分与到期日"),
    "trae-cn": ("need_capture", "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage", None, "登录态",
                "日志里另有 checkin_credits/status、cn_credits_billing_status 等 4 条真实路径"),
    "workbuddy": ("need_capture", "https://copilot.tencent.com/billing/meter/get-user-resource-summary",
                  None, "登录态（IPC 取，不落文件）",
                  "app.asar 里确证一整套 /billing/meter/* 与 /profile/usage；社区方案也拿不到积分"),
    "qwenworkcn": ("none", None, None, None, "只找到 UI 标记文件，非余额"),
    "opencode": ("need_key", None, None, "Bearer", "走各家 relay，用对应上游的接口查"),
}


def _expand(p):
    return (str(p).replace("{LOCALAPPDATA}", os.environ.get("LOCALAPPDATA", ""))
            .replace("%APPDATA%", os.environ.get("APPDATA", "")))


def _hits_glob(glob):
    """目录里的形状字段允许 ~/、%APPDATA%/、相对 home 三种写法，统一判存在。"""
    g = _expand(glob)
    if g.startswith("~/"):
        g = g[2:]
    try:
        if len(g) > 1 and g[1] == ":":                 # 已经展开成绝对路径
            p = Path(g)
            return p.exists() if "*" not in g else next(p.parent.glob(p.name), None) is not None
        return next(HOME.glob(g), None) is not None    # 相对 home
    except (OSError, ValueError):
        return False


def installed_probe(a):
    """在装判断：可执行文件 / 配置目录 / 记忆或会话文件，三者命中任一即算装着。"""
    for cand in a.get("exe") or []:
        if Path(_expand(cand)).exists():
            return True
    cfg = a.get("config") or ""
    if cfg:
        if cfg.startswith("."):
            if (HOME / cfg).exists():
                return True
        elif cfg.startswith("~"):
            if Path(_expand(cfg)).exists():
                return True
        elif (Path(os.environ.get("APPDATA", "")) / cfg).exists():
            return True
    return any(_hits_glob(g) for g in (a.get("memory") or []) + (a.get("sessions") or []))


def catalog_build():
    """把内置目录 + 本机实测（哪些装着、记忆接了哪几条）合成 ~/.agents/catalog.json。"""
    src_globs = {}
    for product, g in load_sources():
        src_globs.setdefault(product, []).append(g)
    out = []
    for a in CATALOG_SEED:
        e = dict(a)
        e["installed"] = installed_probe(a)
        e["memory_synced"] = sorted(src_globs.get(a["id"], []))
        st, url, path, auth, note = QUOTA.get(a["id"], (None, None, None, None, ""))
        e["quota"] = {"status": st, "url": url, "path": path, "auth": auth, "evidence": note}
        out.append(e)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 1, "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "note": "跨工具共享的 Agent 索引；只写本机验证过的事实。驾驶舱与 agenthub/AgentFind 共用。",
           "agents": out}
    CATALOG_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def cmd_catalog(con, args):
    if args.catalog_act == "build":
        doc = catalog_build()
        ins = [a["name"] for a in doc["agents"] if a["installed"]]
        print(f"已写出 {CATALOG_PATH}")
        print(f"目录收录 {len(doc['agents'])} 家；本机在装 {len(ins)} 家：{'、'.join(ins)}")
        return
    doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8")) if CATALOG_PATH.exists() else None
    if not doc:
        print("还没有目录，先跑：agenthub catalog build")
        return
    for a in doc["agents"]:
        q = (a.get("quota") or {}).get("status") or "—"
        print(f"  {'●' if a['installed'] else '○'} {a['name']:<20}{a['vendor']:<10}"
              f"记忆 {len(a['memory_synced']):>2} 条  余额[{q}]")
        if args.detail and (a.get("quota") or {}).get("url"):
            print(f"      {a['quota']['url']}")
            print(f"      鉴权 {a['quota']['auth']}｜{a['quota']['evidence'][:70]}")
    print(f"\n生成于 {doc['updated']}，文件：{CATALOG_PATH}")


# ---------------------------------------------------------------------------
# 本机凭据直读：Qoder 系的积分本来就存在它自己的 state.vscdb 里（Chromium os_crypt v10）。
# 全程只读、不联网、不解他人账号——DPAPI 主密钥只对当前 Windows 登录用户有效。
# ---------------------------------------------------------------------------
QODER_STORES = {"qoder-cn": "QoderCN", "qoder": "Qoder"}
FOREVER_MS = 253402214400000      # 9999-12-31：厂商拿它当"无限期"哨兵


def _dpapi(data):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("CryptUnprotectData 失败：这份凭据不是当前 Windows 用户加密的")
    res = ctypes.create_string_buffer(out.cbData)
    ctypes.memmove(res, out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return res.raw


def _aes_key(ls_path):
    """Local State 里的 os_crypt.encrypted_key = base64("DPAPI" + DPAPI 包裹的 32B AES 主密钥)。"""
    wrapped = base64.b64decode(json.loads(ls_path.read_text(encoding="utf-8"))["os_crypt"]["encrypted_key"])
    if wrapped[:5] != b"DPAPI":
        raise RuntimeError("encrypted_key 缺 DPAPI 前缀，格式变了")
    return _dpapi(wrapped[5:])


def _read_secret(con, aes, key):
    row = con.execute("select value from ItemTable where key=?", (key,)).fetchone()
    if not row:
        return None
    blob = bytes(json.loads(row[0])["data"])
    if blob[:3] != b"v10":
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return json.loads(AESGCM(aes).decrypt(blob[3:15], blob[15:], None))


def qoder_credit(appdata_name):
    """返回 Qoder 自己写的 creditUsage 明文 + 登录身份；没装/没登录/缺依赖都给说明性结果。"""
    ad = Path(os.environ.get("APPDATA", "")) / appdata_name
    ls, db = ad / "Local State", ad / "User" / "globalStorage" / "state.vscdb"
    if not ls.is_file() or not db.is_file():
        return {"error": f"没找到 {appdata_name} 的 Local State 或 state.vscdb"}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM   # noqa: F401 探测可选依赖
    except ImportError:
        return {"error": "缺 cryptography 库（pip install cryptography）"}
    try:
        aes = _aes_key(ls)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            d = _read_secret(con, aes, "secret://aicoding.auth.creditUsage")
            who = _read_secret(con, aes, "secret://aicoding.auth.userInfo")
        finally:
            con.close()
        if d is None:
            return {"error": "库里没有 creditUsage 或不是 v10 格式（可能还没登录过）"}
        return {"data": d, "who": who}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def credit_buckets(d):
    """把积分拆成一桶一桶：主额度 / 附加包 / 每个专属资源包，各自 total-used-remaining。"""
    out = []

    def push(label, q):
        if isinstance(q, dict) and q.get("total") is not None:
            out.append({"pool": label, "total": q.get("total"), "used": q.get("used"),
                        "remaining": q.get("remaining"), "unit": q.get("unit") or "credits"})

    push("主额度", d.get("userQuota"))
    push("附加包", d.get("addOnQuota"))
    for p in d.get("dedicatedResourcePackages") or []:
        push(f"资源包 {p.get('name') or str(p.get('id'))[:8]}", p)
    return out


def exp_text(ms):
    if not ms or ms >= FOREVER_MS:
        return "无限期"
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def cmd_quota(con, args):
    want = [args.product] if args.product else list(QODER_STORES)
    rows, dump = [], []
    for p in want:
        store = QODER_STORES.get(p)
        if not store:
            print(f"不认识的产品 {p}（支持：{'、'.join(QODER_STORES)}）")
            continue
        r = qoder_credit(store)
        if "error" in r:
            rows.append((p, None, r["error"]))
            dump.append({"product": p, "error": r["error"]})
            continue
        d = r["data"]
        who = r.get("who") or {}
        acct = who.get("name") or (who.get("email") or "")[:24] or str(d.get("userId"))[:8] + "…"
        if who.get("email") and who.get("name"):
            acct += f"〈{who['email']}〉"
        buckets = credit_buckets(d)
        left = sum(b["remaining"] or 0 for b in buckets)
        total = sum(b["total"] or 0 for b in buckets)
        rows.append((p, (d, buckets, left, total, acct), None))
        dump.append({"product": p, "account": acct, "userId": d.get("userId"),
                     "userType": d.get("userType"), "left": left, "total": total,
                     "expires": exp_text(d.get("expiresAt")), "exceeded": d.get("isQuotaExceeded"),
                     "buckets": buckets})
    if args.json:
        print(json.dumps(dump, ensure_ascii=False, indent=2))
        return
    for p, ok, err in rows:
        if err:
            print(f"  {p:<12}读不到：{err}")
            continue
        d, buckets, left, total, acct = ok
        flag = "已超额" if d.get("isQuotaExceeded") else f"用了 {d.get('totalUsagePercentage', 0):.0%}"
        print(f"  {p:<12}剩 {left}/{total} 积分 · 账号 {acct} · {d.get('userType') or '?'} · "
              f"到期 {exp_text(d.get('expiresAt'))} · {flag}")
        for b in buckets:
            print(f"      {b['pool']:<26}{b['used']}/{b['total']} → 剩 {b['remaining']} {b['unit']}")
    if args.write:
        for p, ok, _ in rows:
            if not ok:
                continue
            d, buckets, left, total, acct = ok
            line = (f"{p} 余额（账号 {acct}）：剩 {left}/{total} 积分，到期 {exp_text(d.get('expiresAt'))}；"
                    + "；".join(f"{b['pool']} 剩 {b['remaining']}/{b['total']}" for b in buckets))
            insert_note(con, "agenthub", "fact", f"{p} 余额快照", "quota,agenthub", line)
            print(f"  已写入共享记忆：{line[:60]}…")


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(prog="agenthub",
                                 description="多 Agent 共享记忆 + 子 Agent 派工")
    sub = ap.add_subparsers(dest="cmd", required=True)

    mem = sub.add_parser("mem", help="共享记忆：写入/查询/同步")
    msub = mem.add_subparsers(dest="sub", required=True)

    w = msub.add_parser("write", help="写一条共享记忆（内容为 - 时读 stdin）")
    w.add_argument("content", nargs="?", default="")
    w.add_argument("--agent", default="user", help="写入方（哪个 Agent 或 user）")
    w.add_argument("--kind", default="note",
                   choices=["note", "decision", "fact", "handoff"], help="记忆类型")
    w.add_argument("--topic", default="", help="主题")
    w.add_argument("--tags", default="", help="逗号分隔标签")
    w.set_defaults(fn=cmd_mem_write)

    l = msub.add_parser("list", help="最近记忆")
    l.add_argument("-n", type=int, default=15)
    l.add_argument("--agent"); l.add_argument("--kind")
    l.set_defaults(fn=cmd_mem_list)

    r = msub.add_parser("read", help="看某条记忆全文")
    r.add_argument("id", type=int)
    r.set_defaults(fn=cmd_mem_read)

    f = msub.add_parser("forget", help="删某条记忆")
    f.add_argument("id", type=int)
    f.set_defaults(fn=cmd_mem_forget)

    s = msub.add_parser("search", help="全文检索（关键词、产物名、项目名都行）")
    s.add_argument("query")
    s.add_argument("--agent", help="只看某个产品")
    s.add_argument("--kind", help="note/decision/fact/handoff/dispatch/native")
    s.add_argument("--since", help="7d / 30d / 2026-08-01")
    s.add_argument("-n", type=int, default=15)
    s.add_argument("--sort", default="relevance", choices=["relevance", "time"])
    s.set_defaults(fn=cmd_mem_search)

    c = msub.add_parser("context", help="生成可粘进提示词的记忆上下文块")
    c.add_argument("query")
    c.add_argument("-n", type=int, default=5)
    c.add_argument("--agent")
    c.set_defaults(fn=cmd_mem_context)

    sy = msub.add_parser("sync", help="同步各产品原生记忆到共享索引（增量）")
    sy.set_defaults(fn=cmd_mem_sync)

    sc = msub.add_parser("source", help="原生记忆源：list / add / rm / scan（配置存 sources.json）")
    scs = sc.add_subparsers(dest="source_act", required=True)
    scs.add_parser("list", help="列出现有源及各自命中文件数")
    sa = scs.add_parser("add", help="接入一个新产品的记忆文件")
    sa.add_argument("glob", help="相对 home 的 glob，如 .foo/memory/**/*.md")
    sa.add_argument("--product", help="产品名，默认取 glob 的第一段")
    sr = scs.add_parser("rm", help="删掉一条源（该产品的原生记忆同时清出共享库）")
    sr.add_argument("target", help="glob 原文或产品名")
    ss = scs.add_parser("scan", help="扫本机还没接入的记忆目录")
    ss.add_argument("--fix", action="store_true", help="直接把扫到的全部写进 sources.json 并同步")
    sc.set_defaults(fn=cmd_mem_source)

    st = msub.add_parser("stats", help="共享记忆构成")
    st.set_defaults(fn=cmd_mem_stats)

    ag = sub.add_parser("agents", help="列出可派工的子 Agent")
    ag.set_defaults(fn=cmd_agents)

    ct = sub.add_parser("catalog", help="本机 Agent 索引目录：build / show")
    cts = ct.add_subparsers(dest="catalog_act", required=True)
    cts.add_parser("build", help="生成/刷新 ~/.agents/catalog.json（含在装判断）")
    sh = cts.add_parser("show", help="列出目录里每家的记忆/余额接入状态")
    sh.add_argument("--detail", action="store_true", help="连余额端点与鉴权方式一起打印")
    ct.set_defaults(fn=cmd_catalog)

    qt = sub.add_parser("quota", help="从本机直读 Qoder 系积分（DPAPI + v10/AES-GCM，只读、不联网）")
    qt.add_argument("product", nargs="?", help="qoder-cn / qoder；省略则两个都读")
    qt.add_argument("--json", action="store_true", help="输出机器可读的原始结构")
    qt.add_argument("--write", action="store_true", help="把这次快照写进共享记忆，别的 Agent 也能看到")
    qt.set_defaults(fn=cmd_quota)

    ca = sub.add_parser("call", help="把任务派给子 Agent 无头执行")
    ca.add_argument("product", help="codex / claude")
    ca.add_argument("task", help="任务描述（为 - 时读 stdin）")
    ca.add_argument("--cwd", help="子 Agent 的工作目录（默认当前目录）")
    ca.add_argument("--mem", help="派工前按此查询检索共享记忆并注入提示词")
    ca.add_argument("--mem-n", type=int, default=5)
    ca.add_argument("--write", action="store_true", help="codex: 允许写工作区（workspace-write）")
    ca.add_argument("--yolo", action="store_true", help="claude: 跳过权限确认")
    ca.add_argument("--model", help="指定模型")
    ca.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ca.add_argument("--no-proxy", action="store_true",
                    help="不自动探测并注入本机代理给子 Agent")
    ca.set_defaults(fn=cmd_call)

    lg = sub.add_parser("log", help="派工记录（--show ID 看完整记录）")
    lg.add_argument("-n", type=int, default=10)
    lg.add_argument("--show", type=int)
    lg.set_defaults(fn=cmd_log)

    args = ap.parse_args()
    con = connect()
    try:
        # 索引为空时自动做一次原生记忆同步，保证开箱能查
        if args.cmd == "mem" and args.sub in ("search", "context", "list", "stats"):
            if con.execute("select count(*) c from notes").fetchone()["c"] == 0:
                cmd_mem_sync(con, args)
        args.fn(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
