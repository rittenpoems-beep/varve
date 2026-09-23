#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build-search-index.py — 在 turns / traces 上建 FTS5 索引（external content 模式）。

2026-09-23 定稿：**内容与索引分离**
- 内容权威在普通表 `turns`（对话历史）/ `traces`（全量历史），由 session-digest.py 写入
- 本脚本只负责索引：`INSERT INTO xxx_fts(xxx_fts) VALUES('rebuild')`
- external content 模式：索引不重复存文本，内容只存一份

旧结构自动迁移：若 `turns` 仍是 FTS5 虚表（09-23 之前的形态），删表重建，
随后需重跑 session-digest.py 灌入内容。

用法：python -X utf8 build-search-index.py [--data <dir>] [--stats]

`--stats` 是**只读契约**（doctor.ps1 体检依赖它）：只读连接取计数，
不迁移、不建表、不 rebuild。
"""
import argparse
import os
import sqlite3

DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")

TURNS_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
             "question, answer, content='turns', content_rowid='id', tokenize='trigram')")
TRACES_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS traces_fts USING fts5("
              "text, content='traces', content_rowid='id', tokenize='trigram')")

# schema 2（2026-09-24）：触发器让 FTS 与内容表**始终同步** ——
# 不再依赖"digest 之后必须 rebuild"的时序，彻底消除索引陈旧窗口（盲测 #5 彻底修法）。
SYNC_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
         INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
       END""",
    """CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
         INSERT INTO turns_fts(turns_fts, rowid, question, answer)
         VALUES ('delete', old.id, old.question, old.answer);
       END""",
    """CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
         INSERT INTO turns_fts(turns_fts, rowid, question, answer)
         VALUES ('delete', old.id, old.question, old.answer);
         INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
       END""",
    """CREATE TRIGGER IF NOT EXISTS traces_ai AFTER INSERT ON traces BEGIN
         INSERT INTO traces_fts(rowid, text) VALUES (new.id, new.text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS traces_ad AFTER DELETE ON traces BEGIN
         INSERT INTO traces_fts(traces_fts, rowid, text) VALUES ('delete', old.id, old.text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS traces_au AFTER UPDATE ON traces BEGIN
         INSERT INTO traces_fts(traces_fts, rowid, text) VALUES ('delete', old.id, old.text);
         INSERT INTO traces_fts(rowid, text) VALUES (new.id, new.text);
       END""",
]


def migrate_if_legacy(con):
    """旧形态（turns 本身是 FTS5 虚表 / 缺 turns 普通表）-> 删掉全部派生结构。"""
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='turns'").fetchone()
    legacy = bool(row and row[0] and "fts5" in row[0].lower())
    if legacy:
        for t in ("turns", "traces", "turns_fts", "traces_fts"):
            con.execute("DROP TABLE IF EXISTS " + t)
        con.commit()
    return legacy


def main():
    ap = argparse.ArgumentParser(description="建 FTS5 检索索引")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--force", action="store_true", help="强制重建（忽略变更检测）")
    args = ap.parse_args()

    db = os.path.join(args.data, "index", "sessions.db")
    if not os.path.exists(db):
        print("库不存在：" + db + " —— 先跑 session-digest.py")
        return 1

    # --stats 只读：修 2026-09-23 盲测发现（doctor.ps1 承诺只读，实际会触发 rebuild 写库）
    if args.stats:
        try:
            con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
        except sqlite3.Error as e:
            print("stats 无法打开库: " + str(e))
            return 1
        con.execute("PRAGMA busy_timeout=10000")
        try:
            t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            r = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "readonly" in msg.lower() or "locked" in msg.lower():
                print("stats 只读失败（库需要崩溃恢复，如 hot journal；跑一次 session-digest.py 可复位）: " + msg)
            else:
                print("turns/traces 表不存在 —— 先跑 session-digest.py (" + msg + ")")
            con.close()
            return 1
        sz = round(os.path.getsize(db) / 1024.0 / 1024.0, 1)
        print("turns=" + str(t) + " traces=" + str(r) + " db_mb=" + str(sz) + " path=" + db)
        con.close()
        return 0

    con = sqlite3.connect(db, timeout=30)
    # 并发与崩溃健壮性（2026-09-23 盲测 #4）：WAL + busy_timeout，避免 rebuild 期间
    # 被读请求撞上 "database is locked"，或被中断后留下 hot journal 阻塞只读访问。
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")

    if migrate_if_legacy(con):
        print("检测到 09-23 之前的旧结构，已清除 —— 请重跑 session-digest.py 灌入内容")
        con.close()
        return 0

    has = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='turns'").fetchone()[0]
    if not has:
        print("turns 表不存在 —— 先跑 session-digest.py")
        con.close()
        return 1

    # schema 2 检查：旧结构（无 id 主键）必须先由 session-digest.py 升级并重灌
    cols = [r[1] for r in con.execute("PRAGMA table_info(turns)").fetchall()]
    if "id" not in cols:
        print("检测到 schema 1（turns 无 id 主键）—— 先跑 session-digest.py 完成结构升级并重灌")
        con.close()
        return 1

    # 在**建触发器之前**探测是否已处于"触发器同步模式"（schema 3）。
    # 该模式下写入即进索引，全量 rebuild 是纯浪费——旧的条件 rebuild（比时间戳）
    # 会在每次内容变化时触发，等于每次会话启动重建整个 FTS（2026-09-24 修正）。
    had_triggers = all(
        con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name=?",
                    (n,)).fetchone()[0]
        for n in ("turns_ai", "turns_ad", "turns_au", "traces_ai", "traces_ad", "traces_au")
    )

    con.execute(TURNS_FTS)
    con.execute(TRACES_FTS)
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    for trig in SYNC_TRIGGERS:
        con.execute(trig)
    con.commit()

    t0 = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    r0 = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]

    if had_triggers and not args.force:
        print("sync triggers active, skip rebuild (turns=" + str(t0) + " traces=" + str(r0)
              + ") | 强制重建请加 --force")
        con.close()
        return 0

    con.execute("INSERT INTO turns_fts(turns_fts) VALUES('rebuild')")
    con.execute("INSERT INTO traces_fts(traces_fts) VALUES('rebuild')")
    stamp = con.execute("SELECT v FROM meta WHERE k='content_updated_at'").fetchone()
    if stamp:
        con.execute("INSERT OR REPLACE INTO meta VALUES ('index_built_at', ?)", (stamp[0],))
    con.commit()

    t = con.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    r = con.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    sz = round(os.path.getsize(db) / 1e6, 1)   # 与 audit.py 统一口径：MB = 10^6 字节
    print("index rebuilt: turns=" + str(t) + " traces=" + str(r) + " db_mb=" + str(sz)
          + " | 同步触发器已就位（后续写入自动进索引）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
