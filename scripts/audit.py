#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit.py — 记忆库审计（纯程序、无 LLM；建议每月或大改后跑）。

检查项：
  A 内容一致性：file_state 与 turns/traces 的 source 集合是否对齐（孤儿 / 幽灵）
  B 重复入库：同一 (session_id, turn_no) 是否来自多个 source（resume 重叠 rollout）
  C 索引新鲜度：index_built_at 是否落后于 content_updated_at
  D 检索自检：从库内抽样记录反查自身，验证 FTS 通路可用
  E 体量概览：库大小 / turns / traces / 最长文本

用法：python -X utf8 audit.py [--data <dir>] [--json]
退出码：0 = 全部通过；1 = 发现问题
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")


def main():
    ap = argparse.ArgumentParser(description="Varve 记忆库审计")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--samples", type=int, default=3, help="检索自检抽样条数")
    args = ap.parse_args()

    db = os.path.join(args.data, "index", "sessions.db")
    if not os.path.exists(db):
        print("库不存在：" + db)
        return 1

    issues = []
    report = {}
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")

    # A. 内容一致性
    src_turns = {r[0] for r in con.execute("SELECT DISTINCT source FROM turns")}
    src_traces = {r[0] for r in con.execute("SELECT DISTINCT source FROM traces")}
    files = {r[0] for r in con.execute("SELECT rel FROM file_state")}
    orphan_turns = sorted(src_turns - files)
    ghost = sorted(files - (src_turns | src_traces))
    report["A_sources"] = {"turns": len(src_turns), "traces": len(src_traces),
                           "file_state": len(files), "orphan_turns": len(orphan_turns),
                           "ghost_files": len(ghost)}
    if orphan_turns:
        issues.append("A: %d 个 source 有 turns 但不在 file_state（孤儿）：%s"
                      % (len(orphan_turns), orphan_turns[:3]))
    # 注意：file_state 有条目但无 turns/traces 是**正常**的——空会话/无内容文件也会记录签名，
    # 以免每次重复解析。因此这里只作为提示，不算问题。
    report["A_note"] = "empty_files(no turns/traces): %d（正常：空会话签名）" % len(ghost)

    # B. 重复入库
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT session_id, turn_no FROM turns "
        "GROUP BY session_id, turn_no HAVING COUNT(DISTINCT source) > 1)").fetchone()[0]
    report["B_duplicate_turns"] = dup
    if dup:
        issues.append("B: %d 个 (session_id, turn_no) 来自多个 source（resume 重叠入库）" % dup)

    # C. 索引新鲜度
    def meta(k):
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    built, updated = meta("index_built_at"), meta("content_updated_at")
    report["C_index"] = {"built": built, "content_updated": updated,
                         "stale": bool(built and updated and built != updated)}
    if built and updated and built != updated:
        issues.append("C: 索引落后于内容（built=%s / content=%s）→ 跑 build-search-index.py"
                      % (built, updated))

    # D. 检索自检（抽样反查）
    hit = miss = 0
    rows = con.execute("SELECT question FROM turns WHERE LENGTH(question) >= 8 "
                       "ORDER BY RANDOM() LIMIT ?", (args.samples,)).fetchall()
    for (q,) in rows:
        key = q.strip()[:8]
        try:
            n = con.execute("SELECT COUNT(*) FROM turns_fts WHERE turns_fts MATCH ?",
                            ('"' + key.replace('"', '""') + '"',)).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        if n:
            hit += 1
        else:
            miss += 1
    report["D_recall_selftest"] = {"sampled": len(rows), "hit": hit, "miss": miss}
    if miss:
        issues.append("D: 检索自检 %d/%d 未命中（FTS 通路可疑）" % (miss, len(rows)))

    # E. 体量
    t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    tr = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    report["E_size"] = {"db_mb": round(os.path.getsize(db) / 1e6, 1), "turns": t, "traces": tr}
    con.close()

    # F. L2 体积（防注入膨胀：滚动窗口应保持有界）
    l2 = os.path.join(args.data, "STATUS.md")
    if os.path.exists(l2):
        txt = open(l2, encoding="utf-8", errors="replace").read()
        m = re.search(r"<!-- =+ 工程状态区.*?<!-- =+ 工程状态区 结束", txt, re.DOTALL)
        seg = m.group(0) if m else txt
        report["F_l2"] = {"chars": len(seg), "budget": 3500}
        if len(seg) > 3500:
            issues.append("F: 全局卡工程状态区 %d 字符 > 预算 3500（注入会被截断；细节移进 records）" % len(seg))
    else:
        report["F_l2"] = {"chars": 0, "budget": 3500, "note": "全局卡不存在"}

    if args.json:
        print(json.dumps({"issues": issues, "report": report}, ensure_ascii=False, indent=2))
    else:
        print("== Varve 记忆库审计 ==")
        print("  A 一致性: sources turns=%d traces=%d file_state=%d | 孤儿=%d 幽灵=%d"
              % (len(src_turns), len(src_traces), len(files), len(orphan_turns), len(ghost)))
        print("  B 重复入库: %d" % dup)
        print("  C 索引: built=%s content=%s %s" % (built, updated, "（落后）" if report["C_index"]["stale"] else "（一致）"))
        print("  D 检索自检: %d/%d 命中" % (hit, len(rows)))
        print("  E 体量: %.1f MB | turns=%d traces=%d" % (report["E_size"]["db_mb"], t, tr))
        print("  F 全局卡: %d 字符 / 预算 %d" % (report["F_l2"]["chars"], report["F_l2"]["budget"]))
        print("")
        if issues:
            print("发现 %d 个问题：" % len(issues))
            for i in issues:
                print("  [!] " + i)
        else:
            print("RESULT=PASS（无问题）")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
