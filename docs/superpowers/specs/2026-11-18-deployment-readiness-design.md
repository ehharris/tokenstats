# Deployment readiness — design (2026-11-18)

## Intent and why

`tokenstat.py` is meant to be a single file you drop on a machine and run.
Today it is not: its own state (the store, the themes folder) lives **next to
the script**, so a copy in `/usr/local/bin` — or any read-only location —
cannot write its store or save a theme default, and the defaults printed by
`--help` name this machine's layout. It also advertises six options in `-h`
that a new user should never have to think about. The goal is that a stranger
can copy one file anywhere, run it from anywhere, and get a working report on
a fresh machine with OpenCode installed to its defaults.

Three pieces of work, in the order the human partner listed them: portable
default locations, a quieter `--help`, and a redundancy pass over the code.

## Goals

- All of tokenstat's own state lives in one flat, predictable directory:
  `~/.tokenstat/`.
- Source discovery matches the vendor defaults on a fresh machine, including
  `XDG_DATA_HOME` for OpenCode.
- `--help` shows only what a user needs; the six internal options keep working.
- Less duplicated code, same behavior, same bytes on screen.
- Docs (README, AGENTS.md, themes/README.md, module docstring) describe the
  new layout, including the cron line.

## Non-goals

- No new features, no new views, no schema change, no new dependencies, no
  install step, no packaging.
- No XDG directory ceremony for our own state (that was option A, declined).
- No environment variables for our own paths; `--db`/`--src`/`--unsloth-db`
  remain the override mechanism (undocumented, still functional).
- No output changes of any kind: golden files stay byte-identical.

## Constraints

- 393 tests stay green, including under `-W error::ResourceWarning`.
- `golden/` stays byte-identical. `--json` serialises rows with a dict
  comprehension over `r.items()`, so **row key insertion order is part of the
  pinned output** — any refactor that rebuilds a row dict must preserve it.
- Importing `tokenstat` must not touch the filesystem: no `mkdir`, no
  migration, at import time. Tests import the module and patch the
  `THEMES_DIR` / `THEME_CONFIG` module globals at call time, so those names
  stay module-level globals read when used.
- Tests pass `--db`/`--src`/`--unsloth-db` on essentially every CLI call
  (~230 occurrences), so those flags must keep working unchanged.
- The live store on this machine (~1.8 MB, real history) and the hourly cron
  currently point at the repo directory; the change must not lose or split it.

## Approaches considered for "where does our state live"

- **A — XDG dirs** (`$XDG_DATA_HOME/tokenstat`, `$XDG_CONFIG_HOME/tokenstat`).
  Correct by desktop-Linux standards, invisible to anyone who has never heard
  of XDG. Declined: the extra concept buys this tool nothing.
- **B — keep it next to the script**, fix only the source paths. Nothing to
  migrate, but a read-only install cannot write its store or save a theme,
  which is the exact failure we are removing. Declined.
- **C — one flat `~/.tokenstat/`** for store and themes. One path to document,
  one path to back up, one path to delete to start over. **Chosen**, for
  simplicity for end users.

## The how

### Directory layout

```
~/.tokenstat/usage.db            our store (plus its -wal / -shm siblings)
~/.tokenstat/themes/             user theme files, scanned by --theme
~/.tokenstat/themes/config.json  the saved default theme (--theme-default)
~/.tokenstat/record.log          the cron log (the crontab chooses this, not the code)
```

`DEFAULT_DB`, `THEMES_DIR` and `THEME_CONFIG` become path arithmetic off
`os.path.expanduser("~/.tokenstat")` — computed at import (pure string math),
created on demand in `main()`. The repo's `themes/` keeps `README.md` and
`examples/` as documentation only; it is no longer read at runtime.

### Source defaults

- OpenCode: `$XDG_DATA_HOME/opencode/opencode.db` when `XDG_DATA_HOME` is set
  and non-empty, else `~/.local/share/opencode/opencode.db`. Verified against
  the installed binary: OpenCode resolves `XDG_DATA_HOME` first, so this
  matches where the file actually is.
- Unsloth Studio: `~/.unsloth/studio/studio.db`, unchanged — that is Studio's
  own fixed location.

### One-time migration (this machine, and any other old install)

Before the store opens, `main()` runs one small migration step:

- If `~/.tokenstat/usage.db` is absent and `<script dir>/usage.db` exists,
  move `usage.db` and its `-wal`/`-shm` siblings into `~/.tokenstat/` and
  print one line to stderr: `migrated store: <old> -> <new>`.
- If that move fails (read-only repo dir, cross-device), **keep using the
  legacy file as the store for that run** and say so — a failed move must
  never present an empty report as if the history were gone.
- Same rule for themes: if `~/.tokenstat/themes/` is absent and
  `<script dir>/themes/` holds user `.json` files, move those `.json` files
  (never `README.md`, never `examples/`).

No flags, no prompts, idempotent: once the new paths exist, the step does
nothing. Importing the module never runs it.

### `--help`

`help=argparse.SUPPRESS` on `--min-in`, `--min-out`, `--db`, `--src`,
`--unsloth-db`, `--from-unsloth` — the same mechanism `--cost-tier` already
uses. Defaults and destinations are untouched, so every existing invocation
and every test still parses. `--from-runs` stays visible (the human partner's
list named only `--from-unsloth`); help text that mentions a hidden flag is
reworded so `-h` never points at something it does not document.

### Redundancy pass (behavior-preserving)

1. **Filter builders.** `_session_filters`, `_event_filters`, `_run_filters`
   each re-implement the escaped-LIKE model clause and the
   `>= since / < _day_end_ms(until)` date window. Extract `_like_pattern()`
   and a shared date-window helper; each filter function keeps its own table
   and column names.
2. **Aggregate families.** `daily_events`/`daily_runs`/`daily_totals` and
   `total_events`/`total_runs`/`total_totals` are the same query in three
   shapes (the deferred item in AGENTS.md). Collapse each family to one
   parameterised helper that takes the table, the SUM list, the group key and
   the rate groups, and builds the row dict in the exact key order each view
   produces today.
3. **Ingest loops.** The four import functions share
   SELECT-all → per-row probe → monotonic-max INSERT/UPDATE → `skipped`
   counter. Extract one monotonic-max upsert helper; `record()` keeps its
   never-shrink label merge and `import_opencode_steps` keeps its turn
   ordinals and NULL-`step_ms` rule — those are semantics, not duplication.
   `counts` dicts become `collections.Counter`.
4. **`main()` dispatch.** The nested conditional expressions that pick rows
   and columns per source, and the reconcile reject-list built by chaining
   `+ (["--x"] if ... else [])`, become a compact table-driven form. The
   dashboard block and the general render path share one render helper.

Public function names and signatures stay as they are — the test suite calls
them directly.

### Tests to add

- Default paths resolve under a patched `HOME` to `~/.tokenstat/...`, and
  `XDG_DATA_HOME` steers the OpenCode default.
- The migration moves a legacy script-adjacent store and themes, is a no-op
  the second time, and falls back to the legacy store when the move fails.
- `--help` no longer lists the six options; each still parses and behaves.
- The existing golden and 393-test suites, unchanged.

### Verification

- `python3 -m unittest test_tokenstat` and the `-W error::ResourceWarning`
  variant: green.
- `golden/` diff-free without `TOKENSTAT_UPDATE_GOLDEN`.
- Fresh-machine smoke: `env -i HOME=/tmp/fresh PATH=/usr/bin:/bin python3
  <copy>/tokenstat.py` from an unrelated cwd — creates `~/.tokenstat/`,
  warns about absent sources, exits 0, and reports from its own new store.

### Decisions I made (flag anything you'd call differently)

- `~/.tokenstat/` is derived from `$HOME` only; no `--home` or env override.
- Themes get one runtime directory (the repo's `themes/` becomes docs +
  examples only) rather than a two-directory scan.
- The live crontab line is updated to the new paths as part of this work
  (`$HOME/.tokenstat/record.log`), and the repo's `usage.db` migrates out on
  the first run after the change.
- One test (`test_reconcile_rejects_unsupported_flags`) calls `main()` with no
  `--db` and so touches whatever the default store is; it only asserts exit 2.
  Left as-is.
- The stray 11-line `/home/elih/messinaround/test_tokenstat.py` outside the
  repo is a broken leftover from an earlier session; deleting it is proposed,
  not assumed.
