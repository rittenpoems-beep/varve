# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-25

**主题：L2 从"就地改写的文档"改为"只增不减的完整快照序列"。**

### Changed

- **L2 is now an append-only snapshot stream** (2026-09-25). The global state card is no longer a document that
  gets rewritten in place: every update appends a *complete, self-contained snapshot*, and the hook injects only
  the **newest** one. Context cost is therefore constant no matter how long the history grows, and older
  snapshots are never modified, so any past state can be reconstructed. A task that ends is marked once in the
  snapshot where it ended, instead of being collected into a rolling "recently completed" list.
  This fixes two real failure modes of the previous model: a growing card was truncated **head-first**, dropping
  the *newest* content (measured 2026-09-25 on a real card: 4107 characters against the 3500 guard), and items
  rolled off the "recently completed" list were lost permanently.

### Added

- `scripts/migrate-status-snapshots.py` — converts a legacy five-section state card into the first snapshot
  (idempotent, auto-backup, supports `--dry-run`).
- `render_status()` falls back to the legacy whole-section rendering when no snapshot marker is present, so
  existing cards keep working without migration.

### Fixed

- **Truncation dropped the newest state first.** The injection guard cut the card from the front
  (`seg[:3500]`), so once a card exceeded the budget the *most recent* entries disappeared first — measured on a
  real card on 2026-09-25: 4107 chars against a 3500 budget, with the newest line cut mid-sentence. Only the last
  snapshot is rendered now, so card growth can no longer cost the newest information.
- **Rolled-off completions were lost forever.** "Recently completed (≤5)" was a rolling list: a 6th item pushed
  the 1st out of existence. Completion history now lives in the snapshot where the task ended and is never
  deleted — the list can no longer overflow because there is no list.
- **Card drift was invisible.** A delivered task could sit under "in progress" indefinitely (observed: the audit
  script shipped 2026-09-24 was still listed as active the next day), and a stale banner could coexist with
  current work. Snapshots are timestamped and append-only, so a stale line is readable as "last stated N days
  ago" instead of silently passing as current.

## [0.1.1] - 2026-09-24

### Added

- **Claude Code adapter** (second framework): `install-claude.ps1` writes `~/.claude/settings.json`
  (user scope by default, `-Scope project` for a single repo) and merges instead of overwriting.
  The same hook scripts serve both frameworks — `hook-user-prompt.py --json-output` emits
  `hookSpecificOutput.additionalContext`, which Claude Code appends *after* the user message
  (tail-append, so the cache-safety rule holds).
- `probe-claude-transcript.py`: samples Claude Code transcript structure (no message content),
  so the index adapter can be written against real data instead of guesswork.
- README (both languages): explicit compatibility scope — Codex and Claude Code only, with the
  reasoning for why other framework categories are not applicable.

### Fixed

- `build-search-index.py` no longer performs a full rebuild when sync triggers are active — previously
  every session start rebuilt the entire FTS index even though the triggers already kept it current
  (`--force` still rebuilds explicitly).
- `audit.py` index check rewritten: it now verifies trigger presence and reconciles FTS row counts
  against content row counts, instead of comparing `index_built_at` / `content_updated_at`
  (which reported a false "stale index" under the trigger era).
- `init_hint` marker moved out of `pending/` — the 7-day pending cleanup recycled it, so the
  one-time install prompt reappeared roughly every 8 days. Pending cleanup now also skips dotfiles.
- `contract_hint` documented correctly: the contract is injected on first prompt **whether or not**
  a state card exists (implementation was right, the comment was stale).
- `install.ps1` gained `-Scope user|project` (default `user`, writing `~/.codex/hooks.json`), matching
  what the docs described; previously it only wrote project-level hooks.
- `recall.py --deep` de-duplicates cross-source overlapping traces (resume sessions produced duplicate
  trace rows that diluted BM25; this mirrors the existing `turns` de-duplication).
- Size reporting unified to MB = 10^6 bytes across `audit.py` and `build-search-index.py`.

## [0.1.0] - 2026-09-24

First public release.

### Added

- Three-tier memory (environment / state / history) with **append-only injection** at the request tail
- Single SQLite database (`turns` + `traces`) with an FTS5 external-content index
- **Global state card** shared across workspaces (no per-project scoping)
- **Stable sequential ids + SQLite sync triggers** — the FTS index follows content immediately, no rebuild window
- Retrieval CLI (`recall.py`): records → conversation history → trace layer (`--deep`)
- Audit tool (`audit.py`): consistency / duplicates / index freshness / retrieval self-test / size
- Environment scanner (`env-scan.py`): records only what the harness does *not* inject
- Codex session hooks: state marking (SessionStart), tail injection (UserPromptSubmit), silent index refresh
- Installer, 13-point doctor, staging-based concurrent writes, templates
- Chinese and English README; regression ledger ([FIXES.md](FIXES.md))
