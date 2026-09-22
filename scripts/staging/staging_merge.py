#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""staging_merge.py - 机械层扫描 / 执行层提交（主 agent 的两只手）。

分工（见 docs/并发写入设计-staging与主agent判定.md 第三节）：
  机械层（本脚本 --scan）：收集未合并提案 -> 排序 -> 去重 -> 分组 -> 标冲突 -> 输出待判定清单
  语义层（主 agent）     ：裁决冲突、定去向 -> 产出提交计划 JSON
  执行层（本脚本 --plan） ：按计划原子写正式区 -> 提案归档

降级兜底：--scan --auto-target <file> 可生成"机械计划"（无冲突项直接追加到指定文件），
供主 agent 缺席时使用（宁可重复，不可丢）。

用法:
  python -X utf8 staging_merge.py --scan [--out plan.json]
  python -X utf8 staging_merge.py --scan --auto-target <文件> --out plan.json
  python -X utf8 staging_merge.py --plan plan.json
"""
import argparse
import glob
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

DEFAULT_DATA = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")


def load_proposals(data):
    staging = os.path.join(data, "staging")
    rows = []
    for wdir in sorted(glob.glob(os.path.join(staging, "*"))):
        if not os.path.isdir(wdir):
            continue
        name = os.path.basename(wdir)
        if name.startswith(".") or name == "archive":
            continue
        for p in sorted(glob.glob(os.path.join(wdir, "*.json"))):
            try:
                with open(p, encoding="utf-8") as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            rec["_path"] = p
            rows.append(rec)
    rows.sort(key=lambda r: (r.get("ts", ""), r.get("writer_id", ""), r.get("proposal_id", "")))
    return rows


def scan(data):
    rows = load_proposals(data)
    seen = set()
    unique = []
    duplicates = 0
    for r in rows:
        sig = (r.get("target"), r.get("op"), r.get("key"), r.get("content"))
        if sig in seen:
            duplicates += 1
            continue
        seen.add(sig)
        unique.append(r)

    groups = {}
    for r in unique:
        groups.setdefault((r.get("target"), r.get("key")), []).append(r)

    clean, conflicts = [], []
    for (target, key), items in groups.items():
        contents = {i.get("content") for i in items}
        if len(contents) == 1:
            clean.append(items[0])
        else:
            conflicts.append({
                "target": target,
                "key": key,
                "variants": [
                    {"content": i.get("content"), "writer_id": i.get("writer_id"),
                     "ts": i.get("ts"), "proposal_id": i.get("proposal_id"), "_path": i["_path"]}
                    for i in items
                ],
            })
    return rows, unique, duplicates, clean, conflicts


def atomic_append(path, text):
    """读-改-写 + 原子替换：杜绝半写状态。"""
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
    new = old + text
    tmp = path + ".tmp-" + uuid.uuid4().hex[:8]
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new)
    os.replace(tmp, path)


def archive(path, data):
    staging = os.path.join(data, "staging")
    rel = os.path.relpath(path, staging)
    dest = os.path.join(staging, "archive", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.replace(path, dest)
        return True
    except FileNotFoundError:
        return False  # 已被其他合并者处理 -> 幂等


def do_scan(args):
    rows, unique, dup, clean, conflicts = scan(args.data)
    report = {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(rows),
        "unique": len(unique),
        "duplicates": dup,
        "clean": [
            {"target": r.get("target"), "op": r.get("op"), "key": r.get("key"),
             "content": r.get("content"), "writer_id": r.get("writer_id"),
             "ts": r.get("ts"), "proposal_id": r.get("proposal_id"), "_path": r["_path"]}
            for r in clean
        ],
        "conflicts": conflicts,
    }
    print("total=" + str(len(rows)) + " unique=" + str(len(unique)) +
          " duplicates=" + str(dup) + " clean=" + str(len(clean)) + " conflicts=" + str(len(conflicts)))

    if args.auto_target:
        lines = []
        for r in clean:
            if r.get("op") == "append":
                lines.append("- [" + r.get("ts", "") + "] " + r.get("content", "") + "\n")
        plan = {
            "plan_id": uuid.uuid4().hex[:12],
            "mode": "mechanical-fallback",
            "writes": ([{"path": args.auto_target, "op": "append", "text": "".join(lines)}]
                       if lines else []),
            "consumed": [r["_path"] for r in clean if r.get("op") == "append"],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2)
        print("auto_plan=" + args.out + " writes=" + str(len(plan["writes"])) +
              " consumed=" + str(len(plan["consumed"])))
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("report=" + args.out)


def do_plan(args):
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    pid = plan.get("plan_id") or hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    state_path = os.path.join(args.data, "staging", ".merged.json")
    state = {"applied_plans": []}
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            state = {"applied_plans": []}
    if pid in (state.get("applied_plans") or []):
        print("plan_id=" + pid + " already_applied=1 committed_writes=0 archived=0")
        return
    n_writes = 0
    for w in plan.get("writes", []):
        if w.get("op") == "append" and w.get("text"):
            atomic_append(w["path"], w["text"])
            n_writes += 1
    n_arch = 0
    for p in plan.get("consumed", []):
        if archive(p, args.data):
            n_arch += 1
    state.setdefault("applied_plans", []).append(pid)
    state["last_plan_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp-" + uuid.uuid4().hex[:8]
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)
    print("plan_id=" + pid + " committed_writes=" + str(n_writes) + " archived=" + str(n_arch))


def main():
    ap = argparse.ArgumentParser(description="staging 扫描 / 提交")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--out", default="plan.json", help="--scan 的输出文件")
    ap.add_argument("--auto-target", default="", help="机械兜底：无冲突项追加到该文件")
    ap.add_argument("--plan", default="", help="执行提交计划")
    args = ap.parse_args()
    if args.plan:
        do_plan(args)
    else:
        do_scan(args)


if __name__ == "__main__":
    main()
