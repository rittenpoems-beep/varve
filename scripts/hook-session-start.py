#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-session-start.py — SessionStart：**只做判定与标记**，不注入（保缓存安全）。

2026-09-23 定稿（判定与注入拆开，用户拍板）：
- startup / resume / clear / compact → 统一只写 pending 标记；真正的注入由
  UserPromptSubmit **追加**在请求尾部完成（永不触碰固定前缀）
- 原「compact 例外：直接注入」已于 2026-09-23 取消（下方正文同此，docstring 曾滞后于实现）
- staging 待裁决提示并入 pending，随 L2 状态一起在 UPS 时给出

任何异常静默退出（绝不影响会话启动）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import varve_hooks_common as C  # noqa: E402


def main():
    raw = sys.stdin.read()
    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
    cwd = payload.get("cwd") or ""
    sid = payload.get("session_id") or ""
    source = str(payload.get("source") or "").lower()

    try:
        C.sync_projects()
    except Exception:
        pass

    if not sid:
        return 0

    # 所有容器事件统一：只写 pending，由 UserPromptSubmit 追加注入。
    # compact 也走这条路（2026-09-23 起，取消原先的"compact 直接注入"例外）：
    #   - compact 后的 continuation 是"继续完成当前任务"，状态卡的价值在下一轮新问题才最大
    #   - 真需要状态时，模型可按 Skill 契约自行读 STATUS.md（降级路径始终在）
    #   - 好处：注入永不触碰固定前缀，形态统一、少一个特例
    C.write_pending(sid, cwd, source)
    C.cleanup_pending()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
