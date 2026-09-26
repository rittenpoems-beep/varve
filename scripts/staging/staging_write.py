#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""staging_write.py - 写者侧：原子新建一条记忆更新提案（无锁）。

设计要点（见 docs/并发写入设计-staging与主agent判定.md）：
- 写者只在自己的命名空间 <data>/staging/<writer_id>/ 下新建文件，永不修改他人文件
- 文件名含微秒时间戳 + 随机后缀，'x' 独占创建 -> 天然无竞争
- 写者不碰正式区；正式写入由主 agent 判定后统一提交

用法:
  python -X utf8 staging_write.py --writer <id> --kind subagent|worktree|session \
      --project <path> --target status.task|status.recent|records|env \
      --op upsert|append --key <定位键> --content <内容> [--parent <父容器id>] [--evidence <证据>]
"""
import argparse
import json
import os
import uuid
from datetime import datetime, timezone

DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")


def main():
    ap = argparse.ArgumentParser(description="写一条 staging 提案")
    ap.add_argument("--writer", required=True, help="写者 id（子 agent/会话/worktree 唯一名）")
    ap.add_argument("--kind", default="session", choices=["subagent", "worktree", "session"])
    ap.add_argument("--parent", default="", help="父容器 id（子 agent 场景）")
    ap.add_argument("--project", default="", help="项目根（默认取当前目录）")
    ap.add_argument("--target", required=True, choices=["status.task", "status.recent", "records", "env"])
    ap.add_argument("--op", default="upsert", choices=["upsert", "append"])
    ap.add_argument("--key", required=True, help="冲突检测锚（同 target+key 异内容 = 真冲突）")
    ap.add_argument("--content", required=True)
    ap.add_argument("--evidence", default="")
    ap.add_argument("--data", default=DEFAULT_DATA, help="数据根目录（测试时可指向临时目录）")
    args = ap.parse_args()

    staging = os.path.join(args.data, "staging")
    wdir = os.path.join(staging, args.writer)
    os.makedirs(wdir, exist_ok=True)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    proposal_id = uuid.uuid4().hex[:12]
    rec = {
        "writer_id": args.writer,
        "parent_id": args.parent,
        "kind": args.kind,
        "project": args.project or os.getcwd(),
        "target": args.target,
        "op": args.op,
        "key": args.key,
        "content": args.content,
        "evidence": args.evidence,
        "proposal_id": proposal_id,
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(wdir, stamp + "-" + proposal_id[:8] + ".json")
    # 写临时文件再原子改名（2026-09-26 修）：直接 open(path,"x") 会让并发的
    # --scan 有可能读到**只写了一半**的 JSON -> 解析失败 -> 该提案在那一轮隐身
    # （写者以为交了，读者看不到）。同目录改名在 POSIX/NTFS 上都是原子的。
    tmp = path + ".part-" + uuid.uuid4().hex[:8]
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print(path)


if __name__ == "__main__":
    main()
