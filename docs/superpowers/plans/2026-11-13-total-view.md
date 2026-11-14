# `--total` all-time aggregate view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--total` report mode to `tokenstat.py`: one grand-total row across all time, or one row per matching model when combined with `--model`.

**Architecture:** Two new query functions mirroring the existing `daily_totals` / `daily_events` pair (one per data source), sharing filter-building helpers. A single CLI branch renders them with a new `TOTAL_COLUMNS` column set. No schema or ingestion changes.

**Tech Stack:** stdlib-only Python (`sqlite3`, `argparse`), unittest suite in `test_tokenstat.py`.

**Spec:** `docs/superpowers/specs/2026-11-13-total-view-design.md`

## Global Constraints

- Stdlib only; single-file tool `tokenstat.py`; no install step.
- Run everything from `/home/elih/messinaround/token_counter`.
- Never write to source DBs (this feature touches our store read-only anyway).
- Existing modes' behavior and the 41 current tests must stay green at every commit.
- Date windows keep `_day_end_ms` semantics (until-day inclusive, DST-safe).

## Review Focus

- Empty result set: `--total` on an empty store (or nothing matching filters) returns no rows → table path prints "no sessions match", JSON path prints `[]`, exit code 0. Pin in Task 1.
- Flag conflicts: `--total --daily` and `reconcile --total` must exit 2 with a clear message, not silently pick one. Pin in Task 3.
- Same `model_id` under two providers stays two rows in per-model totals (grouping key includes provider). Pin in Task 1.
- Thresholds (`--min-in/--min-out`) pre-filter individual sessions/events before summing — they never apply to the aggregate itself. Pin in Task 1 and Task 2.
- Model filter matches the provider-prefixed string shown in reports (e.g. `unsloth-studio`) in both paths. Pin in Tasks 1–2.

---

### Task 1: Session-mode totals (`_session_filters` + `total_totals`)

**Files:**
- Modify: `tokenstat.py` (add `_session_filters` near `_event_filters` ~line 159; add `total_totals` after `daily_totals`; refactor `query_sessions` and `daily_totals` to use the new helper)
- Test: `test_tokenstat.py` (new class `TotalTotalsTests`; import `total_totals`)

**Interfaces:**
- Consumes: existing `usage` table schema, `_day_end_ms`.
- Produces:
  - `_session_filters(model, since_ms, until_ms, min_in, min_out) -> (where: list[str], params: list)` — same WHERE fragments as today's inline code in `query_sessions`/`daily_totals` (`lower(provider || '/' || model_id) LIKE ?`, `time_created >= ?`, `time_created < _day_end_ms(until_ms)`, `tokens_input >= ?`, `tokens_output >= ?`).
  - `total_totals(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None) -> list[dict]` — dicts with keys `model, sessions, tokens_input, tokens_output, total, cost`. No `model` filter → exactly one row with `model == "(all)"` (or `[]` when zero matching rows). With filter → one row per distinct `provider || '/' || model_id`, ordered by that string ASC. Cost is `SUM(cost)` (float, 0.0 for NULL).

- [ ] **Step 1: Write failing tests** — add to `test_tokenstat.py`:

```python
class TotalTotalsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        record(make_src([
            src_row(sid="s1", tin=1000, tout=200, cost=0.01),          # unsloth-studio
            src_row(sid="s2", model=LEMONADE_JSON, tin=500, tout=50,
                    cost=0.02),                                        # lemonade
        ]), self.dest, now_ms=1)

    def test_total_totals_single_grand_row(self):
        rows = total_totals(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1500, 250, 1750))
        self.assertAlmostEqual(r["cost"], 0.03)

    def test_total_totals_per_model_when_filtered(self):
        record(make_src([src_row(sid="s3", model=LEMONADE_JSON, tin=10, tout=1,
                                 cost=0.0)]), self.dest, now_ms=2)
        rows = total_totals(self.dest, model="lemonade")
        self.assertEqual([r["model"] for r in rows], ["lemonade/gpt-oss-120b"])
        self.assertEqual((rows[0]["sessions"], rows[0]["tokens_input"]), (2, 510))

    def test_total_totals_keeps_same_model_id_separate_per_provider(self):
        a = '{"id":"m1","providerID":"prov-a"}'
        b = '{"id":"m1","providerID":"prov-b"}'
        record(make_src([src_row(sid="p1", model=a), src_row(sid="p2", model=b)]),
               self.dest, now_ms=2)
        rows = total_totals(self.dest, model="m1")
        self.assertEqual(sorted(r["model"] for r in rows), ["prov-a/m1", "prov-b/m1"])

    def test_total_totals_filters_apply_before_summing(self):
        record(make_src([src_row(sid="d1", created=ms(2026, 11, 1), tin=9000),
                         src_row(sid="d2", created=ms(2026, 11, 5), tin=7)]),
               self.dest, now_ms=2)
        r = total_totals(self.dest, since_ms=ms(2026, 11, 1),
                         until_ms=ms(2026, 11, 1))[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (1, 9000))
        # threshold pre-filters individual sessions, not the aggregate:
        # s1 has tin>=1000; s2/d2 excluded by both date and threshold
        r = total_totals(self.dest, min_in=1000)[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (2, 1000 + 9000))

    def test_total_totals_empty_result(self):
        self.assertEqual(total_totals(self.dest, model="nope"), [])
        empty = make_dest()
        self.assertEqual(total_totals(empty), [])
```

Add `total_totals` to the `from tokenstat import (...)` line.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.TotalTotalsTests -v` → ImportError/fails.

- [ ] **Step 3: Implement** in `tokenstat.py`: extract `_session_filters` exactly as specified in Interfaces; replace the inline WHERE-building in `query_sessions` and `daily_totals` with calls to it (pure refactor — no behavior change); implement `total_totals` as two SQL shapes sharing one WHERE clause: no-model → `SELECT COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost) FROM usage <where>` returning `[]` when the count is 0, else the single `(all)` row; with model → same aggregates plus `provider || '/' || model_id AS m`, `GROUP BY m ORDER BY m ASC`.

- [ ] **Step 4: Run full suite** — `python3 -m unittest test_tokenstat -v` → all green (41 existing + 5 new).

- [ ] **Step 5: Commit** — `git add tokenstat.py test_tokenstat.py && git commit -m "feat: session-mode all-time totals with per-model breakdown"`

### Task 2: Event-mode totals (`total_events`)

**Files:**
- Modify: `tokenstat.py` (add `total_events` after `daily_events`)
- Test: `test_tokenstat.py` (new class `TotalEventsTests`; import `total_events`)

**Interfaces:**
- Consumes: `_event_filters` (existing), `UNSLOTH_PROVIDER`.
- Produces: `total_events(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None) -> list[dict]` — same dict shape as `total_totals`; grouping key is the event's own `model` string; no-model → single `(all)` row (`[]` when zero rows); IN = `SUM(prompt_tokens)`, OUT = `SUM(completion_tokens)`, `cost` always 0.0 (mirrors `daily_events`).

- [ ] **Step 1: Write failing tests** — add to `test_tokenstat.py`:

```python
class TotalEventsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        import_unsloth(make_unsloth_src([
            unsloth_event("e1", prompt=900, completion=30),
            unsloth_event("e2", model="other-model", prompt=100, completion=5),
        ]), self.dest)

    def test_total_events_single_grand_row(self):
        rows = total_events(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1000, 35, 1035))
        self.assertEqual(r["cost"], 0.0)

    def test_total_events_per_model_when_filtered(self):
        import_unsloth(make_unsloth_src([unsloth_event("e3", prompt=50)]),
                       self.dest)  # same model as e1
        rows = total_events(self.dest, model="qwen")
        self.assertEqual([r["model"] for r in rows], [UNSLOTH_MODEL_ID])
        self.assertEqual((rows[0]["sessions"], rows[0]["tokens_input"]), (2, 950))

    def test_total_events_provider_prefixed_filter(self):
        self.assertEqual(len(total_events(self.dest, model="unsloth-studio")), 1)

    def test_total_events_thresholds_and_empty(self):
        r = total_events(self.dest, min_in=500)[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (1, 900))
        self.assertEqual(total_events(self.dest, model="nope"), [])
```

Add `total_events` to the import line.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.TotalEventsTests -v`.

- [ ] **Step 3: Implement** `total_events` mirroring Task 1's `total_totals` shape, using `_event_filters` for the WHERE clause.

- [ ] **Step 4: Run full suite** — all green (46 + 4 new).

- [ ] **Step 5: Commit** — `git commit -am "feat: unsloth-event-mode all-time totals with per-model breakdown"`

### Task 3: CLI wiring, guards, docs

**Files:**
- Modify: `tokenstat.py` (argparse `--total`, `TOTAL_COLUMNS`, dispatch branch, reconcile guard extension)
- Modify: `README.md` (Examples section), `AGENTS.md` (Commands list)
- Test: `test_tokenstat.py` (extend the existing CLI test class at ~line 453 with a `CliTotalTests` class reusing its temp-store `run_main` pattern)

**Interfaces:**
- Consumes: `total_totals`, `total_events` from Tasks 1–2.
- Produces: `TOTAL_COLUMNS = [("MODEL", "model", "text"), ("SESSIONS", "sessions", "int"), ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"), ("TOTAL", "total", "int"), ("COST", "cost", "cost")]`; CLI flag `--total` (`action="store_true"`), help text: "grand total (with --model: per-model totals) instead of per-session rows".

- [ ] **Step 1: Write failing tests** — add `CliTotalTests` (copy the setUp/tearDown/run_main pattern from `CliUnslothTests`, seeding via `--src`/`--unsloth-db` file DBs like `test_from_unsloth_query_renders_events` does):

```python
def test_total_flag_renders_grand_row(self): ...
    # seed one unsloth event file db, run ["--db", ..., "--src", <missing>,
    #   "--unsloth-db", ..., "--total"] → exit 0, "(all)" and "TOTAL" in out

def test_total_flag_per_model_rows(self): ...
    # same but with "--model", "other-model" → both model names appear, no "(all)"

def test_total_json_shape(self): ...
    # "--total --json" → json.loads(out) is a list of dicts with the six keys

def test_total_rejects_daily(self): ...
    # main(["--db", self.db, "--src", <missing>, "--unsloth-db", <missing>,
    #       "--total", "--daily"]) raises SystemExit code 2

def test_reconcile_rejects_total(self): ...
    # main(["reconcile", "--db", ..., "--total"]) raises SystemExit code 2
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** in `main()`: add the `--total` argument; append `"--total"` to reconcile's existing unsupported list (guard at ~line 501); add a separate guard immediately after the reconcile block, before any report runs: if `args.total and args.daily`, call `parser.error("--total cannot be combined with --daily")`; add the dispatch branch inside the report-mode chain (after the reconcile branch, ahead of the existing `elif args.from_unsloth:`):

```python
elif args.total:
    rows = (total_events(...) if args.from_unsloth else total_totals(...))
    columns = TOTAL_COLUMNS
```

passing `model=args.model, since_ms=args.since, until_ms=args.until, min_in=args.min_in, min_out=args.min_out` like the neighboring branches.

- [ ] **Step 4: Run full suite** — all green (50 + 5 new).
- [ ] **Step 5: Docs.** README Examples: add `python3 tokenstat.py --total` and `python3 tokenstat.py --total --model unsloth` with a one-line comment each. AGENTS.md Commands block: add the same two lines.
- [ ] **Step 6: Manual smoke test** — `python3 tokenstat.py --total` and `python3 tokenstat.py --total --model unsloth` against the real store; sanity-check the grand total is ≥ the sum of any earlier report and that per-model rows look right.
- [ ] **Step 7: Commit** — `git commit -am "feat: --total CLI flag with per-model breakdown; docs"`
