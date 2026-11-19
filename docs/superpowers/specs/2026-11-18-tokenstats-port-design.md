# tokenstats — design

Date: 2026-11-18
Status: awaiting approval
Author: brainstormed with the human partner

## Intent and why

Create **tokenstats**, a leaner sibling of the existing `token_counter` tool
that records and reports **OpenCode token usage only** — no Unsloth Studio —
and that runs correctly on **Linux, Windows, and macOS**.

`token_counter` snapshots two sources (OpenCode sessions + Unsloth Studio
events/runs) into `~/.tokenstat/usage.db`. tokenstats keeps the OpenCode half,
drops the Unsloth half entirely, and is renamed throughout. It is produced by
**copying the repo (full git history) and renaming it**, not by forking on
GitHub (the source repo has no remote anyway).

*Why (my read — correct me):* a simpler, single-source tool that other people
can actually run on their own machines regardless of OS, without Unsloth being
a hard dependency or a confusing second source.

## Goals

- Copy `/home/elih/messinaround/token_counter` → `/home/elih/messinaround/tokenstats`,
  **including the `.git` directory** (full history preserved; no fork, no remote).
- Rename the tool identity to `tokenstats` everywhere:
  - `tokenstat.py` → `tokenstats.py`
  - `test_tokenstat.py` → `test_tokenstats.py`
  - argparse `prog="tokenstats"`
  - state dir `~/.tokenstat` → `~/.tokenstats` (store, themes, saved theme default)
  - module docstring, README, AGENTS.md, docs references
- Remove **all** Unsloth functionality and `reconcile` (see Removal surface).
- Cross-OS correctness + tests that simulate each OS's path logic.
- Add a minimal, cross-platform **install path** (`pyproject.toml` + a
  `tokenstats` console-script entry point) so `pipx install .` / `pip install .`
  puts a `tokenstats` command on PATH on all three OS — while the file stays
  directly runnable with `python tokenstats.py` (no install required).
- Keep the TDD discipline: the adapted suite passes, including under
  `-W error::ResourceWarning`.

## Non-goals

- No new features beyond the port (no new views, themes, or flags).
- No GitHub remote, PR, or fork workflow (none exists today).
- Executing on Windows/macOS: I can only **run** on Linux. Cross-OS is
  delivered as correct code + per-OS-simulated tests + docs, not as live runs
  on the other OSes.

## Anti-goals

- Do not lose git history.
- Do not leave dead Unsloth code, flags, tables, or docstrings behind.
- Do not change the on-screen bytes of OpenCode session/daily/total/theme
  output on Linux **except** for the tool-name rename: the panel title
  `tokenstat · <view>` becomes `tokenstats · <view>`. Regenerate `golden/` and
  confirm the diff is *only* that tool-name substring (Unsloth removal must not
  touch any session-view golden file).
- Do not break the stdlib-only, single-file, no-install property.

## Constraints

- stdlib-only Python, one file, no install step **required** to run it
  (`python tokenstats.py` always works). Packaging is additive, not a
  replacement: an optional `pipx`/`pip` install for a PATH command.
- Preserve the source invariants that still apply: no double counting (one row
  per `session_id`, monotonic max, rows never deleted); labels never shrink;
  local-time, until-day-inclusive date windows via `_day_end_ms`; sources open
  read-only; warn-and-continue on a missing/locked source in report mode, exit 1
  on explicit `record` failure; WAL + busy_timeout on our store.
- **Invariant dropped:** "the two sources describe the same traffic / never sum
  them" — there is now one source, so `reconcile` and that invariant go away.

## Approaches considered

**A. Surgical port of the copy (chosen).** Copy the repo with `.git`, rename,
then delete the Unsloth code paths and prune/adapt the test suite, and add the
cross-OS handling. Keeps git history, the mature single-file structure, the
theme system, and the golden corpus. Lowest regression risk.

**B. Fresh rewrite.** Write a new, smaller OpenCode-only tool from scratch.
Smaller file, but throws away git history, ~435 tests, and the golden corpus;
higher risk of silently changing display behavior. Rejected — the existing work
already does exactly the OpenCode half we want.

## The how (decisions)

Made by the human partner:
- **Full rename** to `tokenstats` (script, prog, state dir, docs, tests).
- **Full Unsloth removal**, including `reconcile` and dropping the
  `unsloth_events` / `unsloth_runs` tables from the schema.
- **Robust + tested** cross-OS.

My calls (correct me):
- **State dir** `~/.tokenstats` (avoids colliding with the original tool's
  `~/.tokenstat`; a fresh store is correct for a fresh, narrower tool).
- **OpenCode source path resolution** (`default_src`) rewritten to mirror how
  OpenCode actually resolves it, with defensive multi-candidate probing:
  1. `$OPENCODE_DB` if set (OpenCode's own db override).
  2. `$XDG_DATA_HOME/opencode/opencode.db` when `XDG_DATA_HOME` is absolute.
  3. `~/.local/share/opencode/opencode.db` (the `xdg-basedir` default OpenCode
     uses on **all** OS — this is the fallback when nothing exists).
  4. macOS: `~/Library/Application Support/opencode/opencode.db`.
  5. Windows: `%APPDATA%\opencode\opencode.db`.
  6. Legacy: `~/.opencode/opencode.db`.
  First candidate that exists wins; otherwise the xdg default (#3) is returned
  so a fresh machine reports "no sessions" rather than a wrong path. `--src`
  still overrides everything.
- **UTF-8-safe output**: on startup, best-effort `sys.stdout/stderr.reconfigure
  (encoding="utf-8")` guarded by `hasattr`/try-except, so the box-drawing and
  Unicode bar glyphs never crash a Windows cp1252 console. No-op on POSIX.
- **Scheduling docs**: README/AGENTS document cron (Linux/macOS) and Task
  Scheduler (Windows) instead of the Linux-only crontab line.
- **Test strategy**: delete Unsloth-only test classes; adapt shared tests that
  touch Unsloth; add a `PathResolutionTests` class that monkeypatches
  `sys.platform` + env to assert each OS resolves the right candidate; keep the
  golden corpus covers OpenCode session views, so after regeneration its only
  diff is the `tokenstat · <view>` → `tokenstats · <view>` title rename —
  verified as an acceptance check).
- **Commits**: work happens on `master` in the copied repo as new commits
  (history preserved by the copy). No push (no remote).
- **Packaging (new scope, my call — proportionate to "start thinking"):** add a
  minimal `pyproject.toml` (setuptools backend, `py-modules = ["tokenstats"]`,
  `requires-python >= 3.9`) declaring a console script
  `tokenstats = "tokenstats:cli_main"`, plus a thin zero-arg `cli_main()`
  wrapper (`return main(sys.argv[1:])`) so the entry point works. This gives a
  cross-platform `tokenstats` command via `pipx install .` / `pip install .` on
  Linux/macOS/Windows with zero runtime deps. README gains an Install section
  (pipx recommended; `pip install --user` and `python tokenstats.py` as
  alternatives). **Out of scope for now** (flagged for later): self-contained
  binaries (PyInstaller/zipapp), and OS-native package managers (brew/apt/
  winget/choco).

## Removal surface (what "remove Unsloth" touches)

- Schema: drop `unsloth_events`, `unsloth_runs` tables and
  `idx_unsloth_events_created`.
- Functions: `unsloth_db`, `import_unsloth`, `import_unsloth_runs`, the event
  and run query/aggregate functions (`query_events`/`total_events`/
  `daily_events`, `query_runs`/`total_runs`/`daily_runs`, their rate groups and
  filters), `reconcile`, `UNSLOTH_PROVIDER`, `DEFAULT_UNSLOTH_DB`.
- CLI: `command` choices `import-unsloth` and `reconcile`; flags `--unsloth-db`,
  `--from-unsloth`, `--from-runs`; the Unsloth ingestion calls in report mode;
  the `--cache`/`--real-cost`/`--sessions` guards that reference the Unsloth
  views (simplified to session-only); `RECON_COLUMNS` and reconcile rendering.
- Docs/tests: README, AGENTS.md, docstrings, and the Unsloth test classes.

## Left to the builder (implementation plan)

Exact function-by-function deletions, test-by-test edits, the precise
`default_src` candidate order and its tests, golden regeneration diff review,
and the commit breakdown.
