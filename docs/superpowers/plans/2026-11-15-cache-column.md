# CACHE column (`--cache`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `--cache` flag that renders one combined `CACHE` column (cache_read + cache_write) between OUT and TOTAL in the OpenCode-session views.

**Architecture:** Pure display overlay, same shape as the existing `--cost` overlay: the store already ingests both cache classes and every session-family row dict already carries them, so this adds an annotation step plus a column-insertion helper at render time. No schema, ingestion, or stored-value changes.

**Tech Stack:** stdlib-only Python (`tokenstat.py`), unittest suite (`test_tokenstat.py`).

**Spec:** `docs/superpowers/specs/2026-11-15-cache-column-design.md`

## Global Constraints

- Stdlib only; everything runs from `/home/elih/messinaround/token_counter`.
- Tests: `python3 -m unittest test_tokenstat -v`; suite must stay green under `python3 -W error::ResourceWarning -m unittest test_tokenstat`.
- Flag-less (and `--json`) output stays byte-identical to today; `TOTAL` stays `IN + OUT` even with `--cache` (invariants 1–2 of AGENTS.md).
- Rejected combinations exit 2 via `parser.error`, matching existing guards.

## Review Focus

- `--total --model --cache`: the SHARE/BAR rendering must survive column insertion (identity check on `SHARE_COLUMNS` — mirror of `test_cost_keeps_share_bar_padded`).
- `--cost --cache` together: both overlays apply independently; COST still renders last.
- JSON with `--cache`: key set identical to without it (no new `cache` field leaks into `--json`).
- Rows whose sessions have zero/NULL cache columns render `0`, not blank or error.

---

### Task 1: Overlay primitives (`annotate_cache`, `with_cache`, `CACHE_COLUMN`)

**Files:**
- Modify: `tokenstat.py` (after `COST_COLUMN`, ~line 591)
- Test: `test_tokenstat.py` (new class `CacheOverlayTests`, append near `CostTests`)

**Interfaces:**
- Consumes: nothing new; row dicts from `query_sessions`/`daily_totals`/`total_totals` carry `tokens_cache_read`/`tokens_cache_write`.
- Produces: `CACHE_COLUMN = ("CACHE", "cache", "int")`; `annotate_cache(rows) -> list[dict]` (copies with int key `cache`); `with_cache(columns) -> list[tuple]` (copy with `CACHE_COLUMN` inserted before the `("TOTAL", ...)` entry). Task 2 wires these into `main()`.

- [ ] **Step 1: Write failing tests** — new class `CacheOverlayTests(unittest.TestCase)` pinning: combined sum (`{"tokens_cache_read": 7000, "tokens_cache_write": 100}` → `cache == 7100`); missing keys default to 0 (`{}` → `cache == 0`); input rows not mutated (no `cache` key on the originals); `with_cache(SESSION_COLUMNS)` header order contains `"OUT"`, `"CACHE"`, `"TOTAL"` consecutively and does not mutate its argument; works for `DAILY_COLUMNS` and `SHARE_COLUMNS` too.
- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.CacheOverlayTests -v`; expect ImportError/AttributeError on `annotate_cache`.
- [ ] **Step 3: Implement** in `tokenstat.py` below `COST_COLUMN`: the constant, `annotate_cache(rows)` (one-line dict comprehension over `{**r, "cache": r.get("tokens_cache_read", 0) + r.get("tokens_cache_write", 0)}`), and `with_cache(columns)` (insert at the index of the tuple whose header is `"TOTAL"`). Docstrings in house style; note both are display-only copies.
- [ ] **Step 4: Run to verify pass** — same command, then full suite `python3 -m unittest test_tokenstat`; expect all green.
- [ ] **Step 5: Commit** — `git add tokenstat.py test_tokenstat.py && git commit -m "feat: annotate_cache/with_cache primitives for the --cache overlay"`

### Task 2: CLI flag, guards, JSON exclusion, docs

**Files:**
- Modify: `tokenstat.py` (`main()`: argparse block, reconcile reject list ~line 960, guard near `--from-runs` guard ~line 949, overlay block after the `--cost` block ~line 1010, JSON key filter ~line 1013)
- Modify: `README.md`, `AGENTS.md`
- Test: `test_tokenstat.py` (new class `CliCacheTests`)

**Interfaces:**
- Consumes: Task 1's `annotate_cache`, `with_cache`, `CACHE_COLUMN`.
- Produces: user-facing `--cache` flag; no further tasks.

- [ ] **Step 1: Write failing tests** — new class `CliCacheTests(unittest.TestCase)` modeled on `CliTotalTests` (same setUp/`run_main` shape). Pins, all against the seeded fixture (`src_row` defaults: tin=1000, tout=200, tcr=7000, tcw=100):
  - session view with `--cache`: header regex `OUT\s+CACHE\s+TOTAL`; body shows `7,100` for the seeded session.
  - daily view (`--daily --cache`): grand row cache `14,200` (two sessions).
  - total view (`--total --cache`): `(all)` row shows `14,200`.
  - default off: no `CACHE` header without the flag.
  - TOTAL unchanged: with `--total --cache`, the `(all)` row still shows IN/OUT/TOTAL `2,000`/`400`/`2,400` next to cache `14,200`.
  - Review-Focus pins: bars survive (`--total --model --cache` matches `\d+\.\d%   [█░▒▓]`); `--cost --cache` shows both `COST` and `CACHE`; `--json --cache` parsed rows have the same key set as `--json` without it and contain no `"cache"` key.
  - guards exit 2: `reconcile --cache`, `--from-unsloth --cache`, `--from-runs --cache`.
- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.CliCacheTests -v`; expect failures (unrecognized `--cache`).
- [ ] **Step 3: Implement** in `main()`: add `parser.add_argument("--cache", action="store_true", dest="cache", help=...)` after the `--cost-tier` argument; append `+ (["--cache"] if args.cache else [])` to the reconcile reject list; add a guard next to the existing `--from-runs`/`--min-in` guard: `if args.cache and (args.from_unsloth or args.from_runs): parser.error("--cache requires OpenCode session rows; Unsloth event/run sources fold cache hits into prompt tokens with no split")`; after the `--cost` overlay block add `if args.cache: rows = annotate_cache(rows); columns = with_cache(columns)`; extend the JSON filter tuple to `("bar", "share_str", "cache")`.
- [ ] **Step 4: Run to verify pass** — `python3 -m unittest test_tokenstat.CliCacheTests -v`, then full suite, then the strict variant `python3 -W error::ResourceWarning -m unittest test_tokenstat`; expect all green.
- [ ] **Step 5: Manual smoke** — `python3 tokenstat.py --total --model --cache` shows the CACHE column between OUT and TOTAL with plausible numbers (Qwen3.8-Flash-Next ≈ 113M+ cache).
- [ ] **Step 6: Docs** — README: add `--cache` to usage/examples, one sentence: combined read+write, informational, TOTAL unchanged, not for `reconcile`/`--from-unsloth`/`--from-runs`. AGENTS.md: append a line to the increments paragraph and add `python3 tokenstat.py --total --model --cache` to the Commands block.
- [ ] **Step 7: Commit** — `git add tokenstat.py test_tokenstat.py README.md AGENTS.md && git commit -m "feat: --cache overlay column (read+write) in session views"`
