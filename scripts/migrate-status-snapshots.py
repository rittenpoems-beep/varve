#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate-status-snapshots.py — 旧格式全局状态卡（五区文档）-> **快照流**。

背景（2026-09-25 用户定稿）：L2 从"就地改写的文档"改为"只增不减的完整快照序列"，
注入只取最后一条。旧格式的卡在新代码下会走降级路径（仍能注入），但不会积累历史；
本脚本把现有内容**整体转成第一条快照**，此后每次更新都在末尾追加新快照。

幂等：文件里已含快照标记则直接跳过。原文件自动备份为 `<文件>.bak-<日期>-before-snapshots`。

用法：
  python -X utf8 migrate-status-snapshots.py [--data <VARVE_DATA>] [--dry-run]
"""
import argparse
import os
import re
import shutil
import sys
import time

SNAPSHOT_MARK = "<!-- ===== 快照 ===== -->"
SECTION_RE = re.compile(r"<!-- =+ 工程状态区.*?<!-- =+ 工程状态区 结束.*?-->", re.DOTALL)

FILE_HEADER = (
    "<!-- ========== 工程状态区（全局卡 · 快照流） ========== -->\n\n"
    "> **快照流**（2026-09-25 定稿）：只增不减——每次更新在末尾**追加一条完整快照**；旧快照永不修改、永不删除。\n"
    "> 注入只取**最后一条**快照，所以上下文占用恒定，与历史长度无关。\n"
    "> 环境事实 → `ENVIRONMENT.md`；历史与事故 → `records/`。\n\n"
)

# 字段改名（旧 -> 新）。快照标题用 ###，字段用粗体，层级更干净。
RENAME = [
    ("### 最近完成（滚动，≤5 条）", "**本次结束（迁移带入）**"),
    ("### 目标", "**目标**"),
    ("### 进行中", "**进行中**"),
    ("### 下一步", "**下一步**"),
    ("### 待用户决策", "**待用户决策**"),
]


def convert(text):
    """返回 (新文本, 说明)。已是快照流时返回 (None, 'already')。"""
    if SNAPSHOT_MARK in text:
        return None, "already"
    m = SECTION_RE.search(text)
    body = m.group(0) if m else text
    # 剥掉首尾的区标记
    body = re.sub(r"^<!-- =+ 工程状态区.*?-->\s*", "", body)
    body = re.sub(r"\n?<!-- =+ 工程状态区 结束.*?-->\s*$", "", body)
    body = body.strip()
    # 去掉旧的 "## 工程状态" 标题（快照自带标题）
    body = re.sub(r"^##\s*工程状态\s*\n+", "", body)
    for old, new in RENAME:
        body = body.replace(old, new)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    out = (FILE_HEADER
           + SNAPSHOT_MARK + "\n\n"
           + "### " + stamp + " · 快照（迁移首条）\n\n"
           + body + "\n")
    return out, "converted"


def main():
    ap = argparse.ArgumentParser(description="把全局状态卡迁移为快照流")
    ap.add_argument("--data", default=os.environ.get("VARVE_DATA") or os.path.expanduser("~/.varve"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = os.path.join(args.data, "STATUS.md")
    if not os.path.exists(path):
        print("找不到全局卡：" + path)
        return 2
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    out, status = convert(text)
    if status == "already":
        print("已是快照流，跳过（幂等）：" + path)
        return 0
    print("迁移：" + path)
    print("  旧长度 " + str(len(text)) + " 字符 -> 新长度 " + str(len(out)) + " 字符")
    print("  快照条数：1（此后每次更新追加一条）")
    if args.dry_run:
        print("  [dry-run] 未写入。新文件预览（前 600 字符）：")
        print("-" * 60)
        print(out[:600])
        return 0
    bak = path + ".bak-" + time.strftime("%Y%m%d") + "-before-snapshots"
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    print("  已备份：" + bak)
    print("  已写入：" + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
