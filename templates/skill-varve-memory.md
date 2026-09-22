---
name: varve-memory
description: 跨会话记忆检索与写入（Varve）。当用户提到过去（"上次/之前/当时/曾经/那个坑/那个方案"）、需要归因（"为什么这么定"）、或你要断言历史事实（"我们定过 X"）时使用——先检索再回答，查不到就直说。会话收尾更新状态卡时也用。
---

# Varve 记忆检索与写入

安装路径：`<VARVE_HOME>`；数据目录：`<VARVE_DATA>`。

## 层 1 · 判据（出现任一 → 必须先检索、后回答）

- 用户提到**过去**：「上次 / 之前 / 当时 / 那天 / 曾经 / 以前」
- 用户使用**指代**：「那个坑 / 那个方案 / 那件事」
- 用户问**归因**：「为什么这么设计 / 当时怎么想的」（答案在过去）
- 你准备**断言历史事实**：「我们定过 X」「之前用的是 Y」

## 怎么检索

```powershell
python -X utf8 "<VARVE_HOME>\scripts\recall.py" "关键词1" "关键词2" [--since 7d] [--workspace <路径>] [--json]
```

- **多变体一次调用**（程序内部 RRF 合并，比多次单查快且省往返）
- 中文 <3 字词（如「索引」）自动走字面匹配兜底
- 返回含**坐标**（`src: 行号段`）——需要原文时按坐标直接读对应行段
- 查不到 → **直说查不到**，不要补全、不要编

## 层 3 · 断言前置（硬规则）

任何时候要说「我们之前 / 上次 / 曾经…」，**必须先检索**；检索不到就直说检索不到。

## 写入（收尾三问）

1. 这轮有**可迁移原则**（机制/教训）吗？→ 写 `<VARVE_DATA>\records\<项目>.md`（四要素）
2. **环境**变了吗？→ 更新 `<VARVE_DATA>\ENVIRONMENT.md`（动态快照）
3. **任务状态**推进了吗？→ 更新项目 `STATUS.md` 的「工程状态区」

多写者并发（子 agent / 多会话）时，先写暂存提案再裁决：

```powershell
python -X utf8 "<VARVE_HOME>\scripts\staging\staging_write.py" --writer <你的id> --kind session `
    --target status.task --key "进行中/条目名" --content "改动内容"
python -X utf8 "<VARVE_HOME>\scripts\staging\staging_merge.py" --scan --out plan.json
```
