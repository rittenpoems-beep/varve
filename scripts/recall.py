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
  python -X utf8 recall.py --timeline --since 14d                   # 时间线
  python -X utf8 recall.py "worktree" --workspace D:/proj --json
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


# ---------- 对话历史（turns_fts）----------

def run_variant(con, variant, workspace, since, raw, limit=200):
    """返回 [(sid, ws, date, turn, ls, le, source, score, snip)]。"""
    if len(variant) < 3:                      # 短词（多为中文双字）-> 字面兜底
        return run_like(con, variant, workspace, since, limit)
    if not raw and re.search(r"\b(AND|OR|NOT|NEAR)\b", variant):
        print("warn: 查询含 FTS5 语法关键词但未加 --raw，已按普通词拆分处理: " + variant, file=sys.stderr)
    q = variant if raw else fts_quote(variant)
    sql = ("SELECT t.session_id, t.workspace, t.date, t.turn_no, t.src_line_start, t.src_line_end, "
           "t.source, bm25(turns_fts, 5.0, 1.0) AS s, "
           "snippet(turns_fts, -1, '[', ']', '…', 20) "
           "FROM turns_fts JOIN turns t ON t.rowid = turns_fts.rowid "
           "WHERE turns_fts MATCH ?")
    params = [q]
    if workspace:
        sql += " AND t.workspace LIKE ?"
        params.append("%" + workspace + "%")
    if since:
        sql += " AND (t.date = '' OR t.date >= ?)"
        params.append(since)
    sql += " ORDER BY s LIMIT ?"
    params.append(limit)
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print("warn: 查询失败 [" + q + "]: " + str(e), file=sys.stderr)
        return []


def run_like(con, term, workspace, since, limit):
    """短词（<3 字符，多为中文双字）字面兜底。

    该路径无相关性打分（score 固定 -2.0），排序按新近度 —— 盲测显示：目标几乎
    都在候选集里，丢的只是排序（"请求"83 行、"技能"35 行，默认 limit=5 全 MISS）。
    更贴合的做法是按命中位置/次数打分，先以最小改动（新近度）兜住可用性。
    """
    sql = ("SELECT session_id, workspace, date, turn_no, src_line_start, src_line_end, source, "
           "-2.0 AS s, "
           "substr(COALESCE(question,'') || ' ' || COALESCE(answer,''), "
           "max(1, instr(COALESCE(question,'') || ' ' || COALESCE(answer,''), ?) - 40), 160) "
           "FROM turns WHERE (question LIKE ? OR answer LIKE ?)")
    params = [term, "%" + term + "%", "%" + term + "%"]
    if workspace:
        sql += " AND workspace LIKE ?"
        params.append("%" + workspace + "%")
    if since:
        sql += " AND (date = '' OR date >= ?)"
        params.append(since)
    sql += " ORDER BY (date IS NULL OR date = '') ASC, date DESC, turn_no DESC LIMIT ?"
    params.append(limit)
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print("warn: 字面查询失败 [" + term + "]: " + str(e), file=sys.stderr)
        return []


# ---------- 轨迹层（traces_fts，--deep）----------

def run_traces(con, variant, limit=60):
    q = variant if len(variant) >= 3 else variant           # 短词走 LIKE
    try:
        if len(variant) < 3:
            return con.execute(
                "SELECT session_id, seq, kind, src_line, -2.0 AS s, substr(text, 1, 160) "
                "FROM traces WHERE text LIKE ? LIMIT ?", ("%" + variant + "%", limit)).fetchall()
        return con.execute(
            "SELECT tr.session_id, tr.seq, tr.kind, tr.src_line, bm25(traces_fts) AS s, "
            "snippet(traces_fts, -1, '[', ']', '…', 16) "
            "FROM traces_fts JOIN traces tr ON tr.rowid = traces_fts.rowid "
            "WHERE traces_fts MATCH ? ORDER BY s LIMIT ?", (fts_quote(q), limit)).fetchall()
    except sqlite3.OperationalError as e:
        print("warn: 轨迹查询失败 [" + q + "]: " + str(e), file=sys.stderr)
        return []


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
                          "text": block_meta[k][8]} for _, k in top],
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
    ap.add_argument("--timeline", action="store_true", help="时间线视图")
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

    if args.timeline or not args.queries:
        rows = con.execute(
            "SELECT date, workspace, session_id, COUNT(*) FROM turns "
            "WHERE (date = '' OR date >= ?) GROUP BY session_id ORDER BY date DESC LIMIT ?",
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
    results = [run_variant(con, v, args.workspace, sid_filter, args.raw) for v in variants]
    relaxed = False
    if not any(results):
        relaxed = True
        results = [run_variant(con, v, "", "", args.raw) for v in variants]
    total_blocks = sum(len(r) for r in results)
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
    short = [q for q in variants if len(q) < 3]
    if short:
        print("")
        print("（<3 字符变体已走字面匹配：" + " ".join(short) + "）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
