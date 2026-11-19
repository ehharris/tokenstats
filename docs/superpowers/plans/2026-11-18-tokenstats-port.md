# tokenstats port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce `tokenstats` — a copy of `token_counter` (full git history, no fork) renamed throughout, reduced to OpenCode-only token stats, and made to run correctly on Linux, Windows, and macOS.

**Architecture:** Single stdlib-only Python file (`tokenstats.py`) with a companion unittest suite (`test_tokenstats.py`). Work proceeds as a sequence of green commits on `master` in the copied repo: copy → remove Unsloth → rename → cross-OS → package → document → verify. The theme system, store engine, and display layer are preserved; only the Unsloth source and its views are removed, and the OpenCode source-path resolution is made platform-aware.

**Tech Stack:** Python 3.9+ stdlib only (`argparse`, `sqlite3`, `os`, `sys`, `json`, `datetime`, `colorsys`, `re`, `time`, `collections`). setuptools + `pyproject.toml` for the optional install entry point. `unittest` for tests.

**Spec:** `docs/superpowers/specs/2026-11-18-tokenstats-port-design.md` (committed in Task 1).

## Global Constraints

- stdlib-only, single file; `python tokenstats.py` must always work with no install step.
- Python floor: 3.9 (the module uses `list[str]` annotations at def time).
- Tool identity is `tokenstats` everywhere: script `tokenstats.py`, test `test_tokenstats.py`, argparse `prog="tokenstats"`, state dir `~/.tokenstats`, panel title `tokenstats · <view>`, golden env var `TOKENSTATS_UPDATE_GOLDEN`.
- No Unsloth anywhere: no `unsloth_events`/`unsloth_runs` tables, no `import-unsloth`/`reconcile` commands, no `--unsloth-db`/`--from-unsloth`/`--from-runs` flags, no `reconcile`/event/run functions.
- Preserve store invariants that still apply: one row per `session_id` (and per `message_id` in `opencode_steps`), monotonic-max numerics, rows never deleted; labels never shrink; local-time until-day-inclusive date windows via `_day_end_ms`; sources open read-only (`file:...?mode=ro`); report mode warns-and-continues on a missing/locked source (exit 0), explicit `record` exits 1 on failure; our store uses WAL + `busy_timeout=5000`.
- Importing the module touches no filesystem (only `default_store()` and `write_default_theme()` write, and only when asked).
- The suite stays green, including under `-W error::ResourceWarning`.
- No git remote / no push (the copied repo has none).

## Review Focus

Failure modes the spec implies that no single task's happy-path test guarantees — each gets a pinning test in the owning task:

- **Windows console encoding:** printing box-drawing / Unicode bar glyphs on a cp1252 console must not raise `UnicodeEncodeError`. Expect: output renders (best-effort UTF-8), never a traceback. (Task 4)
- **Per-OS OpenCode path resolution:** on each OS the resolved source is the platform's real location when it exists, and the `~/.local/share/opencode` xdg default when nothing exists. Expect: correct candidate chosen by `sys.platform` + env. (Task 4)
- **Fresh machine:** no OpenCode db anywhere → `default_src` returns the xdg default and a bare run prints "no sessions match" and exits 0, creating only `~/.tokenstats`. (Task 4 + Task 7)
- **Legacy store opened:** pointing `--db` at an old `token_counter` store that still has `unsloth_events`/`unsloth_runs` tables must not error — the extra tables are simply unused. (Task 2)
- **Import purity after rename:** importing `tokenstats` under an empty `$HOME` creates nothing and binds `STATE_DIR` to `~/.tokenstats`. (Task 3)

---

## Task 1: Copy the repo with history and commit the design + plan

**Files:**
- Create: `/home/elih/messinaround/tokenstats/` (full copy of `token_counter`, including `.git`)
- Create: `tokenstats/docs/superpowers/specs/2026-11-18-tokenstats-port-design.md`
- Create: `tokenstats/docs/superpowers/plans/2026-11-18-tokenstats-port.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the working tree all later tasks edit, at `/home/elih/messinaround/tokenstats`, on branch `master`, with the original history intact.

- [ ] **Step 1: Copy the directory including `.git`**

```bash
cp -a /home/elih/messinaround/token_counter /home/elih/messinaround/tokenstats
```

- [ ] **Step 2: Verify the copy is a clean git repo with the original history**

Run: `cd /home/elih/messinaround/tokenstats && git log --oneline -1 && git status --short`
Expected: top commit `ab50000 docs: correct the plan's test count...`; empty status.

- [ ] **Step 3: Copy the design doc and this plan into the repo**

Copy `/tmp/opencode/tokenstats-design.md` to `docs/superpowers/specs/2026-11-18-tokenstats-port-design.md` and this plan file to `docs/superpowers/plans/2026-11-18-tokenstats-port.md`.

- [ ] **Step 4: Confirm the suite still passes in the copy (unchanged)**

Run: `cd /home/elih/messinaround/tokenstats && python3 -m unittest test_tokenstat 2>&1 | tail -3`
Expected: `OK` (435 tests).

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-11-18-tokenstats-port-design.md docs/superpowers/plans/2026-11-18-tokenstats-port.md
git commit -m "docs: add tokenstats port design and plan"
```

---

## Task 2: Remove all Unsloth functionality (module still named `tokenstat`)

**Files:**
- Modify: `tokenstat.py` (schema, functions, CLI, `main`)
- Modify: `test_tokenstat.py` (delete Unsloth-only classes; prune shared ones)
- Delete: `golden/unsloth-total.piped.txt`, `golden/unsloth-total.tty.txt`, `golden/unsloth-json.piped.txt`, `golden/unsloth-json.tty.txt`, `golden/runs-total-model.piped.txt`, `golden/runs-total-model.tty.txt`

**Interfaces:**
- Consumes: Task 1 tree.
- Produces: an OpenCode-only `tokenstat` module. Removed symbols (later tasks must not reference them): `unsloth_db`, `DEFAULT_UNSLOTH_DB`, `import_unsloth`, `import_unsloth_runs`, `_event_filters`, `query_events`, `daily_events`, `total_events`, `_run_filters`, `_run_rate_groups`, `query_runs`, `daily_runs`, `total_runs`, `UNSLOTH_PROVIDER`, `reconcile`, `RECON_COLUMNS`. Kept and still used by session views: `_grouped_aggregates`, `_total_aggregates`, `_weighted_mean_rate`, `_weighted_mean_rate_and_weight`, `_session_rate_groups`, `_aggregate_rate` (drop only if the suite shows it unused after removal).

- [ ] **Step 1: Delete the Unsloth-only test classes**

Remove these classes from `test_tokenstat.py`: `EventTokSTests`, `ImportRunsTests`, `RunsQueryTests`, `ImportUnslothTests`, `CliUnslothTests`, `CliUnslothFailTests`, `TotalEventsTests`.

- [ ] **Step 2: Remove the three Unsloth golden cases and their files**

In `ByteIdentityGoldenTests.CASES`, delete the `unsloth-total`, `unsloth-json`, and `runs-total-model` entries. Delete the six golden files listed under **Files:**.

- [ ] **Step 3: Run the suite to see the Unsloth references that still fail**

Run: `python3 -m unittest test_tokenstat 2>&1 | tail -20`
Expected: FAIL/ERROR referencing removed symbols and Unsloth paths (guides the prune in Step 4).

- [ ] **Step 4: Prune Unsloth references from the shared test classes**

Use `grep -n "unsloth\|reconcile\|from_runs\|from_unsloth\|_events\|_runs\|RECON" test_tokenstat.py` to find every remaining reference. In `DefaultPathsTests` delete `test_unsloth_default_is_the_studio_location` and the `DEFAULT_UNSLOTH_DB` assertion in `test_module_defaults_point_at_the_state_dir`. In `SchemaTests` drop assertions on `unsloth_events`/`unsloth_runs`. In `FilterHelperTests`/`LikeEscapingTests` drop the event/run filter legs. In `CliTests`/`ReviewFixTests`/`ReviewFixTests2`/`CliTotalTests`/`CliShareTests`/`CliCacheTests`/`CliSessionsTests`/`DefaultDashboardTests`/`HiddenOptionsTests`/`ViewDispatchTests`/`AggregateRowShapeTests`/`HeroTokSTests`/theme CLI classes, remove assertions and fixtures that exercise `--from-unsloth`/`--from-runs`/`reconcile`/event/run aggregates. Keep every OpenCode-session assertion intact.

- [ ] **Step 5: Remove the Unsloth schema**

In `ensure_schema`, delete the `unsloth_events` and `unsloth_runs` `CREATE TABLE` statements and the `idx_unsloth_events_created` index. Leave `usage`, `opencode_steps`, `idx_usage_time_created`, `idx_opencode_steps_session_turn`.

- [ ] **Step 6: Delete the Unsloth functions and globals**

Delete from `tokenstat.py`: `unsloth_db` and `DEFAULT_UNSLOTH_DB`; `import_unsloth`; `import_unsloth_runs`; `_event_filters`, `query_events`, `daily_events`, `total_events`; `_run_filters`, `_run_rate_groups`, `query_runs`, `daily_runs`, `total_runs`; `UNSLOTH_PROVIDER`, `reconcile`, `RECON_COLUMNS`. If `_aggregate_rate` now has no callers (grep it), delete it too.

- [ ] **Step 7: Strip Unsloth from the CLI and `main`**

In `main`: drop `import-unsloth` and `reconcile` from the `command` choices (leaving only `record`); delete the `--unsloth-db`, `--from-unsloth`, `--from-runs` arguments; delete the `import-unsloth` and `reconcile` branches; delete the two Unsloth `_ingest(...)` calls in report mode; simplify the `--cache`/`--real-cost`/`--sessions` guards and the `compact_dash` condition to drop `from_unsloth`/`from_runs`; in `_view_rows`/`_view_title` remove the event/run/reconcile branches. Update the module docstring and `--help` description to say OpenCode only.

- [ ] **Step 8: Run the suite to green**

Run: `python3 -m unittest test_tokenstat 2>&1 | tail -5`
Expected: `OK` (test count dropped by the removed Unsloth tests).

- [ ] **Step 9: Verify a legacy store with Unsloth tables still opens (Review Focus)**

Add a test `LegacyStoreOpensTests` in `test_tokenstat.py`: create a store, `CREATE TABLE unsloth_events(event_id TEXT PRIMARY KEY)` and `unsloth_runs(run_id TEXT PRIMARY KEY)` by hand, then run `tokenstat.main(["--db", db, "--src", src, "--total"])` and assert exit 0. Run it: `python3 -m unittest test_tokenstat.LegacyStoreOpensTests -v` → PASS.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "refactor: remove Unsloth Studio source; tokenstat is OpenCode-only"
```

---

## Task 3: Rename the tool identity to `tokenstats`

**Files:**
- Rename: `tokenstat.py` → `tokenstats.py`; `test_tokenstat.py` → `test_tokenstats.py`
- Modify: `tokenstats.py` (module identifier refs, state dir, prog, panel title, golden env var), `test_tokenstats.py` (imports, `tokenstat.` refs, path assertions, env var)
- Regenerate: `golden/*.txt` (title rename only)

**Interfaces:**
- Consumes: Task 2 tree.
- Produces: module importable as `tokenstats`; `state_dir()` returns `~/.tokenstats`; panel title `tokenstats · <view>`; golden env var `TOKENSTATS_UPDATE_GOLDEN`.

- [ ] **Step 1: git mv the two files**

```bash
git mv tokenstat.py tokenstats.py && git mv test_tokenstat.py test_tokenstats.py
```

- [ ] **Step 2: Rename the module identifier and identity strings in `tokenstats.py`**

Replace the module identifier `tokenstat` with `tokenstats` where it is the tool name: `prog="tokenstats"`; the panel-title literals `" tokenstat · "` → `" tokenstats · "` and `" tokenstat "` → `" tokenstats "`; the state-dir literal `".tokenstat"` → `".tokenstats"` (in `state_dir`); docstrings and comments that name the tool or the `~/.tokenstat` path. Do NOT rename unrelated words; only the tool name and its state-dir path.

- [ ] **Step 3: Rename the module identifier in `test_tokenstats.py`**

Replace `import tokenstat` → `import tokenstats`, `from tokenstat import` → `from tokenstats import`, and every `tokenstat.` attribute access → `tokenstats.`; update the two `spec_from_file_location("tokenstat_...", tokenstat.__file__)` reloads to `tokenstats.__file__` (module name string may stay a distinct label). Update `DefaultPathsTests` path literals `/.tokenstat` → `/.tokenstats` and the `STATE_DIR`/`DEFAULT_DB`/`THEMES_DIR`/`THEME_CONFIG` assertions to `~/.tokenstats`. Update the golden env var read `TOKENSTAT_UPDATE_GOLDEN` → `TOKENSTATS_UPDATE_GOLDEN` (and the same literal in `tokenstats.py` if present).

- [ ] **Step 4: Run the suite; expect only golden title mismatches**

Run: `python3 -m unittest test_tokenstats 2>&1 | tail -20`
Expected: `ByteIdentityGoldenTests` fails on the `tokenstat · ` → `tokenstats · ` title line; all other tests pass.

- [ ] **Step 5: Regenerate golden and confirm the diff is only the tool-name rename**

```bash
TOKENSTATS_UPDATE_GOLDEN=1 python3 -m unittest test_tokenstats >/dev/null 2>&1
git --no-pager diff --stat golden/
git --no-pager diff golden/ | grep -E '^[+-]' | grep -v '^[+-][+-]' | grep -vi 'tokenstats\?' | head
```
Expected: the last grep prints nothing (every changed line contains the tool name). If it prints other content, stop — an unintended byte changed.

- [ ] **Step 6: Run the full suite to green**

Run: `python3 -m unittest test_tokenstats 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: rename tool to tokenstats (script, prog, state dir, titles)"
```

---

## Task 4: Cross-OS source-path resolution and UTF-8-safe output

**Files:**
- Modify: `tokenstats.py` (`default_src`, new `_ensure_utf8_streams`, call it in `main`)
- Modify: `test_tokenstats.py` (rewrite `default_src` tests; add `PathResolutionTests`, `EnsureUtf8Tests`)

**Interfaces:**
- Consumes: Task 3 tree.
- Produces: `default_src(home=None) -> str` (multi-candidate, first existing wins, else `~/.local/share/opencode/opencode.db`); `_ensure_utf8_streams(out=None, err=None) -> None` (best-effort `reconfigure(encoding="utf-8")`, no-op when a stream lacks `reconfigure`).

- [ ] **Step 1: Write failing `PathResolutionTests`**

Add a class that monkeypatches `sys.platform` and `os.environ` and uses a temp `home` with real files to pin:
- `OPENCODE_DB` (absolute) wins when it exists.
- absolute `XDG_DATA_HOME` wins when its `opencode/opencode.db` exists.
- `sys.platform=="darwin"` finds `~/Library/Application Support/opencode/opencode.db`.
- `sys.platform=="win32"` with `APPDATA` finds `%APPDATA%\opencode\opencode.db`.
- legacy `~/.opencode/opencode.db` is found.
- when nothing exists, returns `<home>/.local/share/opencode/opencode.db`.
- `--src` still overrides (assert via `main(["--src", path, ...])` reading that file).

Run: `python3 -m unittest test_tokenstats.PathResolutionTests -v`
Expected: FAIL (current `default_src` ignores platform/OPENCODE_DB and never probes).

- [ ] **Step 2: Implement the new `default_src`**

Replace `default_src` with the candidate list from the spec (order: `OPENCODE_DB` if absolute → `$XDG_DATA_HOME/opencode/opencode.db` if absolute → `<home>/.local/share/opencode/opencode.db` → darwin `Library/Application Support` → win32 `%APPDATA%` → `<home>/.opencode/opencode.db`), returning the first path that `os.path.exists`, else the `.local/share` default. Keep the `home=None` → `expanduser("~")` behavior.

- [ ] **Step 3: Run `PathResolutionTests` to green**

Run: `python3 -m unittest test_tokenstats.PathResolutionTests -v`
Expected: PASS.

- [ ] **Step 4: Update the existing `default_src` tests in `DefaultPathsTests`**

`test_xdg_data_home_moves_the_opencode_default` must now create the file at the XDG path before asserting it is returned (existence-based). `test_empty_or_relative_xdg_data_home_is_ignored` and the `DEFAULT_SRC` import-time assertion stay valid (nothing exists → xdg default).

- [ ] **Step 5: Write a failing `EnsureUtf8Tests`**

Assert `_ensure_utf8_streams` calls `reconfigure(encoding="utf-8")` on an object that has it (a real `io.TextIOWrapper` over a `BytesIO`), and is a silent no-op on an object without `reconfigure` (a plain `io.StringIO`). Run → FAIL.

- [ ] **Step 6: Implement `_ensure_utf8_streams` and call it in `main`**

```python
def _ensure_utf8_streams(out=None, err=None):
    """Best-effort UTF-8 so box-drawing/Unicode never crashes a Windows cp1252
    console. No-op on streams without reconfigure (StringIO in tests, already
    UTF-8 POSIX consoles)."""
    for stream in (sys.stdout if out is None else out,
                   sys.stderr if err is None else err):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
```
Call `_ensure_utf8_streams()` as the first statement in `main()`.

- [ ] **Step 7: Run `EnsureUtf8Tests` and the full suite to green**

Run: `python3 -m unittest test_tokenstats 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: resolve OpenCode db per-OS and force UTF-8 output (cross-OS)"
```

---

## Task 5: Packaging — installable `tokenstats` command

**Files:**
- Create: `tokenstats/pyproject.toml`
- Modify: `tokenstats.py` (add `cli_main`)
- Modify: `test_tokenstats.py` (add `CliMainTests`)

**Interfaces:**
- Consumes: Task 4 tree.
- Produces: `cli_main() -> int` (zero-arg entry point: `return main(sys.argv[1:])`); a `tokenstats` console script via `pyproject.toml`.

- [ ] **Step 1: Write a failing `CliMainTests`**

Assert `tokenstats.cli_main` is callable, and that with `sys.argv` patched to `["tokenstats", "--db", db, "--src", src, "--total"]` it returns the same int `main(["--db", db, "--src", src, "--total"])` returns. Run → FAIL (`cli_main` undefined).

- [ ] **Step 2: Add `cli_main` to `tokenstats.py`**

```python
def cli_main():
    """Console-script entry point: argv from sys.argv, exit code from main()."""
    return main(sys.argv[1:])
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "tokenstats"
version = "1.0.0"
description = "Permanent local record of OpenCode AI token usage."
requires-python = ">=3.9"
dependencies = []

[project.scripts]
tokenstats = "tokenstats:cli_main"

[tool.setuptools]
py-modules = ["tokenstats"]
```

- [ ] **Step 4: Run `CliMainTests` to green and verify the build metadata**

Run: `python3 -m unittest test_tokenstats.CliMainTests -v && python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['scripts'])"`
Expected: PASS; `{'tokenstats': 'tokenstats:cli_main'}`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: add pyproject.toml and cli_main install entry point"
```

---

## Task 6: Documentation — OpenCode-only, install, cross-OS scheduling

**Files:**
- Modify: `README.md`, `AGENTS.md`, `tokenstats.py` (module docstring)

**Interfaces:**
- Consumes: Tasks 2–5.
- Produces: docs that describe an OpenCode-only, cross-OS, installable `tokenstats`.

- [ ] **Step 1: Rewrite `README.md`**

Remove every Unsloth/`reconcile`/`--from-unsloth`/`--from-runs`/`import-unsloth` mention and example. Rename `tokenstat`→`tokenstats` and `~/.tokenstat`→`~/.tokenstats`. Add an **Install** section: `pipx install .` (recommended) or `pip install --user .` for a `tokenstats` command on Linux/macOS/Windows, plus "run without installing: `python3 tokenstats.py`". Replace the Linux-only crontab line with per-OS scheduling: cron (Linux/macOS) `0 * * * * python3 <path>/tokenstats.py record >> ~/.tokenstats/record.log 2>&1`, and a Windows Task Scheduler note (`schtasks /Create /SC HOURLY ...`). Note the OpenCode db is auto-found per OS.

- [ ] **Step 2: Update `AGENTS.md`**

Drop the Unsloth data-source section, the two-source invariants (keep the single-source ones), and Unsloth commands; rename to `tokenstats`/`~/.tokenstats`; add the install command and the per-OS scheduling; note the cross-OS `default_src` candidate order.

- [ ] **Step 3: Update the module docstring in `tokenstats.py`**

Describe OpenCode-only recording/reporting; drop the Unsloth/runs/reconcile sentences; keep the TOK/S and cost/cache/theme descriptions that still apply.

- [ ] **Step 4: Verify no stale references remain**

Run: `grep -rin "unsloth\|reconcile\|from-runs\|from-unsloth\|import-unsloth" README.md AGENTS.md tokenstats.py | grep -vi "no unsloth\|removed" | head`
Expected: no output (or only intentional "removed" notes).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: OpenCode-only README/AGENTS with install and cross-OS scheduling"
```

---

## Task 7: Final verification

**Files:** none (verification only).

**Interfaces:** Consumes Tasks 1–6.

- [ ] **Step 1: Full suite, plain and strict**

Run: `python3 -m unittest test_tokenstats 2>&1 | tail -3`
Expected: `OK`.
Run: `python3 -W error::ResourceWarning -m unittest test_tokenstats 2>&1 | tail -3`
Expected: `OK`, no warnings.

- [ ] **Step 2: Golden diff is only the tool-name rename**

Run: `git --no-pager diff HEAD~4 -- golden/ | grep -E '^[+-]' | grep -v '^[+-][+-]' | grep -vi 'tokenstats\?' | head`
Expected: no output.

- [ ] **Step 3: Fresh-machine smoke (Review Focus)**

Run with an empty `$HOME` and a temp store:
```bash
tmp=$(mktemp -d); HOME="$tmp" python3 tokenstats.py --db "$tmp/usage.db" --src "$tmp/none.db"; echo "exit=$?"
```
Expected: prints `no sessions match`, `exit=0`, and creates no `~/.tokenstats` under `$tmp` (because `--db` was given).

- [ ] **Step 4: Real-data smoke against this machine's OpenCode**

Run: `python3 tokenstats.py --total --model 2>&1 | head`
Expected: a per-model totals table titled `tokenstats · ...` with real rows (this machine has OpenCode data).

- [ ] **Step 5: Confirm git history is intact and clean**

Run: `git log --oneline | head -8 && git status --short`
Expected: original commits still present, then the new task commits; clean status.

---

## Self-Review

**1. Spec coverage:** copy+history (T1); full rename incl. state dir/prog/titles (T3); full Unsloth removal incl. reconcile + tables (T2); cross-OS path + UTF-8 + per-OS tests (T4); packaging (T5); docs incl. install + scheduling (T6); golden-only-title-rename + fresh-machine + import purity + legacy-store Review Focus (T2/T3/T4/T7). All spec goals have a task.

**2. Step scan:** each step is one action with a checkable result; bodies given only for the two new functions the tests don't fully determine (`default_src` order, `_ensure_utf8_streams`).

**3. Type consistency:** `default_src(home=None)->str`, `_ensure_utf8_streams(out=None, err=None)->None`, `cli_main()->int` used consistently across tasks and tests.

**4. Review Focus:** Windows encoding (T4 S5-6), per-OS path (T4 S1), fresh machine (T4 + T7 S3), legacy store opens (T2 S9), import purity (T3, existing `test_importing_creates_nothing` updated to `~/.tokenstats`).

**5. Proportion:** plan tracks the spec; removal steps point at symbols/greps rather than transcribing the 2,300-line file.
