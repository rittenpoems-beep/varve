#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regression_test.py — 一跑到底的回归检查（零第三方依赖，零真实数据）。

每条用例对应 FIXES.md 里一条 FIX-0xx 的"复发检查"。用例**全部在临时目录里自建自清**，
不碰真实数据根（`VARVE_DATA`），所以可以随时反复跑。

用法：
    python -X utf8 scripts/regression_test.py                 # 全部
    python -X utf8 scripts/regression_test.py --only staging  # 只跑名字含该子串的
    python -X utf8 scripts/regression_test.py --list          # 列出用例
    python -X utf8 scripts/regression_test.py --keep          # 保留临时现场

注意：用例的临时目录是**固定路径**（`%TEMP%/varve-reg-<tag>`），所以同一台机器上**不要并发跑
两份套件** —— 两边会互相删现场（并行跑请先复制一份脚本树再分别指定不同 tag）。

退出码：0 = 全 PASS；1 = 有 FAIL；2 = 有 SKIP 但无 FAIL（环境不具备，如缺 pwsh）。
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PY = sys.executable

CHECKS = []


def check(name, fix):
    """注册一条用例：name 用于 --only，fix 是对应的 FIXES.md 编号。"""
    def deco(fn):
        CHECKS.append((name, fix, fn))
        return fn
    return deco


# ---------- 基础设施 ----------

def run_py(script, *args):
    return subprocess.run([PY, "-X", "utf8", os.path.join(HERE, script)] + [str(a) for a in args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def new_root(tag):
    root = os.path.join(tempfile.gettempdir(), "varve-reg-" + tag)
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "sessions"))
    return root


def write_session(sess_dir, name, sid, pairs, cwd=r"D:\proj"):
    lines = [json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
                        ensure_ascii=False)]
    for q, a in pairs:
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": q}]}},
            ensure_ascii=False))
        lines.append(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": a}]}},
            ensure_ascii=False))
    with open(os.path.join(sess_dir, name), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def build_store(tag, files):
    """建库：files = [(文件名, sid, [(问,答)])]；返回 (root, data, db)。"""
    root = new_root(tag)
    sess = os.path.join(root, "sessions")
    data = os.path.join(root, "data")
    for name, sid, pairs in files:
        write_session(sess, name, sid, pairs)
    r = run_py("session-digest.py", "--data", data, "--sessions", sess)
    assert r.returncode == 0, "digest 失败：" + r.stdout + r.stderr
    r = run_py("build-search-index.py", "--data", data)
    assert r.returncode == 0, "建索引失败：" + r.stdout + r.stderr
    return root, data, os.path.join(data, "index", "sessions.db")


def connect_ro(db):
    con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=10000")
    return con


def turns(db):
    con = connect_ro(db)
    n = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    con.close()
    return n


SID = "0192aaaa-bbbb-cccc-dddd-eeeeffff0001"
OVERLAP = [("第%d轮提问" % j, "回答%d" % j) for j in range(1, 6)]


# ---------- FIX-019 并发 digest ----------

@check("digest-concurrent", "FIX-019")
def c_digest_concurrent():
    """并发写库的**互斥契约**（FIX-019）+ 分叉去重（FIX-027）。

    这里**不靠竞态计时**：旧代码的并发丢数据/索引损坏在这台机器上复现率不稳定
    （2026-09-26 两轮独立验证都没能在旧代码上跑出损坏，第一版用例的"并发锤击"段
    其实每轮都是 `written=0 skipped=6` 的空转 —— 空用例，已删掉）。改成直接构造
    锁的状态，断言互斥契约本身：锁被持有 -> 本轮跳过且一行不写；锁已陈旧（上次被
    kill）-> 抢占并继续。旧代码没有锁，第一条必然过不了。
    """
    out = []

    # 场景 A：6 个文件内容完全相同的 resume 重叠 -> 去重后 5 轮
    root, data, db = build_store("conc-a", [
        ("rollout-2026-09-2%dT01-00-0%d-00000000-0000-0000-0000-00000000000%d.jsonl" % (i, i, i),
         SID, OVERLAP) for i in range(6)])
    sess = os.path.join(root, "sessions")
    lock = os.path.join(data, "index", ".digest.lock")
    if turns(db) != 5:
        out.append("初始应为 5 轮，实得 %d" % turns(db))

    # 再放一个全新会话：真跑起来 turns 会 +2，这样"跳过了"与"跑了但没变"才分得开
    write_session(sess, "rollout-2026-09-30T01-00-00-00000000-0000-0000-0000-000000000099.jsonl",
                  "0192aaaa-bbbb-cccc-dddd-eeeeffff9999", [("新会话提问", "新会话回答")])
    base = turns(db)

    # A1 锁被另一个进程持有（mtime 新鲜）-> 跳过本轮、一行不写
    with open(lock, "w", encoding="utf-8") as fh:
        fh.write("0 placeholder\n")
    r = run_py("session-digest.py", "--data", data, "--sessions", sess)
    if r.returncode != 0:
        out.append("锁被持有时返回码 %d（跳过不是错误，应为 0）" % r.returncode)
    if "跳过" not in (r.stdout or ""):
        out.append("锁被持有却没有跳过（互斥没生效，两个进程会同时写库）："
                   + (r.stdout or "")[-120:])
    if turns(db) != base:
        out.append("锁被持有仍写了库：%d -> %d" % (base, turns(db)))

    # A2 锁文件陈旧（上次进程被 kill，没来得及释放）-> 抢占并继续，否则索引永久停更
    old = time.time() - 7200
    os.utime(lock, (old, old))
    r = run_py("session-digest.py", "--data", data, "--sessions", sess)
    if turns(db) <= base:
        out.append("陈旧锁没有被抢占（库会永久停更）：turns 仍为 %d" % turns(db))
    if os.path.exists(lock):
        out.append("跑完后锁文件没释放（下一次运行会白等 30 秒）")

    # 场景 B：2 个文件分叉（同 sid 同 turn_no，内容不同）-> 10 轮全保
    root2, data2, db2 = build_store("conc-b", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
         [("主线第%d轮" % j, "主线答%d" % j) for j in range(1, 6)]),
        ("rollout-2026-09-21T01-00-01-00000000-0000-0000-0000-000000000001.jsonl", SID,
         [("分叉第%d轮" % j, "分叉答%d" % j) for j in range(1, 6)]),
    ])
    if turns(db2) != 10:
        out.append("分叉用例应为 10 轮，实得 %d（真实轮次被去重吃掉）" % turns(db2))
    return (not out), "; ".join(out) or "A: 持锁时跳过且零写入、陈旧锁被抢占；B: 分叉 10 轮全保"


# ---------- FIX-020 audit C 项 ----------

@check("audit-integrity", "FIX-020")
def c_audit_integrity():
    """audit 的 C 项必须能抓三类损坏；健康库必须 0 问题。"""
    sys.path.insert(0, HERE)
    import audit
    out = []

    def scenario(tag, mutate):
        _root, _data, db = build_store(tag, [
            ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
             [("第%d轮提问内容abcdefgh" % j, "回答%d内容" % j) for j in range(1, 6)])])
        if mutate:
            con = sqlite3.connect(db)
            mutate(con)
            con.commit()
            con.close()
        _rows, _integ, probs = audit.check_index(db)
        return probs

    if scenario("aud-ok", None):
        out.append("健康库被判为有问题")
    if not scenario("aud-miss", lambda c: c.execute(
            "DELETE FROM turns_fts_docsize WHERE id=(SELECT MIN(id) FROM turns)")):
        out.append("索引缺行没被抓到")
    if not scenario("aud-ghost", lambda c: c.execute(
            "INSERT INTO turns_fts_docsize(id, sz) VALUES(999999, x'00')")):
        out.append("幽灵行没被抓到")

    def stale(c):
        c.execute("DROP TRIGGER turns_au")
        c.execute("UPDATE turns SET question='完全换掉的内容 zzzqqq' WHERE id=(SELECT MIN(id) FROM turns)")

    probs = scenario("aud-stale", stale)
    if not probs:
        out.append("令牌级不一致没被抓到（只有 integrity-check 能抓这类）")
    elif not any("令牌级" in p for p in probs):
        out.append("抓到了但没走令牌级检查：" + "; ".join(probs)[:120])
    # 反面对照：旧判据（COUNT 对账）在这类损坏上必须**照样通过**（否则说明用例没造出损坏）
    _root, _data, db = build_store("aud-old", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
         [("第%d轮提问内容abcdefgh" % j, "回答%d内容" % j) for j in range(1, 6)])])
    con = sqlite3.connect(db)
    stale(con)
    con.commit()
    t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    tf = con.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
    con.close()
    if t != tf:
        out.append("反面对照失效：旧判据居然能发现损坏（用例不成立）")
    return (not out), "; ".join(out) or "健康 0 问题；缺行/幽灵/令牌级三类都能抓到；旧判据确认为假绿"


@check("audit-fix", "FIX-020")
def c_audit_fix():
    """audit.py --fix：目录指对要**真修好**（丢的行从源日志重灌回来），指错绝不动数据。

    旧版只断言"fix 后打印 RESULT=PASS"，而 --fix 会把库换掉甚至清空、清空后索引同样
    自洽也打印 PASS —— 用例因此放过了一次真实的删库（2026-09-26 复核抓到）。现在分四条，
    并且**始终显式传 --sessions**，否则 audit 会去读真实的 ~/.codex/sessions。

    四条里第 2、3 条是第二轮复核补的：原用例只测"完全不存在的错目录"，而**部分重叠**
    的错目录（未同步完的镜像 / 只恢复了一个月的机器：与库共享若干个同名 rel 路径）能绕过
    "至少一个重叠即放行"的旧判据，让孤儿清理删掉其余来源 —— 实测 1/3 重叠即删 2 个源、
    rc=0、RESULT=PASS。第 3 条另外把 meta 里的 sessions_root 记录删掉，验证**老库**那条
    "不许有任何来源缺失"的兜底判据也真的拦得住。
    """
    NAMES = ["rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-00000000000%d.jsonl" % i
             for i in range(3)]
    root, data, db = build_store("aud-fix", [
        (NAMES[i], "0192aaaa-bbbb-cccc-dddd-eeeeffff000%d" % i, OVERLAP) for i in range(3)])
    sess = os.path.join(root, "sessions")
    out = []

    # 部分重叠的错目录：只放**第 0 个**会话文件的同名副本（rel 路径一致）
    partial = os.path.join(root, "partial-sessions")
    os.makedirs(partial, exist_ok=True)
    shutil.copyfile(os.path.join(sess, NAMES[0]), os.path.join(partial, NAMES[0]))

    def corrupt():
        con = sqlite3.connect(db)
        # 先拆掉删除触发器，再删内容行 —— 这样删掉的行走不掉索引，留下**幽灵行**
        # （顺序反了的话触发器照常同步，损坏就只有"触发器缺失"这一条）
        con.execute("DROP TRIGGER turns_ad")
        con.execute("DELETE FROM turns WHERE id=(SELECT MAX(id) FROM turns)")
        con.commit()
        con.close()

    def rows():
        con = connect_ro(db)
        n = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        con.close()
        return n

    def sources():
        con = connect_ro(db)
        n = con.execute("SELECT COUNT(DISTINCT source) FROM turns").fetchone()[0]
        con.close()
        return n

    def expect_refusal(label, wrong_dir):
        """指错时必须拒绝、不许打印 PASS、且一行内容都不许动。"""
        b, s = rows(), sources()
        r = run_py("audit.py", "--data", data, "--sessions", wrong_dir, "--fix")
        if r.returncode == 0 or "RESULT=PASS" in (r.stdout or ""):
            out.append("%s：仍报 PASS（等于纵容删库）" % label)
        if "拒绝重灌" not in (r.stdout or ""):
            out.append("%s：没有给出拒绝重灌的提示" % label)
        if rows() != b or sources() != s:
            out.append("%s：--fix 动了内容（%d 行/%d 源 -> %d 行/%d 源）"
                       % (label, b, s, rows(), sources()))

    corrupt()
    base = rows()
    if run_py("audit.py", "--data", data).returncode == 0:
        return False, "造出的损坏没被审计发现（用例不成立）"

    # --- 1) 完全不存在的目录 ---
    expect_refusal("目录不存在", os.path.join(root, "wrong-sessions"))

    # --- 2) 部分重叠的目录（与库共享 1 个同名 rel 路径）---
    expect_refusal("目录部分重叠", partial)

    # --- 3) 老库（无 sessions_root 记录）下同样部分重叠：走兜底判据 ---
    con = sqlite3.connect(db)
    con.execute("DELETE FROM meta WHERE k='sessions_root'")
    con.commit()
    con.close()
    expect_refusal("老库 + 目录部分重叠", partial)

    # --- 4) 目录指对：必须真修好（行数恢复 + 来源数不变 + RESULT=PASS）---
    before_src = sources()
    r = run_py("audit.py", "--data", data, "--sessions", sess, "--fix")
    if r.returncode != 0 or "RESULT=PASS" not in (r.stdout or ""):
        out.append("目录正确时 --fix 未通过：" + (r.stdout or "")[-300:])
    if rows() <= base:
        out.append("--fix 后行数未恢复（%d -> %d：只是把库换成空的自洽状态）" % (base, rows()))
    if sources() != before_src:
        out.append("--fix 后来源数变了（%d -> %d）" % (before_src, sources()))

    # --- 5) 会话根**字符串一致但目录已消失**（轮转 / 卸载 / 改名）---
    #     判据拦不住这条：reingest_verdict 只比路径字符串，路径相等就放行。所以这里靠的是
    #     --fix 给 digest 带的 --keep-orphans —— 结构上删不掉内容。旧行为实测：库被按
    #     "文件全没了"清空（9 行 5 源 -> 0 行 0 源）、rc=0、照样 RESULT=PASS。
    b, s = rows(), sources()
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER turns_au")
    con.execute("INSERT INTO turns_fts(turns_fts, rowid, question, answer) "
                "SELECT 'delete', id, question, answer FROM turns WHERE id=(SELECT MIN(id) FROM turns)")
    con.commit()
    con.close()
    shutil.rmtree(sess)
    r = run_py("audit.py", "--data", data, "--sessions", sess, "--fix")
    if rows() != b or sources() != s:
        out.append("会话根目录消失时 --fix 删了内容（%d 行/%d 源 -> %d 行/%d 源）"
                   % (b, s, rows(), sources()))
    if "没扫到任何会话文件" not in (r.stdout or ""):
        out.append("根目录消失时没提示'内容未被重灌'（报告会被读成内容已按真相源对齐）")

    # --- 6) 空库（sessions.db 存在但一张表都没有）：--fix 应能建起来，而不是喷异常栈 ---
    root2 = new_root("aud-fix-empty")
    sess2 = os.path.join(root2, "sessions")
    data2 = os.path.join(root2, "data")
    os.makedirs(os.path.join(data2, "index"), exist_ok=True)
    sqlite3.connect(os.path.join(data2, "index", "sessions.db")).close()   # 存在、无表
    write_session(sess2, NAMES[0], SID, OVERLAP)
    r = run_py("audit.py", "--data", data2, "--sessions", sess2, "--fix")
    both = (r.stdout or "") + (r.stderr or "")
    if "Traceback" in both:
        out.append("空库上 --fix 抛了异常栈")
    elif r.returncode != 0 or "RESULT=PASS" not in (r.stdout or ""):
        out.append("空库上 --fix 未能建起索引：" + both[-200:])
    return (not out), "; ".join(out) or (
        "不存在/部分重叠/老库部分重叠 -> 都拒绝且零删数据；指对 -> 丢行重灌回来；"
        "根目录消失 -> 不删内容且明说没重灌；空库 -> 能建起来 + PASS")


# ---------- FIX-021 快照标记识别 ----------

@check("snapshot-marks", "FIX-021")
def c_snapshot_marks():
    """代码区里的快照标记不算真快照；模板不能注入说明文字。"""
    sys.path.insert(0, HERE)
    import varve_hooks_common as C
    out = []

    # 1) 模板（标记只出现在代码围栏里）必须"没有快照"
    tpl = os.path.join(REPO, "templates", "global-status.template.md")
    txt = open(tpl, encoding="utf-8", errors="replace").read()
    if C.snapshot_marks(txt):
        out.append("模板被判出真快照（新装用户会注入说明文字）")
    if C.extract_latest_snapshot(txt).strip():
        out.append("模板抽出了非空快照")

    # 2) 真卡 + 文档示例混排：必须取最后一条**真**快照
    card = ("<!-- ===== 快照 ===== -->\n\n### 2026-01-01 · 第一条\n- 老的\n\n"
            "```text\n<!-- ===== 快照 ===== -->\n示例，不是真的\n```\n\n"
            "<!-- ===== 快照 ===== -->\n\n### 2026-02-02 · 第二条\n- 最新\n")
    seg = C.extract_latest_snapshot(card)
    if "最新" not in seg or "示例" in seg:
        out.append("真卡取错快照：" + repr(seg[:60]))

    # 3) 行内代码里的标记同样不算
    inline = "`` `<!-- ===== 快照 ===== -->` ``\n"
    if C.snapshot_marks(inline):
        out.append("行内代码里的标记被当成真快照")

    # 4) ~~~ 围栏与 ``` 等效：旧实现只认反引号，于是把波浪线围栏里的**示例**当成真快照
    #    （示例放最后才是有鉴别力的写法：旧实现会抽出"说明文字"而不是真状态）
    tilde = ("<!-- ===== 快照 ===== -->\n\n### 真快照\n- 内容\n\n"
             "~~~text\n<!-- ===== 快照 ===== -->\n说明文字\n~~~\n")
    if len(C.snapshot_marks(tilde)) != 1:
        out.append("~~~ 围栏里的标记被当成真快照（真快照应为 1 条，实得 %d 条）"
                   % len(C.snapshot_marks(tilde)))
    # 段必须**从真快照起头**（旧实现从示例那条标记起头，注入进去的就是说明文字）。
    # 段尾跟着示例无所谓：快照的定义就是"最后一条真标记到文件末尾"。
    seg_t = C.extract_latest_snapshot(tilde)
    if not seg_t.startswith("### 真快照"):
        out.append("~~~ 围栏混排取错快照（段首不是真状态）：" + repr(seg_t[:60]))

    # 5) 反引号围栏里嵌 ~~~ 不应导致围栏提前闭合
    mixed = "```text\n~~~\n<!-- ===== 快照 ===== -->\n```\n<!-- ===== 快照 ===== -->\n真\n"
    if len(C.snapshot_marks(mixed)) != 1:
        out.append("``` 内嵌 ~~~ 时围栏配对错乱")

    # 6) 闭合围栏必须**不短于**开启围栏（CommonMark）：四反引号块里的三反引号行不是
    #    闭合围栏。旧实现只比字符不比长度，围栏会在那一行提前闭合 —— 块里的**示例**
    #    标记漏成"真快照"，而块外的**真**快照又被重新开启的围栏一路吞到文件末尾。
    #    （夹具故意让示例标记在真标记**之前**：这样两种错法不会互相抵消。
    #      反过来的写法会得到相同的计数，变异体因此抓不到 —— 2026-09-26 实测过。）
    longer = ("````text\n<!-- ===== 快照 ===== -->\n```\n````\n"
              "<!-- ===== 快照 ===== -->\n\n### 真快照\n- 内容\n")
    n_long = len(C.snapshot_marks(longer))
    if n_long != 1:
        out.append("四反引号块被三反引号行提前闭合（真快照应为 1 条，实得 %d 条）" % n_long)
    return (not out), "; ".join(out) or "模板=无快照；混排取末条真快照；行内代码/~~~ 围栏/四反引号块都不算"


# ---------- FIX-022 短词按词分流 ----------

@check("recall-short-words", "FIX-022")
def c_recall_short_words():
    """一个参数里混着 2 字词与长词：2 字词走字面兜底，不能被静默丢弃；片段不得为 NULL。"""
    _root, data, db = build_store("short", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
         [("上下文压缩策略怎么定", "按轮数压缩"),
          ("线上故障怎么修复", "回滚再修"),
          ("快照流怎么写入", "追加一条完整快照")])])
    out = []

    def recall(*args):
        r = run_py("recall.py", *["--data", data] + list(args))
        return r

    # 纯 2 字词（此前恒 0 条）
    r = recall("压缩", "--json")
    hits = json.loads(r.stdout)["coverage"]["matched_blocks"] if r.returncode == 0 else 0
    if not hits:
        out.append("单 2 字词仍 0 命中")
    # 混合：2 字词 + 3 字词同参数（此前整串走 FTS，2 字词被丢）
    r = recall("压缩 快照流", "--json")
    if r.returncode != 0:
        out.append("混合查询崩溃：" + (r.stderr or "")[-200:])
    else:
        cov = json.loads(r.stdout)["coverage"]
        if not cov["matched_blocks"]:
            out.append("混合查询 0 命中（短词被丢）")
    # 两个 2 字词、只有其中一个命中某行 -> 片段不能是 None（SQL 多参数 min() 的 NULL 坑）
    r = recall("压缩 修复", "--json")
    if r.returncode != 0:
        out.append("两短词查询崩溃：" + (r.stderr or "")[-200:])
    else:
        snips = [s["text"] for ses in json.loads(r.stdout)["sessions"] for s in ses["snippets"]]
        if any(s is None for s in snips):
            out.append("出现 NULL 片段")
    # 文本输出路径也要能跑完（此前在这里 TypeError）
    r = recall("压缩 修复")
    if r.returncode != 0:
        out.append("文本输出崩溃：" + (r.stderr or "")[-200:])
    # 短词提示必须与 run_variant 的实际路由一致：非 --raw 时报，--raw 下**不能**报
    # （--raw 整串按 FTS5 原生语法送进 FTS，短词并没有走字面兜底；一律照报就是假话。
    #  这是改成逐词分流时引入的回归，2026-09-26 复核发现。）
    if "字面兜底" not in (r.stdout or ""):
        out.append("非 --raw 下不再提示短词走字面兜底（提示丢了）")
    r = recall("--raw", "压缩 OR 修复")
    if "字面兜底" in (r.stdout or "") or "字面匹配" in (r.stdout or ""):
        out.append("--raw 下仍声称短词走了字面兜底（假报告）")
    return (not out), "; ".join(out) or "单短词/混合/两短词 均有命中且无 NULL 片段；--raw 不误报兜底"


# ---------- FIX-023 --topic 保留最新 ----------

@check("topic-keep-latest", "FIX-023")
def c_topic_keep_latest():
    """--topic 各源触顶时保留**最新**的，并明说已截断。"""
    sys.path.insert(0, HERE)
    import io
    import recall
    # 用真库（含 turns）跑：三源合并的路径必须整体可跑，不能只有快照那一路
    _root, data, db = build_store("topic", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
         [("主题词 会话第%d轮" % j, "答%d" % j) for j in range(1, 4)])])
    # 60 条快照（>上限 40），每条都含主题词；按文档顺序递增
    parts = []
    for i in range(60):
        mm = "%02d" % (i % 12 + 1)
        parts.append("<!-- ===== 快照 ===== -->\n\n### 2026-%s-01 · 第%d条\n- 主题词 编号%03d\n" % (mm, i, i))
    with open(os.path.join(data, "STATUS.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    out = []
    got = recall.scan_snapshots(data, "主题词")
    if len(got) != 40:
        out.append("应有 40 条，实得 %d" % len(got))
    blob = " ".join(t for _d, _l, t in got)
    if "编号059" not in blob:
        out.append("最新一条（编号059）被丢掉了")
    if "编号000" in blob:
        out.append("仍在保留最旧的（编号000）")
    # 覆盖行必须提示触顶
    con = recall.connect_db(db)
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        recall.print_topic_timeline(data, con, "主题词", "", "")
        line = sys.stdout.getvalue()
    finally:
        sys.stdout = old
        con.close()
    if "已达上限" not in line:
        out.append("触顶时没有明示截断")
    if "【覆盖】" not in line:
        out.append("缺【覆盖】行")
    return (not out), "; ".join(out) or "保留了最新 40 条且明示触顶"


# ---------- FIX-024 staging 并发 ----------

@check("staging-concurrent", "FIX-024")
def c_staging_concurrent():
    """并发提交不丢更新 / 幂等记录不丢 / upsert 不被静默丢掉。"""
    from concurrent.futures import ThreadPoolExecutor
    merge = os.path.join(HERE, "staging", "staging_merge.py")
    out = []

    def fresh(tag):
        root = os.path.join(tempfile.gettempdir(), "varve-reg-stg-" + tag)
        if os.path.exists(root):
            shutil.rmtree(root, ignore_errors=True)
        os.makedirs(os.path.join(root, "staging", "w1"))
        return root

    def proposal(data, writer, content, op="append", ts="2026-09-26T01:00:00Z"):
        d = os.path.join(data, "staging", writer)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ts.replace(":", "") + "-" + writer + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"writer_id": writer, "kind": "session", "project": r"D:\p",
                       "target": "records", "op": op, "key": "k", "content": content,
                       "evidence": "", "proposal_id": "p" + writer, "ts": ts}, fh, ensure_ascii=False)
        return p

    def plan(data, name, text, consumed, pid):
        p = os.path.join(data, name + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"plan_id": pid, "writes": [{"path": os.path.join(data, "T.md"),
                                                   "op": "append", "text": text}],
                       "consumed": consumed}, fh, ensure_ascii=False)
        return p

    def run(data, *extra):
        return subprocess.run([PY, "-X", "utf8", merge, "--data", data] + list(extra),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    # A 丢更新
    data = fresh("A")
    a1 = proposal(data, "w1", "A1\n")
    a2 = proposal(data, "w2", "A2\n", ts="2026-09-26T01:00:01Z")
    open(os.path.join(data, "T.md"), "w", encoding="utf-8").write("# base\n")
    p1 = plan(data, "pA1", "- A1\n" * 200, [a1], "pidA1")
    p2 = plan(data, "pA2", "- A2\n" * 200, [a2], "pidA2")
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda p: run(data, "--plan", p), (p1, p2)))
    txt = open(os.path.join(data, "T.md"), encoding="utf-8").read()
    if ("- A1\n" * 200) not in txt or ("- A2\n" * 200) not in txt:
        out.append("并发 append 丢更新（A 场景）")

    # B 幂等记录
    data = fresh("B")
    b1 = proposal(data, "w1", "B1\n")
    b2 = proposal(data, "w2", "B2\n", ts="2026-09-26T01:00:01Z")
    open(os.path.join(data, "T.md"), "w", encoding="utf-8").write("")
    pb1 = plan(data, "pB1", "- B1\n", [b1], "pidB1")
    pb2 = plan(data, "pB2", "- B2\n", [b2], "pidB2")
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda p: run(data, "--plan", p), (pb1, pb2)))
    sp = os.path.join(data, "staging", ".merged.json")
    plans = json.load(open(sp, encoding="utf-8")).get("applied_plans", []) if os.path.exists(sp) else []
    if "pidB1" not in plans or "pidB2" not in plans:
        out.append("幂等记录丢失：%s" % plans)

    # C upsert 不被静默丢
    data = fresh("C")
    up = proposal(data, "w1", "U1 内容", op="upsert")
    run(data, "--scan", "--auto-target", os.path.join(data, "T.md"),
        "--out", os.path.join(data, "plan.json"))
    pj = os.path.join(data, "plan.json")
    if not os.path.exists(pj):
        out.append("--scan 没产出计划")
    else:
        plan_obj = json.load(open(pj, encoding="utf-8"))
        if up not in plan_obj.get("consumed", []) or "U1 内容" not in json.dumps(plan_obj, ensure_ascii=False):
            out.append("upsert 提案被静默丢弃")
    return (not out), "; ".join(out) or "并发 append 不丢、幂等记录双全、upsert 有降级路径"


@check("staging-atomic-write", "FIX-024")
def c_staging_atomic_write():
    """staging_write.py 不得让读者看到半写文件（先写 .part-* 再原子改名）。

    第一版用例是**空用例**：只查"有没有 .part-* 残留、成品能不能解析"，这两条任何
    单线程写都满足，把 open(path,"w") 直接写正式名（非原子）也照样过。
    真正的契约是"写入过程中，读者永远看不到不完整的提案" —— runner 用真实的
    staging_merge.load_proposals()（glob('*.json') + json.load），半写文件会进 bad：
    写者以为交了，读者那一轮看不到（静默丢数据）。

    小提案在 NTFS 上的写入窗口 <1ms，从外部进程采样抓不到，所以这里把写侧的序列化
    **注入延迟**（json.dump 分 6 段写 + flush + sleep），把窗口放大到百毫秒级，再让
    读者线程高频调用真实的 load_proposals。注入点若失效（例如写侧改用 json.dumps）
    直接判 FAIL —— 宁可响亮地失败，也不许用例悄悄退回空转。
    """
    import contextlib
    import importlib.util
    import io
    import threading

    root = os.path.join(tempfile.gettempdir(), "varve-reg-stg-w")
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)

    def load_mod(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    writer = load_mod("varve_stg_writer", os.path.join(HERE, "staging", "staging_write.py"))
    merge = load_mod("varve_stg_merge", os.path.join(HERE, "staging", "staging_merge.py"))

    wdir = os.path.join(root, "staging", "w1")
    st = {"inject": 0, "stop": False, "bad": 0, "rounds": 0, "saw": []}
    real_dump, real_dumps = json.dump, json.dumps

    def slow_dump(obj, fp, **kw):
        text = real_dumps(obj, **kw)
        step = len(text) // 6 + 1
        for i in range(0, len(text), step):
            fp.write(text[i:i + step])
            fp.flush()          # 半截内容真正落到文件名下
            st["inject"] += 1
            time.sleep(0.03)    # 释放 GIL，读者线程必然插进来

    def reader():
        while not st["stop"]:
            _rows, bad = merge.load_proposals(root)
            st["rounds"] += 1
            if bad:
                st["bad"] += 1
                if len(st["saw"]) < 3:
                    st["saw"].append(os.path.basename(bad[0]))
            time.sleep(0.002)

    content = "原子写入校验内容" * 8
    old_argv = sys.argv
    json.dump = slow_dump
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        sys.argv = ["staging_write.py", "--writer", "w1", "--target", "records", "--key", "k",
                    "--content", content, "--data", root]
        with contextlib.redirect_stdout(io.StringIO()):
            writer.main()
    finally:
        json.dump = real_dump
        sys.argv = old_argv
        st["stop"] = True
        t.join()

    if st["inject"] == 0:
        return False, "注入点失效：写侧没走 json.dump，用例已空转（请更新注入点）"
    if st["bad"]:
        return False, ("读者在 %d/%d 轮里看到了半写的提案文件（例如 %s）——"
                       "非原子写会让读者解析失败、静默丢掉该提案"
                       % (st["bad"], st["rounds"], st["saw"]))
    leftovers = glob.glob(os.path.join(wdir, "*.part-*"))
    finals = glob.glob(os.path.join(wdir, "*.json"))
    if leftovers:
        return False, "残留半写文件：%s" % leftovers
    if len(finals) != 1:
        return False, "应恰好一个提案文件，实得 %d" % len(finals)
    rec = json.load(open(finals[0], encoding="utf-8"))
    if rec.get("content") != content:
        return False, "成品内容与提交的不一致（读者会拿到半截提案）"
    return True, ("写入全程读者扫描 %d 轮、零次看到半写提案；成品内容完整"
                  % st["rounds"])


# ---------- FIX-025/026 PowerShell ----------

@check("ps-syntax", "FIX-025")
def c_ps_syntax():
    """三个 .ps1 必须能被 PowerShell 解析（前导 `+` 这类跨行写法会静默炸掉整个脚本）。"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    script = os.path.join(tempfile.gettempdir(), "varve-reg-parse.ps1")
    targets = [os.path.join(HERE, f) for f in ("install.ps1", "doctor.ps1", "install-claude.ps1",
                                               "sync-projects.ps1", "init.ps1")]
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("$bad = 0\n")
        fh.write("foreach ($f in @(" + ",".join("'" + t.replace("\\", "/") + "'" for t in targets) + ")) {\n")
        fh.write("  $errs = $null\n")
        fh.write("  [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$errs)\n")
        fh.write("  if ($errs) { Write-Output ('BAD ' + (Split-Path -Leaf $f) + ' line=' + $errs[0].Extent.StartLineNumber); $bad++ }\n")
        fh.write("  else { Write-Output ('OK  ' + (Split-Path -Leaf $f)) }\n}\n")
        fh.write("exit $bad\n")
    r = subprocess.run([pwsh, "-NoProfile", "-File", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    os.remove(script)
    if r.returncode != 0:
        return False, (r.stdout or r.stderr or "").strip()[:300]
    return True, "5 个脚本解析通过"


@check("ps-install-guard", "FIX-025/032")
def c_ps_install_guard():
    """python 不可用时 install.ps1 必须停手：退出码非 0，且**不写** hooks.json。

    两种"不可用"都要拦：① 压根找不到 python（FIX-025①）② 找得到但版本不够（FIX-032：
    原判据只看"有没有 python"，3.9 这种解释器照样被写进 hooks.json —— 当场只有一行
    [FAIL]，hook 要等运行时才静默失败）。第二种用只打印 `python=3.9.0` 的桩顶掉真 python
    （取版本走的就是 check-env.py 输出里的 `python=` 那一行）。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-inst")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    out = []

    def run(label, stub):
        d = os.path.join(tmp, label)
        home = os.path.join(d, "home")
        os.makedirs(home)
        path = "'C:\\nope'"                      # ① 连 python 都找不到
        if stub:                                # ② 找得到，但只报 3.9.0
            fakebin = os.path.join(d, "bin")
            os.makedirs(fakebin)
            with open(os.path.join(fakebin, "python.cmd"), "w", encoding="ascii",
                      newline="\r\n") as fh:
                fh.write(stub)
            path = "'" + fakebin.replace("\\", "/") + ";' + $env:PATH"
        cmd = ("$env:PATH=" + path + "; $env:USERPROFILE='" + home.replace("\\", "/") + "'; "
               "& '" + os.path.join(HERE, "install.ps1").replace("\\", "/") + "' -DataRoot '"
               + os.path.join(d, "data").replace("\\", "/") + "' -NoSetEnv *>$null; "
               "exit $LASTEXITCODE")
        r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            out.append(label + "：退出码应为非 0，实得 0")
        if os.path.exists(os.path.join(home, ".codex", "hooks.json")):
            out.append(label + "：python 不可用仍写出了 hooks.json（会配出一个跑不起来的 hook）")

    run("找不到 python", None)
    run("版本 3.9", "@echo off\r\necho python=3.9.0\r\nexit /b 0\r\n")
    return (not out), "; ".join(out) or "两种不可用（找不到 / 版本过低）都中止且未写 hooks.json"


@check("ps-dataroot-guard", "FIX-025")
def c_ps_dataroot_guard():
    """`-DataRoot` 非默认且不让改环境变量时，install.ps1 必须报 [FAIL] 并返回非 0。

    旧实现只打一句 Warn：hooks / recall 仍去 `~/.varve` 找数据，表现成"记忆没生效"，
    而且没有任何报错 —— 又一个静默失败。这里全部在**假 USERPROFILE** 里跑（hooks.json /
    Skill 都写到假 home），并且顺手断言真实的用户级 VARVE_DATA 没被动过：回归套件不许改本机。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-dataroot")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    fake_home = os.path.join(tmp, "home")
    droot = os.path.join(tmp, "data")
    os.makedirs(fake_home)

    def pwsh_run(cmd):
        return subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def user_var():
        r = pwsh_run("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                     "[Environment]::GetEnvironmentVariable('VARVE_DATA','User')")
        return (r.stdout or "").strip()

    before = user_var()
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:USERPROFILE='"
           + fake_home.replace("\\", "/") + "'; & '"
           + os.path.join(HERE, "install.ps1").replace("\\", "/") + "' -DataRoot '"
           + droot.replace("\\", "/") + "' -NoSetEnv; exit $LASTEXITCODE")
    r = pwsh_run(cmd)
    text = r.stdout or ""
    after = user_var()

    out = []
    fails = [l for l in text.splitlines() if l.strip().startswith("[FAIL]") and "VARVE_DATA" in l]
    if not fails:
        out.append("数据根非默认又不设环境变量，却没有任何 [FAIL] 提示（用户只会看到'记忆没生效'）")
    if r.returncode == 0:
        out.append("退出码 0（有 [FAIL] 也没让 CI / 自动化感知到）")
    if before != after:
        out.append("用例动了本机用户级 VARVE_DATA：%r -> %r" % (before, after))
    shutil.rmtree(tmp, ignore_errors=True)
    return (not out), "; ".join(out) or "非默认数据根 + -NoSetEnv 报 FAIL、退出码非 0，且未改动本机环境变量"


@check("ps-project-default", "FIX-025")
def c_ps_project_default():
    """不传 -Project 时不得报项目级 hooks 假 [FAIL]（默认安装是用户级，不是当前目录）。

    旧实现默认 `-Project .`：在没装项目级 hooks 的任意目录跑体检都会多一条 [FAIL]，
    用户以为装坏了。这里故意在一个**没有 .codex** 的目录里跑（cwd=tmp）。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-projdef")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '"
           + os.path.join(HERE, "doctor.ps1").replace("\\", "/") + "' -DataRoot '"
           + os.path.join(tmp, "data").replace("\\", "/") + "'")
    r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=tmp)
    lines = [l for l in (r.stdout or "").splitlines() if "hooks（项目级）" in l]
    if not lines:
        return False, "体检结果里没有'项目级 hooks'这一项（检查项消失）"
    if "[FAIL]" in lines[0]:
        return False, "没传 -Project 却报了假 [FAIL]：" + lines[0].strip()
    return True, "未传 -Project 时项目级 hooks 记为跳过，不误报"


@check("ps-doctor-subdirs", "FIX-025")
def c_ps_doctor_subdirs():
    """体检必须逐个报 `index` / `records` / `staging`，且结论随目录**实际**在不在。

    修复前的子目录列表里没有 `staging`：整个 staging 目录被删也报不出来。反过来，把结论
    写死成 OK 等于没查。这里只建 `index`，断言三项都在、且只有 `index` 记 [ OK ]。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-subdirs")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    droot = os.path.join(tmp, "data")
    os.makedirs(os.path.join(droot, "index"))        # 故意只建 index
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '"
           + os.path.join(HERE, "doctor.ps1").replace("\\", "/") + "' -DataRoot '"
           + droot.replace("\\", "/") + "'")
    r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    lines = (r.stdout or "").splitlines()
    out = []
    for d in ("index", "records", "staging"):
        row = [l for l in lines if ("子目录 " + d) in l]
        if not row:
            out.append("体检里没有'子目录 %s'这一项（漏检）" % d)
            continue
        ok = "[ OK ]" in row[0]
        if d == "index" and not ok:
            out.append("index 目录存在却报 FAIL：" + row[0].strip())
        if d != "index" and ok:
            out.append("%s 目录不存在却报通过（结论没跟着实际情况走）：%s" % (d, row[0].strip()))
    shutil.rmtree(tmp, ignore_errors=True)
    return (not out), "; ".join(out) or "三个子目录逐项报到，且只有实际存在的 index 记 OK"


@check("ps-no-output-guard", "FIX-025")
def c_ps_no_output_guard():
    """check-env.py 吐不出可用版本号时，体检里"Python >= 3.10"这一行不能**消失**。

    旧实现直接 `[version]$E["python"]`：值取到**空串**时（`python=` 后面什么都没有）
    转型抛异常 -> 该行整个不见，用户只看到控制台喷红，不知道少了一项。
    （注意 `[version]$null` 并不抛，抛的是 `[version]""` —— 2026-09-26 实测。）
    这里用一个只打印 `python=` 的 `python.cmd` 桩顶掉真 python，断言那一行仍在、且记 [FAIL]。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-pyver")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    fakebin = os.path.join(tmp, "fakebin")
    os.makedirs(fakebin)
    with open(os.path.join(fakebin, "python.cmd"), "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("@echo off\r\necho python=\r\nexit /b 0\r\n")
    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:PATH='"
           + fakebin.replace("\\", "/") + ";' + $env:PATH; & '"
           + os.path.join(HERE, "doctor.ps1").replace("\\", "/") + "' -DataRoot '"
           + os.path.join(tmp, "data").replace("\\", "/") + "'")
    r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    lines = [l for l in (r.stdout or "").splitlines() if "Python >= 3.10" in l]
    if not lines:
        return False, ("'Python >= 3.10' 这一行整个消失了（检查项静默消失，用户看不到原因）；"
                       "stderr：" + (r.stderr or "").strip()[:120])
    if "[FAIL]" not in lines[0]:
        return False, "版本号取不到却记成通过：" + lines[0].strip()
    return True, "版本号取不到时该行仍在并记 [FAIL]，不会静默消失"


@check("ps-hook-path-check", "FIX-025")
def c_ps_hook_path_check():
    """hooks.json 引用的 .py 不存在时，doctor.ps1 必须报 [FAIL] 且退出码非 0。

    hook 跑不起来是**静默**的（Codex 不报错），"装了但指向已搬走的旧安装目录"没人会发现，
    所以这条必须是显式检查。反向也要验：脚本存在时不得误报。
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    tmp = os.path.join(tempfile.gettempdir(), "varve-reg-hookpath")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, ".codex"))
    hooks = os.path.join(proj, ".codex", "hooks.json")
    fwd = tmp.replace("\\", "/")
    missing = fwd + "/nope/missing-hook.py"
    real = fwd + "/real-hook.py"
    with open(real, "w", encoding="utf-8") as fh:
        fh.write("# 占位：体检只验路径存在性\n")

    def write_hooks(pyref):
        body = {"description": "fixture", "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": "python \"%s\"" % pyref, "timeout": 15}]}]}}
        with open(hooks, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2)

    def doctor():
        # pwsh 输出走控制台编码（本机是 GBK）-> 中文行名会变成乱码，断言就永远匹配不上。
        # 先把 [Console]::OutputEncoding 顶成 UTF-8，再调 doctor.ps1。
        cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '"
               + os.path.join(HERE, "doctor.ps1").replace("\\", "/") + "' -Project '"
               + proj.replace("\\", "/") + "' -DataRoot '"
               + os.path.join(tmp, "data").replace("\\", "/") + "'; exit $LASTEXITCODE")
        r = subprocess.run([pwsh, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r, (r.stdout or "").replace("\\", "/")

    out = []
    write_hooks(missing)
    r, text = doctor()
    bad_line = [l for l in text.splitlines() if "hook 脚本路径有效" in l and "[FAIL]" in l]
    if not bad_line:
        out.append("hooks.json 指向不存在的 .py，体检却没报 [FAIL]（漏检）")
    elif missing not in text:
        out.append("报了 FAIL 但没说清是哪个路径不存在，缺 %s" % missing)
    if r.returncode == 0:
        out.append("有 [FAIL] 项却退出码 0（CI 感知不到）")

    write_hooks(real)
    _r2, text2 = doctor()
    for l in text2.splitlines():
        if "hook 脚本路径有效" in l and "[FAIL]" in l and (fwd + "/") in l:
            out.append("引用的是存在的脚本却仍报 [FAIL]：" + l.strip())

    shutil.rmtree(tmp, ignore_errors=True)
    return (not out), "; ".join(out) or "指向缺失脚本报 FAIL（含路径与退出码），指向存在的脚本不误报"


@check("claude-index-hook", "FIX-026")
def c_claude_index_hook():
    """install-claude.ps1 必须同时装索引重建 hook，且重装不叠加。"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    proj = os.path.join(tempfile.gettempdir(), "varve-reg-claude")
    if os.path.exists(proj):
        shutil.rmtree(proj, ignore_errors=True)
    os.makedirs(proj)
    s = os.path.join(HERE, "install-claude.ps1").replace("\\", "/")
    for _ in range(2):
        subprocess.run([pwsh, "-NoProfile", "-File", s, "-Scope", "project", "-Project", proj],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    cfg = os.path.join(proj, ".claude", "settings.json")
    if not os.path.exists(cfg):
        return False, "未生成 settings.json"
    hooks = json.load(open(cfg, encoding="utf-8"))["hooks"]
    cmds = [h["command"] for g in hooks.get("SessionStart", []) for h in g["hooks"]]
    if not any("hook-build-index" in c for c in cmds):
        return False, "SessionStart 缺索引重建 hook（Claude 侧索引永不刷新）"
    if sum(1 for c in cmds if "hook-session-start" in c) != 1:
        return False, "重装后 session-start 条目叠加了：%d 条" % sum(
            1 for c in cmds if "hook-session-start" in c)
    return True, "SessionStart 含 build-index，且重装后不叠加"


# ---------- FIX-027/028 digest 解析与杂项 ----------

@check("digest-multiseg", "FIX-027")
def c_digest_multiseg():
    """多段消息不能只取第一段；跨文件去重要看内容而不是只看 (sid, turn_no)。"""
    root = new_root("parse")
    sess = os.path.join(root, "sessions")
    # 多段 user 消息（文本 + 补充段）——旧实现只看第一段
    lines = [json.dumps({"type": "session_meta", "payload": {"id": "sX", "cwd": r"D:\p"}},
                        ensure_ascii=False)]
    lines.append(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "第一段内容"},
            {"type": "input_text", "text": "第二段内容"},
            {"type": "input_text", "text": "第三段内容"}]}}, ensure_ascii=False))
    lines.append(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "答"}]}},
        ensure_ascii=False))
    with open(os.path.join(sess, "rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl"),
              "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    data = os.path.join(root, "data")
    r = run_py("session-digest.py", "--data", data, "--sessions", sess)
    assert r.returncode == 0, r.stderr
    db = os.path.join(data, "index", "sessions.db")
    con = connect_ro(db)
    q = con.execute("SELECT question FROM turns").fetchone()[0]
    con.close()
    out = []
    if "第二段内容" not in q or "第三段内容" not in q:
        out.append("多段消息的后半截被丢掉：" + repr(q[:60]))
    return (not out), "; ".join(out) or "多段消息全部保留"


@check("digest-fallback-naming", "FIX-028")
def c_digest_fallback_naming():
    """文件名不是 rollout-<日期>T... 形态时：日期取 mtime，sid 取 UUID，不能是脏切片。"""
    # 文件名带连字符，不能直接 import，按路径加载
    import runpy
    import time
    ns = runpy.run_path(os.path.join(HERE, "session-digest.py"), run_name="not_main")
    out = []
    uuid = "0192aaaa-bbbb-cccc-dddd-eeeeffff0009"
    p = os.path.join(tempfile.gettempdir(), "weird-name-" + uuid + ".jsonl")
    # 期望日期按**同一套本地时间口径**现算。旧写法写死 "2023-11-14" 并退让成
    # "d.count('-') == 2 就算过"（怕别的时区误报），代价是**日期错但格式对**照样 PASS
    # —— 等于没测日期（2026-09-26 复核指出）。
    expect = time.strftime("%Y-%m-%d", time.localtime(1700000000))
    d = ns["session_date"](p, 1700000000)
    if d != expect:
        out.append("日期兜底不是 mtime 当天：%s（应为 %s）" % (d, expect))
    sid = ns["fallback_sid"](p)
    if sid != uuid:
        out.append("sid 兜底不是 UUID：" + str(sid))
    # 标准命名仍走文件名日期
    p2 = os.path.join(tempfile.gettempdir(), "rollout-2026-09-20T01-00-00-" + uuid + ".jsonl")
    if ns["session_date"](p2, 1700000000) != "2026-09-20":
        out.append("标准命名的日期解析坏了")
    return (not out), "; ".join(out) or "非标准命名有兜底（mtime 日期 + UUID），标准命名不受影响"


@check("timeline-max-date", "FIX-028")
def c_timeline_max_date():
    """--timeline 跨天会话要按**最新**一轮的日期排，而不是任取一行。

    夹具必须让**同一个会话真的跨天**：同一个 sid 出现在两天（09-20 / 09-25）的 rollout
    里，两轮内容不同因此都保留。只有单个日期的会话下裸列 `date` 与 `MAX(date)` 结果相同，
    用例就抓不到旧实现 —— 2026-09-26 验证 Agent 指出第一版夹具（手工改一行日期）正是
    这样，实为空用例。
    """
    _root, data, _db = build_store("tldate", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", "s-old",
         [("20 号那天的提问 abcdef", "20 号的回答")]),
        ("rollout-2026-09-25T01-00-00-00000000-0000-0000-0000-000000000002.jsonl", "s-old",
         [("25 号续聊的提问 abcdef", "25 号的回答")]),
        ("rollout-2026-09-22T01-00-00-00000000-0000-0000-0000-000000000001.jsonl", "s-mid",
         [("22 号另一个会话 abcdef", "答")])])
    con = sqlite3.connect(os.path.join(data, "index", "sessions.db"))
    dates = [r[0] for r in con.execute(
        "SELECT date FROM turns WHERE session_id='s-old' ORDER BY date")]
    con.close()
    if dates != ["2026-09-20", "2026-09-25"]:
        return False, "夹具没造出跨天会话（s-old 的日期为 %s）" % dates
    r = run_py("recall.py", "--data", data, "--timeline", "--limit", "5")
    lines = [l for l in (r.stdout or "").splitlines() if l.strip() and not l.startswith("timeline:")]
    if not lines:
        return False, "时间线无输出"
    # 一条时间线条目占两行：第一行 "日期  工作区"，第二行 "标题 [sid N轮]"
    head = "\n".join(lines[:2])
    if "2026-09-25" not in lines[0] or "s-old" not in head:
        return False, "跨天会话没按最新一轮（09-25）排到首位：" + head[:120].replace("\n", " | ")
    return True, "跨天会话按最新一轮日期排序（09-25，不是 09-20）"


@check("sync-projects-regex", "FIX-028")
def c_sync_projects_regex():
    """sync-projects.ps1 只登记工作区表头，不把子表（[projects.'x'.trust]）当路径。"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        return "skip", "未找到 pwsh"
    root = os.path.join(tempfile.gettempdir(), "varve-reg-sync")
    if os.path.exists(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    projects = os.path.join(root, "projects.md")
    open(projects, "w", encoding="utf-8").write("# projects\n")
    cfg = os.path.join(root, "config.toml")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write("[projects.'D:\\good']\n")
        fh.write("[projects.\"D:\\good2\"]\n")
        fh.write("[projects.'D:\\nested'.trust]\n")
    s = os.path.join(HERE, "sync-projects.ps1").replace("\\", "/")
    r = subprocess.run([pwsh, "-NoProfile", "-File", s, "-DataRoot", root, "-ConfigPath", cfg],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    body = open(projects, encoding="utf-8").read()
    out = []
    if "D:\\good" not in body or "D:\\good2" not in body:
        out.append("单/双引号工作区漏登记")
    if "nested" in body:
        out.append("子表被当成工作区登记了：" + [l for l in body.splitlines() if "nested" in l][0][:80])
    return (not out), "; ".join(out) or "两种引号都登记，子表不被误登记"


# ---------- FIX-030 全量重灌之后 B 项必须能收敛 ----------

@check("audit-fork-dedup", "FIX-030")
def c_audit_fork_dedup():
    """B 项只数**内容完全相同**的重复入库；同名不同内容的 fork 轮次是合法数据。

    背景（2026-09-26 第三轮验证在真实库副本上抓到）：FIX-027 把跨 source 去重改成
    "内容相同才删"，于是分叉会话在同一 turn_no 上的不同轮次被**保留**——这是设计要的。
    但 B 项还在数"同 (sid, turn_no) 来自多个 source"，把合法 fork 当问题报：任何一次
    --full 重灌之后 B 恒 > 0、rc 恒为 1、RESULT=PASS 永不出现，而 --fix 又修不掉
    （它根本不是问题）。真实库实测 B=46 全部是 fork、真重复恰好 0。

    两条断言互为反面：只有 fork -> 必须 PASS；再注入一条**内容完全相同**的重复行 ->
    必须报出来（否则就是把 B 项彻底废掉，那不叫修复）。
    """
    root, data, db = build_store("fork", [
        ("rollout-2026-09-20T01-00-00-00000000-0000-0000-0000-000000000000.jsonl", SID,
         [("分叉前的问题 快照流", "回答 A"), ("只在这条里", "回答 B")]),
        ("rollout-2026-09-21T01-00-00-00000000-0000-0000-0000-000000000001.jsonl", SID,
         [("分叉前的问题 快照流", "回答 A"), ("只在那条里", "回答 C")])])
    out = []

    def run_audit():
        return run_py("audit.py", "--data", data, "--sessions", os.path.join(root, "sessions"), "--json")

    r = run_audit()
    rep = json.loads(r.stdout)
    forks = rep["report"]["B_note"]
    # fork 轮次必须留下（FIX-027 的承诺），且不算问题
    if rep["report"]["B_duplicate_turns"] != 0:
        out.append("合法 fork 被当成重复入库：B=%d" % rep["report"]["B_duplicate_turns"])
    if r.returncode != 0:
        out.append("只有 fork 的库审计不通过（rc=%d）—— B 项永远收敛不了" % r.returncode)
    if "fork_turns" not in forks or forks.split(":")[1].strip().startswith("0 "):
        out.append("没有把 fork 轮次作为正常项报出来：" + forks)

    # 反面：注入一条 content 完全相同的重复行（模拟去重规则生效前的老库）-> 必须报出来
    con = sqlite3.connect(db)
    row = con.execute("SELECT session_id, workspace, date, turn_no, src_line_start, src_line_end, "
                      "question, answer FROM turns ORDER BY id LIMIT 1").fetchone()
    con.execute("INSERT INTO turns(id, session_id, workspace, date, turn_no, src_line_start, "
                "src_line_end, source, question, answer) VALUES "
                "((SELECT MAX(id)+1 FROM turns),?,'D:/other',?,?,0,0,'other/file.jsonl',?,?)",
                (row[0], row[2], row[3], row[6], row[7]))
    con.commit()
    con.close()
    # file_state 里补上这个来源，免得 A 项先报孤儿
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO file_state VALUES ('other/file.jsonl',1,1)")
    con.commit()
    con.close()
    r = run_audit()
    rep = json.loads(r.stdout)
    if rep["report"]["B_duplicate_turns"] != 1:
        out.append("内容完全相同的重复入库没被数出来：B=%d" % rep["report"]["B_duplicate_turns"])
    if r.returncode == 0:
        out.append("真重复入库却判 PASS")
    return (not out), "; ".join(out) or "fork 只作提示且 PASS；内容相同的重复仍被报出"


# ---------- FIX-031 官方修复路径不能把库撑大 ----------

@check("digest-full-compact", "FIX-031")
def c_digest_full_compact():
    """全量重灌与索引重建之后，库里不许留下"虚胖"（在用的那部分才是真内容）。

    背景（2026-09-26 第三轮验证实测，真实库）：逐行 DELETE+INSERT 会把 trigram 索引切成
    一堆小段，FTS5 只在提交时按有限预算合并 -> 段一多**在用页**就长期虚胖（94.9 -> 151.8 MB
    在用）；`optimize` 合并后腾出的页、以及 `--force` 丢掉旧索引留下的页，都还留在文件里，
    只有 VACUUM 才缩文件（155.9 MB 的文件里 64 MB 是空闲页）。一次官方修复路径之后文件
    95.4 -> 152.4 MB，内容其实只多了 68 轮，而且不会自己回落。

    两个判据各对一处修复，阈值都按实测留了余量（夹具 6 文件 × 80 轮）：
      在用比值  = 重灌后的在用页 / 建库后的在用页。不做 optimize 实测 1.30，做了 1.00 -> 卡 1.10
      文件/在用 = 文件字节 / 在用页字节。--full 后不 VACUUM 实测 1.39，做了 1.00 -> 卡 1.10
    第二段专门测 --force 那侧的 VACUUM。rebuild 要先把旧索引整块丢掉，库里内容变少时
    新索引只要一小部分页，剩下的就留在文件里：先把 3/4 轮次删掉再 --force，不 VACUUM
    实测留 88 页空闲（占 55%），VACUUM 后 0 页 / 文件=在用。
    """
    files = [("rollout-2026-09-%02dT01-00-00-00000000-0000-0000-0000-00000000000%d.jsonl"
              % (10 + i, i), "0192aaaa-bbbb-cccc-dddd-eeeeffff10%02d" % i,
              [("第%d轮的问题内容 %s" % (j, "快照流" * 20), "回答内容 %s" % ("追加一条" * 20))
               for j in range(1, 81)]) for i in range(6)]
    root, data, db = build_store("compact", files)
    sess = os.path.join(root, "sessions")
    out = []

    def pages():
        con = connect_ro(db)
        pc = con.execute("PRAGMA page_count").fetchone()[0]
        fl = con.execute("PRAGMA freelist_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
        con.close()
        return pc, fl, ps

    def filesz():
        return os.path.getsize(db)

    pc0, fl0, ps = pages()
    used0 = pc0 - fl0
    for _ in range(2):          # 一次不够稳：碎片是累积的，两轮把差距拉开（1.00 vs 1.30）
        r = run_py("session-digest.py", "--data", data, "--full", "--sessions", sess,
                   "--keep-orphans")
        if r.returncode != 0:
            return False, "digest --full 失败：" + (r.stdout or "")[-200:]
    pc, fl, _ = pages()
    r1 = (pc - fl) / float(used0)
    if (pc - fl) > used0 * 1.10:
        out.append("重灌后在用页涨到 %.2fx（索引碎片没合并：optimize 丢了）" % r1)
    if filesz() > (pc - fl) * ps * 1.10:
        out.append("--full 之后文件是在用页的 %.2fx（空闲页没回收：VACUUM 丢了）"
                   % (filesz() / float((pc - fl) * ps)))
    r = run_py("audit.py", "--data", data, "--sessions", sess)
    if "RESULT=PASS" not in (r.stdout or ""):
        out.append("重灌后审计不通过：" + (r.stdout or "")[-200:])

    # 第二段：--force 那侧的 VACUUM。rebuild 要先把旧索引整块丢掉：库里内容变少时
    # （删轮次、索引损坏后重建），新索引只要一小部分页，剩下的空闲页就留在文件里。
    # 这里先把 3/4 的轮次删掉（触发器会把它们从索引里摘掉），造出"内容小了、旧索引的页
    # 还占着"的形态，再 --force —— 不 VACUUM 时文件停在被撑大的尺寸上，VACUUM 后回落。
    # 注意不能先 DROP 虚表再重建：那样空闲页刚好够新索引原样拿回去，什么都不剩，
    # 测不出这条修复（2026-09-26 复核：M2 变异体第一版就是这么活下来的）。
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    con.execute("DELETE FROM turns WHERE id IN (SELECT id FROM turns ORDER BY id DESC LIMIT ?)",
                (n * 3 // 4,))
    con.commit()
    con.close()
    pc, fl, ps = pages()
    if fl == 0 or filesz() <= (pc - fl) * ps:
        out.append("造不出内容删掉、旧页还占着的形态（用例前提不成立）")
    r = run_py("build-search-index.py", "--data", data, "--force")
    if r.returncode != 0:
        out.append("--force 重建失败：" + (r.stdout or "")[-200:])
    pc, fl, ps = pages()
    if fl > max(5, pc // 20):
        out.append("重建后仍留 %d 页空闲（占 %.0f%%）—— --force 那侧的 VACUUM 丢了"
                   % (fl, 100.0 * fl / pc))
    elif filesz() > (pc - fl) * ps * 1.10:
        out.append("重建后文件是在用页的 %.2fx —— --force 那侧的 VACUUM 丢了"
                   % (filesz() / float((pc - fl) * ps)))
    return (not out), "; ".join(out) or ("在用比值 %.2f，--force 后文件/在用 %.2f、空闲页 %d"
                                        % (r1, filesz() / float((pc - fl) * ps), fl))


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="Varve 回归检查")
    ap.add_argument("--only", default="", help="只跑名字含该子串的用例")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留临时现场")
    args = ap.parse_args()

    if args.list:
        for name, fix, fn in CHECKS:
            print("%-24s %-9s %s" % (name, fix, (fn.__doc__ or "").strip().splitlines()[0]))
        return 0

    picked = [(n, f, fn) for n, f, fn in CHECKS if args.only in n]
    if not picked:
        print("没有匹配的用例：" + args.only)
        return 1

    n_pass = n_fail = n_skip = 0
    fails = []
    for name, fix, fn in picked:
        try:
            res = fn()
            if isinstance(res, tuple) and len(res) == 2 and res[0] == "skip":
                n_skip += 1
                print("[SKIP] %-24s %-9s %s" % (name, fix, res[1]))
                continue
            ok, detail = res
        except Exception:
            ok, detail = False, "用例抛异常：" + traceback.format_exc(limit=3).replace("\n", " | ")[:300]
        if ok:
            n_pass += 1
            print("[PASS] %-24s %-9s %s" % (name, fix, detail))
        else:
            n_fail += 1
            fails.append(name)
            print("[FAIL] %-24s %-9s %s" % (name, fix, detail))

    print("")
    print("合计：PASS %d · FAIL %d · SKIP %d" % (n_pass, n_fail, n_skip))
    if fails:
        print("失败用例：" + ", ".join(fails))
    if not args.keep:
        for d in glob.glob(os.path.join(tempfile.gettempdir(), "varve-reg-*")):
            shutil.rmtree(d, ignore_errors=True)
    if n_fail:
        return 1
    return 2 if n_skip else 0


if __name__ == "__main__":
    sys.exit(main())
