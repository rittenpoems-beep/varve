# Varve

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**English** | [简体中文](README.md)

**A cross-session memory layer for AI coding agents** (Codex first, framework-agnostic by design) — three tiers (environment / state / history), **append-only injection** that never breaks the prompt cache, **SQLite full-text search**, and a **single global state card** shared across workspaces, kept as an **append-only snapshot stream**.

Zero LLM calls. Zero third-party dependencies (Python standard library + PowerShell 7).

> The name comes from geology: a **varve** is an annual sediment layer in a glacial lake — one layer per year, stacked, never rewritten, traceable back to any layer. That is exactly its three principles: **append-only, rebuildable, addressable**.

> Status: early but usable (v0.1). In daily use on a real project; the index layer ships with an audit tool and regression cases (see [FIXES.md](FIXES.md), in Chinese).

## Contents

- [What it solves](#what-it-solves)
- [Quick start](#quick-start)
- [Tools](#tools)
- [How it works](#how-it-works)
- [Data & privacy](#data--privacy)
- [Known limitations](#known-limitations)
- [Feedback & contributing](#feedback--contributing)
- [License](#license)

## What it solves

| Problem | Common approach | What Varve does |
|---|---|---|
| Every new session starts amnesiac | Paste context by hand | Three tiers available automatically: environment / state / history |
| Dumping all history into context | Tens of thousands of tokens per turn | Inject only the **newest** state snapshot; retrieve details on demand |
| Injection shatters the prompt cache | Silent cost explosion | **State is appended at the tail of the request**, never touching the fixed prefix |
| Finding history means guessing keywords | Brute-force `rg` | SQLite FTS5 index + multi-variant retrieval + **source line coordinates** |
| Concurrent writers clobber a file | Whole-file rewrite | Staging: proposal → arbitration → atomic commit |
| Switching directories or frameworks loses memory | Install one setup per project | **Global card**: one state card for every directory; the mechanism never depends on a "project" concept |

## Real-world scale

These are not illustrative numbers — they come from 8 days of continuous use in the author's own environment:

| Metric | Value |
|---|---|
| Session requests | 4,539 |
| Cumulative input tokens | ≈ 1.4 billion |
| Cache hit rate | 96% (a direct beneficiary of tail-append injection) |
| Index size | 549 conversation turns · 20,738 trace records |
| Database size | 82.5 MB (local SQLite, rebuildable at any time) |
| Audit result | `audit.py` all PASS (0 orphans · 0 duplicates · 6/6 sync triggers) |

That workload is exactly the target scenario: one developer pushing several projects at once, hundreds of agent turns per day.

## Compatibility

### What Varve adapts to

Varve adapts to a **capability contract**, not to a product. A host must provide two things:

| Capability | What it concretely means | What it is used for |
|---|---|---|
| **Session events** | Fire a script on session start / resume / clear / compact, passing `cwd`, session id and trigger source | Decide whether this session should receive the state card |
| **Tail-append injection** | Append text to the context **after the user message** (not rewrite the system prompt) | Deliver the state card without breaking the prompt cache |

The second one is the crux: injection lands at the message tail, the fixed prefix never changes, and the cache stays intact. A host that can only rewrite the prefix cannot support Varve's core value, so we do not support it.

### Which frameworks provide it

| Framework | Session events | Tail-append injection | Status |
|---|---|---|---|
| **Codex** | ✅ `SessionStart` | ✅ `UserPromptSubmit` | Adapted · tested end to end |
| **Claude Code** | ✅ `SessionStart` | ✅ `UserPromptSubmit` | Adapted · verified at script level |
| Prefix-only hosts | ✅ | ❌ | Not supported (cache safety impossible) |
| Hosts without hooks | ❌ | ❌ | Not supported |

The two contracts are **nearly identical** — event names, stdin payload fields (`session_id` / `cwd` / `source` / `prompt`) and the injection field (`hookSpecificOutput.additionalContext`) match almost verbatim, and both inject *after* the user message. That is why **the same pair of scripts serves both**; Claude Code only needs an output-format switch (`--json-output`).

**Categories we do not adapt, and why**:

- IDE-like (Cursor / Trae / Qoder): no tail-append injection channel
- Office-like (WorkBuddy and similar): same, plus a non-coding use case
- Library-like (LangChain / CrewAI and similar): no hook concept; the host would have to run the loop itself

### What each framework needs

**Codex**

- Requirements: Windows + PowerShell 7 + Python 3.10+ (stdlib with SQLite FTS5)
- Install: `pwsh -NoProfile -File scripts\install.ps1 -Project <your-project>`
- Manual step: approve the hooks once in the Codex UI

**Claude Code**

- Requirements: Python 3.10+
- Install: `pwsh -NoProfile -File scripts\install-claude.ps1` (writes user-level `~/.claude/settings.json`; `-Scope project -Project <dir>` for a single repo)
- Manual step: none (settings.json has no trust flow)
- Limit: `additionalContext` is capped at 10,000 characters (the newest snapshot is about 1.3k, safe)
- Status: state injection is implemented and verified with simulated payloads; **history search is not supported yet** — the Claude Code transcript format is unverified, so first run `python -X utf8 scripts\probe-claude-transcript.py` on a machine with Claude Code to sample it

## Quick start

**Requirements**: Windows + PowerShell 7 + Python 3.10+ (stdlib build with SQLite FTS5).

```powershell
# 1. Install: env check -> data dir -> generate .codex/hooks.json -> install Skill
pwsh -NoProfile -File scripts\install.ps1 -Project D:\your-project

# 2. Approve the hooks once in Codex ("New hook - review required", the only manual step)

# 3. Verify (13 checks)
pwsh -NoProfile -File scripts\doctor.ps1 -Project D:\your-project
```

After that, every session start automatically **loads the project state** and **refreshes the search index**; when you mention "last time / earlier / that pitfall", you get a one-line retrieval reminder.

**Without hooks** you can still use the pipeline directly:

```powershell
python -X utf8 scripts\session-digest.py          # session logs -> SQLite
python -X utf8 scripts\build-search-index.py      # build the FTS5 index
python -X utf8 scripts\recall.py "keyword1" "keyword2"   # retrieval, coarse to fine
python -X utf8 scripts\recall.py --topic "snapshots"   # topic timeline (evolution-type; three sources merged)
python -X utf8 scripts\recall.py --timeline --since 14d
python -X utf8 scripts\recall.py "error text" --deep     # include tool-call/output layer
```

## Tools

| Content | Description |
|---|---|
| `install.ps1` | Install: env check / data dir / hooks.json / Skill (idempotent) |
| `doctor.ps1` | 13-point health check (read-only) |
| `check-env.py` | Probe Python / SQLite / FTS5 / trigram support |
| `session-digest.py` | Session logs -> SQLite (conversation history + trace history) |
| `build-search-index.py` | Build the FTS5 index (skips when content is unchanged) |
| `recall.py` | Retrieval CLI: records -> conversation history -> trace layer (`--deep`); `--topic` = topic timeline; output ends with a coverage line |
| `audit.py` | Memory-store audit: consistency / duplicates / index freshness / retrieval self-test / size |
| `env-scan.py` | Environment scan: records only what the harness does *not* inject, refreshes the auto section of `ENVIRONMENT.md` |
| `hook-session-start.py` | Session start: mark pending injection (zero output) |
| `hook-user-prompt.py` | User message: append state card + history-signal reminder |
| `hook-build-index.py` | Silent index refresh (hook wrapper) |
| `varve_hooks_common.py` | Shared hook logic |
| `init.ps1` / `sync-projects.ps1` / `build-docs-index.ps1` | Init / workspace discovery / docs index |
| `staging/` | Concurrent writes: proposal -> arbitration -> atomic commit (incl. stress test) |
| `templates/` | State card / record / Skill / AGENTS snippets |
| `FIXES.md` | Fixed-issue ledger (in Chinese), each entry with a "how to check for regression" recipe |

> Naming: executable scripts use hyphens (`hook-user-prompt.py`); importable Python modules use underscores (`varve_hooks_common.py`).

## How it works

```text
Session logs (read-only)
   |  triggered by SessionStart
   v
Single SQLite database -- turns  (conversation history: prompt + answer)
                        -- traces (tool calls / outputs / reasoning)
                        -- FTS5 index (external content: text stored once)
   |  retrieved on demand
   v
Three-stage funnel: records -> conversation history -> trace layer
```

**Two hard rules**:

1. **Append-only injection** — state lands at the tail of the request and never touches the fixed prefix (SessionStart only marks; UserPromptSubmit appends).
2. **Graceful degradation** — every stage has a fallback; worst case = plain files plus a rule sentence.

**Global card (since 2026-09-24)**: there is exactly one state card — `<VARVE_DATA>\STATUS.md`. It is not scoped to a project or a directory; every session, in any framework, injects the same card (ownership is expressed with a `[project]` prefix inside entries). Trade-offs are listed under Known limitations.

**Snapshot stream (since 2026-09-25)**: the global card is an **append-only sequence of complete snapshots** — every update appends one self-contained snapshot at the end, and injection reads only the **last** one. Context cost stays constant regardless of history length, and older snapshots are never edited, so any past state can be reconstructed. A single snapshot stays under the 3500-character guard (checked by `audit.py`); legacy cards are still read via a fallback path, and `scripts/migrate-status-snapshots.py` converts them.

**Index consistency**: `turns` / `traces` use **stable sequential ids** (assigned on write, unchanged by delete-and-reinsert) and stay **in sync with the FTS5 index through SQLite triggers** — there is no window where content is updated but the index is stale, and no full rebuild is required.

## Data & privacy

- Everything stays local: session logs (`~/.codex/sessions/`) are opened **read-only**; the derived SQLite database and state card live under `<VARVE_DATA>` and are never uploaded.
- The database is **rebuildable**: delete `<VARVE_DATA>/index/` and rerun `session-digest.py` + `build-search-index.py`.
- Retrieval is **local full-text matching** (SQLite FTS5) — no embeddings, no external API calls.
- If you share this setup with a team, note that `<VARVE_DATA>/STATUS.md` contains your task state — keep it out of public repos (e.g. `.gitignore`).

## Known limitations

- **Codex-only today** (via `.codex/hooks.json` + two hooks). The architecture is layered for portability, but **no other framework adapter exists yet**.
- Depends on Codex's session log format (`~/.codex/sessions/**/*.jsonl`).
- Windows / PowerShell first; the Python side is cross-platform.
- **The state card is global**: projects are not isolated; multi-project task state shares one card (distinguished by a `[project]` prefix). This is a deliberate trade-off for cross-framework usability.
- **Global hooks require one manual approval**: after `~/.codex/hooks.json` changes, Codex asks you to trust it again.
- **Clue stitching is only partly solved (honest note, 2026-09-25)**: `--topic` and keyword search both match **literally**.
  Two clues with no shared word (e.g. one says "in Beijing", another says "ate in Shanghai") never land in the same view —
  so their conflict cannot be detected. A semantic index (phase 2) or entity anchors are the candidate directions, but at
  this system's scale the author is still weighing cost against benefit: **the current "anchor-first + coverage self-report"
  is a stopgap, not a final answer.**

## Feedback & contributing

- Bug reports: please attach the output of `python -X utf8 scripts/audit.py` — most issues can be localized from it.
- When submitting a fix, please update [FIXES.md](FIXES.md) as well (problem / fix / regression check; the file is in Chinese).
- Known issues and fix history live in [FIXES.md](FIXES.md) (in Chinese); design documents are not part of this repository (public release contains the artifact only).

## License

[MIT](LICENSE)
