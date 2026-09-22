# Varve

**给 AI 编码助手（Codex）的跨会话记忆层**——三层结构（环境 / 状态 / 历史）、**只追加的注入**（不破坏 prompt 缓存）、**SQLite 全文检索**。

零 LLM 调用、零第三方依赖（Python 标准库 + PowerShell 7）。

> 名字来自地质学：**varve**（纹泥）是冰川湖底的年层沉积——一层记录一年，层层叠加、永不重写、可回溯到任意一层。这正是它的三条原则：**只追加、可重建、可定位**。

## 它解决什么

| 问题 | 常见做法 | 这里怎么做 |
|---|---|---|
| 新会话 = 失忆 | 手动贴上下文 | 三层记忆自动可用：环境 / 状态 / 历史 |
| 把历史全塞进上下文 | 一次灌几十万 token | 只注入轻量状态卡，细节按需检索 |
| 注入把 prompt 缓存打碎 | 无感知，成本暴涨 | **状态注入只追加在请求尾部**，不碰固定前缀 |
| 找历史靠记关键词 | `rg` 硬搜 | SQLite FTS5 全文索引 + 多变体检索 + **可回溯到原文行号** |
| 多会话并发写文件 | 整文件重写 → 丢更新 | staging 三段式：提案 → 裁决 → 原子提交 |

## 快速开始（3 步）

**环境要求**：Windows + PowerShell 7 + Python 3.10+（标准库含 SQLite FTS5）。

```powershell
# ① 安装：检查环境 → 建数据目录 → 生成 .codex/hooks.json → 装 Skill
pwsh -NoProfile -File scripts\install.ps1 -Project D:\your-project

# ② 在 Codex 里点一次 hooks 信任（New hook - review required，唯一手动步骤）

# ③ 验证（13 项体检）
pwsh -NoProfile -File scripts\doctor.ps1 -Project D:\your-project
```

装上后，每次会话启动会自动：**读取工程状态 → 重建检索索引**；你问"上次/之前/那个坑"这类问题时，会收到一行检索提醒。

**不想装 hook 也能用**：

```powershell
python -X utf8 scripts\session-digest.py          # 会话日志 -> SQLite
python -X utf8 scripts\build-search-index.py      # 建 FTS5 索引
python -X utf8 scripts\recall.py "关键词1" "关键词2"   # 检索（由粗到细）
python -X utf8 scripts\recall.py --timeline --since 14d
python -X utf8 scripts\recall.py "报错内容" --deep     # 加搜工具调用/输出层
```

## 工具一览

| 脚本 | 职责 |
|---|---|
| `install.ps1` | 安装：环境检查 / 数据目录 / hooks.json / Skill（幂等） |
| `doctor.ps1` | 环境体检 13 项（只读） |
| `check-env.py` | 探测 Python / SQLite / FTS5 / trigram |
| `session-digest.py` | 会话日志 → SQLite（对话历史 + 轨迹历史） |
| `build-search-index.py` | 建 FTS5 索引（内容未变则跳过） |
| `recall.py` | 检索 CLI：records → 对话历史 → 轨迹层（`--deep`） |
| `hook-session-start.py` | 会话启动：标记待注入（零输出） |
| `hook-user-prompt.py` | 用户消息：追加注入状态 + 历史信号词提醒 |
| `hook-build-index.py` | 静默重建索引（hook 包装） |
| `varve_hooks_common.py` | hook 共享逻辑 |
| `init.ps1` / `sync-projects.ps1` / `build-docs-index.ps1` | 初始化 / 工作区发现 / 文档索引 |
| `staging/` | 并发写入：提案 → 裁决 → 原子提交（含压测脚本） |
| `templates/` | 状态卡模板 / 记录模板 / Skill 模板 / AGENTS 规则句 |

## 工作原理（简版）

```
会话日志（只读）
   │  SessionStart 触发
   ▼
SQLite 单库 ── turns（对话历史：每轮问答）
              traces（轨迹历史：工具调用/输出/推理）
              + FTS5 全文索引（external content：内容只存一份）
   │  需要时检索
   ▼
三段漏斗：records（已提炼的推进记录）→ 对话历史 → 轨迹层
```

**两条硬规则**：

1. **注入只追加**——状态注入落在请求尾部，永不触碰固定前缀（SessionStart 只做标记，UserPromptSubmit 才追加）；
2. **降级永远可用**——任何环节失效都有退路，最差情况 = 文件 + 规则句。

## 已知限制（诚实标注）

- **目前只适配 Codex**（通过 `.codex/hooks.json` + 两个 hook）。架构上按适配层设计、便于扩展，但**其他框架的适配尚未实现**。
- 依赖 Codex 的会话日志格式（`~/.codex/sessions/**/*.jsonl`）。
- Windows / PowerShell 优先；Python 部分跨平台。

## License

[MIT](LICENSE)
