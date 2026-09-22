#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""varve_hooks_common.py — Varve 两个 hook 的共享逻辑（状态渲染 / pending / staging 提示）。

2026-09-23：L2 注入拆分为"判定"（SessionStart 写 pending）与"注入"（UserPromptSubmit 追加）。
"""
import json
import os
import re
import time

DATA_ROOT = os.environ.get("VARVE_DATA") or os.path.join(os.path.expanduser("~"), ".varve")
PENDING_DIR = os.path.join(DATA_ROOT, "pending")
LIMIT = 3500
SECTION_RE = re.compile(r"<!-- =+ 工程状态区.*?<!-- =+ 工程状态区 结束.*?-->", re.DOTALL)


def find_status(cwd):
    d = cwd or ""
    for _ in range(6):
        if not d:
            return None
        cand = os.path.join(d, "STATUS.md")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(d)
        if not parent or parent == d:
            return None
        d = parent
    return None


def render_status(cwd):
    """读 cwd 向上第一个 STATUS.md 的工程状态区（截断）。"""
    status = find_status(cwd)
    if not status:
        return ""
    try:
        with open(status, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    m = SECTION_RE.search(text)
    if not m:
        return ""
    seg = m.group(0)
    if len(seg) > LIMIT:
        seg = seg[:LIMIT] + "\n...(已截断)"
    return "【记忆系统 · 当前项目工程状态（追加注入）】\n\n" + seg + "\n"


def staging_hint():
    """staging 有未合并提案时给一行提示。"""
    try:
        root = os.path.join(DATA_ROOT, "staging")
        if not os.path.isdir(root):
            return ""
        n = 0
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if os.path.isdir(d) and not name.startswith(".") and name != "archive":
                n += len([f for f in os.listdir(d) if f.endswith(".json")])
        if not n:
            return ""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "staging", "staging_merge.py")
        return ("\n[Varve staging] 有 " + str(n) + " 条待裁决提案（多写者临时改动）。"
                "结算：python -X utf8 \"" + script + "\" --scan\n")
    except Exception:
        return ""


def write_pending(sid, cwd, source):
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        with open(os.path.join(PENDING_DIR, sid + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"session_id": sid, "cwd": cwd, "source": source, "ts": int(time.time())},
                      fh, ensure_ascii=False)
    except Exception:
        pass


def take_pending(sid):
    """读取并删除该会话的 pending（消费即清）。"""
    p = os.path.join(PENDING_DIR, sid + ".json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = None
    try:
        os.remove(p)
    except OSError:
        pass
    return data


def cleanup_pending(max_age_days=7):
    try:
        now = time.time()
        for fn in os.listdir(PENDING_DIR):
            p = os.path.join(PENDING_DIR, fn)
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_days * 86400:
                os.remove(p)
    except Exception:
        pass


def sync_projects():
    """从 ~/.codex/config.toml 发现新工作区 -> 追加到 projects.md 观察区。"""
    cfg = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    pf = os.path.join(DATA_ROOT, "projects.md")
    if not (os.path.exists(cfg) and os.path.exists(pf)):
        return
    paths = []
    with open(cfg, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"\s*\[projects\.'([^']+)'\]", line)
            if m:
                paths.append(m.group(1))
    with open(pf, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    new = [p for p in dict.fromkeys(paths) if p not in content]
    if new:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with open(pf, "a", encoding="utf-8") as fh:
            for p in new:
                fh.write("- " + stamp + " 发现: " + p + "\n")
