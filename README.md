# Varve

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | **简体中文**

**给 AI 编码助手（Codex 及后续框架）的跨会话记忆层**——三层结构（环境 / 状态 / 历史）、**只追加的注入**（不破坏 prompt 缓存）、**SQLite 全文检索**、**全局状态卡**（跨工作区 / 跨框架）。

零 LLM 调用、零第三方依赖（Python 标准库 + PowerShell 7）。

> 名字来自地质学：**varve**（纹泥）是冰川湖底的年层沉积——一层记录一年，层层叠加、永不重写、可回溯到任意一层。这正是它的三条原则：**只追加、可重建、可定位**。

> 状态：早期可用（v0.1）。已在真实项目上连续使用，索引层带审计与回归用例（见 [FIXES.md](FIXES.md)）。

## 目录

- [它解决什么](#它解决什么)
- [快速开始](#快速开始)
- [工具一览](#工具一览)
- [工作原理](#工作原理)
- [数据与隐私](#数据与隐私)
- [已知限制](#已知限制)
- [反馈与贡献](#反馈与贡献)
- [许可](#许可)

## 它解决什么

| 问题 | 常见做法 | 这里怎么做 |
|---|---|---|
| 新会话 = 失忆 | 手动贴上下文 | 三层记忆自动可用：环境 / 状态 / 历史 |
| 把历史全塞进上下文 | 一次灌几十万 token | 只注入轻量状态卡，细节按需检索 |
| 注入把 prompt 缓存打碎 | 无感知，成本暴涨 | **状态注入只追加在请求尾部**，不碰固定前缀 |
| 找历史靠记关键词 | `rg` 硬搜 | SQLite FTS5 全文索引 + 多变体检索 + **可回溯到原文行号** |
| 多会话并发写文件 | 整文件重写 → 丢更新 | staging 三段式：提案 → 裁决 → 原子提交 |
| 换目录 / 换框架就失忆 | 每个项目各装一套 | **全局卡**：任何目录共用一张状态卡，机制层不依赖"项目"定义 |

## 兼容性

Varve 目前适配两个框架，**不打算再扩**：

| 框架 | 状态卡注入 | 历史检索 | 说明 |
|---|---|---|---|
| **Codex** | ✅ 已适配 · 端到端实测 | ✅ | 主力目标 |
| **Claude Code** | ✅ 已适配 · 脚本层验证 | ⏳ 待样本 | 与 Codex 同构，同一对脚本复用 |
| 其他框架 | ❌ 不适用 | ❌ | 见下方定位说明 |

### 为什么只做这两家

Varve 的目标用户是**多项目并行的重度 agent 开发者**——把 CLI harness 当主力工具、对 token 成本敏感、愿意配置 hook。这类人的工具选择集中在 Codex 和 Claude Code。

其他类别（IDE 类如 Cursor / Trae / Qoder，办公类如 WorkBuddy，库类如 LangChain）不是"暂时没做"，是**不适用**：它们没有"尾部追加注入"这条通道，而 Varve 缓存安全的核心设计正建立在它之上。

### 各框架需要什么

**Codex**

- 依赖：Windows + PowerShell 7 + Python 3.10+（stdlib 含 SQLite FTS5）
- 安装：`pwsh -NoProfile -File scripts\install.ps1 -Project <你的项目>`
- 手动步骤：在 Codex UI 里点一次 hooks 信任

**Claude Code**

- 依赖：Python 3.10+
- 安装：`pwsh -NoProfile -File scripts\install-claude.ps1`（默认写用户级 `~/.claude/settings.json`；`-Scope project -Project <目录>` 装到项目级）
- 手动步骤：无（settings.json 不需要信任流程）
- 上限：`additionalContext` 10,000 字符（当前状态卡约 2.5k，安全）
- ⚠️ 现状：**状态卡注入**已实现（按官方 hook 契约 + 模拟 payload 验证）；**历史检索暂不支持**——Claude Code 的 transcript 格式未验证，先在有 Claude Code 的机器上跑 `python -X utf8 scripts\probe-claude-transcript.py` 采样，再据此补索引适配

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

| 内容 | 说明 |
|---|---|
| `install.ps1` | 安装：环境检查 / 数据目录 / hooks.json / Skill（幂等） |
| `doctor.ps1` | 环境体检 13 项（只读） |
| `check-env.py` | 探测 Python / SQLite / FTS5 / trigram |
| `session-digest.py` | 会话日志 → SQLite（对话历史 + 轨迹历史） |
| `build-search-index.py` | 建 FTS5 索引（内容未变则跳过） |
| `recall.py` | 检索 CLI：records → 对话历史 → 轨迹层（`--deep`） |
| `audit.py` | 记忆库审计：一致性 / 重复入库 / 索引新鲜度 / 检索自检 / 体量 |
| `env-scan.py` | 环境扫描：只记 harness 不注入的项，刷新 `ENVIRONMENT.md` 的自动探测区 |
| `hook-session-start.py` | 会话启动：标记待注入（零输出） |
| `hook-user-prompt.py` | 用户消息：追加注入状态 + 历史信号词提醒 |
| `hook-build-index.py` | 静默重建索引（hook 包装） |
| `varve_hooks_common.py` | hook 共享逻辑 |
| `init.ps1` / `sync-projects.ps1` / `build-docs-index.ps1` | 初始化 / 工作区发现 / 文档索引 |
| `staging/` | 并发写入：提案 → 裁决 → 原子提交（含压测脚本） |
| `templates/` | 状态卡模板 / 记录模板 / Skill 模板 / AGENTS 规则句 |
| `FIXES.md` | 已修复清单（每条含"复发检查"方法，供回归对照） |

> 命名约定：可执行脚本用连字符（`hook-user-prompt.py`），可导入的 Python 模块用下划线（`varve_hooks_common.py`）。

## 工作原理（简版）

```text
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

**全局卡（2026-09-24 起）**：状态卡只有一张 —— `<VARVE_DATA>\STATUS.md`。不按项目定义、不依赖目录结构，
任何框架 / 任何目录的会话都注入同一张（归属用条目里的【项目】前缀表达）。取舍见下方「已知限制」。

**索引的一致性**：`turns` / `traces` 使用**顺序稳定 id**（写入即定，删除重插不变），并通过 SQLite 触发器与 FTS5 索引**实时同步**——
不存在"内容已更新但索引还是旧的"的中间窗口，也不需要每次全量重建。

## 数据与隐私

- 数据全部落在本机：会话日志（`~/.codex/sessions/`）**只读**，派生的 SQLite 库与状态卡都在 `<VARVE_DATA>` 下，**不上传任何地方**。
- 库是**可重建**的：删掉 `<VARVE_DATA>/index/` 后跑一次 `session-digest.py` + `build-search-index.py` 即可从原始日志重算。
- 检索是**本地全文匹配**（SQLite FTS5），没有 embedding、没有外部 API 调用。
- 若要把本项目用于团队共享，注意 `<VARVE_DATA>/STATUS.md` 会包含你的任务状态——建议放进 `.gitignore` 或单独的私有目录。

## 已知限制（诚实标注）

- **目前只适配 Codex**（通过 `.codex/hooks.json` + 两个 hook）。架构上按适配层设计、便于扩展，但**其他框架的适配尚未实现**。
- 依赖 Codex 的会话日志格式（`~/.codex/sessions/**/*.jsonl`）。
- Windows / PowerShell 优先；Python 部分跨平台。
- **状态卡是全局的**：不按项目隔离，多项目任务状态混在一张卡里（用【项目】前缀区分）——为跨框架可用性做的主动取舍。
- **全局 hooks 需手动点一次信任**：`~/.codex/hooks.json` 内容变更后，Codex 会要求重新信任才生效。

## 反馈与贡献

- 发现 bug 或行为不符：请附上 `python -X utf8 scripts/audit.py` 的输出，多数问题能据此定位。
- 提交修复后请顺带更新 [FIXES.md](FIXES.md)（问题 / 修复点 / 复发检查三段式）。
- 已知问题与修复历史集中在 [FIXES.md](FIXES.md)，设计文档不在本仓库（公开版只含成品）。

## License

[MIT](LICENSE)
