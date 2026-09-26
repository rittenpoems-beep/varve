#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recall.py — 记忆检索 CLI（由粗到细的漏斗，零 LLM）。

检索顺序（2026-09-23 定稿）：
    【粗记录】records/*.md 命中行        ← 已提炼的推进记录，几乎免费，先给
    【历史】  对话历史 turns_fts         ← 未提炼的原始对话，必要时才看
    【轨迹】  traces_fts（--deep）       ← 工具调用/输出，"当时具体怎么做的"

用法：
  python -X utf8 recall.py "并发写入"
  python -X utf8 recall.py "hook" "SessionStart" "静默失败"        # 多变体一次调用
  python -X utf8 recall.py "报错" --deep                            # 加搜轨迹层
  python -X utf8 recall.py --topic "快照流"                          # 主题时间线（演化型：三源合并，按时间排开）
  python -X utf8 recall.py --timeline --since 14d                   # 会话时间线（按日期列会话）
  python -X utf8 recall.py "worktree" --workspace D:/proj --json

输出末尾的【覆盖】行报告"命中总量 / 展示量"——**未展示不等于不存在**，程序按相关性截断。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")
RRF_K = 60
# 快照流标记（与 varve_hooks_common.py 同源）：--topic 时间线要读 L2 快照序列
SNAPSHOT_MARK = "<!-- ===== 快照 ===== -->"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from varve_hooks_common import snapshot_marks      # 代码区里的标记不算真快照
except Exception:                                       # 兜底：独立运行时不致命
    def snapshot_marks(text):
        return [m.start() for m in re.finditer(re.escape(SNAPSHOT_MARK), text)]


def fts_quote(v):
    """把自然语言查询转成 FTS5 表达式：多词 → 各自成短语、按 OR 连接。

    背景（2026-09-23 盲测复现）：trigram 分词下，把 "compact pending" 整串包成
    单个短语，等于要求原文里字面连续出现该串，命中率从 100% 掉到 0% —— 而且是
    **静默** 0 条（看起来像"历史上没这回事"）。拆词 + OR 才符合"回忆关键词"的直觉。

    需要精确短语或原生 FTS5 语法时走 --raw（例如：--raw '"并发 裁决"'）。
    """
    parts = [p for p in v.split() if p]

    def esc(s):
        return '"' + s.replace('"', '""') + '"'

    if not parts:
        return '""'
    if len(parts) == 1:
        return esc(parts[0])
    return " OR ".join(esc(p) for p in parts)


def connect_db(db):
    """打开记忆库：优先只读，失败时降级为可写连接。

    降级场景（2026-09-23 实遇）：库处于 hot journal 状态时（上次写入被杀），
    SQLite 必须拿到写权限才能完成崩溃恢复，纯 mode=ro 会直接报
    "attempt to write a readonly database" —— 此时若不复位，检索会整体不可用。
    本函数返回的连接只用于 SELECT，降级不会修改数据。
    """
    uri = "file:" + db.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()   # 触碰一次，暴露 hot journal
        return con
    except sqlite3.OperationalError as e:
        print("note: 只读打开失败（" + str(e) + "），降级为可写连接读取", file=sys.stderr)
        con = sqlite3.connect(db, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con


# ---------- 粗记录（records 漏斗第一级）----------

def scan_records(data_root, terms, limit=6):
    hits = []
    rdir = os.path.join(data_root, "records")
    if not os.path.isdir(rdir):
        return hits
    for fn in sorted(os.listdir(rdir)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(rdir, fn), encoding="utf-8", errors="replace") as fh:
                for ln, line in enumerate(fh, 1):
                    s = line.strip()
                    if not s or s.startswith("<!--"):
                        continue
                    if any(t in s for t in terms):
                        hits.append((fn, ln, s[:150]))
                        if len(hits) >= limit:
                            return hits
        except OSError:
            continue
    return hits


# ---------- 主题时间线（--topic，演化型查询）----------

def scan_snapshots(data_root, topic, limit=40):
    """L2 快照流里含 topic 的条目 -> [(date, label, text)]。

    只保留**最新**的 limit 条（2026-09-26 修）：快照是追加写的，从头往下取到上限
    等于"只保留最旧的"，最新的状态反而全被丢掉 —— 与"最新快照优先"的设计正好相反。
    """
    path = os.path.join(data_root, "STATUS.md")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return []
    marks = snapshot_marks(txt)
    out = []
    for i, pos in enumerate(marks, 1):
        end = marks[i] if i < len(marks) else len(txt)
        seg = txt[pos + len(SNAPSHOT_MARK):end]
        m = DATE_RE.search(seg)
        date = m.group(1) if m else ""
        for ln in seg.splitlines():
            s = ln.strip()
            if not s or topic not in s:
                continue
            if s.startswith("- ") or s.startswith("**"):
                out.append((date, "快照#" + str(i), s[:150]))
    return out[-limit:]


def scan_records_dated(data_root, topic, limit=40):
    """records 命中 + 用上文最近的 ### 日期标题补日期 -> [(date, label, text)]。

    同样只保留**最新**的 limit 条：records 是追加写的，正序取到上限会先丢最新记录。
    """
    rdir = os.path.join(data_root, "records")
    if not os.path.isdir(rdir):
        return []
    out = []
    for fn in sorted(os.listdir(rdir)):
        if not fn.endswith(".md"):
            continue
        date = ""
        try:
            with open(os.path.join(rdir, fn), encoding="utf-8", errors="replace") as fh:
                for ln, line in enumerate(fh, 1):
                    s = line.strip()
                    if s.startswith("### "):
                        m = DATE_RE.search(s)
                        if m:
                            date = m.group(1)
                        continue
                    if s and not s.startswith("<!--") and topic in s:
                        out.append((date, fn + ":" + str(ln), s[:150]))
        except OSError:
            continue
    return out[-limit:]


def scan_turns_dated(con, topic, workspace, since, limit=60):
    """L3 命中 -> [(date, label, text)]。"""
    rows, _total = run_variant(con, topic, workspace, since, False, limit)
    out = []
    for sid, ws, date, turn, ls, le, source, score, snip in rows:
        out.append((date or "", "会话" + sid[:8] + " turn" + str(turn),
                    (snip or "").replace("\n", " ")[:150]))
    return out


def print_topic_timeline(data_root, con, topic, workspace, since):
    """演化型查询：按主题拉全部线索，按时间升序排开（设计出处：索引规格 §3.6）。

    每源有上限（快照/records 各 40、会话 60），且**达到上限时保留最新的**。
    命中触顶必须**明说**——否则用户会把"截断后的视图"误当成"全部线索"（2026-09-26 修）。
    """
    t0 = time.time()
    snaps = scan_snapshots(data_root, topic)
    recs = scan_records_dated(data_root, topic)
    turns = scan_turns_dated(con, topic, workspace, since)
    items = snaps + recs + turns
    items.sort(key=lambda x: (x[0] or "9999-99-99"))
    elapsed = round(time.time() - t0, 3)
    print("【主题时间线】" + topic + "   共 " + str(len(items)) + " 条线索（时间升序）   "
          + str(elapsed) + "s")
    if not items:
        print("  （三个来源——L2 快照 / records / 对话历史——均无命中；"
              "可能是词不对，也可能是真没有）")
        return 0
    for date, label, text in items:
        print("  " + (date or "无日期") + "  " + label + "  " + text)
    print("")
    print("【覆盖】三源合并：L2 快照 " + str(len(snaps)) + " / records " + str(len(recs))
          + " / 对话历史 " + str(len(turns)) + " 条；按主题**逐字匹配**——"
          "无共同词的关联线索不会被本视图捞到。")
    capped = []
    for name, n, cap, flt in (("L2 快照", len(snaps), 40, "（每源上限 40）"),
                              ("records", len(recs), 40, "（每源上限 40）"),
                              ("对话历史", len(turns), 60, "（每源上限 60）")):
        if n >= cap:
            capped.append(name + flt)
    if capped:
        print("  注意：" + " / ".join(capped) + " 已达上限，**只保留了最新的**——"
              "这不是全部线索。收窄主题词可看到更早的部分。")
    return 0


# ---------- 对话历史（turns_fts）----------

def run_variant(con, variant, workspace, since, raw, limit=200):
    """返回 (rows, total)；rows = [(sid, ws, date, turn, ls, le, source, score, snip)]。

    total = 该变体的**总命中块数**（不截断）——供【覆盖】行报告完整度。

    按**词**分流（2026-09-26 修）：trigram 分词器的匹配下限是 3 字符，2 字中文词
    在 FTS 路径下**恒为 0 命中**（静默）。旧实现只看整个参数字符串的长度，于是
    "压缩 修复"（长度 5）走 FTS、两个词各自被丢 -> 0 条且无任何提示。现在逐词判定：
    <3 字的词走字面兜底，>=3 字的走 FTS，两路结果并集（FTS 在前，保相关性排序）。
    """
    if raw:                                   # --raw = 原生 FTS5 语法，整串交给 FTS
        return _run_fts(con, variant, workspace, since, True, limit)
    words = [w for w in variant.split() if w]
    if not words:
        return [], 0
    long_fts = [w for w in words if len(w) >= 3]
    short_lit = [w for w in words if len(w) < 3]
    rows, total, seen = [], 0, set()
    paths = []
    if long_fts:
        paths.append(_run_fts(con, " ".join(long_fts), workspace, since, False, limit))
    if short_lit:
        paths.append(run_like(con, short_lit, workspace, since, limit))
    for part, t in paths:
        total += t
        for row in part:
            key = (row[0], row[6], row[3])     # (sid, source, turn) —— 与 aggregate 同键
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows[:limit], total


def _run_fts(con, variant, workspace, since, raw, limit=200):
    """FTS5 路径（变体已保证 >=3 字符，或 --raw 原生语法）。返回 (rows, total)。"""
    if not raw and re.search(r"\b(AND|OR|NOT|NEAR)\b", variant):
        print("warn: 查询含 FTS5 语法关键词但未加 --raw，已按普通词拆分处理: " + variant, file=sys.stderr)
    q = variant if raw else fts_quote(variant)
    base = ("FROM turns_fts JOIN turns t ON t.rowid = turns_fts.rowid "
            "WHERE turns_fts MATCH ?")
    params = [q]
    if workspace:
        base += " AND t.workspace LIKE ?"
        params.append("%" + workspace + "%")
    if since:
        base += " AND (t.date = '' OR t.date >= ?)"
        params.append(since)
    try:
        total = con.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        sql = ("SELECT t.session_id, t.workspace, t.date, t.turn_no, t.src_line_start, t.src_line_end, "
               "t.source, bm25(turns_fts, 5.0, 1.0) AS s, "
               "snippet(turns_fts, -1, '[', ']', '…', 20) " + base + " ORDER BY s LIMIT ?")
        return con.execute(sql, params + [limit]).fetchall(), total
    except sqlite3.OperationalError as e:
        print("warn: 查询失败 [" + q + "]: " + str(e), file=sys.stderr)
        return [], 0


def run_like(con, terms, workspace, since, limit):
    """短词（<3 字符，多为中文双字）字面兜底。terms 为词表，任一命中即算。返回 (rows, total)。

    trigram 分词器的最小单位是 3 字符：2 字词走 FTS 恒为 0 命中。该路径无相关性
    打分（score 固定 -2.0），排序按新近度 —— 盲测显示：目标几乎都在候选集里，
    丢的只是排序（中文双字词在默认 limit=5 下全 MISS）。
    片段锚点取**所有词中最早出现的位置**，避免多词时锚在没命中的那个词上。
    """
    if isinstance(terms, str):
        terms = [terms]
    terms = [t for t in terms if t]
    if not terms:
        return [], 0
    txt = "COALESCE(question,'') || ' ' || COALESCE(answer,'')"
    where = "WHERE (" + " OR ".join("(question LIKE ? OR answer LIKE ?)" for _ in terms) + ")"
    params = []
    for t in terms:
        params += ["%" + t + "%", "%" + t + "%"]
    if workspace:
        where += " AND workspace LIKE ?"
        params.append("%" + workspace + "%")
    if since:
        where += " AND (date = '' OR date >= ?)"
        params.append(since)
    if len(terms) == 1:
        anchor = "NULLIF(instr(" + txt + ",?),0)"
    else:
        # 注意：SQLite 的**多参数**标量 min() 只要有一个参数是 NULL 就返回 NULL
        # （与聚合 min 忽略 NULL 的行为相反）——所以每个词先 COALESCE 成大哨兵值，
        # 否则"只命中其中一词"的行会拿到 NULL 片段，展示时直接崩（2026-09-26 实测）。
        anchor = "min(" + ",".join(
            "COALESCE(NULLIF(instr(" + txt + ",?),0),1000000000)" for _ in terms) + ")"
    try:
        total = con.execute("SELECT COUNT(*) FROM turns " + where, params).fetchone()[0]
        sql = ("SELECT session_id, workspace, date, turn_no, src_line_start, src_line_end, source, "
               "-2.0 AS s, "
               "substr(" + txt + ", max(1, " + anchor + " - 40), 160) "
               "FROM turns " + where +
               " ORDER BY (date IS NULL OR date = '') ASC, date DESC, turn_no DESC LIMIT ?")
        return con.execute(sql, terms + params + [limit]).fetchall(), total
    except sqlite3.OperationalError as e:
        print("warn: 字面查询失败 [" + " ".join(terms) + "]: " + str(e), file=sys.stderr)
        return [], 0


# ---------- 轨迹层（traces_fts，--deep）----------

def run_traces(con, variant, limit=60):
    """轨迹层检索（--deep）。

    跨 source 重叠去重（2026-09-24）：resume 会话会产生内容重叠的多个 rollout，
    同一条轨迹会以不同 source 重复入库并稀释 BM25。这里按
    (session, kind, 文本前 240 字符) 去重 —— 与 turns 侧的写入去重对称。
    为抵消去重带来的损耗，查询侧多取一些再截断。
    """
    cap = max(limit * 2, limit + 20)
    try:
        if len(variant) < 3:
            rows = con.execute(
                "SELECT session_id, seq, kind, src_line, -2.0 AS s, substr(text, 1, 160) "
                "FROM traces WHERE text LIKE ? LIMIT ?", ("%" + variant + "%", cap)).fetchall()
        else:
            rows = con.execute(
                "SELECT tr.session_id, tr.seq, tr.kind, tr.src_line, bm25(traces_fts) AS s, "
                "snippet(traces_fts, -1, '[', ']', '…', 16) "
                "FROM traces_fts JOIN traces tr ON tr.rowid = traces_fts.rowid "
                "WHERE traces_fts MATCH ? ORDER BY s LIMIT ?",
                (fts_quote(variant), cap)).fetchall()
    except sqlite3.OperationalError as e:
        print("warn: 轨迹查询失败 [" + variant + "]: " + str(e), file=sys.stderr)
        return []
    seen, out = set(), []
    for row in rows:
        key = (row[0], row[2], (row[5] or "")[:240])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:limit]


def since_to_date(since):
    if not since:
        return ""
    import datetime
    m = re.match(r"^(\d+)d$", since)
    if m:
        return (datetime.date.today() - datetime.timedelta(days=int(m.group(1)))).isoformat()
    try:
        datetime.date.fromisoformat(since)
        return since
    except ValueError:
        raise SystemExit("--since 需要 Nd（如 7d）或合法 YYYY-MM-DD，收到: " + since)


def aggregate(results_by_variant, limit):
    """RRF 融合 + 会话聚合（每会话只计 top-3 块）。"""
    block_rrf, block_meta = {}, {}
    for rows in results_by_variant:
        for rank, row in enumerate(rows, 1):
            key = (row[0], row[6], row[3])         # (sid, source, turn) —— 分片安全
            block_rrf[key] = block_rrf.get(key, 0.0) + 1.0 / (RRF_K + rank)
            block_meta.setdefault(key, row)
    per_session = {}
    for key, sc in block_rrf.items():
        per_session.setdefault(key[0], []).append((sc, key))
    sessions = []
    for sid, items in per_session.items():
        items.sort(reverse=True)
        top = items[:3]
        row = block_meta[top[0][1]]
        sessions.append({
            "session_id": sid, "workspace": row[1], "date": row[2],
            "score": round(sum(sc for sc, _ in top), 5), "hits": len(items),
            # 盲测 #14：该字段是命中片段预览（FTS snippet），不是"标题"——改名以免误导
            "preview": re.sub(r"\s+", " ", block_meta[top[0][1]][8] or "")[:60],
            "snippets": [{"turn": k[2], "src": [block_meta[k][4], block_meta[k][5]],
                          "text": block_meta[k][8] or ""} for _, k in top],
        })
    sessions.sort(key=lambda x: -x["score"])
    return sessions[:limit]


def main():
    ap = argparse.ArgumentParser(description="Varve 记忆检索")
    ap.add_argument("queries", nargs="*", help="一个或多个查询变体")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--workspace", default="")
    ap.add_argument("--since", default="", help="7d / 30d / YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=5, help="会话数上限")
    ap.add_argument("--deep", action="store_true", help="并查轨迹层（工具调用/输出）")
    ap.add_argument("--no-records", action="store_true", help="跳过粗记录扫描")
    ap.add_argument("--raw", action="store_true", help="查询按 FTS5 原生语法传入")
    ap.add_argument("--timeline", action="store_true", help="时间线视图（按日期列会话）")
    ap.add_argument("--topic", default="", help="主题时间线：按主题拉全部线索并按时间排开（演化型查询）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    db = os.path.join(args.data, "index", "sessions.db")
    if not os.path.exists(db):
        print("库不存在：" + db + " —— 先跑 session-digest.py + build-search-index.py")
        return 2
    sid_filter = since_to_date(args.since)
    con = connect_db(db)
    t0 = time.time()

    if args.stats:
        a = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        b = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        print("turns=" + str(a) + " traces=" + str(b))
        con.close()
        return 0

    if args.topic:
        rc = print_topic_timeline(args.data, con, args.topic, args.workspace, sid_filter)
        con.close()
        return rc

    if args.timeline or not args.queries:
        # MAX(date)：GROUP BY 下裸列 date 是**任取一行**的值（跨天会话会显示成启动那天，
        # 排序也跟着乱）。取该会话最新一轮的日期才是"最近活动"（2026-09-26 修）。
        rows = con.execute(
            "SELECT MAX(date) AS d, workspace, session_id, COUNT(*) FROM turns "
            "WHERE (date = '' OR date >= ?) GROUP BY session_id ORDER BY d DESC LIMIT ?",
            (sid_filter or "0000-00-00", args.limit)).fetchall()
        for d, ws, sid, c in rows:
            title = con.execute("SELECT question FROM turns WHERE session_id=? ORDER BY turn_no LIMIT 1",
                                (sid,)).fetchone()
            print((d or "?") + "  " + ws + "\n    " + ((title[0] or "")[:56] if title else "")
                  + "   [" + sid[:8] + " " + str(c) + "轮]")
        print("timeline: " + str(len(rows)) + " 会话  " + str(round(time.time() - t0, 2)) + "s")
        con.close()
        return 0

    variants = list(args.queries)
    pairs = [run_variant(con, v, args.workspace, sid_filter, args.raw) for v in variants]
    relaxed = False
    if not any(rows for rows, _ in pairs):
        relaxed = True
        pairs = [run_variant(con, v, "", "", args.raw) for v in variants]
    results = [rows for rows, _ in pairs]
    total_hits = sum(t for _, t in pairs)          # 总命中块数（不截断）
    total_blocks = sum(len(r) for r in results)    # 本轮取回块数（受 limit 限制）
    sessions = aggregate(results, args.limit)
    rec_hits = [] if args.no_records else scan_records(args.data, variants)
    deep_hits = []
    if args.deep:
        seen = set()
        for v in variants:
            for row in run_traces(con, v):
                k = (row[0], row[1])
                if k not in seen:
                    seen.add(k)
                    deep_hits.append(row)
        deep_hits = deep_hits[:8]
    elapsed = round(time.time() - t0, 3)

    if args.json:
        print(json.dumps({
            "queries": variants, "relaxed": relaxed,
            "stats": {"blocks": total_blocks, "sessions": len(sessions),
                      "records": len(rec_hits), "traces": len(deep_hits), "elapsed_s": elapsed},
            "coverage": {"matched_blocks": total_hits, "taken_blocks": total_blocks,
                         "shown_sessions": len(sessions),
                         "shown_snippets": sum(len(s["snippets"]) for s in sessions),
                         "records_shown": len(rec_hits)},
            "records": [{"file": f, "line": l, "text": t} for f, l, t in rec_hits],
            "sessions": sessions,
            "deep": [{"session_id": r[0], "seq": r[1], "kind": r[2], "src_line": r[3], "text": r[5]}
                     for r in deep_hits],
        }, ensure_ascii=False, indent=2))
        con.close()
        return 0

    tag = "（已放宽：忽略过滤器）" if relaxed else ""
    print("查询: " + " | ".join(variants) + tag + "   " + str(elapsed) + "s")
    if rec_hits:
        print("")
        print("【粗记录】records 命中 " + str(len(rec_hits)) + " 处（已提炼，先看这个）")
        for f, ln, tx in rec_hits:
            print("  " + f + ":" + str(ln) + "  " + tx)
    print("")
    print("【历史】" + str(total_blocks) + " 块 / " + str(len(sessions)) + " 会话")
    for i, s in enumerate(sessions, 1):
        print("[" + str(i) + "] " + (s["date"] or "?") + " · " + s["workspace"]
              + " · score=" + str(s["score"]) + " · " + str(s["hits"]) + "块")
        print("    " + (s["preview"] or "(无片段)") + "   [" + s["session_id"][:8] + "]")
        for sn in s["snippets"]:
            print("    ├ turn " + str(sn["turn"]) + "  src:" + str(sn["src"][0]) + "-" + str(sn["src"][1]))
            print("    │  " + sn["text"].replace("\n", " ")[:180])
    if args.deep:
        print("")
        print("【轨迹】命中 " + str(len(deep_hits)) + " 条（工具调用/输出）")
        for sid, seq, kind, src, score, snip in deep_hits[:6]:
            print("  [" + kind + "] " + sid[:8] + " seq=" + str(seq) + " 行" + str(src))
            print("      " + (snip or "").replace("\n", " ")[:170])
    shown_snips = sum(len(s["snippets"]) for s in sessions)
    print("")
    print("【覆盖】历史命中 " + str(total_hits) + " 块 / 本轮取回 " + str(total_blocks)
          + " 块 -> 展示 " + str(len(sessions)) + " 会话 / " + str(shown_snips) + " 片段"
          + "；records " + str(len(rec_hits)) + " 行（上限 6）")
    print("  未展示不等于不存在：程序按相关性截断。要完整线索，用 --topic <主题> 拉时间线，"
          "或收窄查询（加词 / --workspace / --since）。")
    # --raw 下整串按 FTS5 原生语法送进 FTS，短词**没有**走字面兜底：这里一律照报就是
    # 句假话（例如 `--raw "a OR b"` 会声称 a/b 走了字面匹配）。旧实现判的是整个变体的
    # 长度、与 run_variant 的路由一致，改成逐词分流后这层对应关系断了，2026-09-26 复核发现。
    short = [] if args.raw else sorted({w for q in variants for w in q.split() if 0 < len(w) < 3})
    if short:
        print("")
        print("（<3 字符的词已走字面兜底（trigram 分词器下限 3 字符）：" + " ".join(short) + "）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
