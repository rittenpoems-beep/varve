# 已修复清单（回归 / 压测对照用）

> **English note**: this document is written in Chinese. It is a regression ledger — each entry lists the
> problem, the fix, and a minimal "how to check for regression" recipe. For a project overview, see
> [README.en.md](README.en.md). The only non-Chinese content is this note.

> **用途**：其他 Agent（本地 / 云端）对本系统做压力测试时，逐条对照检查**是否复发**。
> 约定：每条给出最小的复发检查方式；全量体检跑 `python -X utf8 scripts/audit.py`，应为 `RESULT=PASS`；
> 2026-09-26 起这些检查已固化为可执行用例：`python -X utf8 scripts/regression_test.py`（临时目录自建自清，不碰真实数据）。
> 来源标注：**[盲测]** = 2026-09-23 纯盲 Agent 交叉验证发现；**[审查]** = 外部交叉验证审查发现；**[自检]** = 本仓库 audit.py 发现；**[实况]** = 真实故障；**[实测]** = 本轮在干净副本上实际复现过。

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
| FIX-010 | **[自检]** 46 个 `(session_id, turn_no)` 来自多个 source（resume 重叠 rollout 重复入库，稀释 BM25） | 写入时按 `(session_id, turn_no)` 跨 source 去重；一次性迁移清理历史数据。**注意：这条的去重判据已被 FIX-027 / FIX-030 两次修正** —— 只看 `(session_id, turn_no)` 会把 resume 分叉会话的真实轮次一起删掉，现在只有**内容完全相同**才算重复入库 | 见 FIX-030（"B 项应为 0"这条旧检查已作废：合法 fork 会让 B 项非 0，库却完全健康） |
| FIX-011 | **[盲测]** `recall.py` 聚合结果的 `title` 字段实际是 FTS 片段预览 | 改名 `preview` | JSON 输出含 `preview` 字段、无 `title` |
| FIX-012 | **[审查]** 触发器时代 `build-search-index.py` 仍会全量 rebuild（每次会话启动白烧一次，~80MB 库下持续浪费） | 建触发器前探测 `had_triggers`，已存在即跳过；`--force` 仍可强制重建 | 有触发器的库跑 `build-search-index.py`，应输出 `sync triggers active, skip rebuild` |
| FIX-013 | **[审查]** `audit.py` C 项比 `index_built_at` / `content_updated_at`，触发器时代恒报「索引落后」 | 改为触发器存在性检查（**注意：本条当时的"FTS 行数 vs 内容行数对账"已被 FIX-020 证伪** —— external content 模式下行数恒等，是假绿，别再退回去） | `audit.py` 的 C 行应显示 `触发器 6/6`；一致性判据见 FIX-020 |
| FIX-014 | **[审查]** `init_hint` 的一次性标记放在 `pending/`，被 7 天清理器回收（约每 8 天复活一次） | 标记移到 `<VARVE_DATA>/.init_prompted`；`cleanup_pending` 跳过点开头文件 | 把标记 mtime 拨到 8 天前再跑清理，文件应仍在 |
| FIX-015 | **[审查]** `contract_hint` 注释说「随状态卡注入」，实现是无状态卡也注入 | 注释与实现对齐（实现是对的） | 状态卡缺失时，首轮注入仍应含契约句 |
| FIX-016 | **[审查]** `install.ps1` 只写项目级 hooks，与「全局 hooks」的文档描述不符 | 新增 `-Scope user\|project`（默认 `user`，写 `~/.codex/hooks.json`） | 不带参数运行，输出路径应为 `%USERPROFILE%\.codex\hooks.json` |
| FIX-017 | **[审查]** traces 跨 source 重叠未去重，稀释 `--deep` 的 BM25 | `run_traces` 按 (session, kind, 文本前 240 字符) 去重；查询侧多取再截断 | 对重叠会话跑 `recall.py <词> --deep`，同一条轨迹不应重复出现 |
| FIX-018 | **[审查]** `db_mb` 口径不一：`audit.py` 用 `1e6`、`build-search-index.py --stats` 用 `1024²`、`doctor.ps1` 用 PowerShell 的 `1MB`（同为 `1024²`）—— 同一库三处报三个数 | 全部统一为 MB = 10^6 字节（三处都改了，不是两处） | 对同一库跑 `audit.py` / `build-search-index.py --stats` / `doctor.ps1`，三者报出的 MB 应一致 |

## 2026-09-24 · 架构变更（非 bug；压测时注意语义已变）

| # | 变更 | 影响 |
|---|---|---|
| CHG-001 | L2 由"每项目一张卡"改为**全局卡** `<VARVE_DATA>/STATUS.md` | 任何框架 / 任何目录注入同一张卡；**不再有项目级隔离**（归属用卡内【项目】前缀表达） |
| CHG-002 | L1/L3 契约改由 hook 在**会话首次用户消息尾部**注入（`contract_hint()`） | 不再依赖 SKILL.md 头部；`.codex` 头部契约若出现即视为回归 |
| CHG-003 | 全局 hooks 落地 `~/.codex/hooks.json`（SS 判定 + SS 建索引 + UPS 注入） | 覆盖所有工作区；安装后需在 Codex UI 点一次信任 |
| CHG-004 | 新增 `audit.py`（月度审计，纯程序）+ `scripts/audit.py` 进工具表 | 体检项：一致性 / 重复入库 / **索引一致性**（2026-09-26 起含 `--fix` 自动修复）/ 检索自检 / 体量 |

## 2026-09-26 · 外部审查批次

> 来源：用户交叉验证后整理的外部审查清单（10 类）。每条都先在干净副本上复现、再修，最后固化成
> `scripts/regression_test.py` 里的用例（`--only <名字>` 单跑）。并发与索引损坏类问题**都是实测复现的**，不是读代码推测。

| # | 问题 | 修复点 | 复发检查 |
|---|---|---|---|
| FIX-019 | **[审查][实测]** 两个 `session-digest.py` 并发写同一张表 → **丢数据 + FTS 索引损坏**，且损坏**静默**（`COUNT(*)` 对账照样相等）。批量场景 6/6 复现 | 跨进程文件锁 `RunLock`（`O_EXCL` 创建，拿不到锁就跳过本轮、下轮补齐，按 mtime 超时抢占死锁）；id 分配从进程内存挪进**写事务内**（`MAX(id)+1`）；每个源文件一个 `BEGIN IMMEDIATE` 事务；显式 DELETE+INSERT 取代 `INSERT OR REPLACE`；`recursive_triggers=ON` 让 REPLACE 的删除也触发 FTS 清理 | `--only digest-concurrent` |
| FIX-020 | **[审查][实测]** 索引已损坏，`audit.py` 的 C 项**恒真**：external content 下 `COUNT(turns_fts) == COUNT(turns)` 是同义反复。同一个损坏库上旧判据打印 PASS，`integrity-check` 报 `database disk image is malformed` | C 项改为三段：触发器在位 + `_docsize` 影子表**行集**对账（抓缺行/幽灵行）+ `integrity-check(rank=1)` **令牌级**完整性（必须可写连接）；新增 `--fix`（`session-digest.py --full` + `build-search-index.py --force`）并在修后**原地复检、重新数触发器** | `--only audit-integrity`、`--only audit-fix` |
| FIX-020a | **[自检][实测]** **上面那个 `--fix` 的第一版自己有数据毁灭 bug**（修完 FIX-020 后由独立验证 Agent 抓到）：`run_fix()` 调 `session-digest.py --full` 时**不带 `--sessions`**，于是退回默认的 `~/.codex/sessions` 重灌；而 digest 的孤儿清理按 `--sessions` 判定"文件已消失"，用其它目录建的库会被**整段删空** —— 删空后索引自洽，审计照样 `RESULT=PASS`（实测 turns 2 → 0、哨兵行消失、rc=0）。另一处：修复放在测量**之后**，同一份报告里 A/D/E 是修前值、C 是修后值（曾并排出现 "C turns 0行" 与 "E turns=4"） | ① `run_fix()` 透传 `--sessions`（新增同名 CLI 参数，默认与 `session-digest.py` 一致）② 重灌前拦截，不安全就**不修**并报错 ③ 修后比对内容指纹（`file_state` 来源集 + turns/traces 行数），"仍在磁盘上的来源"丢了即判失败 ④ `--fix` 挪到**所有测量之前**，全篇报告都是修后状态 | `--only audit-fix`（不存在/部分重叠/老库部分重叠 → 都拒绝且零删数据；指对 → 丢行重灌回来且 `RESULT=PASS`）|
| FIX-020b | **[自检][实测]** 上面 ② 的**拦截判据第一版又太松**（同一个独立验证 Agent 第二轮抓到）：取的是"库来源与 `--sessions` **至少有一个**重叠就放行"。可只要错误目录与该库**共享任意一个同名 rel 路径**（未同步完的镜像 / 只恢复了一个月的机器 / 部分备份），孤儿清理就会删掉其余来源；而兜底的 `lost` 判据**结构上抓不到** —— 被删的来源在给定目录下本就不存在。实测（变异测试夹具：库 3 源 15 轮，错目录只含 1 个同名路径）：`--fix` 把库从 **14 行 3 源删成 5 行 1 源**，仍打印 `RESULT=PASS`、`rc=0`；独立验证 Agent 用自己的夹具（5 源 10 轮）复现出同族结果：`orphans=4`、来源 5→2、turns 9→5 | 判据改成两层从严：① `session-digest.py` 每次运行把 `os.path.abspath(--sessions)` 记进 `meta.sessions_root`；`--fix` 时该记录**必须指向同一处**才放行（与磁盘文件在不在无关）② 老库没这个记录 → 退化为"**不许有任何来源**在 sessions 下找不到"（比"至少一个重叠"严得多）。拒绝时给出三条出路：指向建库目录 / 搬过目录就先跑一次 digest 更新记录 / 只重建索引用 `build-search-index.py --force` | `--only audit-fix`（第 2、3 条断言就测这个：部分重叠的目录必须被拒；把 `sessions_root` 删掉造出"老库"后再测同一目录，兜底判据也必须拒） |
| FIX-020c | **[自检]** 空库（`sessions.db` 存在但没有表）上跑 `--fix` 会抛 `sqlite3.OperationalError` 栈。不带 `--fix` 也崩，属既有行为；但 `--fix` 恰恰是能救它的路径（digest 会建表并灌入），不该在测量阶段就死 | `store_sources()` 对 `OperationalError` 返回空指纹而不是抛异常；`store_meta()` 表缺失返回 `None` | `--only audit-fix`（临时建一个**没有表**的 `sessions.db`，`--fix` 应能建成并 `RESULT=PASS`，不再喷栈） |
| FIX-021 | **[审查][实测]** 模板 / 文档里为教学展示的快照标记被当成真快照 → 新装用户注入的是**说明文字**而不是状态；行内代码里的标记同样误判 | 新增 `code_spans()` / `snapshot_marks()`：围栏代码块 + 行内代码（按**等长反引号**配对，CommonMark 规则）内的标记不算快照；`extract_latest_snapshot` / `render_status` / `audit.py` F 项 / `recall.py --topic` 全部走同一判据；模板重写（示例标记移进围栏 + 明示"还没有快照"） | `--only snapshot-marks` |
| FIX-021a | **[自检][实测]** 上一行的围栏识别**只认反引号**（`FENCE_RE` 写死三个反引号），于是用 `~~~` 围栏展示标记示例时原样复发；且旧实现按"出现顺序奇偶配对"，开闭字符不同也会凑成一对 | `FENCE_RE` 改为 `` ^\s*(`{3,}\|~{3,}) ``，配对加**同字符**约束（开、闭围栏必须都是反引号或都是 `~~~`），未闭合的围栏视为"其后全是代码" | `--only snapshot-marks`（含 `~~~` 围栏里示例在**末尾**、以及反引号 / `~~~` 混排两个新断言） |
| FIX-021b | **[自检][实测]** 上面的配对**只比字符、不比长度**，于是四反引号块里的三反引号行会**提前闭合**围栏：块内的**示例**标记漏成"真快照"，块外的**真**快照又被重新开启的围栏一路吞到文件末尾（CommonMark 要求闭合围栏**不短于**开启围栏） | 配对条件加长度约束：`ch == 开启字符 and len(闭合) >= len(开启)`；开、闭、长度三元组一起记 | `--only snapshot-marks`（新断言 6：` ```` ` 块内嵌 ``` 时真快照应恰好 1 条。**夹具必须让示例标记排在真标记之前** —— 反过来的写法两种错法会互相抵消、计数相同，变异体就抓不到了，2026-09-26 实测过） |
| FIX-022 | **[审查][实测]** 一个参数里多个 **<3 字符**的词被静默丢弃（"压缩 修复" 返回 0 条且无任何提示）—— 旧实现只看整个参数字符串的长度，词级下限被绕过 | `run_variant` 改为**逐词**分流：≥3 字走 FTS，<3 字走字面兜底（`instr`），两路结果按 `(sid, source, turn)` 去重合并（FTS 在前，保相关性序）；结尾提示哪些词走了兜底。另修 SQLite **多参数标量 `min()` 遇 NULL 返回 NULL** 导致的片段 NULL 崩溃 | `--only recall-short-words` |
| FIX-023 | **[审查][实测]** `--topic` 各源触顶时保留**最旧**的（`out[:limit]`），最新的线索被丢掉；触顶也不告知用户 | `scan_snapshots` / `scan_records_dated` 改 `out[-limit:]`（留最新）；覆盖行按源报数，触顶时明说"**只保留了最新的**，这不是全部线索"。顺带删掉覆盖行里"每源上限 60，**受 --limit 影响**"的假声明 —— `args.limit` 根本没传进 `print_topic_timeline`（2026-09-26 复核时发现） | `--only topic-keep-latest` |
| FIX-024 | **[审查][实测]** staging 四处：并发提交**丢更新**（`records` 读-改-写无互斥）、`.merged.json` 幂等记录丢、`--auto-target` 下 upsert 被静默丢弃、提案文件非原子写（读者可见半写文件） | 新增 `MergeLock`（同 `RunLock` 机制，锁内完成读-改-写，拿不到锁返回码 2 让人重试）；`load_proposals` 回报不可解析文件（不再静默跳过）；upsert 在机械兜底路径**降级为追加**并打标 + stderr 警告；`staging_write.py` 先写 `.part-<hex>` 再 `os.replace` | `--only staging-concurrent`、`--only staging-atomic-write` |
| FIX-025 | **[审查][实测]** 安装器 / `doctor.ps1` 六处：① 找不到 python 仍继续装，写出解释器路径为空的 `hooks.json`（hook 失败是**静默**的）② `check-env.py` 取不到版本号时 `[version]$E["python"]` 直接抛异常（体检项消失 + 控制台喷红；实测抛的是空串 `[version]""`，`[version]$null` 反而不抛）③ `-DataRoot` 非默认目录时只打一句 Warn、不设 `VARVE_DATA` → 各脚本仍去 `~/.varve` 找，表现为"记忆没生效"且无报错 ④ 没有 `-NoSetEnv`，"已装目录 vs 环境变量指向"不一致也不报错 ⑤ `doctor.ps1 -Project` 默认 `.` → 在没装项目级 hooks 的目录必报假 `[FAIL]`（默认安装是用户级）⑥ 体检本身漏检：子目录少查 `staging`、`hooks.json` 里引用的 `.py` 是否存在完全不查 | ① 无 python 立即中止且**不写** `hooks.json` ② 新增 `Get-PyVer` 安全解析（空/不可解析都返回 `$null`），该行保留并写明原因 ③ `VARVE_DATA` 三态判定：显式 `-DataRoot` 则持久化、已一致则 OK、不一致则 `[FAIL]` ④ 新增 `-NoSetEnv` 开关 ⑤ `-Project` 默认改 `""`，仅显式传入时检查 ⑥ 子目录补 `staging`；新增"hook 脚本路径有效"检查 | 六条各有用例：`--only ps-syntax`（5 个脚本能解析）、`--only ps-install-guard`（①）、`--only ps-no-output-guard`（② 用只打印 `python=` 的桩顶掉真 python，断言那一行**不得消失**）、`--only ps-dataroot-guard`（③④ 假 USERPROFILE 下 `-DataRoot` 非默认 + `-NoSetEnv` 必须 `[FAIL]` 且退出码非 0，另断言本机用户级 `VARVE_DATA` 没被动过）、`--only ps-project-default`（⑤ 在**没有** `.codex` 的目录跑，不得报假 `[FAIL]`）、`--only ps-doctor-subdirs`（⑥ 三个子目录逐项报到，结论跟着目录实际在不在 —— 少了 `staging` 这一项就抓得到）、`--only ps-hook-path-check`（⑥ 指向缺失脚本报 `[FAIL]`，指向存在的脚本不误报）。唯一的例外是 `install.ps1` 里"环境变量指向别处、本次用默认目录"那一支（第 105 行）：它读的是**用户注册表**里的值，要让这个用例在**任何机器上都稳定复现**就得先去改注册表（本机 `VARVE_DATA` 恰好已是非默认值，零写入也能撞上），不适合放进"不碰本机"的回归套件，只做人肉核对 |
| FIX-026 | **[审查][实测]** `install-claude.ps1` 只装 SessionStart 标记 + UserPromptSubmit 注入，**漏装索引重建 hook** → Claude Code 侧索引永不更新；重复安装会叠加 hook 条目；不校验 Python / FTS5 | SessionStart 一组内挂两个 hook（`hook-session-start.py` + `-X utf8 hook-build-index.py`，timeout 120）；清理正则覆盖 `hook-(session-start\|user-prompt\|build-index)\.py` 保证幂等；安装前校验 Python 版本与 FTS5/trigram，不达标 `exit 1` | `--only claude-index-hook` |
| FIX-027 | **[审查]** 解析丢内容 / 跨文件去重误删真实轮次：① 多段消息（文本 + 图片说明 + 补充段落）只取**第一段**就 `break`，后半截静默丢弃 ② 跨 source 去重只看 `(session_id, turn_no)`，resume 分叉会话在同一序号上的**不同真实轮次**被整条吃掉 | ① 拼接**全部** `input_text` / `output_text` 分段 ② 去重判据改为**内容相同**才删（`question` 与 `answer` 都比） | `--only digest-multiseg` |
| FIX-028 | **[审查]** 三个小问题：① `--timeline` 用裸列 `date`（`GROUP BY` 下**任取一行**，跨天会话显示成启动那天、排序也乱）② `sync-projects.ps1` 把子表 `[projects.'D:\x'.trust]` 也抓成路径，登记出 `D:\x'.trust` 这种垃圾项 ③ 日期按 `basename[8:18]` 硬切、sid 取末 40 字符（换命名就得到空日期 / "时间戳尾巴 + UUID"的脏 id） | ① 改 `MAX(date)` 再排序 ② 正则只认紧跟引号就闭合的表头（与 `varve_hooks_common.py` 的 `sync_projects` 同判据）③ `session_date()` 先取文件名里的 `YYYY-MM-DD`、取不到回退 mtime；`fallback_sid()` 取文件名里的 UUID，没有才用词干 | `--only timeline-max-date`、`--only sync-projects-regex`、`--only digest-fallback-naming` |
| FIX-029 | **[审查]** 文档与实现漂移：README 的"13 项体检"/"最新快照约 1.3k"/"索引新鲜度"/"目前只适配 Codex"、安装示例仍写 `-Project`（默认已是**用户级**）、`--raw` 没写进文档；`FIXES.md` 的 FIX-013 复发检查还写着已被证伪的"行数对账" | 中英 README 同步改（数字改成不写死的表述；补 `--raw`、`regression_test.py`；"只适配 Codex"改为"Codex 端到端实测 / Claude Code 注入已验证、历史检索未适配"）；README 补"库是会话**明文**副本"的隐私提示；本轮 FIXES 表与 CHANGELOG 同步 | 见下一行 FIX-029a：复现命令**必须限定在 README 两个文件**（中英各自措辞都查；全仓 grep 会命中 FIXES/CHANGELOG 里对旧措辞的引述，那样的检查永远不可能通过） |
| FIX-029a | **[自检]** 上面那条复发检查**自己写错了**：原文给的是全仓 `git grep -n "索引新鲜度\|13 项"` 应为空，可这两个词被 FIX-029 本行和 CHANGELOG 的漂移条目**原样引用着**，这个检查永远不可能通过（一条永远失败的检查等于没有检查） | 复现命令限定到 README 两个文件（中英各自的措辞都查）；并在此处明说"FIXES/CHANGELOG 里出现这些词是**引述**，不算漂移" | 应为空（rc=1）：`git grep -n -e 索引新鲜度 -e "13 项" -e "1\.3k" -e "只适配 Codex" -- README.md README.en.md` 与 `git grep -n -i -e "13 checks" -e "index freshness" -e "Codex-only" -- README.md README.en.md` |

| FIX-020d | **[自检][实测]** `--fix` 的孤儿清理按 `--sessions` 判定"文件已消失"，可**会话日志搬走**（换机器 / 归档 / 只是换个目录名）和**目录被删**在它眼里是同一件事：`--fix` 会把整个库删空，然后审计自洽、`RESULT=PASS`。第三轮独立验证 Agent 把它列为 F2（"删库 + 报 PASS"是本项目最不能接受的一类结果） | `session-digest.py` 新增 `--keep-orphans`；`audit.py` 的 `run_fix()` 走这个开关（宁可留着旧行，也不冒"目录不在 = 删库"的险）。另加提示：`session-digest.py` 输出 `files=0 written=0` 时 `--fix` 明说"**内容未被重灌**（只重建了索引），若会话日志已搬走，用 `--sessions` 指向现位置后重跑" —— 否则报告会被读成"内容已按真相源对齐" | `--only audit-fix`（第 5 条断言：删掉会话根目录后 `--fix`，行数与来源数都不得变化，且必须出现"没扫到任何会话文件"提示） |
| FIX-020e | **[自检]** 已知限制（第三轮验证 Agent 的 F3，**按现状记录，不修**）：`run_fix()` 的放行判据（`same_dir()`）本质是**路径字符串比对**（只把大小写、斜杠、`.`/`..` 归一），所以同一个库用**文本上不同**的写法指过去会被拒 —— `\\?\` 长路径前缀、`\\.\` 设备前缀、`subst`/映射盘符、UNC 形态、junction / 符号链接的别名路径。反过来，**纯大小写差异**（`d:\proj` vs `D:\proj`）与斜杠、尾部分隔符、相对段差异会被**接受**（2026-09-26 实测 11 种写法；上一版这里把"大小写不同的盘符"也列进了被拒名单，是**错的**，`normcase` 正是把大小写归一的那一步）。这是**失败安全**方向（拒绝而不是误删），出路是"按记录里的写法指"或"只重建索引" | 不改代码；`audit.py` 的拒绝提示已给出三条出路 | 无需检查；遇到误拒就按提示做 |
| FIX-022a | **[自检][实测]** 我修 FIX-022 时**自己引入的假报告**：`recall.py --raw` 把整串按 FTS5 原生语法送进 FTS，短词**没有**走字面兜底，可收尾提示仍照着 `variants` 里的短词报"走了字面兜底"（`--raw "a OR b"` 会声称 a/b 走了字面匹配）。根因：这层提示原先靠"整个变体的长度"和 `run_variant` 的路由对上，改成**逐词**分流后对应关系就断了；2026-09-26 复核自读 diff 时发现 | `short = [] if args.raw else sorted({...})` —— `--raw` 下不再声称任何词走了兜底 | `--only recall-short-words`（新增两条断言：非 `--raw` 必须提示兜底；`--raw` 不得出现"字面兜底 / 字面匹配"） |
| FIX-030 | **[审查][实测]** `audit.py` 的 B 项（重复入库）旧判据数的是**所有**同名 `(session_id, turn_no)`：resume **分叉**会话在同一序号上的真实轮次被算成"重复"，于是 B 项永远收敛不到 0、`RESULT=PASS` 永远拿不到（与 FIX-027"分叉轮次合法保留"直接矛盾，第三轮验证 Agent 的 F1） | B 项改为只数**内容完全相同**（`GROUP BY session_id, turn_no, question, answer`）的重复入库；同名不同内容的另计一条 `B_note` 作提示（"fork_turns，合法保留"）。文本报告同步改成"B 重复入库: N（另 M 个同名不同内容 = fork，合法）" | `--only audit-fork-dedup`（只有 fork 的库必须 `B=0` 且 `rc=0`；注入一条内容相同的重复行后再跑，必须 `B=1` 且 `rc≠0`） |
| FIX-031 | **[审查][实测]** 一次官方修复路径（`--full` 重灌 + `--force` 重建）之后库**不会自己缩回去**：① 逐行 DELETE+INSERT 把 trigram 索引切成一堆小段，FTS5 只在提交时按有限预算合并，段一多**在用页**就长期虚胖（真实库 94.9 → 151.8 MB 在用，内容只多了 68 轮）② `optimize` 合并后腾出的页、以及 `--force` 丢掉旧索引留下的页，都还留在**文件**里（155.9 MB 的文件里 64 MB 是空闲页）。实测真实库跑完 `--full` + `--force` 文件到 152.4 MB / 13833 页空闲 | `session-digest.py` 在 `--full`（或结构升级）后：先对 `turns_fts` / `traces_fts` 各做一次 `optimize`（合并段、缩在用页），再 `VACUUM`（缩文件）；`build-search-index.py --force` 的重建路径在 `commit` 后也 `VACUUM`（重建后内容变少时，新索引只吃得下旧页的一部分）。两处都容错：优化失败只打 warn，不影响"索引已建好" | `--only digest-full-compact`（夹具 6 文件 × 80 轮：重灌两轮后在用页比值卡 1.10；第二段先删 3/4 轮次再 `--force`，空闲页卡 `max(5, 页数/20)`） |

**本轮新增工具**：`scripts/regression_test.py` —— 把上表**能自动验的**"复发检查"写成可执行用例（现 22 条；
需要写用户注册表的那一支不进去，FIX-025 行末已注明）。零第三方依赖，临时目录自建自清，不碰 `VARVE_DATA`
与用户环境变量（`ps-dataroot-guard` 会顺手断言本机 `VARVE_DATA` 没被动过）。
退出码 `0` = 全 PASS / `1` = 有 FAIL / `2` = 有 SKIP（环境不具备，如缺 pwsh）。

**用例本身不是空的（变异测试）**：把修复逐条退回旧写法、在**临时副本**里构造变异体后重跑对应用例。
五条全部被对应用例杀掉（`rc=1`），证明这些用例真的能抓到回归：`run_fix` 去掉 `--sessions` 透传 /
`FENCE_RE` 退回只认反引号 / 重灌判据退回"至少一个重叠即放行"（这条的破坏最直观：库从
`14 行 3 源` 被删成 `5 行 1 源` 且仍报 PASS）/ 围栏配对退回只比字符 / `store_sources` 去掉空库容错。
变异只在临时目录里做，不碰仓库与真实数据。

**第三轮验证后的再次变异测试**（2026-09-26，覆盖 FIX-020d / 022a / 030 / 031）：5/5 被杀 ——
B 项退回旧判据（"数所有同名 sid+turn_no"→ 只有 fork 的库审计不通过）、`--force` 去掉 VACUUM、
`--full` 之后不做 optimize、digest 去掉 `--keep-orphans` 守卫、recall 短词提示退回无条件照报。
其中 `digest-full-compact` 用例**第一版是空的**：它先 `DROP` 两张 FTS 虚表再重建，而 SQLite 会把
刚释放的页原样交还给新索引（空闲页剩 0），不 VACUUM 也照样通过 —— 变异测试把这个假绿抓出来，
改成"先删 3/4 轮次再 `--force`"（新索引吃不下旧页，不 VACUUM 实测留 88 页 = 55%）后才真正生效。

**第四轮：拿变异体反查"用例本身是不是空的"**（2026-09-26，第三轮验证 Agent 提的 C2–C8）。
这一轮改的不是产品代码，而是**用例**：15 个变异体（含 4 份"退回 HEAD 旧实现"）+ 9 个对照（未变异的
副本／原样仓库），共 24 次跑，全部在临时副本里做 —— 仓库与真实数据一个字节都没动。

| 用例 | 变异体 | 结果 |
|---|---|---|
| `digest-concurrent` | 锁永不生效（`acquire` 直接 True）／锁永不释放（`release` no-op）／永不抢占（`acquire` 直接 False）／HEAD 无锁旧实现 | 4 个变异体全杀，原样对照 PASS。"`acquire` 恒 False"这条死得更早：digest 每轮都跳过，连库都建不出来，夹具构建那一步就断言失败 —— 报的是"建索引失败：库不存在"，也算杀，但理由不是锁的互斥断言 |
| `staging-atomic-write` | 直接写正式名（非原子）／提前占位（先建空的正式名）／HEAD `open(path,'x')` | 3 个变异体全杀，原样对照 PASS |
| `ps-install-guard` | 删掉"无 python 就停手"守卫 | 杀（"无 python 仍写出了 hooks.json"） |
| `ps-no-output-guard` | 退回 `[version]$E["python"]` | 杀（"那一行整个消失了"） |
| `ps-dataroot-guard` | 数据根不匹配退回 `Warn`（修复前行为） | 杀（无 `[FAIL]` 且退出码 0） |
| `ps-project-default` | `-Project` 默认退回 `"."` | 杀（在无 `.codex` 的目录报假 `[FAIL]`） |
| `ps-doctor-subdirs` | 子目录列表里去掉 `staging` | 杀（"体检里没有'子目录 staging'这一项"） |
| `ps-hook-path-check` | 删掉 hook 路径检查段／HEAD 旧 `doctor.ps1` | 2 个变异体全杀 |
| `timeline-max-date` | `MAX(date)` 退回裸列 `date` | 杀（跨天会话排到 09-22） |

两个"用例自己是空转"的实例（都是第四轮抓出来的，产品代码没问题；`regression_test.py` 本轮才进仓库，
所谓"第一版"只在工作过程中出现过，仓库历史里查不到 —— 这里如实记下，不当作可回溯的凭证）：

1. `staging-atomic-write` 第一版只查"有没有 `.part-*` 残留、成品能不能解析"—— 任何单线程写都满足，
   把 `open(path,"w")` 直接写正式名（非原子）照样通过，HEAD 也通过。重写为**把写侧的序列化注入延迟**
   （`json.dump` 分 6 段写 + flush + sleep，窗口从 <1ms 放大到百毫秒级），再让读者线程高频调用**真实的**
   `staging_merge.load_proposals()`，断言读者永远看不到半写提案；注入点失效（写侧若改用 `json.dumps`）时
   用例直接判 FAIL，不许悄悄退回空转。
2. `digest-concurrent` 第一版那段"并发锤击"每轮实际是 `written=0 skipped=6` 的空转（见该用例 docstring）。
   旧代码的并发损坏在这台机器上复现率不稳定（两轮独立验证都没能在旧代码上跑出损坏），所以改成直接构造
   **锁的状态**、断言互斥契约本身（锁被持有 -> 跳过且零写入；锁陈旧 -> 抢占并继续）—— 旧代码没有锁，
   第一条必然过不了（已用 HEAD 变异体验证）。
3. `ps-no-output-guard` 的夹具**第一版造错了触发条件**：我按 FIX-025 原文的"`[version]$null` 会抛异常"
   只让桩 python 什么都不输出（键缺失 -> `$E["python"]` 是 `$null`），结果变异体**没被杀掉**。实测
   PowerShell 语义：`[version]$null` 返回 `$null`（不抛），抛的是**空串** `[version]""`。夹具改成只打印
   `python=` 之后变异体立刻被杀；FIX-025 那行描述也一并改正。这条说明"变异体没杀掉"既可能是用例空转，
   也可能是**夹具没造出真正的触发条件** —— 两种都得回查，不能只看对应用例过没过。

同一轮还修掉两处**文档说了假话**：`audit.py` 的已知限制里"大小写不同的盘符会被拒"是错的
（`same_dir()` 的 `normcase` 正是归一大小写那一步；实测 11 种写法，纯大小写差异被**接受**，
被拒的是 `\\?\` / `\\.\` / `subst` 盘 / UNC / junction 这类**文本上不同**的写法），
以及 CHANGELOG 里"用例自带变异测试"的说法（变异在临时副本里手工做，套件本身没有变异逻辑）。

## 历史批次

（此前由 ISSUES.md 记录，编号 A–F；本轮起新的修复统一记到本文件。）
