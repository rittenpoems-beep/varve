# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
