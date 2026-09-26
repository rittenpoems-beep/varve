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
import sys
import time

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


class RunLock:
    """跨进程互斥（文件系统 O_EXCL 实现，无第三方依赖）。

    为什么必须串行：两个 digest 同时写同一张表会**同时**踩两个坑（2026-09-26 实测复现）：
      1) 两个进程各自把 id_map 的当前最大值读进内存当计数器起点 → id 撞车 →
         turns 的 INSERT OR REPLACE 覆盖掉对方的真实数据行（丢数据），
         id_map 的 INSERT OR IGNORE 静默丢弃条目（映射永久丢失 → 下次换 id）。
      2) 冲突的 REPLACE 在默认 `recursive_triggers=OFF` 下**不触发**删除触发器 →
         FTS5 索引留下指向已删行的幽灵项（检索命中不存在的内容），
         而 COUNT(*) 行数依然相等 -> 行数对账抓不到。

    拿不到锁就跳过本轮（索引下一轮补齐），绝不并发写。死锁（上次被 kill）按 mtime 超时抢占。
    """

    def __init__(self, path, wait=30.0, stale=900.0):
        self.path = path
        self.wait = wait
        self.stale = stale
        self.fd = None

    def acquire(self):
        deadline = time.time() + self.wait
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, ("%d %s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%S"))).encode("utf-8"))
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                if time.time() >= deadline:
                    return False
                time.sleep(0.2)
            except OSError:
                return True          # 锁文件建不了（权限等）：降级为无锁，不阻塞索引更新

    def release(self):
        if self.fd is None:
            return
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.fd = None


class IdAllocator:
    """顺序分配的稳定 id：同一 (kind, source, no) 永远拿到同一个 id。

    为什么不用哈希 id（v2 试过）：FTS5 的 rowid 会写进每一条索引项，63-bit 随机值
    让 trigram 索引从 ~75MB 涨到 ~145MB。顺序小整数既稳定（不错位）又不膨胀。
    id 只增不减，删除内容行也不会让 id 被复用。

    分配在**写事务内**用 `MAX(id)+1` 完成（调用方必须已持有写事务）——旧实现把计数器
    缓存在进程内存里，两个并发进程会拿到同一个起点，撞车后丢数据（见 RunLock 注释）。
    """

    def __init__(self, con):
        self.con = con
        self.cache = {}

    def get(self, kind, source, no):
        key = (kind, source, no)
        v = self.cache.get(key)
        if v is None:
            self.con.execute(
                "INSERT OR IGNORE INTO id_map(kind, source, no, id) "
                "VALUES (?,?,?,(SELECT COALESCE(MAX(id),0)+1 FROM id_map))", key)
            v = self.con.execute(
                "SELECT id FROM id_map WHERE kind=? AND source=? AND no=?", key).fetchone()[0]
            self.cache[key] = v
        return v


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


DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")


def session_date(path, mtime):
    """日期取文件名里的 YYYY-MM-DD，取不到就用文件 mtime 的日期。

    旧实现硬切 basename[8:18]，只对 `rollout-YYYY-MM-DDTHH-...` 这一种命名成立；
    换成别的命名（或非 Codex 适配器的日志）会静默得到空日期 —— 时间线里显示
    "无日期"、--since 过滤名存实亡（2026-09-26 修）。
    """
    m = DATE_IN_NAME_RE.search(os.path.basename(path))
    if m:
        return m.group(1)
    try:
        return time.strftime("%Y-%m-%d", time.localtime(mtime))
    except (OSError, OverflowError, ValueError):
        return ""


def fallback_sid(path):
    """没写 session_meta 时的兜底 id：取文件名里的 UUID；没有就取整个词干。

    旧实现切 basename 末 40 字符 —— UUID 是 36 字符，切出来的是"时间戳尾巴 + UUID"
    的混合串，既不是 UUID 也不稳定（2026-09-26 修）。
    """
    base = os.path.splitext(os.path.basename(path))[0]
    m = UUID_RE.search(base)
    return m.group(1) if m else base[-64:]


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
                    # 拼接**全部**文本分段：旧实现只取第一段就 break，多段消息
                    # （文本 + 图片说明 + 补充段落）的后半截会被静默丢掉
                    txt = "\n".join(
                        c["text"] for c in (p.get("content") or [])
                        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
                        and c.get("text"))
                    if not txt.strip():
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
    # 兜底 sid 不能带扩展名（旧实现直接切 basename 末 40 字符 -> "…ffff.jsonl" 这种脏 id）
    return {"cwd": cwd, "sid": sid or fallback_sid(path),
            "turns": turns, "traces": traces}


def main():
    ap = argparse.ArgumentParser(description="会话日志 -> SQLite")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--sessions", default=SESSIONS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    # --fix（audit.py）专用：重灌内容时**不许删内容**。孤儿清理本身是设计内的 GC，
    # 但它的判据是"文件在 --sessions 下找不到了"，而"找不到"也可能是目录被轮转 /
    # 网络盘没挂载 / 改名 —— 那种情况下重灌会把库整个清空，且清空后索引自洽、
    # 审计照样 RESULT=PASS（2026-09-26 复核实测）。--fix 只想修索引，不该冒这个险；
    # 该删的孤儿留给下一次常规 digest（hook 每次会话启动都会跑）去清。
    ap.add_argument("--keep-orphans", action="store_true",
                    help="跳过孤儿清理（--fix 用：宁可留着旧行，也不冒'目录不在=删库'的险）")
    args = ap.parse_args()

    idx = os.path.join(args.data, "index")
    os.makedirs(idx, exist_ok=True)
    db = os.path.join(idx, "sessions.db")

    # 并发第一道闸：跨进程互斥。两个 digest 并发写同一张表会丢数据 + 损坏 FTS 索引，
    # 且损坏是**静默**的（行数对账照样相等）。拿不到锁就跳过本轮，下一轮补齐。
    lock = RunLock(os.path.join(idx, ".digest.lock"))
    if not lock.acquire():
        print("另一个 session-digest 正在写库，本轮跳过（索引下一轮补齐）")
        return 0
    try:
        return run(args, db, idx)
    finally:
        lock.release()


def run(args, db, idx):
    con = sqlite3.connect(db, timeout=30)
    # 显式控制事务边界：下面的 id 分配必须和写入处在同一个写事务里才不会被并发插入挤掉
    con.isolation_level = None
    # 并发与崩溃健壮性（2026-09-23 盲测 #4 / 深夜 hot journal 实况）：
    # WAL 让读写不互斥；busy_timeout 遇锁等待而不是立刻抛 "database is locked"。
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    # REPLACE 冲突删除的行也要触发删除触发器，否则 FTS5 索引会留下幽灵项
    con.execute("PRAGMA recursive_triggers=ON")
    upgraded = ensure_schema(con)
    if upgraded:
        args.full = True          # 结构升级 -> 必须全量重灌（内容可从 jsonl 重建）
        print("schema 升级到 v" + str(SCHEMA_VERSION) + "：全量重建内容")

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
            continue
        sid = rec["sid"]
        date = session_date(p, mtime)
        # 每个文件一个写事务（BEGIN IMMEDIATE = 立刻拿写锁，避免升级锁时才发现冲突）
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM turns WHERE source=?", (rel,))
            con.execute("DELETE FROM traces WHERE source=?", (rel,))
            for no, (ls, le, q, a) in enumerate(rec["turns"], 1):
                # 跨 source 去重（盲测 #6）：resume 会产生内容重叠的多个 rollout。
                # 判据必须是**内容相同**才删——旧实现只看 (session_id, turn_no)，
                # 分叉会话在同一个序号上是不同的真实轮次，会被整条吃掉（丢真实数据）。
                con.execute("DELETE FROM turns WHERE session_id=? AND turn_no=? "
                            "AND question=? AND answer=?", (sid, no, q, a))
                # 显式 INSERT（不用 INSERT OR REPLACE）：REPLACE 的冲突删除在
                # recursive_triggers=OFF 时不触发删除触发器，会让 FTS 索引残留幽灵项
                con.execute("INSERT INTO turns "
                            "(id, session_id, workspace, date, turn_no, src_line_start, src_line_end, source, question, answer) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (alloc.get("turn", rel, no), sid, rec["cwd"], date, no, ls, le, rel, q, a))
            for seq, kind, txt, ln in rec["traces"]:
                con.execute("INSERT INTO traces "
                            "(id, session_id, seq, kind, text, src_line, source) VALUES (?,?,?,?,?,?,?)",
                            (alloc.get("trace", rel, seq), sid, seq, kind, txt, ln, rel))
            con.execute("INSERT OR REPLACE INTO file_state VALUES (?,?,?)", (rel, mtime, size))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        written += 1
        n_turns += len(rec["turns"])
        n_traces += len(rec["traces"])

    # 孤儿清理：源文件已消失（删除/改名）的记录一并清掉，索引不会指向不存在的会话。
    # 删除前**再确认文件真的不在**：本轮的文件列表是启动时的快照，如果同时有另一个
    # 进程刚索引了新出现的会话文件，只按快照判断会把这个新会话的行整段删掉。
    orphans = 0
    for (rel,) in con.execute("SELECT rel FROM file_state").fetchall():
        if rel in existing:
            continue
        if os.path.exists(os.path.join(args.sessions, rel.replace("/", os.sep))):
            continue
        if args.keep_orphans:
            continue
        con.execute("DELETE FROM turns WHERE source=?", (rel,))
        con.execute("DELETE FROM traces WHERE source=?", (rel,))
        con.execute("DELETE FROM file_state WHERE rel=?", (rel,))
        orphans += 1

    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    if written or orphans:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('content_updated_at', datetime('now'))")

    # 记录"这次是拿哪个目录建的库"。audit.py --fix 会拿它跟 --sessions 比对：不一致就
    # 拒绝重灌。没有这个记录时 --fix 只能靠"来源是否重叠"猜，而**部分重叠**（未同步完
    # 的镜像 / 只恢复了一个月的机器）会让孤儿清理静默删掉其余来源、审计照样 PASS
    # （2026-09-26 复核实测：1/5 重叠 -> 删 4 个源、rc=0、RESULT=PASS）。
    root_abs = os.path.abspath(args.sessions)
    row = con.execute("SELECT v FROM meta WHERE k='sessions_root'").fetchone()
    if not row or row[0] != root_abs:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('sessions_root', ?)", (root_abs,))

    if args.full or upgraded:
        # 全量重灌/结构升级会把库撑大，两步都要做：
        #   ① optimize：逐行 DELETE+INSERT 会把 trigram 索引切成一堆小段，FTS5 只在提交时
        #      按有限预算合并，段一多**在用页**就长期虚胖（真实库实测 94.9 -> 151.8 MB 在用，
        #      optimize 后回到 91.9 MB —— 内容只多 68 轮，涨的全是索引碎片）。
        #   ② VACUUM：optimize 合并后腾出的页仍留在文件里（DROP/重建同理），只有 VACUUM
        #      才会缩文件（实测 155.9 MB 的文件里 64 MB 是空闲页）。
        # 两步都不碰内容表，失败也不算错误 —— 索引本身已经建好了。
        for t in ("turns_fts", "traces_fts"):
            if not con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name=?",
                               (t,)).fetchone()[0]:
                continue
            try:
                con.execute("INSERT INTO " + t + "(" + t + ") VALUES('optimize')")
            except sqlite3.OperationalError as e:
                print("warn: " + t + " optimize 跳过（" + str(e) + "）")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
