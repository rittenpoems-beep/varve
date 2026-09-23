# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
