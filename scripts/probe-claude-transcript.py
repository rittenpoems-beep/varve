#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe-claude-transcript.py — 采样 Claude Code 的会话日志结构（供实现索引适配用）。

为什么需要它：Varve 的索引侧（session-digest）目前只认 Codex 的 rollout 格式。
Claude Code 的 transcript 落在 `~/.claude/projects/**/*.jsonl`，但**字段结构未被验证过**，
不猜格式——先在真机上采一次结构样本，再据此实现解析器。

输出：只报"结构"（键名、类型、条数、长度），**不输出正文内容**，可直接贴回。

用法：
  python -X utf8 probe-claude-transcript.py
  python -X utf8 probe-claude-transcript.py --max-files 3
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def shape(v, depth=0):
    """返回值的类型签名；dict 只展开一层键名，避免泄露内容。"""
    if isinstance(v, dict):
        if depth >= 2:
            return "{...}"
        return "{" + ", ".join(k + ":" + shape(val, depth + 1) for k, val in list(v.items())[:12]) + "}"
    if isinstance(v, list):
        return "[" + (shape(v[0], depth + 1) if v else "") + "]x" + str(len(v))
    if isinstance(v, str):
        return "str(" + str(len(v)) + ")"
    return type(v).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-files", type=int, default=3)
    args = ap.parse_args()

    if not os.path.isdir(ROOT):
        print("未找到 " + ROOT + " —— 请确认本机装有 Claude Code 且至少用过一次。")
        return 1
    files = sorted(glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True))
    print("找到 transcript 文件: " + str(len(files)) + " 个")
    if not files:
        return 1

    for path in files[-args.max_files:]:
        print("")
        print("=== " + os.path.relpath(path, ROOT) + "  (" + str(os.path.getsize(path)) + " B) ===")
        kinds = Counter()
        shapes = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                try:
                    obj = json.loads(line)
                except Exception:
                    kinds["<解析失败>"] += 1
                    continue
                t = str(obj.get("type") or obj.get("role") or "?")
                kinds[t] += 1
                if t not in shapes and i <= 200:
                    shapes[t] = shape(obj)
        print("  记录类型分布: " + ", ".join(k + "=" + str(v) for k, v in kinds.most_common(8)))
        for t, s in list(shapes.items())[:4]:
            print("  [" + t + "] 结构: " + s[:400])
    print("")
    print("把以上输出贴回即可（不含正文内容）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
