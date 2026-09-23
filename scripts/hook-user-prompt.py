#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-user-prompt.py — UserPromptSubmit：① 消费 pending（L2 状态**追加注入**）② 历史信号提醒。

2026-09-23 定稿：
- 判定/注入拆开：SessionStart 只写 pending；本 hook 把状态区**追加**在当前请求尾部
  （不动固定前缀 —— 逃开"注入即碎缓存"的老问题）
- 同时保留 L3 层 2 的信号词提醒（同一行注入，成本零）

输出为空 = 无话可说（不污染会话）；任何异常静默退出。

跨框架：加 `--json-output` 时改用 Claude Code 的 JSON 形态输出
（`hookSpecificOutput.additionalContext`，官方文档明确"追加在用户消息之后"）。
不带参数时输出纯文本 stdout，供 Codex 使用。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import varve_hooks_common as C  # noqa: E402

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECALL = os.path.join(HOME, "scripts", "recall.py")

SIGNALS = re.compile(
    r"上次|之前|当时|那天|曾经|以前|那个坑|那个方案|那件事|我们做过|我们不是|"
    r"为什么.{0,6}(定|设计|改|选)|怎么.{0,4}想"
)


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0

    parts = []

    # ① 消费 pending -> 追加注入 L2 状态区（+ staging 提示）
    sid = str(payload.get("session_id") or "")
    if sid:
        info = C.take_pending(sid)
        if info:
            status = C.render_status(info.get("cwd") or "")
            if status:
                parts.append(status)
            # 契约句随状态卡一起注入（仅会话首次消费时）——替代原先放在 SKILL.md 头部
            # 的契约（头部属于固定前缀，改动会碎缓存；2026-09-24 用户要求改尾部）
            parts.append(C.contract_hint())
            parts.append(C.init_hint())          # 仅安装后首次出现（LLM 回问 L1 补充）
            parts.append(C.staging_hint())

    # ② 历史回溯信号 -> 检索提醒
    prompt = str(payload.get("prompt") or "")
    m = SIGNALS.search(prompt) if prompt else None
    if m:
        parts.append("[Varve 记忆] 本条疑似涉及历史事实（命中「" + m.group(0) + "」）。"
                     "回答前先检索：python -X utf8 \"" + RECALL + "\" \"关键词1\" \"关键词2\"；"
                     "检索不到就直说找不到，不要编。")

    out = "\n".join(p for p in parts if p)
    if out.strip():
        if "--json-output" in sys.argv:
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": out,
                }
            }, ensure_ascii=False))
        else:
            sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
