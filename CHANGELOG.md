# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`recall.py --topic <X>` - topic timeline.** Merges the three sources (L2 snapshots / records / conversation
  history) into one time-ascending list, for evolution-type questions ("why did we abandon approach A back then").
  This closes a long-standing gap: the behaviour was specified in the index spec (3.6) on 2026-09-22, but the
  parameter never existed - `--timeline` had degenerated into "list sessions by date, without a query term".
- **Coverage self-report.** Every retrieval now ends with a coverage line giving matched / taken / shown counts,
  so the model can tell that it has *not* seen everything. This addresses the most dangerous failure mode:
  silently answering from a partial set as if it were complete.
- **`scripts/regression_test.py` - executable regression suite.** The "how to check for regression" recipes of
  this review batch ([FIXES.md](FIXES.md)) are now runnable cases (22 today; the older batches keep their
  hand-written recipes, and the single branch that would have to write the user registry - `install.ps1`'s
  "env var points elsewhere" check - stays hand-checked). Zero third-party dependencies, self-built and
  self-cleaning temp directories, never touches the real data root. Exit codes: `0` all PASS / `1` any FAIL /
  `2` SKIP only (environment lacks something, e.g. no `pwsh`). Mutation testing is **not** part of the suite:
  the fixes that have a case were reverted one by one in a throwaway copy of the tree (15 mutants, including
  four reverts to the pre-fix code) and the matching case had to fail - that is how two cases were caught being
  vacuous in their first version (store bloat, staging atomicity; mutants and results are recorded in FIXES.md,
  rows without a case keep their hand-written recipe).

### Fixed (external review batch, 2026-09-26)

Every item below was reproduced on a clean copy before being fixed.

- **Two concurrent `session-digest.py` runs lost data and corrupted the FTS index - silently.** Both processes
  read the same id counter, so ids collided; the `INSERT OR REPLACE` that followed deleted the other process's
  real rows, and because `PRAGMA recursive_triggers` defaults to OFF the conflict-delete did not fire the FTS
  delete trigger, leaving ghost index entries - while `COUNT(*)` still reconciled. Fixed with a cross-process
  file lock (skip this round if not acquired, steal on mtime timeout), id allocation moved *inside* the write
  transaction, one `BEGIN IMMEDIATE` transaction per source file, explicit DELETE+INSERT instead of REPLACE,
  and `recursive_triggers=ON`.
- **`audit.py`'s index check was a tautology (reported PASS on a corrupt index).** Under external-content FTS5,
  `COUNT(turns_fts) == COUNT(turns)` is *always* true. On the same corrupt database the old criterion printed
  PASS while `integrity-check` reported `database disk image is malformed`. The check is now three-part: trigger
  presence, `_docsize` shadow-table **row-set** reconciliation (missing / ghost rows), and a token-level
  `integrity-check(rank=1)` (which needs a writable connection). New `--fix` re-ingests and rebuilds, then
  re-checks in place.
- **That first `--fix` could destroy a store and still report success** (found by the independent verification
  pass, not by the original review). `run_fix()` invoked `session-digest.py --full` *without* `--sessions`, so it
  re-ingested the default `~/.codex/sessions`; the digest's orphan cleanup then deleted every row whose origin
  was not under that default root. Because the wiped store is self-consistent, the audit still printed
  `RESULT=PASS` (measured: turns 2 -> 0, sentinel row gone, rc=0). Separately, the repair ran *after* the
  measurements, so one report could show pre-fix A/D/E next to post-fix C. Now `--sessions` is plumbed through
  (new CLI flag, same default as the digest), a pre-flight check refuses unsafe re-ingestion, the post-fix content
  fingerprint (source set + row counts) fails the run if a source still on disk disappeared, and the repair
  happens **before** all measurements.
- **The first pre-flight check was still too weak** (found by a second independent verification pass). It
  accepted the root as long as *at least one* of the store's sources existed under it. But if the wrong root
  shares even a single same-named relative path - a partial backup, a mirror that never finished syncing, a
  machine where only one month of sessions was restored - the digest's orphan cleanup deletes every other source,
  and the post-fix fingerprint cannot see it: the deleted sources never existed under that root to begin with.
  Measured on a mutation fixture (3-source / 15-turn store, wrong root holding one same-named path): `--fix`
  shrank the store from **14 rows / 3 sources to 5 rows / 1 source** while printing `RESULT=PASS` and exiting 0;
  the second verifier reproduced the same family of results on its own 5-source / 10-turn fixture (`orphans=4`,
  sources 5 -> 2, turns 9 -> 5). The check is now
  two-tier and strict: `session-digest.py` records `os.path.abspath(--sessions)` into `meta.sessions_root` on
  every run, and `--fix` requires the record to point at the *same* place; stores predating that record fall back
  to "**no** source may be missing", which is far stricter than "at least one overlaps". A refusal prints three
  ways out: point at the build-time directory, run the digest once with the new directory to update the record, or
  rebuild the index only via `build-search-index.py --force`. Also: `--fix` on a database with no tables used to
  raise a traceback instead of building it.
- **Documentation examples were injected as if they were snapshots.** A fresh install injected the template's
  explanatory text instead of state, because the marker inside a code fence (or inline code) was treated as a
  real snapshot. Markers are now located with a fence- and inline-code-aware scan (backtick runs pair by equal
  length, per CommonMark), shared by the hooks, `audit.py` and `recall.py --topic`. The fence scan itself was
  still backtick-only in the first pass, so a `~~~` fence reproduced the bug verbatim; opening and closing
  fences must now use the *same* character — and the closing fence must be **at least as long** as the opening
  one, otherwise a three-backtick line inside a four-backtick block closed the block early and leaked its
  example marker out as a real snapshot.
- **Words shorter than 3 characters were silently dropped when passed together with longer ones** ("压缩 修复"
  returned 0 hits, no warning): the trigram tokenizer's minimum is 3 characters. Retrieval now dispatches
  **per word** - long words to FTS, short ones to a literal fallback - and merges the two result sets. Also
  fixed the NULL-snippet crash caused by SQLite's multi-argument scalar `min()` returning NULL if any argument
  is NULL.
- **`--topic` kept the oldest entries at the per-source cap** and dropped the newest ones, without saying so.
  Both the snapshot and records scans now keep the newest, and the coverage line states explicitly when a source
  hit its cap. A coverage line also used to claim it was affected by `--limit`, which was never passed to that
  function; the claim is gone.
- **Staging could lose updates.** Concurrent commits did an unguarded read-modify-write of `records`, the
  `.merged.json` idempotency record could be lost, `--auto-target` silently dropped upserts, and proposal files
  were written non-atomically (readers could observe a half-written file). Now: a merge lock, unparsable
  proposals are reported instead of skipped, upserts fall back to append with a marker and a warning, and
  proposal writes go through `.part-*` + `os.replace`.
- **Installer and `doctor.ps1` (6 issues).** No usable Python no longer writes a `hooks.json` that points at an
  empty interpreter (hook failures are silent); an unparsable Python version no longer deletes a health-check row
  (the cast `[version]""` threw, so the row vanished and only a red error remained); a non-default `-DataRoot` now
  either persists `VARVE_DATA` or fails loudly instead of letting every script look in `~/.varve`; new
  `-NoSetEnv` switch; `doctor.ps1 -Project` no longer defaults to `.` (which reported a false `[FAIL]` for
  user-scope installs); the health check now also covers the `staging` subdirectory and verifies that every
  `.py` path referenced by `hooks.json` actually exists.
- **`install-claude.ps1` never installed the index-rebuild hook**, so the index would never refresh on the
  Claude Code side; reinstalling also stacked duplicate entries, and Python/FTS5 were not validated. It now
  installs both SessionStart hooks as one group, is idempotent, and exits non-zero when the environment fails.
- **Parsing dropped content and cross-file de-duplication deleted real turns.** Multi-segment messages only
  kept the first segment (the rest was silently discarded), and cross-source de-duplication matched on
  `(session_id, turn_no)` only, so a forked session's genuinely different turn at the same index was deleted
  whole. All text segments are now joined, and de-duplication requires identical content.
- **Three smaller defects:** `--timeline` used a bare `date` column (with `GROUP BY`, SQLite picks an
  arbitrary row, so cross-day sessions displayed their start date); `sync-projects.ps1` treated the sub-table
  `[projects.'D:\x'.trust]` as a workspace path; and date/session-id fallbacks sliced the filename by fixed
  offsets, producing empty dates or dirty ids for any other naming convention.
- **Documentation drift:** both READMEs still advertised "13 checks", "newest snapshot about 1.3k", "index
  freshness", and "Codex-only today"; the install examples still passed `-Project` (the default scope is user);
  `--raw` was undocumented. The READMEs now avoid hardcoded counts, document `--raw` and the regression suite,
  state the Claude Code adapter accurately, and warn that the database is a **plain-text** copy of session
  content. The regression recipe for this item was itself wrong at first — it grepped the whole repository for
  the old wording, which FIXES.md and this changelog quote verbatim, so it could never pass; it is now scoped to
  the two README files.
- **`--fix` deleted the whole store when the session logs had merely been *moved*, and still reported success.**
  The digest's orphan cleanup treats "file not under `--sessions`" and "file gone from disk" as the same thing, so
  pointing `--fix` at a store whose logs live elsewhere wiped every row - after which the store is self-consistent
  and the audit prints `RESULT=PASS`. `--fix` now passes a new `--keep-orphans` flag ("rather keep stale rows than
  risk treating a missing directory as a deletion order"), and when the digest reports `files=0 written=0` the
  repair says so explicitly: the content was **not** re-ingested, re-run with `--sessions` pointing at the current
  location. Without that warning the report reads as "content has been reconciled with the source of truth".
- **A false report of my own making: under `--raw`, short words were claimed to use the literal fallback.** The
  hint used to agree with `run_variant`'s routing because both looked at the whole variant string; once routing
  became per-word, `--raw "a OR b"` still announced that `a`/`b` had gone through literal matching, which is not
  what `--raw` does (it hands the string to FTS5 verbatim). The hint is simply suppressed under `--raw`.
- **The "B: duplicate turns" check could never reach zero.** It counted *every* repeated `(session_id, turn_no)`,
  so a legitimate **forked** session - same index, genuinely different turn, which the parser deliberately keeps -
  was reported as duplicate ingestion, `RESULT=PASS` was unreachable, and the check contradicted the
  fork-preserving rule. It now counts only rows whose content is **identical** across sources; same-name
  different-content turns are reported separately as a note ("fork, kept on purpose").
- **One official repair path left the store permanently bloated.** After `--full` + `--force` the file stayed at
  its inflated size and never came back down: row-by-row DELETE+INSERT shreds the trigram index into many small
  segments and FTS5 only merges them under a limited budget, so **used** pages stay inflated (a real store went
  94.9 MB -> 151.8 MB used while its content grew by 68 turns); and the pages freed by `optimize` - or dropped by
  a rebuild - remain in the **file** (measured: 64 MB of a 155.9 MB file was free pages; 152.4 MB with 13833 free
  pages). The digest now runs `optimize` on both FTS tables followed by `VACUUM` after a `--full` (or schema
  upgrade), and `build-search-index.py --force` vacuums after its rebuild (when the new index needs fewer pages
  than the old one, the leftovers stay behind otherwise). Both steps are non-fatal on failure - the index itself
  is already built. Real store: back to 95.4 MB, 0 free pages.

### Known limitations (honest note, 2026-09-25)

- `--topic` matches **literally**. Two clues with no shared word (one says "in Beijing", another says "ate in
  Shanghai") cannot be pulled into the same view - so their conflict cannot be detected. **That part of "how do I
  stitch scattered clues back together" is not solved.**
- Coverage reporting makes the model *aware* of missing evidence; it does not *fill* it.
- Candidate directions (semantic index / entity anchors) are known, but at this system's scale the author is still
  weighing cost against benefit - the current combination is a **stopgap, not a final answer**.

### Known limitation (2026-09-26)

- `audit.py --fix` compares the recorded session root as an **absolute path string** (only case, slashes and
  `.`/`..` are normalized), so the same store reached through any *textually different* alias - a symlink or
  junction, a `subst`/mapped drive, a `\\?\` long-path prefix, a `\\.\` device prefix, a UNC spelling - is
  refused. Plain case differences are accepted (`d:\proj` vs `D:\proj`; measured 2026-09-26 over 11 spellings).
  This errs on the safe side (a refusal, never a deletion); the refusal message lists the three ways out
  (point at the recorded spelling, run the digest once from the new directory to update the record, or rebuild the
  index only with `build-search-index.py --force`).

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
  (which reported a false "stale index" under the trigger era). **Note (2026-09-26): the row-count half of
  this criterion turned out to be a tautology under external-content FTS5 and was replaced — see the external
  review batch above. Do not reintroduce it.**
- `init_hint` marker moved out of `pending/` — the 7-day pending cleanup recycled it, so the
  one-time install prompt reappeared roughly every 8 days. Pending cleanup now also skips dotfiles.
- `contract_hint` documented correctly: the contract is injected on first prompt **whether or not**
  a state card exists (implementation was right, the comment was stale).
- `install.ps1` gained `-Scope user|project` (default `user`, writing `~/.codex/hooks.json`), matching
  what the docs described; previously it only wrote project-level hooks.
- `recall.py --deep` de-duplicates cross-source overlapping traces (resume sessions produced duplicate
  trace rows that diluted BM25; this mirrors the existing `turns` de-duplication).
- Size reporting unified to MB = 10^6 bytes across `audit.py` and `build-search-index.py`. (2026-09-26:
  `doctor.ps1` was a third divergent site - PowerShell's `1MB` is 1024^2 - and is now aligned too.)

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
