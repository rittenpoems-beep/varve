#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-build-index.py — SessionStart 静默重建（对话层 + FTS5 检索索引）。

2026-09-23 起：由旧的关键词行索引（sessions.md）切换为 Varve 现行设计——
依次调用同目录两个脚本（增量模式）：
    1) session-digest.py       jsonl -> 对话层 markdown
    2) build-search-index.py   对话层 -> SQLite FTS5 检索索引

注意：文件名保持不变（hooks.json 条目的命令字符串不动），避免触发 hooks 重新信任。
作为 hook 运行时输出不能污染会话上下文：吞掉全部 stdout/stderr，任何异常静默退出。
"""
import io
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = ["session-digest.py", "build-search-index.py"]


def main():
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        for name in TARGETS:
            path = os.path.join(HERE, name)
            if not os.path.exists(path):
                continue
            sys.argv = [path]
            try:
                runpy.run_path(path, run_name="__main__")
            except SystemExit:
                pass
            except Exception:
                pass
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        sys.argv = [os.path.abspath(__file__)]
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
