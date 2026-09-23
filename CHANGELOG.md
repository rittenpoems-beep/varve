# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
