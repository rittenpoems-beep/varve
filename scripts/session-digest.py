#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""session-digest.py — 会话日志直入 SQLite（对话层 turns + 轨迹层 traces）。

2026-09-23 定稿设计（用户拍板）：
- **对话历史** → SQLite `turns` 表（权威存储，**不再落 markdown**）
- **全量历史** → SQLite `traces` 表（工具调用/输出/推理的可检索文本）
- 真相源 `~/.codex/sessions/**/*.jsonl` 只读；增量靠文件签名（mtime+size）
- FTS 索引由 build-search-index.py 在两张表上建（external content 模式）

用法：python -X utf8 session-digest.py [--data <dir>] [--sessions <dir>] [--limit N] [--full]
"""
import argparse
import glob
import json
import os
import re
import sqlite3

DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")
SESSIONS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")

ENV_RE = re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL)
SKILLS_RE = re.compile(r"<skills_instructions>.*?</skills_instructions>", re.DOTALL)
HARNESS_TAGS = {"environment_context", "skills_instructions", "turn_aborted",
                "recommended_plugins", "user_instructions", "system-reminder"}

Q_LIMIT = 1500      # 单条提问上限
A_LIMIT = 3000      # 单条回答上限
TRACE_LIMIT = 1500  # 单条轨迹文本上限

# schema 3（2026-09-24）：稳定 id 改由 id_map 顺序分配 ——
# 修 rowid 复用错位（盲测 #5），同时避免大整数 rowid 撑爆 trigram 索引
SCHEMA_VERSION = 3

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY,
        session_id TEXT, workspace TEXT, date TEXT, turn_no INTEGER,
        src_line_start INTEGER, src_line_end INTEGER, source TEXT,
        question TEXT, answer TEXT,
        UNIQUE(source, turn_no))""",
    """CREATE TABLE IF NOT EXISTS traces (
        id INTEGER PRIMARY KEY,
        session_id TEXT, seq INTEGER, kind TEXT, text TEXT, src_line INTEGER, source TEXT,
        UNIQUE(source, seq))""",
    """CREATE TABLE IF NOT EXISTS file_state (
        rel TEXT PRIMARY KEY, mtime INTEGER, size INTEGER)""",
    """CREATE TABLE IF NOT EXISTS id_map (
        kind TEXT, source TEXT, no INTEGER, id INTEGER PRIMARY KEY,
        UNIQUE(kind, source, no))""",
    """CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""",
]


class IdAllocator:
    """顺序分配的稳定 id：同一 (kind, source, no) 永远拿到同一个 id。

    为什么不用哈希 id（v2 试过）：FTS5 的 rowid 会写进每一条索引项，63-bit 随机值
    让 trigram 索引从 ~75MB 涨到 ~145MB。顺序小整数既稳定（不错位）又不膨胀。
    id 只增不减，删除内容行也不会让 id 被复用。
    """

    def __init__(self, con):
        self.con = con
        self.cache = {}
        self.next = 1
        for kind, src, no, i in con.execute("SELECT kind, source, no, id FROM id_map"):
            self.cache[(kind, src, no)] = i
            if i >= self.next:
                self.next = i + 1
        self.pending = []

    def get(self, kind, source, no):
        key = (kind, source, no)
        v = self.cache.get(key)
        if v is None:
            v = self.next
            self.next += 1
            self.cache[key] = v
            self.pending.append((kind, source, no, v))
        return v

    def flush(self):
        if self.pending:
            self.con.executemany("INSERT OR IGNORE INTO id_map VALUES (?,?,?,?)", self.pending)
            self.pending = []


def ensure_schema(con):
    """建表 + schema 版本迁移。返回 True 表示结构升级过、需要全量重灌。"""
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    ver = int(row[0]) if row and str(row[0]).isdigit() else 0
    # 结构探测优先于版本号：老库没有 schema_version 记录（或被误写），只看版本会漏掉升级
    cols = [r[1] for r in con.execute("PRAGMA table_info(turns)").fetchall()]
    has_idmap = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='id_map'").fetchone()[0]
    # 缺 id 列（v1）或缺 id_map（v2 哈希 id）都要升级
    stale = bool(cols) and ("id" not in cols or not has_idmap)
    if stale or (ver and ver < SCHEMA_VERSION):
        # 内容全部可从 sessions/*.jsonl 重建（可重建原则），结构升级直接重灌
        for t in ("turns", "traces", "turns_fts", "traces_fts", "file_state", "id_map"):
            con.execute("DROP TABLE IF EXISTS " + t)
        for t in ("turns_ai", "turns_ad", "turns_au", "traces_ai", "traces_ad", "traces_au"):
            con.execute("DROP TRIGGER IF EXISTS " + t)
        ver = 0
    for stmt in SCHEMA:
        con.execute(stmt)
    if ver < SCHEMA_VERSION:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return stale


def strip_harness(text):
    """剥离 harness 注入段（白名单判据，避免误杀以 < 开头的正常提问）。"""
    t = SKILLS_RE.sub("", ENV_RE.sub("", text))
    s = t.strip()
    m = re.match(r"^<\s*([A-Za-z_\-]+)", s)
    if m and m.group(1).lower() in HARNESS_TAGS and len(s) < 600:
        return ""
    if s.startswith("# AGENTS.md instructions") or "<INSTRUCTIONS>" in s[:200]:
        return ""
    return s


def extract_trace(payload):
    """从非 message 的 response_item 提取可检索文本 -> (kind, text)。"""
    t = payload.get("type") or ""
    if t == "function_call":
        txt = (payload.get("name") or "") + " " + str(payload.get("arguments") or "")
        return "call", txt[:TRACE_LIMIT]
    if t in ("function_call_output", "custom_tool_call_output"):
        return "output", str(payload.get("output") or "")[:TRACE_LIMIT]
    if t == "custom_tool_call":
        return "call", ((payload.get("name") or "") + " " + str(payload.get("input") or ""))[:TRACE_LIMIT]
    if t == "reasoning":
        parts = payload.get("summary") or []
        txt = " ".join(x.get("text", "") for x in parts if isinstance(x, dict))
        return ("reasoning", txt[:600]) if txt.strip() else (None, "")
    if t == "local_shell_call":
        return "call", json.dumps(payload.get("action") or {}, ensure_ascii=False)[:TRACE_LIMIT]
    return None, ""


def parse_session(path):
    """解析 jsonl -> dict(cwd, sid, date, turns, traces)；无内容返回 None。"""
    cwd = sid = ""
    turns, traces = [], []
    cur = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                if '"session_meta"' in line:
                    try:
                        p = (json.loads(line).get("payload") or {})
                    except Exception:
                        continue
                    cwd = p.get("cwd") or cwd
                    sid = p.get("id") or sid
                    continue
                if '"response_item"' not in line:
                    continue
                try:
                    p = (json.loads(line).get("payload") or {})
                except Exception:
                    continue
                if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    txt = ""
                    for c in p.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text") and c.get("text"):
                            txt = c["text"]
                            break
                    if not txt:
                        continue
                    if p["role"] == "user":
                        txt = strip_harness(txt)
                        if len(txt) < 2:
                            continue
                        cur = [ln, ln, txt.strip()[:Q_LIMIT], ""]
                        turns.append(cur)
                    elif cur is not None:
                        cur[1] = ln
                        cur[3] = txt.strip()[:A_LIMIT]
                else:
                    kind, txt = extract_trace(p)
                    if kind and txt.strip():
                        traces.append((len(traces) + 1, kind, txt, ln))
    except OSError:
        return None
    if not turns and not traces:
        return None
    return {"cwd": cwd, "sid": sid or os.path.basename(path)[-40:],
            "turns": turns, "traces": traces}


def main():
    ap = argparse.ArgumentParser(description="会话日志 -> SQLite")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--sessions", default=SESSIONS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    idx = os.path.join(args.data, "index")
    os.makedirs(idx, exist_ok=True)
    db = os.path.join(idx, "sessions.db")
    con = sqlite3.connect(db, timeout=30)
    # 并发与崩溃健壮性（2026-09-23 盲测 #4 / 深夜 hot journal 实况）：
    # WAL 让读写不互斥；busy_timeout 遇锁等待而不是立刻抛 "database is locked"。
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    upgraded = ensure_schema(con)
    if upgraded:
        args.full = True          # 结构升级 -> 必须全量重灌（内容可从 jsonl 重建）
        print("schema 升级到 v" + str(SCHEMA_VERSION) + "：全量重建内容")
    con.commit()

    alloc = IdAllocator(con)

    all_files = sorted(glob.glob(os.path.join(args.sessions, "**", "*.jsonl"), recursive=True))
    files = all_files[-args.limit:] if args.limit else all_files

    written = skipped = empty = 0
    n_turns = n_traces = 0
    # 孤儿清理的基准必须是**完整**文件列表。若用 --limit 截断后的列表做基准，
    # 本轮未参与的文件会被误判为"源已删除"，其 turns/traces 被整段清空
    # （数据破坏；2026-09-23 盲测复现：3 条 turns 跑 --limit 1 后只剩 2 条）。
    existing = {os.path.relpath(p, args.sessions).replace("\\", "/") for p in all_files}
    for p in files:
        rel = os.path.relpath(p, args.sessions).replace("\\", "/")
        try:
            st = os.stat(p)
        except OSError:
            continue
        mtime, size = int(st.st_mtime), st.st_size
        if not args.full:
            row = con.execute("SELECT mtime, size FROM file_state WHERE rel=?", (rel,)).fetchone()
            if row and row[0] == mtime and row[1] == size:
                skipped += 1
                continue
        rec = parse_session(p)
        if rec is None:
            empty += 1
            con.execute("INSERT OR REPLACE INTO file_state VALUES (?,?,?)", (rel, mtime, size))
            con.commit()
            continue
        sid = rec["sid"]
        date = os.path.basename(p)[8:18]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = ""
        con.execute("DELETE FROM turns WHERE source=?", (rel,))
        con.execute("DELETE FROM traces WHERE source=?", (rel,))
        for no, (ls, le, q, a) in enumerate(rec["turns"], 1):
            # 跨 source 去重（盲测 #6）：resume 会话会产生内容重叠的多个 rollout，
            # 同一 (session_id, turn_no) 只保留最后写入的一份，避免 BM25 统计被稀释。
            con.execute("DELETE FROM turns WHERE session_id=? AND turn_no=?", (sid, no))
            con.execute("INSERT OR REPLACE INTO turns "
                        "(id, session_id, workspace, date, turn_no, src_line_start, src_line_end, source, question, answer) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (alloc.get("turn", rel, no), sid, rec["cwd"], date, no, ls, le, rel, q, a))
        for seq, kind, txt, ln in rec["traces"]:
            con.execute("INSERT OR REPLACE INTO traces "
                        "(id, session_id, seq, kind, text, src_line, source) VALUES (?,?,?,?,?,?,?)",
                        (alloc.get("trace", rel, seq), sid, seq, kind, txt, ln, rel))
        con.execute("INSERT OR REPLACE INTO file_state VALUES (?,?,?)", (rel, mtime, size))
        alloc.flush()
        con.commit()
        written += 1
        n_turns += len(rec["turns"])
        n_traces += len(rec["traces"])

    # 孤儿清理：源文件已消失（删除/改名）的记录一并清掉，索引不会指向不存在的会话
    orphans = 0
    for (rel,) in con.execute("SELECT rel FROM file_state").fetchall():
        if rel not in existing:
            con.execute("DELETE FROM turns WHERE source=?", (rel,))
            con.execute("DELETE FROM traces WHERE source=?", (rel,))
            con.execute("DELETE FROM file_state WHERE rel=?", (rel,))
            orphans += 1

    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    if written or orphans:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('content_updated_at', datetime('now'))")
    con.commit()

    if upgraded:
        # 结构升级会留下大量空闲页（DROP 不缩文件）；升级后回收一次，避免体积虚高
        try:
            con.execute("VACUUM")
            print("已 VACUUM 回收碎片")
        except sqlite3.OperationalError:
            pass

    t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    r = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    print("files=" + str(len(files)) + " written=" + str(written) + " skipped=" + str(skipped)
          + " empty=" + str(empty) + " orphans=" + str(orphans)
          + " | +turns=" + str(n_turns) + " +traces=" + str(n_traces)
          + " | total_turns=" + str(t) + " total_traces=" + str(r))
    con.close()


if __name__ == "__main__":
    main()
