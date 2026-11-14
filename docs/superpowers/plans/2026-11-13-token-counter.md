# Token Counter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI tool that permanently records AI token usage from OpenCode's SQLite database into its own store, and reports it filtered by model, date range, and token thresholds.

**Architecture:** One stdlib-only Python program (`tokenstat.py`). A `record` step upserts per-session usage rows from OpenCode's `session_v2` table (read-only) into our own `usage.db`; every query implicitly records first. Reports are SUMs/rows over distinct sessions keyed by OpenCode's session id, so re-running record never double counts.

**Tech Stack:** Python 3.13 stdlib only (`sqlite3`, `argparse`, `json`, `datetime`); tests via `python3 -m unittest`.

**Spec:** `docs/superpowers/specs/2026-11-13-token-counter-design.md`

## Global Constraints

- Product code: single file `tokenstat.py`, stdlib only — no third-party imports.
- Tests: stdlib `unittest`, run with `python3 -m unittest`.
- OpenCode source DB path constant: `DEFAULT_SRC = os.path.expanduser("~/.local/share/opencode/opencode.db")`; open it read-only (`file:...?mode=ro&immutable=0`).
- Our store: `token_counter/usage.db` (path overridable via `--db` for tests).
- OpenCode timestamps are epoch **milliseconds**; `--since/--until` take local-time calendar dates `YYYY-MM-DD`; the `--until` day is inclusive.
- Token/cost values in our store only ever increase per session (monotonic max); rows are never deleted.
- All work happens in `/home/elih/messinaround/token_counter/`, which becomes its own git repo in Task 1.

## Review Focus

- Source DB missing or locked mid-write during an implicit record → warn on stderr, still print the report from our own store, exit 0 (Task 3).
- Source `model` column NULL or invalid JSON → recorded with empty provider/model strings, no crash (Task 1).
- Recording identical source data repeatedly → zero inserts/updates, totals unchanged — the no-double-counting guarantee (Task 1).
- Source row regressed (rewritten smaller) between runs → stored totals keep the larger values (Task 1).
- Malformed `--since`/`--until` value → clear argparse error, exit code 2 (Task 2).

---

### Task 1: Repo scaffold + schema + recorder

**Files:**
- Create: `tokenstat.py`, `test_tokenstat.py`, `.gitignore` (`usage.db`, `__pycache__/`)
- Git: `git init` in `/home/elih/messinaround/token_counter/` (docs/ already exist there; commit them too)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ensure_schema(dest: sqlite3.Connection) -> None` — creates table `usage(session_id TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', tokens_input INTEGER NOT NULL DEFAULT 0, tokens_output INTEGER NOT NULL DEFAULT 0, tokens_reasoning INTEGER NOT NULL DEFAULT 0, tokens_cache_read INTEGER NOT NULL DEFAULT 0, tokens_cache_write INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, title TEXT, directory TEXT, agent TEXT, time_created INTEGER NOT NULL, time_updated INTEGER, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL)`.
  - `record(src: sqlite3.Connection, dest: sqlite3.Connection, now_ms: int) -> dict` — reads all rows from src table `session_v2` (columns: `id, model, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, cost, title, directory, agent, time_created, time_updated`), upserts one row per session into `usage`. Returns `{"inserted": int, "updated": int, "unchanged": int}` where *unchanged* means no token/cost value changed.
  - `parse_model(raw: str | None) -> tuple[str, str]` — parses the source `model` JSON (`{"id": ..., "providerID": ...}`, may carry other keys like `variant`, which are ignored) into `(provider, model_id)`; returns `("", "")` for NULL/invalid JSON.

- [ ] **Step 1: Init repo and commit existing docs**

```bash
cd /home/elih/messinaround/token_counter && git init -b master
printf 'usage.db\n__pycache__/\n' > .gitignore
git add .gitignore docs && git commit -m "docs: add token counter design spec"
```

- [ ] **Step 2: Write the failing tests** in `test_tokenstat.py`

Build a helper that makes an in-memory fake source DB with the `session_v2` columns listed above and inserts one row (given model JSON, token values, `id`, `time_created`). Tests (each asserts on `record()`'s return dict and/or the stored row via a plain SELECT):
1. `test_record_inserts_new_session` — one src row → `{"inserted": 1, "updated": 0, "unchanged": 0}`; stored row has parsed `provider="unsloth-studio"`, `model_id="unsloth/Qwen3.8-Flash-Next-GGUF"`, and the token values.
2. `test_record_is_idempotent_no_double_counting` — run `record(src, dest, now1)` twice with identical src → second returns `{"inserted": 0, "updated": 0, "unchanged": 1}`; stored `tokens_input` equals the single session's value (not doubled).
3. `test_record_updates_grown_session` — after first record, UPDATE the src row to double its tokens → `record` returns `{"inserted": 0, "updated": 1, "unchanged": 0}`; stored values equal the new larger totals (replace, not add).
4. `test_record_never_shrinks_on_source_regression` — after recording value 100, rewrite src row to 50 → stored value stays 100.
5. `test_record_null_and_malformed_model` — src rows with `model=NULL` and `model='not json'` → recorded with `provider=''`, `model_id=''`, no exception.

- [ ] **Step 3: Run tests, verify they fail** with `python3 -m unittest test_tokenstat -v` (ModuleNotFoundError / AttributeError is the expected failure).

- [ ] **Step 4: Implement** `ensure_schema`, `parse_model`, `record` in `tokenstat.py`.

Approach for `record`: iterate src rows; per session, SELECT existing row from `usage`; if absent → INSERT with all token columns and `first_seen=last_seen=now_ms`; if present → compare each of the six numeric columns using `max(existing, source)`, write back metadata (`title`, `directory`, `agent`, `time_updated`) plus `last_seen=now_ms` always, and classify as `updated` only if any numeric max differs from the stored value, else `unchanged`. Coerce NULL token/cost in source to 0.

- [ ] **Step 5: Run tests, verify all pass.**
- [ ] **Step 6: Commit** `git add tokenstat.py test_tokenstat.py && git commit -m "feat: record OpenCode session usage with monotonic no-double-count upsert"`

---

### Task 2: Query filters + table output

**Files:**
- Modify: `tokenstat.py`, `test_tokenstat.py`

**Interfaces:**
- Consumes: Task 1's `usage` schema.
- Produces:
  - `query_sessions(dest, model=None, since_ms=None, until_ms=None, min_in=None, min_out=None) -> list[dict]` — rows matching ALL given filters: `model` is a case-insensitive substring of `provider || '/' || model_id`; date window `since_ms <= time_created < until_ms + 86_400_000` (until-day inclusive); `min_in`/`min_out` are lower bounds on `tokens_input`/`tokens_output`. Sorted by `time_created` ascending. Returned dicts have keys `date` (`YYYY-MM-DD` local), `model` (`provider/model_id`), `tokens_input`, `tokens_output`, `total` (in+out), `cost`, `title`.
  - `parse_date(s: str) -> int` — `YYYY-MM-DD` → epoch ms at local midnight; raises `argparse.ArgumentTypeError(f"invalid date {s!r}, expected YYYY-MM-DD")` otherwise.
  - `render_table(rows: list[dict]) -> str` — aligned columns `DATE MODEL IN OUT TOTAL COST SESSION` (title truncated to 40 chars), right-aligned numbers, thousands separators on token columns, cost with 4 decimals. Empty input → `"no sessions match"` plus trailing newline.
  - `main(argv: list[str]) -> int` — argparse; flags `--model`, `--since`/`--until` (`type=parse_date`), `--min-in`/`--min-out` (`type=int`), `--db` (default `usage.db` next to the script). For now: open `--db`, ensure schema, run `query_sessions`, print `render_table`. No subcommands yet.

- [ ] **Step 1: Write the failing tests** — seed a dest DB via Task 1's `record` with fake src rows spanning two models and two dates (e.g. unsloth on 2026-11-01, lemonade on 2026-11-05):
  1. `test_filter_by_model_substring` — `model="unsloth"` returns only the unsloth session; case-insensitive (`"UNSLOTH"` same result).
  2. `test_filter_date_window_inclusive_endpoints` — since=first day, until=second day returns both; until=day before second returns only the first.
  3. `test_filter_min_tokens` — `min_out` excludes a low-output session; `min_in` behaves analogously.
  4. `test_parse_date_rejects_bad_value` — `parse_date("13-2026")` raises `argparse.ArgumentTypeError`.
  5. `test_render_table_smoke` — one row renders a header line and a data line containing the model string; `render_table([])` contains `"no sessions match"`.

- [ ] **Step 2: Run, verify new tests fail.**
- [ ] **Step 3: Implement** per the signatures. SQL-side filtering (`WHERE` clauses built from non-None args) is fine; `date` formatting via `datetime.fromtimestamp(ms/1000)`.
- [ ] **Step 4: Run, verify all pass.** Also smoke: `python3 tokenstat.py --since not-a-date; echo $?` → exit code 2.
- [ ] **Step 5: Commit** `-m "feat: query and render usage with model/date/token filters"`

---

### Task 3: Daily totals, JSON output, implicit record + fallback

**Files:**
- Modify: `tokenstat.py`, `test_tokenstat.py`

**Interfaces:**
- Consumes: Task 1's `record`, Task 2's `query_sessions`/`parse_date`.
- Produces:
  - `daily_totals(dest, model=None, since_ms=None, until_ms=None) -> list[dict]` — same filters minus thresholds; groups by local date; dicts `{"date", "sessions", "tokens_input", "tokens_output", "total", "cost"}`, ascending.
  - `main` gains: subcommand-style usage — bare invocation = implicit record + report (existing behavior); `tokenstat.py record` = record only, prints `"inserted=N updated=N unchanged=N"`; new flags `--daily` (render daily totals instead of session rows) and `--json` (emit JSON array of the same dicts either mode produces, instead of a table).
  - Implicit record opens `DEFAULT_SRC` read-only; if the file is missing OR any `sqlite3.Error` occurs, print `warning: could not read <path>: <error>` to stderr and continue with existing data (exit code unchanged).

- [ ] **Step 1: Write the failing tests:**
  1. `test_daily_totals_groups_by_date` — two sessions on one date + one on another → two groups with correct session counts and summed tokens.
  2. `test_implicit_record_fallback_missing_src` — call `main(["--db", tmp, "--model", "x"])` with a nonexistent src path (make the src path injectable via module-level variable or an added optional `--src` flag; prefer `--src` so tests and future use stay honest) → exit 0, warning on stderr (`assertLogs` or captured stderr), report still rendered.
  3. `test_record_subcommand_prints_counts` — `main(["record", "--db", tmp, "--src", fake_src])` prints a line matching `inserted=\d+ updated=\d+ unchanged=\d+`.
  4. `test_json_output_parses` — `--json --daily` output is valid JSON with the daily dicts' keys.

- [ ] **Step 2: Run, verify new tests fail.**
- [ ] **Step 3: Implement.** For implicit record inside query mode, reuse `record()`; `now_ms = int(time.time()*1000)`.
- [ ] **Step 4: Run, verify all pass.**
- [ ] **Step 5: Commit** `-m "feat: daily totals, json output, implicit recording with fallback"`

---

### Task 4: README + real-database smoke test

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: the finished CLI.
- Produces: user documentation; no code changes unless a smoke test finds a bug (then fix + commit).

- [ ] **Step 1: Write `README.md`** — what it does, examples for every flag combination from the spec (`--model unsloth`, `--since/--until`, `--min-in/--min-out`, `--daily`, `--json`, `record`), the no-double-counting guarantee in one plain paragraph, and this retention line verbatim: *"Schedule hourly snapshots so history stays complete even if you never run a query: add to crontab (`crontab -e`): `0 * * * * /usr/bin/python3 $HOME/messinaround/token_counter/tokenstat.py record >> $HOME/messinaround/token_counter/record.log 2>&1`"*.
- [ ] **Step 2: Real smoke test** against the live OpenCode DB:
  - `python3 tokenstat.py record` → nonzero inserts on first run.
  - `python3 tokenstat.py record` again → all unchanged, zero inserted/updated.
  - `python3 tokenstat.py --model unsloth --since 2026-01-01` → non-empty table including the known sessions (source DB has ~22 unsloth-studio sessions as of 2026-11-13).
  - `python3 tokenstat.py --daily --json | python3 -m json.tool` → valid JSON.
  - Cross-check totals: SUM(tokens_input) over unsloth rows in our store equals the source DB's `SUM(tokens_input)` for `model LIKE '%unsloth-studio%'` sessions present at snapshot time.
- [ ] **Step 3: Commit** `git add README.md && git commit -m "docs: usage, examples, and hourly snapshot instructions"`
