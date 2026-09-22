#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stress_test.py - staging 并发压测：验证零丢失、零损坏、幂等、冲突裁决闭环。

A. 多写者并发（20 writer x 5 条）
B. 同写者并发（1 writer x 10 进程）
C. 机械层扫描（去重/分组/标冲突）
D. 机械提交（仅无冲突项；冲突留给主 agent）
E. 幂等复跑同一 plan
F. 主 agent 裁决冲突（模拟：每组取最新 ts）-> 全部清零

用法: python -X utf8 stress_test.py [--data <测试数据目录>]
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
WRITE_PY = os.path.join(HERE, "staging_write.py")
MERGE_PY = os.path.join(HERE, "staging_merge.py")


def run_write(data, writer, key, content):
    return subprocess.run(
        [sys.executable, "-X", "utf8", WRITE_PY, "--data", data,
         "--writer", writer, "--kind", "session", "--target", "records",
         "--op", "append", "--key", key, "--content", content],
        capture_output=True, text=True,
    )


def run_merge(data, *extra):
    return subprocess.run(
        [sys.executable, "-X", "utf8", MERGE_PY, "--data", data] + list(extra),
        capture_output=True, text=True,
    )


def count_files(data):
    return len(glob.glob(os.path.join(data, "staging", "*", "*.json")))


def read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(tempfile.gettempdir(), "varve-staging-test"))
    args = ap.parse_args()
    data = args.data
    if os.path.exists(data):
        shutil.rmtree(data)
    os.makedirs(data, exist_ok=True)

    print("== A. 多写者并发：20 writer x 5 条 ==")
    tasks = [(f"w{j:02d}", f"k{i}", f"content-{j}-{i}") for j in range(20) for i in range(5)]
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(lambda t: run_write(data, t[0], t[1], t[2]), tasks))
    n_a = count_files(data)
    print("files_after_A=" + str(n_a) + " (expect 100)")

    print("== B. 同写者并发：1 writer x 10 进程 ==")
    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(lambda i: run_write(data, "same-writer", f"s{i}", f"same-{i}"), range(10)))
    n_b = count_files(data)
    print("files_after_B=" + str(n_b) + " (expect 110)")

    print("== C. 机械层扫描 ==")
    report_p = os.path.join(data, "report.json")
    r = run_merge(data, "--scan", "--out", report_p)
    print("scan: " + r.stdout.strip())
    report = json.loads(read(report_p))
    conflict_files = sum(len(g["variants"]) for g in report["conflicts"])
    print("report: unique=%d duplicates=%d clean=%d conflict_groups=%d conflict_files=%d" % (
        report["unique"], report["duplicates"], len(report["clean"]),
        len(report["conflicts"]), conflict_files))

    print("== D. 机械提交（仅无冲突项） ==")
    target = os.path.join(data, "out.md")
    auto_p = os.path.join(data, "auto-plan.json")
    r = run_merge(data, "--scan", "--auto-target", target, "--out", auto_p)
    print("auto: " + r.stdout.strip())
    r = run_merge(data, "--plan", auto_p)
    print("commit: " + r.stdout.strip())
    n_d = count_files(data)
    lines_d = len([l for l in read(target).splitlines() if l.strip()])
    print("files_after_D=" + str(n_d) + " (expect " + str(conflict_files) + " = 冲突项留守)")
    print("out_lines=" + str(lines_d) + " (expect " + str(len(report["clean"])) + ")")

    print("== E. 幂等复跑同一 plan ==")
    before = read(target)
    r = run_merge(data, "--plan", auto_p)
    print("rerun: " + r.stdout.strip())
    idem1 = (read(target) == before)
    print("idempotent=" + str(idem1))

    print("== F. 主 agent 裁决冲突（模拟：每组取最新 ts） ==")
    r = run_merge(data, "--scan", "--out", report_p)
    print("rescan: " + r.stdout.strip())
    rep2 = json.loads(read(report_p))
    w_lines, consumed = [], []
    for grp in rep2["conflicts"]:
        latest = sorted(grp["variants"], key=lambda v: v["ts"])[-1]
        w_lines.append("- [adjudicated " + grp["key"] + "] " + latest["content"] + "\n")
        consumed.extend(v["_path"] for v in grp["variants"])
    adj_p = os.path.join(data, "adjudicate-plan.json")
    with open(adj_p, "w", encoding="utf-8") as fh:
        json.dump({"plan_id": "adjudicate-demo", "mode": "coordinator-adjudicated",
                   "writes": [{"path": target, "op": "append", "text": "".join(w_lines)}],
                   "consumed": consumed}, fh, ensure_ascii=False, indent=2)
    r = run_merge(data, "--plan", adj_p)
    print("adjudicated: " + r.stdout.strip())
    n_f = count_files(data)
    print("files_after_F=" + str(n_f) + " (expect 0)")

    ok = (n_a == 100 and n_b == 110 and report["duplicates"] == 0
          and n_d == conflict_files and lines_d == len(report["clean"])
          and idem1 and n_f == 0)
    print("RESULT=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
