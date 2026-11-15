# Design: tokens/second stat + `--cost` estimate + cleanup pass

Date: 2026-11-14
Status: draft — awaiting human approval

## Intent / why

Two reporting features for `tokenstat`, then a tidy-up pass:

1. A **tokens/second** rate visible in every report view, so throughput is
   legible at a glance.
2. An optional **`--cost`** flag that overlays an estimated dollar cost based
   on Claude Sonnet pricing. Off by default; works with any existing view.
3. A **cleanup / performance pass** over the codebase afterward.

The human partner chose the **hybrid** approach for tokens/second (below) and
left cleanup scope to the plan.

## Goals

- **G1** Every view that prints token totals also prints a per-row tokens/second
  figure, computed honestly from whatever duration that view's data supports.
- **G2** The true per-request *generation* rate (output tokens over generation
  time) is captured from the one source that records it, and surfaced.
- **G3** `--cost` adds a Sonnet-tier cost estimate to any view, only when the
  flag is passed; default display and `--json` are unchanged.
- **G4** Remove clear redundancies and fix the low-risk performance/correctness
  items already flagged in `AGENTS.md`; deeper refactors only if the plan finds
  them safe and worth it.

## Non-goals

- Not changing the two-source invariants (no double counting; never sum the
  sources; sources stay read-only; our store stays monotonic-max, rows never
  deleted).
- Not importing the rest of `studio.db` (chat threads, research, library, etc.)
  — only the run-timing table needed for the real rate.
- Not making cost "accurate." Most tracked models are not Claude; this is a
  uniform *reference* estimate, explicitly distinct from the stored `cost`
  column (which comes from OpenCode and is real for that traffic).

## Anti-goals

- Do not present the idle-inflated session wall-clock rate as "generation
  speed" without labeling it as a wall-clock average.
- Do not re-add a COST column to the default (flag-less) display.
- Do not let a new ingestion path weaken the no-double-count guarantee.

## Background: what duration data actually exists

Verified live against the source DBs on 2026-11-14.

- **OpenCode `session_v2`** exposes `time_created` and `time_updated` — a
  **wall-clock session span**. It is idle-inflated and can stretch across
  multi-day resume gaps (observed spans from ~5 s to ~5 days). Its
  `time_busy` / `time_idle` columns are **last-state-change timestamps, not
  durations** (observed `time_busy ≈ time_created`, `time_idle ≈ time_updated`,
  with deltas ranging wildly), so they cannot yield an active-generation rate.
  → Only a wall-clock-span rate is derivable here.
- **Unsloth `api_usage_events`** (already ingested into our `unsloth_events`)
  has `prompt_tokens`, `completion_tokens`, `total_tokens`, `created_at`. **No
  duration per event.** → A per-event generation rate is not derivable.
- **Unsloth `chat_generation_runs`** (NOT currently ingested) has
  `started_at`, `completed_at`, `progress_tokens`, `status`, and
  `request_json` (which contains the model string). This is the **only** source
  of a genuine per-request generation rate: `progress_tokens /
  ((completed_at − started_at) / 1000)` reads ~10–24 tok/s in the live data.
  Caveat: only **5 runs** exist today, and they are Unsloth Studio's own
  web-chat sessions — a population largely **disjoint** from the 1,576
  `api_usage_events`. So the true rate covers a small sliver of tracked traffic.

## Approaches considered (tokens/second)

- **A — Ingest the real rate only.** Add `chat_generation_runs` ingestion and
  show true tok/s in a dedicated view; OpenCode views fall back to a labeled
  span rate or blank. Real number, but nothing in the everyday session/daily/
  total views unless separately derived.
- **B — Derive a span rate everywhere.** `tok/s = tokens / row-span` for every
  view; no new ingestion. Works in all views today, but it is a wall-clock
  average (lower than generation speed) and events have no duration.
- **C — Hybrid (chosen).** Do both: ingest `chat_generation_runs` so the true
  generation rate is captured and shown, **and** derive a labeled wall-clock
  span rate for the OpenCode session/daily/total views so every standard view
  prints a number. Most complete and honest; costs one new ingestion path plus
  a derived fallback.

`--cost` had one sensible design (Sonnet tier, opt-in overlay) and one decision
(the tier), which the human settled as **Sonnet**.

## The design

### Tokens/second — derived wall-clock rate (all standard views)

Add a derived display field `tok_s` (float, tokens/sec; `None` when no usable
duration) to every row-producing query. "Tokens" for this rate = the row's
existing `total` (`tokens_input + tokens_output` for sessions;
`prompt + completion` for events), so it tracks the displayed TOTAL column.

Duration per view:

- **Per-session** (`query_sessions`): `duration_s = (time_updated −
  time_created) / 1000`. `tok_s = total / duration_s` when `duration_s ≥ 1`,
  else `None`. This is a **wall-clock average** (includes idle); the header and
  README say so.
- **Daily** (`daily_totals`, `daily_events`): bucket span = `(max last-activity
  − min start) / 1000` over the bucket's contributing rows (sessions:
  `time_updated`/`time_created`; events: `created_at`/`created_at`).
  `tok_s = day_total / duration_s` when `duration_s ≥ 1`, else `None`.
- **Total** (`total_totals`, `total_events`): overall span = `(max last_seen −
  min first_seen) / 1000` (sessions) or `(max − min ``created_at``) / 1000`
  (events) over the included rows. `tok_s = grand_total / duration_s` when
  `≥ 1`, else `None`. The grand-total row always has a span when any data exists.
- **`--from-unsloth` per-event** (`query_events`): events have no duration →
  `tok_s = None` (blank cell). The real rate for this traffic lives in the runs
  view below.

`add_shares` and `paint_bars` pass `tok_s` through unchanged (they already copy
row dicts). Rendering adds a `TOK/S` column (right-aligned, one decimal like
`15.2`, blank when `None`) wherever a table is printed, controlled by whether
the rows carry a non-`None` `tok_s`. To keep "available in all views" true even
when a particular row has no duration, the column is always present in table
output but blank for rows without a duration; `--json` rows carry `tok_s`
(`null` when absent).

My call (flag for review): the derived figure is labelled as a **wall-clock
average**, distinct from the true generation rate, so nobody reads a low
session number as "the model is slow."

### Tokens/second — true generation rate (new runs source + view)

- **New store table** `unsloth_runs`:
  ```sql
  CREATE TABLE IF NOT EXISTS unsloth_runs (
     run_id TEXT PRIMARY KEY,
     model TEXT NOT NULL DEFAULT '',
     progress_tokens INTEGER NOT NULL DEFAULT 0,
     started_at INTEGER NOT NULL,
     completed_at INTEGER,
     status TEXT NOT NULL DEFAULT ''
  )
  ```
  Monotonic-max on `progress_tokens`; one row per `run_id`; rows never deleted
  — same no-double-count discipline as the other two tables (invariant 1).
  Verified live: `chat_generation_runs` columns are `id`, `status`,
  `started_at`, `completed_at`, `progress_tokens` (epoch **ms**) plus
  `request_json`. The model has **no dedicated column** — extract it with
  `json_extract(request_json, '$.model')`; it is a **raw string** (an LMStudio
  file path or `unsloth/Ornith-1.5-122B-GGUF`), *not* the `providerID`/`id`
  JSON sessions store, so `--from-runs` `--model` substring-matches that raw
  string directly (no `provider || '/' || model_id` assembly).
- **Ingestion**: fold into the existing `import-unsloth` step (so the hourly
  cron captures it for free) as a new function `import_unsloth_runs(src, dest)`
  reading `chat_generation_runs` (read-only). `--json`/verbose counters gain a
  `runs` inserted/updated/unchanged line. My call (flag for review): folding
  into `import-unsloth` rather than a new command keeps cron and the report
  modes' implicit snapshot simple; the implicit snapshot in report modes also
  runs it.
- **New report flag `--from-runs`**: reports the runs table with a real
  per-request `tok_s = progress_tokens / ((completed_at − started_at)/1000)`
  (blank when the run hasn't completed or span < 1 s). Session-shaped columns
  (`DATE`, `MODEL`, `IN`(=progress_tokens), `OUT`(blank), `TOTAL`, `TOK/S`),
  plus `--daily` and `--total`/bare-`--model` aggregates that carry the same
  derived span rate. `--from-runs` participates in the same flag-conflict
  handling as `--from-unsloth`.

  My calls (flag for review), settling the builder-check gaps:
  - **Filters:** `--from-runs` supports `--model` (substring on the raw stored
    string), `--since`, and `--until`. It does **not** support `--min-in` /
    `--min-out` — runs store only `progress_tokens` (no in/out split), so the
    thresholds don't map; reject them via `--from-runs`'s existing
    unsupported-flag guard (same mechanism `reconcile` uses), not a new error.
  - **Daily bucket key:** a run's day is keyed on `started_at` (generation
    start), matching the session "creation day" convention; the daily span's
    max uses `completed_at`.

### `--cost` estimate (opt-in overlay)

- New flag `--cost` (default off). Optional `--cost-tier {haiku,sonnet,opus}`
  (my call: default **sonnet**, since the human picked Sonnet; the flag exists
  so the reference tier is swappable without code changes).
- Pricing constants (per-million-tokens), verified against current Anthropic
  Sonnet list prices at implementation time; approximate today:
  input `$3`, output `$15`, cache-read `$0.30` (0.1× input),
  cache-write `$3.75` (1.25× input).
- `cost_est` per row:
  - **Sessions** (`usage` row has all five token columns):
    `in*3 + out*15 + cache_read*0.30 + cache_write*3.75`, all `/ 1e6`.
    Reasoning tokens are not separately priced → fold into input (or ignore;
    decide at impl — default: ignore, since OpenCode's `tokens_reasoning`
    overlaps output).
  - **Events** (`unsloth_events`): only prompt/completion known; approximate as
    `prompt*3 + completion*15 / 1e6` (prompt includes KV-cache hits — noted as
    an over-estimate for the input side).
  - **Runs** (`unsloth_runs`): output-only, `progress_tokens*15 / 1e6`.
- Display: when `--cost` is set, add a right-aligned `COST` column formatted as
  `$%.2f` (or `$%.4f` for tiny rows) to every table view; `--json` rows gain a
  numeric `cost_est`. When the flag is off, neither the column nor the field
  appears — preserving the current default. `--cost` works with `--daily`,
  `--total`, `--from-unsloth`, and `--from-runs`.
- The stored `cost` column is untouched; `--cost` is purely a display/JSON
  overlay. (This also resolves the stale "COST dropped from display" state into
  an explicit opt-in rather than a permanent removal.)
- **`reconcile` is excluded from both `TOK/S` and `--cost`** (human decision,
  2026-11-14). Its rows merge both sources, so there is no single per-row
  duration and no unambiguous cost basis — any figure would be invented.
  `reconcile` keeps its current columns, and `--cost` is added to its existing
  unsupported-flag reject-list alongside `--daily`/`--total`. The G1/G3
  "every view" / "any view" wording means every *reporting* view; `reconcile`
  is a comparison view, not a reporting one.

### Cleanup / performance pass (scope to be finalized in the plan)

Candidates, roughly in priority order; the plan will include the safe ones and
evaluate the rest:

1. **Indexes** — `CREATE INDEX` on `unsloth_events(created_at)` (flagged
   missing; ~70k rows/yr) and `usage(time_created)` (session filters). Low
   risk, clear query speedup.
2. **Per-row isolation in `record()`** — wrap each OpenCode row upsert in
   try/except like `import_unsloth`, so one malformed source row can't abort
   the whole run (invariant 6 intent). Low risk.
3. **LIKE metacharacter escaping** — escape `%`/`_` in both `--model` paths so
   a literal search works and wildcards aren't injected. Correctness fix.
4. **Refresh stale docs** — module docstring, `--help`, and README currently
   describe OpenCode as the sole source and predate `--from-unsloth`,
   `--total`, and (now) the new flags. Update to match reality.
5. **Redundancy removal** — `total_events`/`total_totals` and
   `daily_events`/`daily_totals` are near-duplicate structures; a shared
   aggregate helper would cut duplication. Medium risk (touches hot query
   paths) — plan decides whether the dedup is clean enough to include now.
6. **O(N) re-ingest loops** — `record()`/`import_unsloth` do per-row
   SELECT-then-UPDATE. A batched `INSERT ... ON CONFLICT ... DO UPDATE`
   (SQLite upsert) would cut round-trips. Higher risk + behavior-sensitive
   (monotonic-max semantics) — plan decides; likely deferred with a note.

The plan will state explicitly which of 5–6 (if any) it includes and why.

## Constraints

- stdlib-only Python, single file, no install step.
- Source DBs opened read-only (`file:...?mode=ro`); never write to OpenCode's
  or Studio's databases.
- Our store keeps WAL + busy_timeout; `.gitignore` keeps covering `usage.db*`,
  `unsloth_runs` lives in the same store, and `record.log` stays gitignored.
- Date windows stay local-time and until-day inclusive via `_day_end_ms`
  (DST-safe; do not replace with `+86_400_000`).
- Report modes warn-and-continue (exit 0) when a source is missing/locked;
  explicit snapshot commands exit 1 on failure. `--from-runs` inherits this.
- Development convention: spec → plan → TDD; tests via
  `python3 -m unittest test_tokenstat -v`.

## What's left to the builder

- Exact pricing constants (verify current Sonnet list prices at implementation).
- Whether `tokens_reasoning` folds into input or is ignored for cost.
- Column header text and alignment for `TOK/S` / `COST`.
- The precise shared-helper shape for the redundancy removal (if included).
- Test cases covering: runs ingestion idempotency + monotonic-max; tok_s
  derivation per view (including the blank/None cases); cost math per tier;
  `--cost` off leaves default output untouched; flag conflicts for
  `--from-runs`; LIKE escaping; the new indexes existing.
- **Existing tests to update (known):** because `tok_s` rides in `--json` and
  on every query row by design, three exact-shape tests break and must be
  updated to expect the new key: `test_query_returns_expected_dict_keys`,
  `test_json_output_parses`, `test_total_json_shape`.
- README / AGENTS.md / `--help` updates reflecting all of the above.
