# 已修复清单（回归 / 压测对照用）

> **English note**: this document is written in Chinese. It is a regression ledger — each entry lists the
> problem, the fix, and a minimal "how to check for regression" recipe. For a project overview, see
> [README.en.md](README.en.md). The only non-Chinese content is this note.

> **用途**：其他 Agent（本地 / 云端）对本系统做压力测试时，逐条对照检查**是否复发**。
> 约定：每条给出最小的复发检查方式；全量体检跑 `python -X utf8 scripts/audit.py`，应为 `RESULT=PASS`。
> 来源标注：**[盲测]** = 2026-09-23 纯盲 Agent 交叉验证发现；**[自检]** = 本仓库 audit.py 发现；**[实况]** = 真实故障。

## 2026-09-24 · P0 批次

| # | 问题 | 修复点 | 复发检查 |
|---|---|---|---|
| FIX-001 | **[盲测]** `session-digest.py --limit N` 会清空未参与本轮的文件（数据破坏） | 孤儿清理基准改为**完整**文件列表（`all_files`） | `session-digest.py --data <临时库> --limit 3` 再用 `--limit 1` 跑一次，`SELECT COUNT(*) FROM turns` 不得下降 |
| FIX-002 | **[盲测]** 多词写成一个参数返回 0 条且**无报错**（0/20 静默失败） | `fts_quote()` 多词拆短语 + OR 连接 | `recall.py "compact pending"` 必须返回非 0 块（此前恒为 0） |
| FIX-003 | **[盲测]** 中文 2 字词在默认 limit 下 MISS（排序失败，非召回失败） | `run_like()` 加新近度 `ORDER BY date DESC, turn_no DESC` | `recall.py "压缩"` 默认 limit=5 应有结果 |
| FIX-004 | **[盲测]** `doctor.ps1` 承诺只读，实际触发索引 rebuild（写库） | `build-search-index.py --stats` 走只读连接（mode=ro），失败给可读提示 | `--stats` 前后 `SELECT v FROM meta WHERE k='index_built_at'` 不变 |
| FIX-005 | **[盲测]** `install.ps1` 检查失败仍退出码 0 | 结尾 `exit 0 / exit 1` | `install.ps1 -Project <不存在路径>` → `$LASTEXITCODE=1` |
| FIX-006 | **[盲测][实况]** SQLite 无 WAL / busy_timeout，写入中断产生 hot journal 后**只读访问全废** | 写路径加 `journal_mode=WAL` + `busy_timeout`；`recall.py` 只读失败时降级可写连接 | 库目录不应残留 `.db-journal`；`recall.py` 在库被占用时仍能返回结果 |
| FIX-007 | **[盲测]** `hook-session-start.py` docstring 与实现矛盾（compact 特例） | docstring 对齐实现 | 读文件头，不应再出现"compact → 直接注入" |
| FIX-008 | **[盲测]** `digest/` 目录残留（install 仍建、doctor 仍查） | install 不再创建、doctor 不再检查 | `install.ps1` / `doctor.ps1` 输出中不应出现 digest 相关行 |
| FIX-009 | **[盲测]** 只认单引号 `[projects.'path']`，双引号写法会静默漏发现工作区 | `varve_hooks_common.py` 与 `sync-projects.ps1` 同时兼容单/双引号 | 构造 `[projects."D:\\x"]`，跑 `sync-projects.ps1` 应登记成功 |
| FIX-010 | **[自检]** 46 个 `(session_id, turn_no)` 来自多个 source（resume 重叠 rollout 重复入库，稀释 BM25） | 写入时按 `(session_id, turn_no)` 跨 source 去重；一次性迁移清理历史数据 | `audit.py` 的 B 项应为 0 |
| FIX-011 | **[盲测]** `recall.py` 聚合结果的 `title` 字段实际是 FTS 片段预览 | 改名 `preview` | JSON 输出含 `preview` 字段、无 `title` |
| FIX-012 | **[审查]** 触发器时代 `build-search-index.py` 仍会全量 rebuild（每次会话启动白烧一次，~80MB 库下持续浪费） | 建触发器前探测 `had_triggers`，已存在即跳过；`--force` 仍可强制重建 | 有触发器的库跑 `build-search-index.py`，应输出 `sync triggers active, skip rebuild` |
| FIX-013 | **[审查]** `audit.py` C 项比 `index_built_at` / `content_updated_at`，触发器时代恒报「索引落后」 | 改为触发器存在性 + FTS 行数与内容行数对账 | `audit.py` 的 C 行应显示 `触发器 6/6` 且两侧行数相等 |
| FIX-014 | **[审查]** `init_hint` 的一次性标记放在 `pending/`，被 7 天清理器回收（约每 8 天复活一次） | 标记移到 `<VARVE_DATA>/.init_prompted`；`cleanup_pending` 跳过点开头文件 | 把标记 mtime 拨到 8 天前再跑清理，文件应仍在 |
| FIX-015 | **[审查]** `contract_hint` 注释说「随状态卡注入」，实现是无状态卡也注入 | 注释与实现对齐（实现是对的） | 状态卡缺失时，首轮注入仍应含契约句 |
| FIX-016 | **[审查]** `install.ps1` 只写项目级 hooks，与「全局 hooks」的文档描述不符 | 新增 `-Scope user\|project`（默认 `user`，写 `~/.codex/hooks.json`） | 不带参数运行，输出路径应为 `%USERPROFILE%\.codex\hooks.json` |
| FIX-017 | **[审查]** traces 跨 source 重叠未去重，稀释 `--deep` 的 BM25 | `run_traces` 按 (session, kind, 文本前 240 字符) 去重；查询侧多取再截断 | 对重叠会话跑 `recall.py <词> --deep`，同一条轨迹不应重复出现 |
| FIX-018 | **[审查]** `db_mb` 两处口径不一（`1e6` vs `1024²`） | 统一为 MB = 10^6 字节 | 两个脚本对同一库报出的数值应一致 |

## 2026-09-24 · 架构变更（非 bug；压测时注意语义已变）

| # | 变更 | 影响 |
|---|---|---|
| CHG-001 | L2 由"每项目一张卡"改为**全局卡** `<VARVE_DATA>/STATUS.md` | 任何框架 / 任何目录注入同一张卡；**不再有项目级隔离**（归属用卡内【项目】前缀表达） |
| CHG-002 | L1/L3 契约改由 hook 在**会话首次用户消息尾部**注入（`contract_hint()`） | 不再依赖 SKILL.md 头部；`.codex` 头部契约若出现即视为回归 |
| CHG-003 | 全局 hooks 落地 `~/.codex/hooks.json`（SS 判定 + SS 建索引 + UPS 注入） | 覆盖所有工作区；安装后需在 Codex UI 点一次信任 |
| CHG-004 | 新增 `audit.py`（月度审计，纯程序）+ `scripts/audit.py` 进工具表 | 体检项：一致性 / 重复入库 / 索引新鲜度 / 检索自检 / 体量 |

## 历史批次

（此前由 ISSUES.md 记录，编号 A–F；本轮起新的修复统一记到本文件。）
