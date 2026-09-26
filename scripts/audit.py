#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit.py — 记忆库审计（纯程序、无 LLM；建议每月或大改后跑）。

检查项：
  A 内容一致性：file_state 与 turns/traces 的 source 集合是否对齐（孤儿 / 幽灵）
  B 重复入库：同一 (session_id, turn_no) 在多个 source 上**内容完全相同**（真重复入库；
    同名但内容不同的是分叉会话的真实轮次，FIX-027 明确保留，只作提示不算问题）
  C 索引一致性：触发器在位 + FTS **行集**对账（docsize 影子表）+ **令牌级**完整性
  D 检索自检：从库内抽样记录反查自身，验证 FTS 通路可用
  E 体量概览：库大小 / turns / traces / 最长文本
  F L2 体积：**最新一条快照**是否贴住 3500 护栏（快照流下文件总长不设上限）

C 项为什么改了两次（历史别删，防复发）：
  * 初版比 index_built_at / content_updated_at —— 触发器模式下写入即入索引，两个
    时间戳本就分叉，恒报"落后"（2026-09-24 修）。
  * 二版比 `COUNT(*) FROM turns_fts` 与 `COUNT(*) FROM turns` —— external content
    模式下这是**同义反复**（永远相等），索引损坏时照样 PASS，是假绿（2026-09-26 修）。
  * 现版：影子表 `_docsize` 行集比对（抓"行丢了/幽灵行"）+ `integrity-check(rank=1)`
    （抓令牌级损坏，需可写连接）。详见 FIXES.md FIX-002。

用法：python -X utf8 audit.py [--data <dir>] [--sessions <dir>] [--json] [--fix]
退出码：0 = 全部通过；1 = 发现问题

--fix 的安全边界（2026-09-26，三轮）：它会调 session-digest.py --full 重灌内容，而 digest 的
孤儿清理按 `--sessions` 判定"文件已消失"。所以 --sessions 必须指向**建库时那个目录**：指错了
会把库整个清空，且清空后索引自洽、审计仍报 PASS。
do_fix() 先用 reingest_verdict() 拦截：库内记了建库时的会话根（meta.sessions_root）就要求完全
一致，老库则要求"没有任何来源缺失"—— 判据必须这么严，因为"部分重叠"（含同名路径的部分副本）
照样会让孤儿清理删掉其余来源（第二轮复核实测 1/5 重叠即删 4/5）。修完再比对内容指纹兜底。
第三轮补的是一条**判据拦不住的路**：目录字符串一致但目录本身被轮转 / 卸载 / 改名了，重灌照样
清空（实测 9 行 5 源 → 0 行 0 源、rc=0、RESULT=PASS）。所以 --fix 再给 digest 加 `--keep-orphans`：
索引修复不该有删内容的能力，孤儿交给下一次常规 digest 清。判据 + 无删能力，两层都要。
"""
import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")
# 与 session-digest.py 的 SESSIONS 保持一致（--fix 重灌时要把它透传过去）
SESSIONS_DEFAULT = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
# 快照流标记（与 scripts/varve_hooks_common.py 同源；此处保留兜底定义避免跨模块硬依赖）
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import varve_hooks_common as _C
    SNAPSHOT_MARK = _C.SNAPSHOT_MARK
    snapshot_marks = _C.snapshot_marks          # 代码区里的标记是文档示例，不是真快照
except Exception:
    SNAPSHOT_MARK = "<!-- ===== 快照 ===== -->"

    def snapshot_marks(text):
        return [m.start() for m in re.finditer(re.escape(SNAPSHOT_MARK), text)]


def docsize_rows(con, content, fts):
    """FTS **行集**对账：影子表 `<fts>_docsize` 与内容表的主键集合。

    返回 (content_n, docsize_n, missing, extra)；任一步失败返回 None。
    external content 模式下对 FTS 建索引的每一行都应在 _docsize 里有一行，
    所以"内容有行、docsize 无行"（索引缺行）与"docsize 有行、内容无行"（幽灵）
    都能被抓到 —— 这是 COUNT(*) 对比做不到的。
    """
    try:
        n_c = con.execute("SELECT COUNT(*) FROM " + content).fetchone()[0]
        n_d = con.execute("SELECT COUNT(*) FROM " + fts + "_docsize").fetchone()[0]
        miss = con.execute(
            "SELECT COUNT(*) FROM " + content + " c WHERE NOT EXISTS "
            "(SELECT 1 FROM " + fts + "_docsize d WHERE d.id = c.id)").fetchone()[0]
        extra = con.execute(
            "SELECT COUNT(*) FROM " + fts + "_docsize d WHERE NOT EXISTS "
            "(SELECT 1 FROM " + content + " c WHERE c.id = d.id)").fetchone()[0]
        return n_c, n_d, miss, extra
    except sqlite3.OperationalError:
        return None


def fts_integrity(db, fts):
    """FTS5 令牌级完整性：`integrity-check`(rank=1)。返回 (ok:bool, msg)。

    这是**唯一**能抓出"索引里有指向已删行的幽灵项"的检查（行数、docsize 都可能
    依然一致，但倒排表里的 token 指向不存在的行 → 检索命中读不到的内容）。
    必须用**可写**连接：该命令会写 FTS 内部结构，只读连接直接报
    "attempt to write a readonly database"（2026-09-26 实测）。它不改内容。
    """
    try:
        con = sqlite3.connect(db, timeout=15)
    except sqlite3.Error as e:
        return False, "打不开库: " + str(e)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("INSERT INTO " + fts + "(" + fts + ", rank) VALUES('integrity-check', 1)")
        return True, "ok"
    except sqlite3.DatabaseError as e:
        return False, str(e)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def run_fix(data, sessions):
    """--fix：重灌内容 + 强制重建索引（FTS 损坏的唯一程序化修复手段）。

    顺序有讲究：先 session-digest.py --full 把内容表按真相源对齐（顺带修好
    "丢行"这类内容侧损坏），再 --force 重建两张 FTS（清掉幽灵项）。

    **必须带上 --sessions**：不传就退回默认的 ~/.codex/sessions，而 session-digest
    的孤儿清理会删掉"不在该目录下"的全部 source —— 用别的目录建的库会被整个清空，
    且清空后索引自洽、审计照样报 PASS（2026-09-26 实测：turns 2 → 0 仍 RESULT=PASS）。
    调用前必须先过 reingest_verdict()。

    **还必须带上 --keep-orphans**：--fix 是索引类修复，不该有删内容的能力。只靠
    "会话根字符串一致"拦不住一种情况 —— 那个目录被轮转 / 卸载 / 改名了，字符串照样
    相等（2026-09-26 第三轮验证实测：`--fix --sessions <同一字符串>` 把库从 9 行 5 源
    清成 0 行 0 源，rc=0、RESULT=PASS）。带上 --keep-orphans 后这条路径在结构上就
    删不掉任何东西；真正的孤儿交给下一次常规 digest（hook 每轮都会跑）去清。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    steps = (("session-digest.py", ["--full", "--sessions", sessions, "--keep-orphans"]),
             ("build-search-index.py", ["--force"]))
    for script, extra in steps:
        cmd = [sys.executable, "-X", "utf8", os.path.join(here, script), "--data", data] + extra
        print("  [fix] 运行 " + script + " " + " ".join(extra))
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        text = r.stdout or ""
        out = text.strip().splitlines()
        if out:
            print("        " + out[-1])
        if r.returncode != 0:
            print("        [!] 返回码 " + str(r.returncode) + "：" + (r.stderr or "").strip()[:300])
            return False
        # digest 一个文件都没扫到 = 内容其实**没有**被重灌（目录被轮转 / 卸载 / 改名，
        # 或 --sessions 指到了空目录）。索引那半仍然修好了，但"重灌内容"这半是空转，
        # 必须说出来，否则报告上的"修复后 turns N→N"会被读成"内容已按真相源对齐"。
        if script == "session-digest.py" and "files=0 written=0" in text:
            print("        [!] " + sessions + " 下没扫到任何会话文件：内容未被重灌（只重建了索引）。"
                  "若会话日志已搬走，用 --sessions 指向现位置后重跑。")
    return True


def store_sources(data):
    """库内内容指纹：file_state 的来源集合 + turns/traces 行数（--fix 前后比对用）。

    表还没建（空库/半初始化）时返回空指纹而不是抛异常：那种库恰恰应该让 --fix 走下去
    （digest 会建表并灌入），而不是在测量阶段就崩给人看。
    """
    db = os.path.join(data, "index", "sessions.db")
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        try:
            rels = {r[0] for r in con.execute("SELECT rel FROM file_state")}
            n_t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            n_r = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
            return rels, n_t, n_r
        except sqlite3.OperationalError:
            return set(), 0, 0
    finally:
        con.close()


def store_meta(data, key):
    """读 meta 表里的一个值；表不存在或键缺失都返回 None。"""
    db = os.path.join(data, "index", "sessions.db")
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")
    try:
        try:
            row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row[0] if row else None
    finally:
        con.close()


def same_dir(a, b):
    """两个目录路径是否指向同一处（大小写、斜杠、尾部分隔符都不算差异）。"""
    norm = lambda p: os.path.normcase(os.path.abspath(p)).replace("\\", "/").rstrip("/")
    return norm(a) == norm(b)


def reingest_verdict(data, sessions):
    """重灌前判定：这次 --fix 会不会把库里的来源删掉？返回 (ok, code, detail)。

    两层判据，从严：
      1. 库内记了建库时的会话根（`meta.sessions_root`，2026-09-26 起由
         session-digest.py 写入）→ **必须指向同一处**才放行。指错目录与"磁盘上文件
         在不在"无关，直接拒绝。
      2. 老库没有这个记录 → 退化为"不许有任何来源在 sessions 下找不到"。
         这条必须比"至少有一个重叠就放行"严：判据放宽到"部分重叠"时，一个**含同名
         路径的部分副本**（未同步完的镜像、只恢复了一个月的机器）就能让孤儿清理删掉
         其余来源 —— 实测 1/5 重叠即删掉 4 个源、rc=0、`RESULT=PASS`（2026-09-26
         第二轮复核抓到）。老库若真有会话文件被删，用 build-search-index.py --force
         只重建索引（不动内容），再跑一次 digest 即可把会话根记进去。
    """
    rels, _, _ = store_sources(data)
    recorded = store_meta(data, "sessions_root")
    if recorded:
        if same_dir(recorded, sessions):
            return True, "ok", "与建库时的会话根一致"
        return False, "root-mismatch", "建库时的会话根是 " + recorded
    absent = sorted(r for r in rels
                    if not os.path.exists(os.path.join(sessions, r.replace("/", os.sep))))
    if absent:
        return False, "missing-sources", "%d 个来源在 %s 下找不到" % (len(absent), sessions)
    return True, "ok", "库内无 sessions_root 记录，且来源在该目录下齐全"


def do_fix(args, db, data):
    """--fix 的全部逻辑，返回要计入报告的问题（空 = 无需修、或已修好）。

    与旧实现的三点不同：
      1. 真带上 --sessions（旧版不传 → 退回默认目录 → 孤儿清理删库）；
      2. 重灌前用 reingest_verdict 拦截，不安全就**不修**并报错 —— 宁可留着索引问题，
         也不能把内容删掉；
      3. 修完比对内容指纹，丢了"仍在磁盘上的来源"一律判失败。
    第 3 条只是兜底：被删的来源若在给定目录下本就不存在（指错目录的典型形态），它抓不到，
    所以拦住删库的**只能是第 2 条**，判据不能放松。
    """
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")
    pre_missing = missing_triggers(con)
    con.close()
    _, _, pre_problems = check_index(db)

    if not args.json:
        print("")
        print("== 自动修复（索引类问题）==")
    if not pre_missing and not pre_problems:
        if not args.json:
            print("  索引侧无需修复")
        return []

    if not args.json:
        if pre_missing:
            print("  修前：缺同步触发器 " + ", ".join(pre_missing))
        for p in pre_problems:
            print("  修前：" + p)

    safe, code, detail = reingest_verdict(data, args.sessions)
    if not safe:
        if not args.json:
            if code == "root-mismatch":
                print("  [x] 拒绝重灌：--sessions 与**建库时的会话根**不是同一处")
                print("      --sessions   = " + args.sessions)
                print("      " + detail)
            else:
                print("  [x] 拒绝重灌：" + detail)
                print("      库内没有 sessions_root 记录（老库），且该目录装不下库里的来源。")
            print("      强行重灌会触发孤儿清理、把这些行整段删掉（删完索引自洽，审计仍会 PASS）。三选一：")
            print("        a) 指向建库时的目录：audit.py --data <库> --sessions <那个目录> --fix")
            print("        b) 确实搬过会话目录：先跑 session-digest.py --data <库> --sessions <新目录>"
                  "（它会更新记录），再跑 --fix")
            print("        c) 只重建索引、不动内容："
                  "python -X utf8 scripts\\build-search-index.py --data <库> --force")
        return ["FIX: --fix 未执行（" + code + "）—— --sessions 与库的来源不符，重灌会删数据"]

    before_rels, before_t, before_r = store_sources(data)
    if not run_fix(data, args.sessions):
        return ["FIX: 修复脚本返回非 0，未完成（内容可能只改了一半，请重跑或改用 --force）"]

    after_rels, after_t, after_r = store_sources(data)
    # 只对"修复前还在磁盘上"的来源要求存活：真被删掉的会话文件本就该被孤儿清理带走。
    lost = sorted(r for r in before_rels
                  if r not in after_rels
                  and os.path.exists(os.path.join(args.sessions, r.replace("/", os.sep))))
    if not args.json:
        print("  修复后：turns %d→%d · traces %d→%d · 来源 %d→%d"
              % (before_t, after_t, before_r, after_r, len(before_rels), len(after_rels)))
    if lost:
        return ["FIX: 修复后丢失了 %d 个仍存在的来源（%s%s）→ 不要再跑 --fix，改用 "
                "build-search-index.py --force" % (len(lost), ", ".join(lost[:3]),
                                                   " 等" if len(lost) > 3 else "")]

    _, _, post_problems = check_index(db)
    if post_problems:
        return ["FIX: 重灌 + 重建后索引仍不一致：" + "; ".join(post_problems[:2])]
    return []


NEED_TRIGGERS = ("turns_ai", "turns_ad", "turns_au", "traces_ai", "traces_ad", "traces_au")


def missing_triggers(con):
    """当前库里缺失的同步触发器名（升序按 NEED_TRIGGERS 顺序）。"""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return [n for n in NEED_TRIGGERS if n not in have]


def check_index(db):
    """C 项全部检查（自开只读连接 + 各自的可写连接跑 integrity-check）。

    返回 (rows, integrity, problems)。抽成函数是为了 --fix 之后能**原地复检**。
    """
    problems, rows, integrity = [], {}, {}
    try:
        con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
        con.execute("PRAGMA busy_timeout=10000")
    except sqlite3.Error as e:
        return rows, integrity, ["C: 打不开库（" + str(e) + "）"]
    try:
        for content, fts in (("turns", "turns_fts"), ("traces", "traces_fts")):
            r = docsize_rows(con, content, fts)
            if r is None:
                problems.append("C: %s 索引结构缺失 → 跑 session-digest.py + build-search-index.py" % fts)
                continue
            n_c, n_d, miss, extra = r
            rows[content] = {"content": n_c, "docsize": n_d, "missing": miss, "extra": extra}
            if miss or extra:
                problems.append("C: %s 行集不一致（索引缺行 %d · 幽灵行 %d）→ 跑 build-search-index.py --force"
                                % (fts, miss, extra))
    finally:
        con.close()
    for content, fts in (("turns", "turns_fts"), ("traces", "traces_fts")):
        if content not in rows:
            continue
        ok, msg = fts_integrity(db, fts)          # 必须用可写连接：只读连接会拒绝这条命令
        integrity[fts] = {"ok": ok, "msg": msg[:200]}
        if not ok:
            problems.append("C: %s 令牌级完整性失败（%s）→ 跑 audit.py --fix（重灌 + 重建索引）"
                            % (fts, msg[:120]))
    return rows, integrity, problems


def main():
    ap = argparse.ArgumentParser(description="Varve 记忆库审计")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--samples", type=int, default=3, help="检索自检抽样条数")
    ap.add_argument("--sessions", default=SESSIONS_DEFAULT,
                    help="会话日志目录（--fix 重灌时的真相源；默认同 session-digest.py。"
                         "必须与建库时的目录一致，否则 --fix 拒绝执行）")
    ap.add_argument("--fix", action="store_true", help="发现索引问题时自动重灌内容 + 重建索引")
    args = ap.parse_args()

    db = os.path.join(args.data, "index", "sessions.db")
    if not os.path.exists(db):
        print("库不存在：" + db)
        return 1

    issues = []
    report = {}

    # --fix 必须在所有测量**之前**跑。旧实现把修复放在测量之后，于是同一份报告里
    # A/D/E 是修前值、C 是修后值，自相矛盾（2026-09-26 实测到 "C turns 0行" 与
    # "E turns=4" 并排出现）。现在修完才开连接，全篇都是修后状态。
    if args.fix:
        issues.extend(do_fix(args, db, args.data))

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
    # 判据必须与写库时的去重规则（FIX-027）对齐：**内容相同**才删。分叉会话在同一
    # turn_no 上是内容不同的真实轮次，必须保留 —— 所以"同 (sid, turn_no) 但内容不同"
    # 是合法数据，不是问题。旧判据把这两类混在一起数，于是任何一次全量重灌之后
    # B 恒 > 0、rc 恒为 1、`RESULT=PASS` 永不出现，而 `--fix` 又修不掉它（因为它不是
    # 问题）。2026-09-26 第三轮验证在真实库副本上实测：B=46 全部是 fork、
    # identical-content=0 —— 即"真重复"恰好为 0。故 B 只数内容完全相同的那些。
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT session_id, turn_no FROM turns "
        "GROUP BY session_id, turn_no, question, answer "
        "HAVING COUNT(DISTINCT source) > 1)").fetchone()[0]
    report["B_duplicate_turns"] = dup
    if dup:
        issues.append("B: %d 个 (session_id, turn_no) 在多 source 上内容完全相同（重复入库）" % dup)
    forks = con.execute(
        "SELECT COUNT(*) FROM (SELECT session_id, turn_no FROM turns "
        "GROUP BY session_id, turn_no HAVING COUNT(DISTINCT source) > 1)").fetchone()[0] - dup
    report["B_note"] = ("fork_turns(同 sid+turn_no、内容不同): %d（正常：分叉会话的真实轮次，"
                        "FIX-027 明确保留）" % forks)

    # C. 索引一致性（schema 3 语义：触发器在位 + 行集对账 + 令牌级完整性）
    # 不再比较 index_built_at / content_updated_at —— 触发器模式下写入即入索引，
    # 这两个时间戳本就会分叉，用它们判断会恒报"落后"（2026-09-24 修正）。
    missing = missing_triggers(con)
    if missing:
        issues.append("C: 缺同步触发器 " + ", ".join(missing) + " → 跑 build-search-index.py 补建")

    # C1/C2 行集对账 + 令牌级完整性（详见 check_index / 顶部注释）
    index_rows, index_integrity, index_problems = check_index(db)
    issues.extend(index_problems)
    report["C_index"] = {"triggers_missing": missing, "rows": index_rows,
                         "integrity": index_integrity,
                         "ok": (not missing) and not index_problems}
    t_cnt = index_rows.get("turns", {}).get("content", 0)
    r_cnt = index_rows.get("traces", {}).get("content", 0)

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
    report["E_size"] = {"db_mb": round(os.path.getsize(db) / 1e6, 1), "turns": t_cnt, "traces": r_cnt}
    con.close()

    # F. L2 体积（快照流：只查**最后一条真快照**是否贴住护栏；文件总长不设上限）
    # 标记识别必须跳过代码区 —— 模板/文档里为了教人怎么写会原样展示标记，
    # 旧实现用 txt.rfind 会把示例当成真快照，量到的是说明文字（2026-09-26 修）。
    l2 = os.path.join(args.data, "STATUS.md")
    if os.path.exists(l2):
        txt = open(l2, encoding="utf-8", errors="replace").read()
        marks = snapshot_marks(txt)
        if marks:
            seg = txt[marks[-1] + len(SNAPSHOT_MARK):].strip()
            scope, n_snap = "最新快照", len(marks)
        else:
            m = re.search(r"<!-- =+ 工程状态区.*?<!-- =+ 工程状态区 结束", txt, re.DOTALL)
            seg = m.group(0) if m else txt
            scope, n_snap = "旧格式整区", 0
        report["F_l2"] = {"chars": len(seg), "budget": 3500, "scope": scope, "snapshots": n_snap}
        if len(seg) > 3500:
            issues.append("F: %s %d 字符 > 预算 3500（注入会被截断；细节移进 records）" % (scope, len(seg)))
    else:
        report["F_l2"] = {"chars": 0, "budget": 3500, "note": "全局卡不存在"}

    # --fix 已在测量之前执行完（见 main 开头）；此处 C 项反映的是修复后的状态。

    if args.json:
        print(json.dumps({"issues": issues, "report": report}, ensure_ascii=False, indent=2))
    else:
        print("== Varve 记忆库审计 ==")
        print("  A 一致性: sources turns=%d traces=%d file_state=%d | 孤儿=%d 幽灵=%d"
              % (len(src_turns), len(src_traces), len(files), len(orphan_turns), len(ghost)))
        print("  B 重复入库: %d（同 sid+turn_no 且内容相同；另 %d 个同名不同内容 = fork，合法）"
              % (dup, forks))
        c = report["C_index"]
        detail = []
        for name, d in c["rows"].items():
            detail.append("%s %d行(docsize %d, 缺%d/幽灵%d)" % (name, d["content"], d["docsize"],
                                                            d["missing"], d["extra"]))
        integ = " ".join(k.replace("_fts", "") + ":" + ("ok" if v["ok"] else "坏")
                         for k, v in c["integrity"].items())
        print("  C 索引: 触发器 %d/6 | %s | 完整性 %s %s"
              % (6 - len(c["triggers_missing"]), " · ".join(detail) or "-", integ or "-",
                 "（一致）" if c["ok"] else "（异常）"))
        print("  D 检索自检: %d/%d 命中" % (hit, len(rows)))
        print("  E 体量: %.1f MB | turns=%d traces=%d"
              % (report["E_size"]["db_mb"], report["E_size"]["turns"], report["E_size"]["traces"]))
        f2 = report["F_l2"]
        print("  F 全局卡: %s %d 字符 / 预算 %d（共快照 %d 条）"
              % (f2.get("scope", "-"), f2["chars"], f2["budget"], f2.get("snapshots", 0)))
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
