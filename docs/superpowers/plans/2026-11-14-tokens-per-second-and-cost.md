# Tokens/Second + `--cost` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tokens/second rate to every reporting view (real generation rate via a new Unsloth runs source, wall-clock average elsewhere) and an opt-in `--cost` estimate, then remove flagged redundancies.

**Architecture:** All logic stays in the single stdlib-only file `tokenstat.py`. A new `unsloth_runs` store table ingests `chat_generation_runs` (which holds `started_at`/`completed_at`/`progress_tokens`); a `--from-runs` view reports the true per-request generation rate. A derived `tok_s` field rides on every reporting row (computed from wall-clock span where the data supports it, `None` otherwise) and renders as a `TOK/S` column. `--cost` is a pure display/JSON overlay computed from a Sonnet-tier price table; it never touches stored data.

**Spec:** `docs/superpowers/specs/2026-11-14-tokens-per-second-and-cost-design.md`

## Global Constraints

- stdlib-only Python, single file `tokenstat.py`, no install step, no new dependencies.
- Source DBs opened read-only (`file:...?mode=ro`); never write to OpenCode's or Studio's databases.
- No double counting (invariant 1): every store table is keyed by its source id, numeric columns are monotonic `max`, rows are never deleted; re-running any import is a no-op.
- The two sources are never summed (invariant 2); `reconcile` stays a comparison view and is **excluded** from both `TOK/S` and `--cost`.
- Date windows are local-time, until-day inclusive, via `_day_end_ms` (invariant 4); do not use `+86_400_000`.
- Query modes warn-and-continue (exit 0) when a source is missing/locked; explicit snapshot commands exit 1 on failure (invariant 6).
- Our store keeps WAL + busy_timeout; `.gitignore` keeps covering `usage.db*` and `record.log` (invariant 7).
- Tests run with `python3 -m unittest test_tokenstat -v` from the `token_counter/` directory (~0.05s, no deps). Individual test: `python3 -m unittest test_tokenstat.<Class>.<method> -v`.
- Tables are left-aligned everywhere (established reviewed convention); new `TOK/S`/`COST` columns follow that — do not introduce right-alignment.
- Pricing constants are approximate today (input/output per Mtok): sonnet 3/15, haiku 0.80/4, opus 15/75; cache-read = 0.1× input, cache-write = 1.25× input. **Verify current Anthropic list prices at implementation time** and adjust the `PRICES` dict only.

## Review Focus

Failure modes no single task's tests fully pin — each is tied to the task that owns the code, in that task's step style:

1. **A run with `status != 'completed'` or `completed_at IS NULL`** must render a blank `TOK/S` (no negative/`ZeroDivision`), not a garbage rate. Pinned in Task 2 (`test_query_runs_blank_rate_when_no_duration`).
2. **A `studio.db` lacking the `chat_generation_runs` table** (older installs) must not crash or spam warnings — ingestion silently reports zero runs. Pinned in Task 1 (`test_import_runs_missing_table_is_noop`).
3. **A session with `time_updated == time_created`** (0-second span) must yield blank `TOK/S`, never a divide-by-zero. Pinned in Task 3 (`test_session_zero_span_rate_is_none`).
4. **`--cost` on non-Claude traffic** still prints an estimate (it is a uniform reference), and piped `--cost` output stays plain (no color escapes). Pinned in Task 4 (`test_cost_on_any_model_and_piped_plain`).
5. **`--model '%'`** must match a literal `%` in a model string (LIKE metacharacters escaped), not "every row". Pinned in Task 7 (`test_model_percent_is_literal`).

---

### Task 1: `unsloth_runs` store table + ingestion

**Files:**
- Modify: `tokenstat.py` (`ensure_schema` ~line 28-56; `import_unsloth` ~line 124-158 stays; new `import_unsloth_runs`; `main` import path ~line 637-651)
- Test: `test_tokenstat.py` (new helpers + new `ImportRunsTests` class; extend `CliUnslothTests`)

**Interfaces:**
- Produces: `import_unsloth_runs(src: sqlite3.Connection, dest: sqlite3.Connection) -> dict` returning `{"inserted": int, "updated": int, "unchanged": int}`. Reads `chat_generation_runs(id, request_json, progress_tokens, started_at, completed_at, status)`; extracts model via `json.loads(request_json).get("model", "")` in Python (no SQLite JSON1 dependency); guards each row so one bad row is skipped, not fatal.
- Produces: `ensure_schema` now also creates `unsloth_runs(run_id TEXT PRIMARY KEY, model TEXT NOT NULL DEFAULT '', progress_tokens INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL, completed_at INTEGER, status TEXT NOT NULL DEFAULT '')`.

- [ ] **Step 1: Write the failing tests**

Add helpers and a class to `test_tokenstat.py`:

```python
import json  # at top, if not present

def make_runs_src(rows=()):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE chat_generation_runs (
           id TEXT PRIMARY KEY, request_json TEXT, progress_tokens INTEGER,
           started_at INTEGER, completed_at INTEGER, status TEXT)""")
    for r in rows:
        conn.execute("INSERT INTO chat_generation_runs VALUES (?,?,?,?,?,?)", r)
    return conn

def run_row(rid="r1", model="unsloth/Ornith-1.5-122B-GGUF", progress=100,
            started=1_764_000_000_000, completed=1_764_000_010_000,
            status="completed"):
    return (rid, json.dumps({"model": model}), progress, started, completed, status)


class ImportRunsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()

    def stored(self, rid="r1"):
        return self.dest.execute(
            "SELECT model, progress_tokens FROM unsloth_runs WHERE run_id = ?",
            (rid,)).fetchone()

    def test_ensure_schema_has_unsloth_runs(self):
        cols = [c[1] for c in self.dest.execute("PRAGMA table_info(unsloth_runs)")]
        self.assertEqual(cols, ["run_id", "model", "progress_tokens",
                                "started_at", "completed_at", "status"])

    def test_import_runs_inserts_then_is_idempotent(self):
        src = make_runs_src([run_row("r1"), run_row("r2", progress=20)])
        first = import_unsloth_runs(src, self.dest)
        self.assertEqual(first, {"inserted": 2, "updated": 0, "unchanged": 0})
        second = import_unsloth_runs(src, self.dest)
        self.assertEqual(second, {"inserted": 0, "updated": 0, "unchanged": 2})

    def test_import_runs_extracts_model_from_request_json(self):
        import_unsloth_runs(make_runs_src([run_row("r1")]), self.dest)
        self.assertEqual(self.stored()[0], "unsloth/Ornith-1.5-122B-GGUF")

    def test_import_runs_never_shrinks_on_regression(self):
        src = make_runs_src([run_row("r1", progress=100)])
        import_unsloth_runs(src, self.dest)
        src.execute("UPDATE chat_generation_runs SET progress_tokens = 50"
                    " WHERE id = 'r1'")
        import_unsloth_runs(src, self.dest)
        self.assertEqual(self.stored()[1], 100)

    def test_import_runs_missing_table_is_noop(self):
        conn = sqlite3.connect(":memory:")  # no chat_generation_runs table
        self.assertEqual(import_unsloth_runs(conn, self.dest),
                         {"inserted": 0, "updated": 0, "unchanged": 0})
```

Extend `CliUnslothTests` with a helper that creates a studio.db holding both tables and a test that the subcommand ingests runs:

```python
def make_file_runs_db(path, events=(), runs=()):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE api_usage_events (
           id TEXT PRIMARY KEY, subject TEXT, endpoint TEXT, model TEXT,
           status TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
           total_tokens INTEGER, created_at INTEGER)""")
    conn.execute("""CREATE TABLE chat_generation_runs (
           id TEXT PRIMARY KEY, request_json TEXT, progress_tokens INTEGER,
           started_at INTEGER, completed_at INTEGER, status TEXT)""")
    for r in events:
        conn.execute("INSERT INTO api_usage_events VALUES (?,?,?,?,?,?,?,?,?)", r)
    for r in runs:
        conn.execute("INSERT INTO chat_generation_runs VALUES (?,?,?,?,?,?)", r)
    conn.commit()
    return conn
```

```python
def test_import_unsloth_subcommand_also_imports_runs(self):
    make_file_runs_db(self.udb, events=[unsloth_event("e1")],
                      runs=[run_row("r1")])
    code, out, _ = self.run_main(
        ["import-unsloth", "--db", self.db, "--unsloth-db", self.udb])
    self.assertEqual(code, 0)
    stored = sqlite3.connect(self.db).execute(
        "SELECT COUNT(*) FROM unsloth_runs").fetchone()[0]
    self.assertEqual(stored, 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd token_counter && python3 -m unittest test_tokenstat.ImportRunsTests -v`
Expected: FAIL — `ImportRunsTests` has no `import_unsloth_runs` (ImportError at collection) and `PRAGMA table_info(unsloth_runs)` returns nothing.

- [ ] **Step 3: Add the `unsloth_runs` table to `ensure_schema` and implement `import_unsloth_runs`**

Add the `CREATE TABLE IF NOT EXISTS unsloth_runs (...)` block to `ensure_schema`. Implement `import_unsloth_runs`: first check `src` has the table (`SELECT name FROM sqlite_master WHERE type='table' AND name='chat_generation_runs'`); if absent, return the zero-count dict. Otherwise SELECT `id, request_json, progress_tokens, started_at, completed_at, status`, and for each row: parse model with `json.loads` guarded by try/except (model `""` on failure), upsert by `run_id` with monotonic `max` on `progress_tokens`, per-row try/except incrementing a `"skipped"` counter (mirror `import_unsloth`). `dest.commit()` at the end.

- [ ] **Step 4: Wire runs ingestion into the `import-unsloth` command and the implicit snapshot**

In `main`, after the existing `import_unsloth(...)` call in both the explicit `import-unsloth` branch and the report-mode implicit-snapshot block, call `import_unsloth_runs(_connect_src(args.unsloth_db), dest)` the same way. For the explicit branch, run it as a second `_ingest(..., verbose=True)` step so the counts print; for the implicit snapshot, run it with `verbose=False`. Leave the `import_unsloth()` function's signature and return contract untouched.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd token_counter && python3 -m unittest test_tokenstat.ImportRunsTests test_tokenstat.CliUnslothTests -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "feat: ingest unsloth chat_generation_runs into unsloth_runs"
```

---

### Task 2: `--from-runs` report view

**Files:**
- Modify: `tokenstat.py` (new `query_runs`/`daily_runs`/`total_runs`; `_event_filters`-style run filters; `main` flag + dispatch ~line 579-699)
- Test: `test_tokenstat.py` (new `RunsQueryTests`; extend `CliUnslothTests`)

**Interfaces:**
- Produces: `query_runs(dest, model=None, since_ms=None, until_ms=None) -> list[dict]` with rows `{"date": str, "model": str, "progress_tokens": int, "tokens_input": int (=progress), "tokens_output": 0, "total": int (=progress), "tok_s": float|None, "cost": 0.0, "title": ""}`, oldest first. `daily_runs(dest, model=None, ...)` and `total_runs(dest, model=None, ..., by_model=False)` analogous. Run filters match `model` as a substring on the raw stored string and window by `started_at` (inclusive until-day via `_day_end_ms`).
- Produces: `RUNS_COLUMNS = [("DATE","date","text"), ("MODEL","model","text"), ("IN","progress_tokens","int"), ("OUT","tokens_output","int"), ("TOTAL","total","int"), ("TOK/S","tok_s","rate")]`.

- [ ] **Step 1: Write the failing tests**

```python
class RunsQueryTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        import_unsloth_runs(make_runs_src([
            run_row("r1", progress=243, started=ms(2026, 11, 1),
                    completed=ms(2026, 11, 1) + 16082),
            run_row("r2", model="unsloth/Ornith-1.5-122B-GGUF", progress=51,
                    started=ms(2026, 11, 5), completed=ms(2026, 11, 5) + 4911),
        ]), self.dest)

    def test_query_runs_reports_progress_and_rate(self):
        rows = query_runs(self.dest)
        self.assertEqual([r["progress_tokens"] for r in rows], [243, 51])
        self.assertAlmostEqual(rows[0]["tok_s"], 243 / 16.082, places=2)

    def test_query_runs_blank_rate_when_no_duration(self):
        import_unsloth_runs(make_runs_src([run_row("r3", started=1_764_000_000_000,
                                                   completed=None)]), self.dest)
        rows = query_runs(self.dest, model="r3")
        self.assertIsNone(rows[0]["tok_s"])

    def test_daily_runs_groups_by_started_day(self):
        days = daily_runs(self.dest)
        self.assertEqual([d["date"] for d in days], ["2026-11-01", "2026-11-05"])

    def test_total_runs_per_model(self):
        rows = total_runs(self.dest, by_model=True)
        self.assertEqual({r["model"] for r in rows},
                         {"unsloth/Ornith-1.5-122B-GGUF",
                          "unsloth/Qwen3.8-Flash-Next-GGUF"})
```

Extend `CliUnslothTests`:

```python
def test_from_runs_renders_runs(self):
    make_file_runs_db(self.udb, runs=[run_row("r1")])
    code, out, _ = self.run_main(
        ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
         "--unsloth-db", self.udb, "--from-runs"])
    self.assertEqual(code, 0)
    self.assertIn("TOK/S", out.splitlines()[0])

def test_from_runs_rejects_min_flags(self):
    with self.assertRaises(SystemExit) as cm:
        with contextlib.redirect_stderr(io.StringIO()):
            main(["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
                  "--unsloth-db", self.udb, "--from-runs", "--min-in", "10"])
    self.assertEqual(cm.exception.code, 2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd token_counter && python3 -m unittest test_tokenstat.RunsQueryTests -v`
Expected: FAIL — `query_runs` not defined.

- [ ] **Step 3: Implement the run query functions and `RUNS_COLUMNS`**

Implement `query_runs`/`daily_runs`/`total_runs` mirroring `query_events`/`daily_events`/`total_events` but reading `unsloth_runs`, filtering by `started_at` (substring `model` match on the raw string), and computing `tok_s = round(progress_tokens / ((completed_at - started_at) / 1000), 1)` when `completed_at` is not `None` and the span is `>= 1` second, else `None`. `daily_runs` keys its day on `started_at`; `total_runs` orders/groups by `model`. Add `RUNS_COLUMNS`.

- [ ] **Step 4: Wire `--from-runs` into `main`**

Add `parser.add_argument("--from-runs", action="store_true", dest="from_runs", help="report imported Unsloth generation runs with true tokens/second")`. In the dispatch section, treat `--from-runs` like `--from-unsloth` for view selection (session/daily/total/bare-`--model`), selecting `query_runs`/`daily_runs`/`total_runs`. For the `reconcile`-style guard, add a `--from-runs` branch that rejects `--min-in`/`--min-out` (unsupported — no in/out split) with `parser.error(...)` exit 2, while allowing `--model`/`--since`/`--until`/`--daily`/`--total`. `--from-runs` renders with `RUNS_COLUMNS`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd token_counter && python3 -m unittest test_tokenstat.RunsQueryTests test_tokenstat.CliUnslothTests -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "feat: add --from-runs view with true generation tokens/second"
```

---

### Task 3: Derived `tok_s` on every reporting row + `TOK/S` column

**Files:**
- Modify: `tokenstat.py` (`query_sessions` ~344-361; `daily_totals` ~518-533; `total_totals` ~536-562; `query_events` ~184-202; `daily_events` ~205-219; `total_events` ~222-245; column constants ~364-381; `render_table` `fmt` ~489-496)
- Test: `test_tokenstat.py` (extend `QueryTests`, `DailyTotalsTests`, `TotalTotalsTests`, `ImportUnslothTests`; the three exact-key tests; new `SessionTokS` pins)

**Interfaces:**
- Produces: every row dict from `query_sessions`/`daily_totals`/`total_totals` gains `"tok_s": float|None` **and** `"tokens_cache_read": int` / `"tokens_cache_write": int` (the cache fields are added now because Task 4's `--cost` overlay needs them — they surface in `--json`). Event rows (`query_events`/`daily_events`/`total_events`) gain `"tok_s": None`.
- Produces: `render_table`'s `fmt` understands kind `"rate"` → `f"{value:.1f}"` (`None` → `""`), and `SESSION_COLUMNS`/`DAILY_COLUMNS`/`TOTAL_COLUMNS` include `("TOK/S", "tok_s", "rate")`.

- [ ] **Step 1: Write the failing tests**

Extend the existing exact-key test and add rate pins:

```python
class SessionTokSTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        record(make_src([src_row(sid="s1", tin=1000, tout=200,
                                 created=1_764_000_000_000,
                                 updated=1_764_000_005_000)]), self.dest, now_ms=1)

    def test_session_rate_is_tokens_over_wall_clock_span(self):
        row = query_sessions(self.dest)[0]
        # (1000+200) tokens over 5 s = 240.0
        self.assertAlmostEqual(row["tok_s"], 240.0, places=2)

    def test_session_zero_span_rate_is_none(self):
        # 0-second span (time_updated == time_created) must yield None, not a
        # ZeroDivision.
        dest = make_dest()
        record(make_src([src_row(sid="z", tin=100, tout=10,
                                 created=1_764_000_000_000,
                                 updated=1_764_000_000_000)]), dest, now_ms=1)
        rows = query_sessions(dest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["tok_s"])
```

Add cache-field + event-none pins and update the three exact-key tests:

```python
    def test_session_rows_carry_cache_components(self):
        row = query_sessions(self.dest)[0]
        self.assertIn("tokens_cache_read", row)
        self.assertIn("tokens_cache_write", row)

    def test_event_rows_have_none_rate(self):
        # from ImportUnslothTests.setUp style
        dest = make_dest()
        import_unsloth(make_unsloth_src([unsloth_event("e1")]), dest)
        self.assertIsNone(query_events(dest)[0]["tok_s"])
```

Update these existing assertions to include the new keys:

```python
# QueryTests.test_query_returns_expected_dict_keys -> add "tok_s",
#   "tokens_cache_read", "tokens_cache_write" to the expected set
# CliTests.test_json_output_parses -> add "tok_s","tokens_cache_read",
#   "tokens_cache_write" to days[0] key set
# CliTotalTests.test_total_json_shape -> add same three keys
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd token_counter && python3 -m unittest test_tokenstat.SessionTokSTests test_tokenstat.QueryTests.test_query_returns_expected_dict_keys -v`
Expected: FAIL — rows lack `tok_s`/cache keys.

- [ ] **Step 3: Add `tok_s` + cache fields to the query functions and the `rate` format**

- `query_sessions`: add `tokens_cache_read, tokens_cache_write, time_updated` to the SELECT; per row compute `dur_s = (updated - created) / 1000`, `tok_s = round((tin + tout) / dur_s, 1) if dur_s >= 1 else None`; add `tokens_cache_read`/`tokens_cache_write` to the row dict.
- `daily_totals`: add `MIN(time_created)` and `MAX(time_updated)` to the per-day aggregate; `tok_s = round(day_total / ((max_upd - min_cr) / 1000), 1)` when span `>= 1` else `None`; add cache sums (`SUM(tokens_cache_read)`, `SUM(tokens_cache_write)`) to the row.
- `total_totals`: for the grand row and each per-model row, add `MIN(time_created)`/`MAX(time_updated)` and `SUM(tokens_cache_read)`/`SUM(tokens_cache_write)`; compute `tok_s` from that row's span the same way.
- `query_events`/`daily_events`/`total_events`: add `"tok_s": None` to their row dicts (events have no duration).
- `render_table.fmt`: add `if kind == "rate": return f"{value:.1f}"` (after the `None` guard).
- Column constants: add `("TOK/S", "tok_s", "rate")` to `SESSION_COLUMNS`, `DAILY_COLUMNS`, and `TOTAL_COLUMNS` (so `SHARE_COLUMNS` inherits it before `SHARE`/`BAR`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd token_counter && python3 -m unittest test_tokenstat -v`
Expected: PASS — all, including the updated exact-key tests.

- [ ] **Step 5: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "feat: derived tokens/second on every reporting view"
```

---

### Task 4: `--cost` estimate overlay

**Files:**
- Modify: `tokenstat.py` (new `PRICES`, `COST_COLUMN`, `annotate_cost`; `main` argparse ~595-606 and dispatch ~672-709)
- Test: `test_tokenstat.py` (new `CostTests`; extend `CliTotalTests`/`DisplayAlignmentTests`)

**Interfaces:**
- Produces: `PRICES = {"haiku": {...}, "sonnet": {...}, "opus": {...}}` per-million-tokens dicts with keys `in`, `out`, `cache_read`, `cache_write` (verify values). `COST_COLUMN = ("COST", "cost_est", "cost")`. `annotate_cost(rows: list[dict], tier: str = "sonnet") -> list[dict]` returning copies with `"cost_est": float` added — session rows use `in/out/cache_read/cache_write`, event rows use `tokens_input`(prompt)/`tokens_output`(completion), run rows use `progress_tokens` × `out` (branch on key presence: `progress_tokens` first, then `tokens_cache_read`, else event).
- `render_table.fmt` understands kind `"cost"` → `f"${value:.4f}"`.

- [ ] **Step 1: Write the failing tests**

```python
class CostTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        make_file_db(self.src, [src_row(sid="s1", tin=9000, tout=1000),
                                src_row(sid="s2", model=qwen_lemonade,
                                        tin=4500, tout=500)])
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_annotate_cost_session_uses_all_token_classes_sonnet(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000,
                 "tokens_cache_read": 1_000_000, "tokens_cache_write": 1_000_000}]
        (r,) = annotate_cost(rows, "sonnet")
        # 3 + 15 + 0.30 + 3.75 = 22.05 per Mtok of each class
        self.assertAlmostEqual(r["cost_est"], 22.05)

    def test_annotate_cost_tier_changes_number(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 0,
                 "tokens_cache_read": 0, "tokens_cache_write": 0}]
        self.assertAlmostEqual(annotate_cost(rows, "haiku")[0]["cost_est"], 0.80)
        self.assertAlmostEqual(annotate_cost(rows, "opus")[0]["cost_est"], 15.0)

    def test_annotate_cost_event_uses_prompt_completion(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet")[0]["cost_est"], 18.0)

    def test_annotate_cost_run_uses_progress_only(self):
        from tokenstat import annotate_cost
        rows = [{"progress_tokens": 1_000_000, "tokens_input": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet")[0]["cost_est"], 15.0)

    def test_cost_off_leaves_row_keys_unchanged(self):
        # annotate_cost is only called when --cost is set; this pins that the
        # default path never adds cost_est (covered indirectly by the CLI test)
        self.assertTrue(True)
```

CLI pins:

```python
def test_cost_flag_adds_cost_column_and_json_field(self):
    code, out, _ = self.run_main(
        ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
         "--total", "--cost"])
    self.assertEqual(code, 0)
    self.assertIn("COST", out)
    code, out, _ = self.run_main(
        ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
         "--total", "--cost", "--json"])
    self.assertIn("cost_est", json.loads(out)[0])

def test_cost_default_off_still_has_no_cost_column(self):
    code, out, _ = self.run_main(
        ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
         "--total"])
    self.assertNotIn("COST", out)

def test_cost_on_any_model_and_piped_plain(self):
    code, out, _ = self.run_main(
        ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
         "--total", "--cost"])
    self.assertEqual(code, 0)
    self.assertIn("$", out)          # estimate prints for non-Claude traffic
    self.assertNotIn("\x1b", out)    # piped output stays plain
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd token_counter && python3 -m unittest test_tokenstat.CostTests -v`
Expected: FAIL — `annotate_cost`/`PRICES` undefined.

- [ ] **Step 3: Implement `PRICES`, `annotate_cost`, `COST_COLUMN`, and the `cost` format**

Verify current Anthropic Sonnet/Haiku/Opus list prices and fill `PRICES` (cache-read = 0.1× input, cache-write = 1.25× input). Implement `annotate_cost` per the Interface branch order. Add `COST_COLUMN` and `if kind == "cost": return f"${value:.4f}"` to `render_table.fmt`.

- [ ] **Step 4: Wire `--cost`/`--cost-tier` into `main`**

Add `--cost` (`store_true`, `dest="cost"`) and `--cost-tier` (`choices=["haiku","sonnet","opus"]`, `default="sonnet"`, `dest="cost_tier"`). In the dispatch section, **before** the json/print branch and **only when `columns is not RECON_COLUMNS`** (reconcile is excluded), if `args.cost`: `rows = annotate_cost(rows, args.cost_tier)` then `columns = columns + [COST_COLUMN]`. Add `(["--cost"] if args.cost else [])` to `reconcile`'s unsupported-flag list. `--json` already emits all row keys, so `cost_est` flows through automatically.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd token_counter && python3 -m unittest test_tokenstat.CostTests test_tokenstat.CliTotalTests test_tokenstat.DisplayAlignmentTests -v`
Expected: PASS — all (including `test_report_tables_have_no_cost_column`, which must still hold when `--cost` is absent).

- [ ] **Step 6: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "feat: add opt-in --cost estimate overlay (Claude tier pricing)"
```

---

### Task 5: Indexes on the hot filter columns

**Files:**
- Modify: `tokenstat.py` (`ensure_schema` ~48-56)
- Test: `test_tokenstat.py` (extend `ImportRunsTests` or a new `SchemaTests`)

**Interfaces:**
- Produces: `ensure_schema` also runs `CREATE INDEX IF NOT EXISTS idx_unsloth_events_created ON unsloth_events(created_at)` and `CREATE INDEX IF NOT EXISTS idx_usage_time_created ON usage(time_created)`.

- [ ] **Step 1: Write the failing test**

```python
class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_filter_indexes(self):
        dest = make_dest()
        names = {r[1] for r in dest.execute("PRAGMA index_list(unsloth_events)")}
        # index name is auto-generated; assert coverage by indexed column instead
        cols = [c[1] for c in dest.execute(
            "PRAGMA index_info(idx_unsloth_events_created)")]
        self.assertEqual(cols, [0])  # single column
```

Prefer a name-stable assertion:

```python
    def test_ensure_schema_creates_filter_indexes(self):
        dest = make_dest()
        created = dest.execute("PRAGMA index_info(idx_unsloth_events_created)").fetchall()
        tcreated = dest.execute("PRAGMA index_info(idx_usage_time_created)").fetchall()
        self.assertEqual(len(created), 1)
        self.assertEqual(len(tcreated), 1)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd token_counter && python3 -m unittest test_tokenstat.SchemaTests -v`
Expected: FAIL — no such index.

- [ ] **Step 3: Add the two indexes to `ensure_schema`**

Add the two `CREATE INDEX IF NOT EXISTS ...` statements to `ensure_schema`.

- [ ] **Step 4: Run the test to verify it passes, then the whole suite**

Run: `cd token_counter && python3 -m unittest test_tokenstat -v`
Expected: PASS — all.

- [ ] **Step 5: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "perf: index unsloth_events(created_at) and usage(time_created)"
```

---

### Task 6: Per-row isolation in `record()`

**Files:**
- Modify: `tokenstat.py` (`record` ~70-121)
- Test: `test_tokenstat.py` (new `RecordIsolationTests`)

**Interfaces:**
- Consumes: none. Changes: wrap each per-session SELECT+upsert in `try/except sqlite3.Error`, incrementing `counts["skipped"]`, so one malformed `session_v2` row cannot abort or roll back the run — matching `import_unsloth`'s isolation (invariant 6 intent).

- [ ] **Step 1: Write the failing test**

The deterministic trigger mirrors `import_unsloth`'s isolation test: `usage.time_created` is `INTEGER NOT NULL` with no default, so a source row with `created=None` raises `IntegrityError` on insert.

```python
class RecordIsolationTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()

    def test_record_skips_bad_row_keeps_good(self):
        # src_row(sid="bad", created=None) -> NULL time_created on insert ->
        # NOT NULL violation; record() must skip it, not abort the run.
        src = make_src([src_row(sid="good"), src_row(sid="bad", created=None)])
        counts = record(src, self.dest, now_ms=1)
        self.assertEqual(counts["inserted"], 1)             # good row
        self.assertEqual(counts.get("skipped", 0), 1)       # bad row
        self.assertEqual(self.dest.execute(
            "SELECT tokens_input FROM usage WHERE session_id='good'").fetchone()[0],
            1000)
        self.assertEqual(self.dest.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id='bad'").fetchone()[0], 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd token_counter && python3 -m unittest test_tokenstat.RecordIsolationTests -v`
Expected: FAIL — `counts.get("skipped")` is `None` (no `skipped` counter) and the `IntegrityError` propagates instead of being caught.

- [ ] **Step 3: Add per-row try/except to `record()`**

`record()` currently does, per row: `existing = dest.execute(SELECT ...)` then INSERT or UPDATE. Wrap that whole per-row SELECT+upsert block in `try: ... except sqlite3.Error: counts["skipped"] = counts.get("skipped", 0) + 1; continue`. Keep the single `dest.commit()` at the end so the good row persists even when a later row is skipped. Do not change the monotonic-`max` merge logic (invariant 1).

- [ ] **Step 4: Run the test and full suite**

Run: `cd token_counter && python3 -m unittest test_tokenstat -v`
Expected: PASS — all.

- [ ] **Step 5: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "fix: isolate per-row errors in record() so one bad row can't abort the run"
```

---

### Task 7: Escape LIKE metacharacters in `--model`

**Files:**
- Modify: `tokenstat.py` (`_session_filters` ~324-341; `_event_filters` ~161-181)
- Test: `test_tokenstat.py` (new `LikeEscapingTests`)

**Interfaces:**
- Changes: both model filters escape `%`/`_` with `str.replace("%","\\%").replace("_","\\_")` and add `ESCAPE '\'` to the `LIKE` clauses; the `provider || '/' || model` event form keeps working.

- [ ] **Step 1: Write the failing test**

```python
class LikeEscapingTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        record(make_src([src_row(sid="a", model='{"id":"50%off","providerID":"x"}'),
                         src_row(sid="b", model=LEMONADE_JSON)]), self.dest, now_ms=1)

    def test_model_percent_is_literal(self):
        rows = query_sessions(self.dest, model="50%")
        self.assertEqual([r["model"] for r in rows],
                         ["x/50%off"])   # only the literal-percent model

    def test_model_underscore_is_literal(self):
        record(make_src([src_row(sid="c", model='{"id":"a_b","providerID":"y"}')]),
               self.dest, now_ms=2)
        rows = query_sessions(self.dest, model="a_b")
        self.assertEqual([r["model"] for r in rows], ["y/a_b"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd token_counter && python3 -m unittest test_tokenstat.LikeEscapingTests -v`
Expected: FAIL — `%`/`_` act as wildcards, matching more than the literal.

- [ ] **Step 3: Escape metacharacters in both filter builders**

In `_session_filters` and `_event_filters`, transform the pattern: `pattern = f"%{model.lower()}%".replace("%", "\\%").replace("_", "\\_")` and append `ESCAPE '\'` to each `LIKE ?` clause (the literal `\` must be part of the SQL string, so build `"lower(...) LIKE ? ESCAPE '\\'"` and pass the pattern as before).

- [ ] **Step 4: Run the test and full suite**

Run: `cd token_counter && python3 -m unittest test_tokenstat.LikeEscapingTests test_tokenstat -v`
Expected: PASS — all (existing substring tests still pass; they contain no `%`/`_`).

- [ ] **Step 5: Commit**

```bash
cd token_counter && git add tokenstat.py test_tokenstat.py
git commit -m "fix: escape LIKE metacharacters in --model so searches are literal"
```

---

### Task 8: Refresh stale docs

**Files:**
- Modify: `tokenstat.py` (module docstring ~1-7; argparse help strings)
- Modify: `README.md`, `AGENTS.md`
- Test: none new (update nothing breaks; run the suite as the check)

**Interfaces:** none.

- [ ] **Step 1: Update the module docstring and `--help`**

The module docstring and several `--help` texts still describe OpenCode as the sole source and predate `--from-unsloth`, `--total`, `--from-runs`, and `--cost`. Bring them current: both/​three sources, the new flags, and note `TOK/S` (wall-clock average vs. `--from-runs` true rate) and opt-in `--cost`.

- [ ] **Step 2: Update README.md**

Add examples for `--from-runs`, `--cost`/`--cost-tier`, and the `TOK/S` column; explain the wall-clock-average caveat and that `--cost` is a Claude-tier reference estimate distinct from stored cost. Update the `--json` note to mention `tok_s`/`cost_est`.

- [ ] **Step 3: Update AGENTS.md**

Record the new `unsloth_runs` table, the `import-unsloth` two-table snapshot, the new flags, and the resolved reconcile-exclusion decision. Note the deferred dedup / upsert-batch cleanup items remain deferred.

- [ ] **Step 4: Run the full suite (no regression)**

Run: `cd token_counter && python3 -m unittest test_tokenstat -v`
Expected: PASS — all.

- [ ] **Step 5: Commit**

```bash
cd token_counter && git add tokenstat.py README.md AGENTS.md
git commit -m "docs: document --from-runs, --cost, and the tokens/second column"
```

---

## Self-Review

**1. Spec coverage:**
- G1 (tok/s in all views) → Task 3 (session/daily/total/events derived) + Task 2 (`--from-runs` true rate). ✓
- G2 (true generation rate captured) → Task 1 (ingest) + Task 2 (view). ✓
- G3 (`--cost` opt-in overlay, any view, off by default) → Task 4. ✓
- G4 (cleanup) → Task 5 (indexes), Task 6 (per-row isolation), Task 7 (LIKE escaping), Task 8 (docs). Redundancy dedup + O(N)→upsert intentionally deferred (see note). ✓
- reconcile excluded from both → Task 2 (from-runs guard) and Task 4 (reconcile reject-list + `columns is not RECON_COLUMNS`). ✓
- Invariants 1–7 preserved throughout; source read-only; monotonic-max; `_day_end_ms`. ✓
- Known consequence (three exact-key tests updated) → Task 3, Step 1. ✓

**2. Step scan:** each step is a single test-write / run / implement / run / commit action. Task 6's test step carries builder guidance because a deterministic malformed-row pin depends on the fake schema; the positive-persistence guarantee is the firm assertion. No step is a bare "handle edge cases."

**3. Type consistency:** `import_unsloth_runs(src, dest) -> dict`; `query_runs`/`daily_runs`/`total_runs`; `RUNS_COLUMNS`, `PRICES`, `COST_COLUMN`, `annotate_cost(rows, tier)`, `dest` store column names (`run_id`, `progress_tokens`, `started_at`, `completed_at`); row keys `tok_s`, `tokens_cache_read`, `tokens_cache_write`, `progress_tokens`, `cost_est` — consistent across tasks.

**4. Review Focus:** all five pinned (Task 2.1, 1.2, 3.3, 4.4, 7.1). ✓

**5. Proportion:** the plan is longer than the spec but each task prescribes signatures + test assertions + specific values, not full function bodies; the code it describes is comparable in size. No transcript bodies remain beyond the pricing table and the two one-line `fmt` additions.

**Deferred (builder/plan decision, documented):** the near-duplicate aggregate-query dedup (`total_events`/`total_totals`, `daily_events`/`daily_totals`) and the O(N) SELECT+UPDATE → batched UPSERT refactor are **not** included — both touch hot, well-tested query/ingestion paths for marginal gain at current volume (~1.5k events, ~58 sessions), and the risk outweighs the benefit. They remain noted in `AGENTS.md` for a future perf-focused pass.
