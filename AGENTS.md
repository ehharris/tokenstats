# tokenstats — working notes for agents

**What this is:** a permanent local record of all AI token usage on this
machine. `tokenstats.py` (stdlib-only Python) snapshots OpenCode's per-session
totals and per-message steps (read-only) into its own SQLite store and reports
them filtered by model, date, and token counts. Everything the tool owns lives
in one directory, `~/.tokenstats/` (store, user themes, saved theme default),
so the script itself can sit anywhere, read-only included. It is cross-OS
(Linux, macOS, Windows): it finds OpenCode's database automatically per OS and
forces UTF-8 on the console so box-drawing never crashes a cp1252 terminal. It
is installable (`pyproject.toml` → a `tokenstats` console command) but still
runs straight from the file. Ported 2026-11-18 from the two-source
`token_counter` tool: the second data source and its comparison view were
removed, leaving a single-source OpenCode tool. Design doc in
`docs/superpowers/specs/`, implementation plan in `docs/superpowers/plans/`.
User docs: `README.md` (quick start + screenshots), `docs/reference.md` (full
option list, number methodology, guarantees), `themes/README.md` (theme file
format); screenshots live in `docs/images/`.

**State at handoff (2026-11-18):** work sits on `master` (no remote configured —
local-only repo, no push). 401 tests green, clean under
`-W error::ResourceWarning`. Connection hygiene: every sqlite connection
`main()` opens is closed — the store via try/finally and the read-only source by
`_ingest` owning its open/close; `NoResourceWarningTests` subprocess-pins a
`-W error::ResourceWarning` CLI run (the suite runs warning-free under the same
flag). The store holds two tables — `usage` (one row per OpenCode session) and
`opencode_steps` (one row per ingested assistant message) — and two indexes,
`idx_usage_time_created` and `idx_opencode_steps_session_turn`.

## Feature notes (design decisions that still apply)

**`--total` all-time aggregate view.** `--model PATTERN` = rows per matching
model, bare `--model` = rows for every model. Tables are left-aligned. COST is
not a displayed column by default (still stored, and in `--json`).

**Share columns.** Per-model `--total` tables end with `SHARE %` (row total as
% of the overall total across displayed rows) and `SHARE`, a bar of smooth
Unicode blocks (█ full + one ░▒▓ shade fraction, scaled to the largest row;
deliberately NOT eighth glyphs U+2581..2587 — their vertical metrics differ
from █ and rag the bar bottoms), padded one space either side and colored
truecolor by a per-glyph gradient whose transition marks each row's in/out
split (endpoints are the active theme's `tin`/`tout`; interpolated via colorsys
HLS sweeping UP through purple — a direct RGB lerp goes through gray;
`paint_bars`, table-only; tty-gated via `_color_enabled` — plain when piped or
under `NO_COLOR`). Bare `--model` (no value, no `--total`) is a shortcut for
`--total --model`; with `--daily` it still means "all models" and renders the
daily table. `--json` `--total` rows carry a float `share` field (no bar).
Share views sort rows by `SHARE` descending — ties keep alphabetical-by-model
order (stable sort) and `--json` rows carry the same order; session/daily views
are unchanged.

**TOK/S (turn-level).** Every report table has a `TOK/S` column. A session row's
is the UNWEIGHTED MEAN of its per-turn rates — Σ(tokens_generated)*1000/Σ(step_ms)
per turn (a turn is excluded when any step never streamed, when it moved no
tokens, or when its pooled span is < 1 s), then averaged across the surviving
turns — the number OpenCode shows per prompt. There is deliberately NO
wall-clock fallback: mixing the two bases is the bug this replaced. The store
table `opencode_steps` (message_id PK, session_id, turn, step_ms,
tokens_generated, time_created) is filled by `import_opencode_steps` inside
`record()` — one row per OpenCode assistant message. `turn` is a dense 0-based
ordinal (first-appearance order of the input boundary returned by the ported
`_turn_index`), `step_ms` is the step duration (streamed − firstToken, or
streamed − created when firstToken is NULL, NULL when the step never streamed),
and `tokens_generated` = output+reasoning. Blank means "no rateable turn / no
usable step timing", NOT zero speed.

**Aggregate TOK/S.** Daily/total rows are the TOKEN-WEIGHTED MEAN of the rates
of the rows they fold up (`_weighted_mean_rate` over `_session_rate_groups`,
which reuse `query_sessions`' own formula, so an aggregate stays reproducible
from the rates displayed above it). The old denominator was
`MIN(created)→MAX(updated)` across the group, which counts every idle second
between and inside sessions. Weight = the row's GENERATED tokens
(`tokens_generated` summed over the session's step rows) so the weight matches
the rate's own numerator; as a result the hero over any session-family table
reproduces the pooled grand total to 0.1. Rows with no rate or no generated
tokens drop out of BOTH sides of the average; a group with nothing left renders
blank. Applies to `total_totals`/`daily_totals`, and the hero (`report_header`)
computes the same weighted mean over the rows it summarizes and carries
`NNN.N tok/s` as its second segment (right after the grand total) — omitted
whole when no displayed row has a rate. Only hero
NUMBERS are painted: every unit word stays in the terminal's own color. The
table TOK/S and SESSIONS columns wear the accent/teal hues (`tok_s` and
`sessions` joined `_PAINT_CELLS`, the rate formatted exactly as `render_table`
would, so column widths never shift).

**Cache column.** Opt-in `--cache` adds one combined `CACHE` column (read+write)
after OUT in the session-family views only, plus a matching `· X cache` hero
segment after the `out` stat (the dashboard hero sums ALL sessions, like the
cost tails); informational — TOTAL stays IN+OUT, `--json` gains no field (both
classes already carried); primitives `annotate_cache`/`with_cache`.

**Stat order (human decision 2026-11-18).** Hero segments and table columns
share one order: grand total, TOK/S, row count, IN, OUT — then opt-in CACHE
(right after OUT), the SHARE pair, and cost columns last; row-label columns
(DATE/MODEL/SESSION title) stay at the edges. `with_cache` inserts after the
OUT entry; `report_header` takes `cache_total=` (an int precomputed by the
caller, like `cost_est`) and omits the segment when it is None.

**Default dashboard.** A bare run renders hero + top-5-by-share model panel +
5 most-recent sessions (newest first); the trigger is a truly bare invocation
(`--cost`/`--cache`/`--theme` overlays allowed; `--json` and every flagged view
unchanged); SHARE computed over all models then truncated to 5.

**Themes.** Ten built-in palettes live in `tokenstats.py`: `THEMES` maps slug →
(display name, colors) for `neon-midnight` (the default; `NEON` minus the legacy
`track`), `tokyo-night`, `dracula`, `nord`, `gruvbox-dark`, `solarized-dark`,
`monokai`, `one-dark`, `solarized-light` and `paper` (the last two for light
terminals). `THEME_ROLES` is the nine-role schema (`border`, `accent`, `label`,
`num`, `tin`, `tout`, `tcach`, `ttotal`, `tshare`); `track` is LEGACY — it stays
in `NEON` (a test pins the key set), is read by nothing, and is not in the file
schema. `main` resolves ONE theme (`resolve_theme`) and threads it explicitly
through `report_header`/`panel`/`_table_panel`/`paint_cells`/`paint_bars`/
`paint_header_block` — no mutable module state — and the bar gradient takes its
HLS endpoints from the theme's `tin`/`tout` (`gradient_stops`). CLI:
`--theme=SLUG` paints one run (a display overlay like `--cost`, so the bare
dashboard still renders), bare `--theme` lists the themes and marks the default,
`--theme-default=SLUG` saves the default into
`~/.tokenstats/themes/config.json` (`{"default": "<slug>"}`, written by
`write_default_theme`, read by `read_default_theme` on any run that resolves a
theme without an explicit `--theme=SLUG`; bare `--theme-default` saves the same
run's `--theme=SLUG`, and a save wins over a list when both flags are given).
Both actions short-circuit before implicit ingestion and never touch SQLite.
Precedence: `--theme` > `~/.tokenstats/themes/config.json` > Neon Midnight, and
the two failure modes split on purpose: an explicit `--theme=<unknown>` is exit
2 naming the slug and listing the slugs, while a saved default that no longer
resolves warns naming `~/.tokenstats/themes/config.json` and renders in Neon
Midnight (a cosmetic default never costs a report). `~/.tokenstats/themes/` is
scanned by `scan_themes` whenever a theme is resolved or listed: one `.json` per
theme, filename stem = slug, a file shadows a built-in of the same slug, a file
named exactly `config.json` is skipped (it is the saved default, not a theme),
and a file that fails to parse or validate is skipped with a one-line stderr
warning. The repo's `themes/` keeps only `README.md` (the format reference) and
`examples/`, which ship tracked reference themes that stay inert until copied
into `~/.tokenstats/themes/` — the scan does not walk subfolders, and a test
loads every example so a shipped one cannot rot. Neon Midnight is the default
and flag-off output stays byte-identical; piped/`NO_COLOR`/`--json` output stays
byte-plain under every theme — and that promise is an artifact: `golden/` holds
exact stdout for 15 invocations x {piped, tty}, compared by
`ByteIdentityGoldenTests`, regenerated only on purpose (see `golden/README.md`).
CLI tests share one harness base (`CliHarness`: `run_main` captures both
streams, `colored_main` also forces the color gate).

**Cost overlay.** One option `--cost[=<slug>]` (argparse `nargs="?"`,
`const="sonnet-5"`; prefer the `=` form because `--cost record` swallows the
command → exit 2). `PRICES` is keyed to specific model slugs with display
`name`s (haiku-4.5, sonnet-5, opus-5, gpt-5.1-codex-mini, gpt-5.1-codex,
deepseek-v3.2, qwen-turbo, qwen-plus, qwen-max; list prices verified 2026-11-17
— Claude Sonnet 5 $2/$10, Opus 5 $5/$25; non-Anthropic vendors bill no
cache-write premium → `cache_write = in`), plus `FAMILY_ALIASES` mapping a
family name to the provider's MID-TIER model (claude→sonnet-5,
codex/openai→gpt-5.1-codex, deepseek→deepseek-v3.2, qwen→qwen-plus), resolved
once in `main` via `resolve_cost_tier`. The COST header is built per-run by
`cost_column(tier)` and names the pinned model on ONE line
(`COST · Claude Sonnet 5 Prices`) — a stale price table is visible and the head
stays one line tall. Table head: header text BOTTOM-left aligns and every table
gets a `─` rule between the header block and the data (`header_height`/
`paint_header_block`; rule wears the border hue in color). Cost cells render
`$X.XX` (sub-cent rows render `$0.00`). The number is a UNIFORM REFERENCE for
all traffic (most tracked models are not the pinned one), explicitly distinct
from the stored `cost` column. `--real-cost` adds a `REAL COST` column from the
stored `cost` field (the real USD OpenCode reported; rows already carry it —
pure display, `--json` shape unchanged), coexists with `--cost` (estimate column
first, real after), and adds a `· $X.XX` hero tail (no tilde — it is real)
summed over ALL sessions on the dashboard. The retired `--cost-tier` is kept
only as a suppressed shim that answers stale shell history with a migration
message.

**Sessions cap.** `--sessions[=N]` scopes the OpenCode session views to the N
most recent sessions (by creation time, newest first). A valueless `--sessions`
means the 20 most recent in the list view but ALL sessions in the aggregate
views (an aggregate is a summary by default; `--total` alone already IS the
all-sessions view); `--sessions=0` means all matching sessions in any view; an
explicit N caps every view the same way. The cap is ONE predicate inside
`_session_filters` (`session_id IN (SELECT session_id FROM usage <same
where-params> ORDER BY time_created DESC, session_id LIMIT ?)` — the cap takes
the N most recent of what survives the existing --model/--since/--until/threshold
filters), which `query_sessions`, `daily_totals`, `total_totals`, and the rate
groups all funnel through, so rows, aggregates, and the hero's TOK/S share one
scope (None and 0 simply add no cap predicate — the uncapped case is reached by
omission, not a second code path). Alone it renders the capped rows newest
first, titled `sessions · N most recent` or `sessions · all`; with a POSITIVE
cap in force the aggregate titles say so (`daily · last 3 sessions`, etc.) and
with no cap or a 0 cap every title stays byte-identical. Negative N is rejected
(exit 2; SQLite degenerates a negative LIMIT to no-limit, so the CLI is the
only rejection point). The bare-run dashboard is UNCHANGED.

**State directory + migration.** tokenstats' own state lives in one flat
directory, `~/.tokenstats/` — `usage.db` (plus its `-wal`/`-shm` siblings),
`themes/` (user theme files), and `themes/config.json` (the saved default). Path
math lives in `state_dir`, `default_db`, `themes_dir` and `theme_config` (pure,
each takes `home=`), bound at import to the globals
`STATE_DIR`/`DEFAULT_DB`/`THEMES_DIR`/`THEME_CONFIG`; importing `tokenstats`
creates nothing on disk (it only `stat`s candidate paths to resolve
`DEFAULT_SRC`). `default_store()` is the only function here that
creates directories or moves files on its own (`write_default_theme` also
writes, but only because `--theme-default` asked it to), and `main()` is its only
caller — and only when `--db` was not given, so every test and every run that
names its own store never touches `$HOME`. It performs the one-time migration: a
store, or top-level `themes/*.json`, left beside an older copy of the script
travels into `~/.tokenstats/` and says so on stderr (`migrated store: <old> ->
<new>`). The order cannot lose history: within the store group the `-wal`/`-shm`
siblings travel first and `usage.db` LAST, a failed move rolls the moved files
back and keeps using the legacy path, and a move lost to a rival run lands on
the store that run migrated — decided by what is on disk, not by what was
attempted. Theme files move after the store, and only when the state dir has no
themes of its own.

**Cross-OS source resolution.** `default_src(home=None)` probes per-OS
candidates and returns the first that exists (see "Data source" below).
`_ensure_utf8_streams()` runs first in `main()` and best-effort
`reconfigure(encoding="utf-8")` on stdout/stderr — a no-op on streams without
`reconfigure` (StringIO in tests, already-UTF-8 POSIX consoles) — so the
box-drawing frame never crashes a Windows cp1252 console.

**Redundancy pass (behavior and on-screen bytes unchanged).** One LIKE-escape
helper and one date-window builder behind the filter paths (`_like_pattern`,
`_date_clauses`); one aggregate engine behind the daily and total views
(`_grouped_aggregates`, `_total_aggregates`); one monotonic-max upsert for every
ingest path (`_upsert`, whose column classes are numeric / `overwrite` /
`insert_only` / label — `insert_only` caught a real bug: usage's `time_created`
rode the label class, so a source that ever rewrote it could move the stored
value; it is now written once and never touched again); and a flatter `main()`
view layer (`_view_rows`, `_view_title`, `render_report`: one reject list, one
dispatch, one render path). A hardening pass made `_upsert` reject a column-class
name the table does not have — before writing any row, so a typo cannot demote a
token column to the label class — widened its per-row isolation to
`(sqlite3.Error, TypeError, ValueError)` to match `import_opencode_steps`, and
gated the migration rollback on the legacy store still being there, so a move
lost to a rival run leaves the `-wal`/`-shm` siblings with the winner instead of
stranding them. Test classes include: `DefaultPathsTests`, `PathResolutionTests`
(per-OS `default_src` candidates), `EnsureUtf8Tests`, `MigrationTests`,
`LegacyStoreOpensTests` (a store carrying the old two-source tables still opens
and reports), `FreshMachineTests` (one copied file, an empty `$HOME`, an
unrelated cwd — the end-user promise end to end), `HiddenOptionsTests`,
`CliMainTests`, `FilterHelperTests`, `AggregateRowShapeTests`,
`UpsertEngineTests`, `ViewDispatchTests`.

**Final review pass (2026-11-18, fresh-eyes reviewer + coordinator).** Fixes
that landed after the port: `_connect_src` now routes the path through
`pathlib.Path.resolve().as_uri()` before appending `?mode=ro` — a raw `#` or
`?` in a path used to truncate the URI filename, silently drop `mode=ro`, and
let SQLite CREATE a stray writable file at the truncated path (invariant 4);
`record()` coerces every numeric field per row (`int`/`float`) so a source
row carrying text in `time_created`/`cost` bumps `skipped` at ingest instead
of poisoning the never-shrinking store and crashing every later report;
`PRAGMA busy_timeout` now runs before `journal_mode=WAL` (the first-run
rollback→WAL conversion is the one moment a concurrent writer could bounce
the store open); `_color_enabled` survives a stream without `isatty`
(pythonw-style `stdout=None`); `--db`/`--src` get `expanduser` (Windows cmd
does not expand `~`); `--model=` (empty pattern) normalizes to absent so the
title matches the filter; aggregate titles singularize (`last 1 session`);
`compact()` never renders `1000.0k`; the hero is not built for piped output.
New test classes: `SourceUriTests`, `PoisonedSourceRowTests`,
`ColorGateTests`, `TitleGrammarTests`, `TildePathTests`,
`CompactBoundaryTests`.

## Commands (run from this directory)

```bash
pipx install .                                             # a `tokenstats` command on PATH (Linux/macOS/Windows)
pip install --user .                                       # the same command, into your user site
python3 tokenstats.py                                      # run without installing: snapshot + compact dashboard
python3 tokenstats.py --model qwen --since 2026-11-01 --until 2026-11-13
python3 tokenstats.py --min-in 10000 --daily               # thresholds apply to daily mode too
python3 tokenstats.py --total                              # all-time grand total, one row
python3 tokenstats.py --total --model qwen                 # all-time totals, one row per model
python3 tokenstats.py --total --model                      # ...for ALL models (bare --model)
python3 tokenstats.py --model                              # same as --total --model (shortcut)
python3 tokenstats.py --sessions                           # list the 20 most recent sessions, newest first
python3 tokenstats.py --sessions=10 --daily                # daily aggregates over the 10 most recent sessions
python3 tokenstats.py --cost[=<slug>]                      # reference $ overlay (default sonnet-5; slugs + family aliases)
python3 tokenstats.py --total --real-cost                  # stored real USD column (session views only)
python3 tokenstats.py --total --model --cache              # combined cache column (session views only)
python3 tokenstats.py --theme[=SLUG]                       # list themes / paint this run (display-only overlay)
python3 tokenstats.py --theme-default=SLUG                 # save the default theme in ~/.tokenstats/themes/config.json
python3 tokenstats.py record                               # explicit snapshot (exit 1 if the source is unreadable)
python3 -m unittest test_tokenstats -v                     # tests (~0.6s, no deps)
python3 -W error::ResourceWarning -m unittest test_tokenstats   # strict variant: must stay warning-clean
TOKENSTATS_UPDATE_GOLDEN=1 python3 -m unittest test_tokenstats   # re-pin golden/ only AFTER reading the diff
```

`--min-in`, `--min-out`, `--db` and `--src` work exactly as before but are
deliberately absent from `--help`; the store override is what keeps the suite out
of `$HOME`. tokenstats' own state is `~/.tokenstats/` (store, `themes/`,
`themes/config.json`), and the scheduler log lives beside the store at
`~/.tokenstats/record.log`.

## Data source (verified live)

- **OpenCode** is the only source. `default_src(home)` probes these in order and
  returns the first that exists; with none present it returns the
  `.local/share` default so the caller still has a real path to report:
  1. `$OPENCODE_DB` (when it is an absolute path)
  2. `$XDG_DATA_HOME/opencode/opencode.db` (when `XDG_DATA_HOME` is absolute)
  3. `<home>/.local/share/opencode/opencode.db` (the XDG default, every OS)
  4. macOS — `<home>/Library/Application Support/opencode/opencode.db`
  5. Windows — `%APPDATA%\opencode\opencode.db`
  6. legacy `<home>/.opencode/opencode.db`

  Table `session_v2` — per-session totals only (no per-request data), plus
  `session_message` for the per-message step snapshot. OpenCode **prunes** these
  tables; snapshot promptly or history is lost. The model column is JSON
  (`providerID` + `id`). Timestamps are epoch **ms**. `--src PATH` overrides the
  whole probe.

## Invariants — do not break these

1. **No double counting.** One row per source key (`session_id`); every numeric
   column stores the latest observed total via monotonic `max`, never an
   increment; rows are never deleted. Same for the step snapshot:
   `opencode_steps` has one row per `message_id`, with `step_ms`/
   `tokens_generated` kept at the monotonic max and rows never deleted, so
   ingested steps survive OpenCode pruning `session_message` (a NULL `step_ms`
   means "never streamed" and never maxes against a real duration). Re-running
   any import is a provable no-op (the numeric token columns and step rows are
   unchanged; `last_seen` is the one field that moves, and it is not a token
   count). Reports always SUM over distinct keys.
2. **Labels never shrink:** `record()` keeps the best existing
   provider/model/title when a source nulls them.
3. **Date windows are local-time and until-day inclusive**, computed via
   `_day_end_ms` (calendar next-midnight, DST-safe — do not replace with
   `+86_400_000`; this machine observes MST/MDT).
4. **The source opens read-only** (`file:...?mode=ro`) — never write to OpenCode's
   database; it belongs to a running service.
5. Query modes warn-and-continue when the source is missing/locked (exit 0);
   the explicit `record` command exits 1 on failure.
6. Our store uses WAL + busy_timeout so the scheduler and interactive runs
   overlap safely. It lives in `~/.tokenstats/usage.db`, outside the repo, and
   the scheduler log sits beside it. Keep `.gitignore` covering `usage.db*` and
   `record.log` anyway: those entries now only catch a legacy copy left beside
   the script, which the first run migrates away.

## Known gotchas / deferred work (from review rounds)

- **Not a bug (investigated 2026-11-14):** "bare `tokenstats.py` looks stale but
  `--total --model` is fresh" — all report modes run identical implicit
  ingestion; verified store == OpenCode source right after a bare run. The
  impression is display: in session mode only the *live* session's row ever
  changes (finished rows are immutable by invariant 1) and it sits last in the
  oldest-first table, while per-model totals aggregate everything so any
  activity visibly grows a number. OpenCode also writes `session_v2` totals only
  at message completion, so live numbers trail the UI counter by one response —
  invisible in aggregates, exposed per-session. Possible UX fix if ever wanted:
  sort/show most-recently-active sessions first.
- `SHARE` is computed over the **displayed rows**, so with `--since`/`--until`/
  threshold filters it means "% of the filtered total", not "% of all time". The
  grand-total row renders 100.0% and a full bar.
- Dates in session mode bucket by **session creation day**; some real sessions
  span multiple days, so per-day session numbers differ from a request-dated
  daily breakdown. Totals agree; attribution differs.
- **DST zones where midnight itself shifts** (America/Havana, America/Santiago:
  the clock jumps at 00:00) get a one-hour-shifted `--since`/`--until` window
  and a filter/bucket mismatch for the affected day. The calendar-midnight math
  is correct for zones that shift at other hours (all documented cases, incl.
  MST/MDT); a `fold`-aware rewrite is the only real fix and was judged not
  worth it.
- `--theme-default=SLUG --theme=bogus` saves SLUG and exits 0 without ever
  validating the paint-only `--theme` (the save short-circuits before
  resolution). Cosmetic surprise only.
- `--model` patterns are LITERAL: LIKE metacharacters `%`/`_` are escaped
  (`ESCAPE '\'`) in the filter path.
- Ingestion has per-row error isolation: `record()` — one malformed source row
  bumps a `skipped` counter (printed only when nonzero) and the run continues.
- **Source-level discrepancy, not our bug (verified 2026-11-17):** for the
  reference session, Σ`tokens_generated` over our step rows is 77,696 and Σ over
  OpenCode's own assistant message rows is also exactly 77,696, while
  `session_v2`'s `tokens_output+tokens_reasoning` is 77,951 — a 255-token delta
  in OpenCode's own data (its per-session totals disagree with the sum of its own
  message rows). `rate_weight` is faithful to the message rows it is defined
  over; do not "correct" it to the 77,951 total.
- Deferred perf pass (verified safe to postpone): the near-duplicate aggregate
  queries (`daily_totals`/`total_totals`) are not deduplicated, and the per-row
  SELECT+UPDATE ingest loops are O(N) full rewrites each run (~0.4s per 100k
  rows; fine for years at current volume).
- Displayed tables have no COST column by default; opt-in `--cost` adds a
  REFERENCE estimate column (header names the pinned model); the stored real cost
  is displayed only by opt-in `--real-cost` (and always was in `--json`).
- Sessions whose source model JSON was null/malformed group under `MODEL="/"` in
  per-model reports (empty provider + empty model_id joined by `/`). Honest
  display — any fix belongs in ingestion labeling (invariant 2), not display
  filtering.
- Deferred polish (all verified safe): flag-conflict guard tests assert exit 2
  but not message text. Guards (all modes) run after implicit ingestion — side
  effects are idempotent no-op writes to our own store.

## NOT DONE (decisions left to the human)

- **Scheduling is documented, not installed on this machine.** The README shows
  the per-OS way to snapshot hourly: cron (Linux/macOS)
  `0 * * * * python3 <path>/tokenstats.py record >> ~/.tokenstats/record.log 2>&1`
  and Windows Task Scheduler (`schtasks /Create /SC HOURLY ...`). OpenCode
  prunes `session_v2`, so an hourly snapshot is the only thing protecting that
  history — set it up on any machine that must not lose history.
- No git remote; no PR workflow. If one is wanted, follow the parent workspace's
  GitHub App auth notes in `/home/elih/messinaround/AGENTS.md` (that setup is
  for the openmw repo — this repo has no bot access yet).
