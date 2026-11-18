# `--sessions` cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--sessions[=N]`, which scopes every session-family view to the N most recent sessions and, on its own, lists them newest-first.

**Architecture:** The cap becomes one more predicate produced by `_session_filters`, the choke point every session-family query already funnels through (`query_sessions`, `daily_totals`, `total_totals`, and the rate groups they hand to `_session_rate_groups`). Because rows, aggregates, and rate groups are all built from the same `where`/`params`, they cannot disagree about which sessions are in scope. The CLI adds the flag, resolves its view-dependent default, and renders the list view from the same recency helper the default dashboard already uses.

**Tech Stack:** stdlib Python 3 (`argparse`, `sqlite3`), unittest. Single module `tokenstat.py`, tests in `test_tokenstat.py`.

**Spec:** `docs/superpowers/specs/2026-11-17-sessions-view-design.md`

## Global Constraints

- stdlib only — no third-party imports, no install step.
- No SQLite JSON1 anywhere.
- Sources are opened read-only (`file:...?mode=ro`); never write to OpenCode's or Studio's databases.
- Invariant 1 (no double counting) is untouched: this feature only narrows which rows are read.
- Rates round to one decimal; a session with no valid turn renders blank.
- Colour stays tty-gated via `_color_enabled`; piped output stays byte-plain.
- The suite must stay green under `python3 -W error::ResourceWarning -m unittest test_tokenstat`.
- Semantics table, binding (from the spec):

  | command | sessions in scope |
  |---|---|
  | `--sessions` | 20 most recent, listed newest-first |
  | `--sessions=50` | 50 most recent, listed |
  | `--sessions=0` | all, listed newest-first |
  | `--sessions --daily` | all — each day covers every session created that day |
  | `--sessions --total` | all ever recorded |
  | `--sessions=10 --daily` / `--sessions=10 --total` | the 10 most recent |
  | `--daily` / `--total` with no `--sessions` | unchanged from today |

## Review Focus

The spec implies these but no single task's happy-path test exercises them; each is pinned by a named test in the task that owns the code.

1. **Cap applied twice, or to different sets on each side of an aggregate.** `daily_totals`/`total_totals` compute token sums and per-session rates from two queries; if the cap reaches one and not the other, the hero reproduces a total over a different session set than the rows on screen. Pinned by `AggregateLimitTests.test_rate_groups_see_the_same_cap_as_the_sums` (Task 1).
2. **Parameter order in the nested cap subquery.** Placeholders bind in statement-text order, so the inner `WHERE` placeholders come *before* the `LIMIT` placeholder but *after* the outer ones. Get this wrong and every filtered query silently binds the wrong values. Pinned by `SessionLimitTests.test_limit_with_a_model_filter_binds_in_order` (Task 1).
3. **`--sessions` must not trigger the default dashboard.** `compact_dash` is a conjunction of "no view flag"; a flag that is not added to it makes `--sessions` print the dashboard instead of its own table. Pinned by `CliSessionsTests.test_sessions_does_not_trigger_the_dashboard` (Task 2).
4. **Filters narrow first, then the cap.** `--model X --sessions=5` means the 5 most recent of X's sessions, not the 5 most recent sessions then filtered down to however many happen to match. Pinned by `SessionLimitTests.test_limit_applies_after_other_filters` (Task 1).
5. **Absent flag changes nothing.** Every existing view must render byte-identically when `--sessions` is not passed, including the bare dashboard's 5-row panel. Pinned by `CliSessionsTests.test_dashboard_panel_unchanged` (Task 2).

---

### Task 1: The session cap as a filter

**Files:**
- Modify: `tokenstat.py` — `_session_filters` (~line 780), `query_sessions` (~line 825), `daily_totals` (~line 1241), `total_totals` (~line 1268)
- Test: `test_tokenstat.py` — new `SessionLimitTests`, new `AggregateLimitTests`

**Interfaces:**
- Consumes: the existing `_session_filters(model, since_ms, until_ms, min_in, min_out) -> (where: list[str], params: list)`.
- Produces:
  - `_session_filters(model, since_ms, until_ms, min_in, min_out, limit=None)`. `limit` is `None` or `0` for "no cap", a positive int for "the N most recent".
  - `query_sessions(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None, limit=None)`
  - `daily_totals(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None, limit=None)`
  - `total_totals(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None, by_model=False, limit=None)`
  - Existing call sites pass no `limit` and behave exactly as before.

- [ ] **Step 1: Write the failing cap tests**

Add `SessionLimitTests` to `test_tokenstat.py`, following the fixture style of `SessionTokSTests` (build a store with `make_dest()`, insert sessions with the existing `usage` row helper, `addCleanup(dest.close)`). Sessions must have distinct `time_created` values, plus one pair sharing a `time_created` to pin the tie-break.

Pins, each a real assertion on `query_sessions(dest, limit=...)`:

- **`limit=None` and `limit=0` both return every session** (same count, same order — oldest first, unchanged).
- **`limit=2` of five sessions returns the two newest**, still in oldest-first order (so the caller can reverse for display).
- **`limit` larger than the population returns everything**, no error.
- **`limit` applies after the other filters**: with three sessions for model `provA` and two for `provB`, `query_sessions(dest, model="provA", limit=2)` returns the two most recent `provA` sessions — and specifically not the two most recent overall.
- **Parameter order is correct when a filter and a cap combine**: the same call as above returns rows whose `model` field all contain `provA` (a mis-ordered bind raises `IndexError`/`InterfaceError` or returns the wrong set).
- **Ties break on `session_id`**: two sessions share `time_created`; `limit=1` returns the one with the lexicographically smaller `session_id`, deterministically across repeated calls.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.SessionLimitTests -v`
Expected: FAIL — `TypeError: query_sessions() got an unexpected keyword argument 'limit'`.

- [ ] **Step 3: Add the cap predicate to `_session_filters`**

Append the `limit=None` parameter and, after the existing predicates are built, add:

```python
    if limit:  # None and 0 both mean "no cap"
        inner = list(params)
        where.append("session_id IN (SELECT session_id FROM usage"
                     + (" WHERE " + " AND ".join(where) if where else "")
                     + " ORDER BY time_created DESC, session_id LIMIT ?)")
        params = inner + inner + [int(limit)]
```

The inner subquery repeats the outer predicates so the cap counts survivors of the filters, not everything in the table. The `params = inner + inner + [limit]` line is the positional-bind order: outer predicates, then the same predicates inside the subquery, then the `LIMIT`. `session_id` is the tie-break so a cap boundary landing on equal `time_created` values is deterministic.

- [ ] **Step 4: Run the cap tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.SessionLimitTests -v`
Expected: PASS

- [ ] **Step 5: Write the failing aggregate tests**

Add `AggregateLimitTests`. Fixture: four sessions across two days and two models, each with step rows so every session has a rate (reuse `make_step_src`/`step_msg`). Pins:

- **`daily_totals(dest, limit=2)`** returns only the day(s) the two most recent sessions fall on, and its `sessions` count sums to at most 2.
- **`total_totals(dest, by_model=True, limit=2)`** returns only the models of those two sessions.
- **`total_totals(dest, limit=2)`**'s `rate_weight` equals the Σ `rate_weight` of the same two sessions from `query_sessions(dest, limit=2)` — this is the Review Focus 1 pin: the rate side and the sum side saw the same set.
- **No `limit` still aggregates everything** (counts match the uncapped values).

- [ ] **Step 6: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.AggregateLimitTests -v`
Expected: FAIL — unexpected keyword `limit`.

- [ ] **Step 7: Thread `limit` through the three query functions**

Add `limit=None` to `query_sessions`, `daily_totals`, and `total_totals`, pass it to each function's `_session_filters(...)` call, and mention the cap in each docstring in one clause. `total_totals` passes it in both its branches (grand-total and per-model). Nothing else changes: `_session_rate_groups` receives the same `where`/`params` its caller just built, so it inherits the cap for free.

- [ ] **Step 8: Run the aggregate tests and the full suite**

Run: `python3 -m unittest test_tokenstat.AggregateLimitTests -v` then `python3 -m unittest test_tokenstat`
Expected: PASS, all green.

- [ ] **Step 9: Commit**

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: session cap as a shared filter predicate"
```

---

### Task 2: The `--sessions` flag and the list view

**Files:**
- Modify: `tokenstat.py` — argparse block in `main` (~line 1320), `compact_dash` (~line 1430), the view dispatch tail (~line 1530), the default dashboard's session panel (~line 1443)
- Test: `test_tokenstat.py` — new `CliSessionsTests`

**Interfaces:**
- Consumes: `limit=` on `query_sessions`/`daily_totals`/`total_totals` from Task 1.
- Produces:
  - `most_recent(rows: list[dict], n: int) -> list[dict]` — `rows` oldest-first in, newest-first out, truncated to `n`; `n <= 0` returns all.
  - `args.sessions`: `None` when the flag is absent, the string `SESSIONS_BARE` when present without a value, otherwise an `int`.
  - `_sessions_limit(value, daily: bool, total: bool) -> int | None` — resolves the three cases above into the `limit` passed to the query functions (`None` = flag absent).

- [ ] **Step 1: Write the failing CLI tests**

Add `CliSessionsTests` following `DefaultDashboardTests`: a temp store built from `src_row(...)` with at least 25 sessions of distinct `time_created`, a `run_main(argv)` helper returning `(code, stdout, stderr)`, and `_ANSI_RE.sub` to strip colour before asserting. Pins:

- **`--sessions` alone lists 20 rows, newest first**, and the table title is `sessions · 20 most recent`.
- **`--sessions=5` lists 5**; **`--sessions 5`** (space form) behaves identically.
- **`--sessions=0` lists all** and the title is `sessions · all`.
- **`--sessions` does not trigger the dashboard**: output contains no `usage by model · top 5 by share`.
- **`--sessions=-1` exits 2** with a message naming `--sessions`, and prints no table.
- **`--sessions=abc` exits 2** (argparse's int error).
- **`--sessions` with `--from-unsloth`, `--from-runs`, or `reconcile` exits 2** naming the unsupported combination.
- **`--sessions=3 --total` totals only those 3**: the grand-total row's `total` equals the Σ `total` of `--sessions=3`'s listed rows.
- **`--sessions --total` equals plain `--total`** (byte-identical table).
- **`--sessions --daily` equals plain `--daily`** — every session from each day is counted.
- **`--sessions=3 --daily`** shows no more days than those 3 sessions span.
- **`--sessions=4 --model PATTERN`** lists the 4 most recent matching sessions.
- **`--sessions --json`** emits a JSON array, newest first, length 20.
- **Dashboard regression pin**: a bare run still prints `sessions · 5 most recent` with 5 rows.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.CliSessionsTests -v`
Expected: FAIL — argparse rejects the unknown `--sessions` flag (SystemExit / exit 2) or the title assertions fail.

- [ ] **Step 3: Add the flag, the resolver, and the recency helper**

In the argparse block:

```python
    parser.add_argument("--sessions", nargs="?", const=SESSIONS_BARE,
                        type=int, default=None, metavar="N",
                        help="scope to the N most recent sessions "
                             "(default 20 when listing, all when aggregating)")
```

with `SESSIONS_BARE = "bare"` as a module constant next to the other view sentinels. `argparse` does not run `type` on `const`, so a valueless flag yields the sentinel while `--sessions=5` yields `5`.

`_sessions_limit(value, daily, total)` returns `None` when `value is None`, `0` when the value is `SESSIONS_BARE` and either `daily` or `total` is set, `20` when it is `SESSIONS_BARE` otherwise, and the int otherwise. `most_recent(rows, n)` is `list(reversed(rows))[:n] if n else list(reversed(rows))`.

- [ ] **Step 4: Wire validation and rejections**

Alongside the existing per-view rejections, `parser.error` when: the resolved limit is negative; `--sessions` is combined with `--from-unsloth`, `--from-runs`, or the `reconcile` command. Add `and args.sessions is None` to the `compact_dash` conjunction so the flag never falls into the dashboard path.

- [ ] **Step 5: Render the list view**

In the session branch of the dispatch tail, when `args.sessions is not None` and neither `--daily` nor `--total` is set: pass `limit=` to `query_sessions`, order the rows with `most_recent(rows, limit)`, and set the table title to `sessions · N most recent`, or `sessions · all` when the limit is `0`. The hero keeps summarising the rows actually displayed. `--json` needs no special case — it serialises the same row list.

- [ ] **Step 6: Share the recency step with the dashboard**

Replace the dashboard's `list(reversed(all_rows))[:5]` with `most_recent(all_rows, 5)` so the panel and the new view order and truncate through one function. The panel title stays `sessions · 5 most recent`.

- [ ] **Step 7: Run the CLI tests and the full suite**

Run: `python3 -m unittest test_tokenstat.CliSessionsTests -v` then `python3 -W error::ResourceWarning -m unittest test_tokenstat`
Expected: PASS, all green, warning-clean.

- [ ] **Step 8: Commit**

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: --sessions scopes session views to the N most recent"
```

---

### Task 3: Aggregate titles and documentation

**Files:**
- Modify: `tokenstat.py` — the title/hero-unit block in `main` (~line 1545)
- Modify: `AGENTS.md`, `README.md`
- Test: `test_tokenstat.py` — extend `CliSessionsTests`

**Interfaces:**
- Consumes: the resolved limit from Task 2.
- Produces: capped-aggregate table titles; documentation matching shipped behaviour.

- [ ] **Step 1: Write the failing title tests**

Extend `CliSessionsTests`:

- **`--sessions=3 --daily`** renders the title `daily · last 3 sessions`.
- **`--sessions=3 --total`** renders `totals · last 3 sessions`.
- **`--sessions=3 --total --model`** renders a title that contains both `usage by model` and `last 3 sessions`.
- **Uncapped views keep their exact current titles**: `daily`, `totals`, `usage by model · sorted by share`, `sessions`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.CliSessionsTests -v`
Expected: FAIL — titles render without the `· last N sessions` suffix.

- [ ] **Step 3: Append the cap suffix to aggregate titles**

Where the title is chosen, when a positive limit is in force and the view is an aggregate (`--daily` or `--total`), append ` · last N sessions` to the title that view already produces. The list view's title comes from Task 2 and is not touched here; `hero_unit` values do not change.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.CliSessionsTests -v`
Expected: PASS

- [ ] **Step 5: Update `AGENTS.md`**

Add a dated increment paragraph in the existing dense style covering: the flag's semantics table, that the cap is a predicate in `_session_filters` so rows, aggregates, and rate groups share one scope, the view-dependent default (20 listing, all aggregating), and that the bare dashboard is unchanged. Add the spec path to the design-doc list.

- [ ] **Step 6: Update `README.md`**

Document `--sessions` in the usage/options list with the same semantics, and add the command to the examples block.

- [ ] **Step 7: Run the full suite and commit**

Run: `python3 -W error::ResourceWarning -m unittest test_tokenstat`
Expected: all green.

```bash
git add tokenstat.py test_tokenstat.py AGENTS.md README.md
git commit -m "docs: document the --sessions scope flag"
```
