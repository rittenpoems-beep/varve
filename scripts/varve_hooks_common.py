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
GLOBAL_STATUS = os.path.join(DATA_ROOT, "STATUS.md")
LIMIT = 3500
SECTION_RE = re.compile(r"<!-- =+ 工程状态区.*?<!-- =+ 工程状态区 结束.*?-->", re.DOTALL)


def find_status(cwd=None):
    """全局卡（2026-09-24 用户拍板）：不再按项目定义，任何框架 / 任何目录共用一张卡。

    旧行为（从 cwd 向上 6 层找 STATUS.md）已退役。取舍：放弃"项目级隔离"，
    换取跨框架 / 跨目录的可用性——归属用卡内【项目】前缀表达，而不是目录结构。
    """
    return GLOBAL_STATUS if os.path.exists(GLOBAL_STATUS) else None


def render_status(cwd):
    """读全局卡的工程状态区（截断）。"""
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
    return "【记忆系统 · 全局工程状态（追加注入）】\n\n" + seg + "\n"


def contract_hint():
    """L1/L3 读写契约（极简；随状态卡一起在会话首次用户消息尾部注入）。

    2026-09-24 用户要求：契约不进 SKILL.md 头部（那属于固定前缀，改了会碎缓存），
    改为尾部注入一次。形态与 L2 状态注入完全一致。
    """
    home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    recall = os.path.join(home, "scripts", "recall.py")
    return (
        "\n[Varve 契约 · 尾部注入]\n"
        "- 回溯：提到「上次 / 之前 / 当时 / 那个坑」或要断言历史事实时，先检索再回答——\n"
        "  python -X utf8 \"" + recall + "\" \"关键词1\" \"关键词2\"\n"
        "  （多变体一次调用；中文 2 字词直接查，短词走字面兜底）检索不到就直说「没找到」，不要编。\n"
        "- 收尾：任务状态有推进 → 更新全局卡 " + GLOBAL_STATUS + "；可迁移的教训 → records；环境变化 → ENVIRONMENT.md。\n"
        "- 环境/工具异常（命令报错、依赖缺失、路径不对）→ 读 " + os.path.join(DATA_ROOT, "ENVIRONMENT.md") + "。\n"
        "  该文件**不注入**（harness 已自动注入 cwd/shell/日期/时区/工作区根/权限档，重复注入纯属浪费）。\n"
        "- 兜底：当前目录未被登记时（projects.md 无此项），跑 pwsh -NoProfile -File \""
        + os.path.join(home, "scripts", "sync-projects.ps1") + "\"（hook 正常时应自动完成）。\n"
    )


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
            # 兼容单/双引号两种写法（盲测 #8：Codex 若写双引号，旧正则永远发现不到）
            m = re.match(r"""\s*\[projects\.(?:'([^']+)'|"([^"]+)")\]""", line)
            if m:
                paths.append(m.group(1) or m.group(2))
    with open(pf, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    new = [p for p in dict.fromkeys(paths) if p not in content]
    if new:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with open(pf, "a", encoding="utf-8") as fh:
            for p in new:
                fh.write("- " + stamp + " 发现: " + p + "\n")
