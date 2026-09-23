#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""env-scan.py — 环境扫描，产出可写进 ENVIRONMENT.md 的「自动探测区」（markdown 表格）。

设计原则（2026-09-24）：**只扫 harness 框架层不注入的项**。
Codex 每次会话已自动注入：cwd / shell / current_date / timezone / workspace_roots / permission_profile。
重复记录这些属于纯浪费（双份 token，还可能不一致），因此本脚本刻意跳过它们。

用法：
  python -X utf8 env-scan.py            # 打印 markdown 片段
  python -X utf8 env-scan.py --update <ENVIRONMENT.md>   # 就地更新 AUTO-SCAN 区
"""
import argparse
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BEGIN = "<!-- AUTO-SCAN-BEGIN -->"
END = "<!-- AUTO-SCAN-END -->"


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        return out[0].strip() if out else ""
    except Exception:
        return ""


def tool_version(name):
    if not shutil.which(name):
        return "(未安装)"
    flag = {"node": "-v", "rg": "--version", "curl.exe": "--version"}.get(name, "--version")
    first = run([name, flag])
    return first[:80] if first else "(已安装)"


def sqlite_caps():
    v = sqlite3.sqlite_version
    caps = []
    for name, sql in (("FTS5", "CREATE VIRTUAL TABLE t USING fts5(x)"),
                      ("trigram", "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")):
        try:
            con = sqlite3.connect(":memory:")
            con.execute(sql)
            con.close()
            caps.append(name)
        except Exception:
            pass
    return v + "（" + ("+".join(caps) if caps else "无 FTS5") + "）"


def build_rows():
    home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")
    edge = ""
    for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(cand):
            edge = cand
            break
    rows = [
        ("扫描时间", time.strftime("%Y-%m-%d %H:%M")),
        ("操作系统", platform.system() + " " + platform.release() + " (" + platform.version() + ")"),
        ("PowerShell", run(["pwsh", "-NoProfile", "-Command", "($PSVersionTable.PSVersion).ToString()"]) or "(未安装)"),
        ("Python", sys.version.split()[0] + " @ " + sys.executable),
        ("Node.js", tool_version("node")),
        ("git", tool_version("git")),
        ("ripgrep", tool_version("rg")),
        ("curl", tool_version("curl.exe")),
        ("Edge（headless 备用通道）", edge or "(未找到)"),
        ("SQLite", sqlite_caps()),
        ("VARVE_HOME", home),
        ("VARVE_DATA", data),
        ("会话日志根", os.path.join(os.path.expanduser("~"), ".codex", "sessions")),
    ]
    return rows


def render(rows):
    out = [BEGIN,
           "> 本节由 `scripts/env-scan.py` 生成（勿手改）。**只记 harness 不注入的项**——",
           "> cwd / shell / 日期 / 时区 / workspace_roots / 权限档由 Codex 每次会话自动注入，重复记录纯属浪费。",
           "",
           "| 项 | 值 |",
           "|---|---|"]
    for k, v in rows:
        out.append("| " + k + " | " + str(v).replace("|", "\\|") + " |")
    out.append(END)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="环境扫描（只记 harness 不注入的项）")
    ap.add_argument("--update", default="", help="就地更新该 md 文件的 AUTO-SCAN 区")
    args = ap.parse_args()

    block = render(build_rows())
    if not args.update:
        print(block)
        return 0

    path = args.update
    if not os.path.exists(path):
        print("文件不存在：" + path)
        return 1
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if BEGIN in text and END in text:
        head = text[:text.index(BEGIN)]
        tail = text[text.index(END) + len(END):]
        new = head + block + tail
    else:
        # 首次：插到第一个二级标题之前（"当前事实"放最前，但不打断开头的说明引用块）
        lines = text.split("\n")
        idx = len(lines)
        for i, ln in enumerate(lines):
            if ln.startswith("## "):
                idx = i
                break
        new = "\n".join(lines[:idx]).rstrip() + "\n\n" + block + "\n\n" + "\n".join(lines[idx:])
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print("已更新：" + path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
