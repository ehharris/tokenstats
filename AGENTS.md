# token_counter — working notes for agents

**What this is:** a permanent local record of all AI token usage on this
machine. `tokenstat.py` (stdlib-only Python, no install step) snapshots
three tables from two sources (OpenCode sessions; Unsloth Studio request
events and generation runs) into its own SQLite store and reports them
filtered by model, date, and token counts. Built 2026-11-13 via spec →
plan → TDD; design doc in `docs/superpowers/specs/`, implementation plan
in `docs/superpowers/plans/`.

**State at handoff (2026-11-14):** current work sits on branch
`tokens-per-second-and-cost` (no remote configured — local-only repo, no
push; branch workflow used for increments, fast-forward merged after clean
review rounds). 119 tests green. Increments since the original build: `--total` all-time
aggregate view (session and `--from-unsloth` paths; `--model PATTERN` = rows
per matching model, bare `--model` = rows for every model), left-aligned
tables everywhere, COST dropped from displayed reports (still stored and in
`--json`). Share increment: per-model `--total` tables end with `SHARE`
(row total as % of the overall total across displayed rows) and a `BAR` of
smooth Unicode blocks (█ full + one ░▒▓ shade fraction, scaled to the
largest row; deliberately NOT eighth glyphs U+2581..2587 — their vertical
metrics differ from █ and ragged the bar bottoms), padded one space either
side and colored truecolor by a per-glyph blue->orange gradient whose
transition marks each row's in/out split (`TOKEN_IN_RGB`/`TOKEN_OUT_RGB`,
interpolated via colorsys HLS sweeping UP through purple — a direct RGB
lerp goes through gray; hue/lightness constants derived from the endpoint
RGBs so endpoints round-trip exactly,
`paint_bars`, table-only; tty-gated
via `_color_enabled` — plain when piped or under `NO_COLOR`);
bare `--model` (no value, no `--total`)
is now a shortcut for `--total --model` instead of an error — with `--daily`
it still means "all models" and renders the daily table; `reconcile --model`
unchanged. `--json` `--total` rows carry a float `share` field (no bar).
Tokens-per-second increment (branch `tokens-per-second-and-cost`): new store
table `unsloth_runs` (run_id PK, model extracted in Python from Studio's
`request_json` — no JSON1 dependency; `progress_tokens` monotonic max;
`started_at`/`completed_at`/`status`; a studio.db without
`chat_generation_runs` imports as a zero-count no-op); `import-unsloth` is
now a TWO-TABLE snapshot (events then runs, each step prints its counts,
exit 0 only if both succeed) and report-mode implicit ingestion snapshots
all three tables; `--from-runs` view (base rows show each run's TRUE
generation rate — progress over the run's wall-clock seconds, blank for
unfinished or sub-second runs; IN carries progress_tokens, OUT renders 0 —
runs have no in/out split; `--daily` buckets by started_at; `--total`/bare
`--model` give per-model aggregates with the usual SHARE/BAR treatment;
supports `--model`, `--since`, `--until`; rejects `--min-in`/`--min-out`);
derived `tok_s` on every report-view row plus a `TOK/S` column (reconcile
excepted — session/daily/total views show a WALL-CLOCK average — tokens
over the created→updated span, idle drags it down, blank when the span is
unknown or < 1 s; per-event rows have no
duration so their TOK/S is blank while daily/total event aggregates rate
over their created_at span; `--json` rows carry `tok_s`); opt-in `--cost`
overlay plus `--cost-tier haiku|sonnet|opus` (default sonnet) adding a
`COST` column (`$X.XXXX`) and `cost_est` in `--json`, computed from a
PRICES table of Anthropic LIST PRICES as of 2026-11-14 (Sonnet 4.6 $3/$15,
Haiku 4.5 $1/$5, Opus 4.8 $5/$25 per Mtok; cache-read 0.1x / cache-write
1.25x input) — a UNIFORM REFERENCE for all traffic (most tracked models are
not Claude), explicitly distinct from the stored `cost` column (real
OpenCode data, still `--json` only); off by default, flag-less output
unchanged; runs price output-only, events price prompt+completion (prompt
includes KV-cache hits → input side over-estimates), sessions use all four
token classes. Reconcile is a comparison view: excluded from TOK/S and
`--cost` BY DESIGN (flag reject list + `columns is not RECON_COLUMNS`
guard). Also: indexes `idx_unsloth_events_created` /
`idx_usage_time_created`; `record()` gained per-row error isolation
(`skipped` counter, mirrors the Unsloth imports); `--model` LIKE
metacharacters (`%`, `_`) are literal in all three filter paths.
Store `usage.db` holds full recoverable history: ~73 OpenCode
sessions, ~1,850 Unsloth request events, and a handful of generation runs
back to 2026-11-06.

## Commands (run from this directory)

```bash
python3 tokenstat.py                                   # snapshot both + report sessions
python3 tokenstat.py --model unsloth --since 2026-11-01 --until 2026-11-13
python3 tokenstat.py --min-in 10000 --daily            # thresholds apply to daily mode too
python3 tokenstat.py --total                           # all-time grand total, one row
python3 tokenstat.py --total --model unsloth           # all-time totals, one row per model
python3 tokenstat.py --total --model                   # ...for ALL models (bare --model)
python3 tokenstat.py --model                           # same as --total --model (shortcut)
python3 tokenstat.py --from-unsloth [--daily|--json]   # Unsloth's own per-request log
python3 tokenstat.py --from-runs [--daily|--total]     # Studio runs, TRUE generation tok/s
python3 tokenstat.py --cost [--cost-tier haiku|opus]   # reference $ overlay (off by default)
python3 tokenstat.py reconcile                         # lost-history view per model
python3 tokenstat.py record | import-unsloth           # explicit snapshots (import-unsloth = events + runs; exit 1 if source unreadable)
python3 -m unittest test_tokenstat -v                  # tests (~0.05s, no deps)
```

## Data sources (verified live)

- **OpenCode:** `~/.local/share/opencode/opencode.db`, table `session_v2` —
  per-session totals only (no per-request data). OpenCode **prunes** this
  table; import promptly or history is lost. Model column is JSON
  (`providerID` + `id`). Providers seen: `unsloth-studio`, `lemonade`,
  `opencode`. Timestamps are epoch **ms**.
- **Unsloth Studio:** `~/.unsloth/studio/studio.db`, tables
  `api_usage_events` — per-request prompt/completion tokens, model, ms
  timestamp. Survives OpenCode pruning. `prompt_tokens` includes KV-cache
  hits (maps to OpenCode's input+cache_read, NOT input alone) — and
  `chat_generation_runs` — per-generation runs (`progress_tokens`,
  `started_at`/`completed_at`, `status`); the model lives inside
  `request_json` (extracted in Python), and older studio.db builds without
  the table import as a zero-count no-op. Studio also
  has Prometheus metrics on the llama-server port and richer event payloads,
  deliberately not imported (ephemeral / UI-only — see conversation history
  in plan docs).

## Invariants — do not break these

1. **No double counting.** One row per source key (`session_id` /
   `event_id`); every numeric column stores the latest observed total via
   monotonic `max`, never an increment; rows are never deleted. Re-running
   any import is a provable no-op. Reports always SUM over distinct keys.
2. **The two sources describe the same traffic** — never sum them together.
   `reconcile` exists to compare them; positive gap = OpenCode lost it,
   negative gap = Studio log missing it (rotation/retry).
3. **Labels never shrink:** `record()` keeps the best existing
   provider/model/title when a source nulls them.
4. **Date windows are local-time and until-day inclusive**, computed via
   `_day_end_ms` (calendar next-midnight, DST-safe — do not replace with
   `+86_400_000`; this machine observes MST/MDT).
5. **Sources open read-only** (`file:...?mode=ro`) — never write to
   OpenCode's or Studio's databases; they belong to running services.
6. Query modes warn-and-continue when a source is missing/locked (exit 0);
   explicit snapshot commands exit 1 on failure.
7. Our store uses WAL + busy_timeout so cron and interactive runs overlap
   safely. Keep `.gitignore` covering `usage.db*` and `record.log`.

## Known gotchas / deferred work (from review rounds)

- **Not a bug (investigated 2026-11-14):** "bare `tokenstat.py` looks stale
  but `--total --model` is fresh" — all report modes run identical implicit
  ingestion; verified store == OpenCode source right after a bare run. The
  impression is display: in session mode only the *live* session's row ever
  changes (finished rows are immutable by invariant 1) and it sits last in
  the oldest-first table, while per-model totals aggregate everything so any
  activity visibly grows a number. OpenCode also writes `session_v2` totals
  only at message completion, so live numbers trail Studio's UI counter by
  one response — invisible in aggregates, exposed per-session. Possible UX
  fix if ever wanted: sort/show most-recently-active sessions first.
- `SHARE` is computed over the **displayed rows**, so with `--since`/
  `--until`/threshold filters it means "% of the filtered total", not
  "% of all time". The grand-total row renders 100.0% and a full bar.

- Dates in session mode bucket by **session creation day**; 7 of 53 real
  sessions span multiple days, so per-day session numbers differ from
  Unsloth's request-dated daily numbers. Totals agree; attribution differs.
- `--model` patterns are LITERAL: LIKE metacharacters `%`/`_` are escaped
  (`ESCAPE '\'`) in all three filter paths (sessions, events, runs);
  reconcile's model filter is a plain Python substring check.
- All ingestion paths have per-row error isolation: `record()` mirrors the
  Unsloth imports — one malformed source row bumps a `skipped` counter
  (printed only when nonzero) and the run continues.
- Deferred perf pass (verified safe to postpone): the near-duplicate
  aggregate queries (`total_events`/`total_totals`, `daily_events`/
  `daily_runs`/`daily_totals`) are not deduplicated, and the per-row
  SELECT+UPDATE ingest loops are O(N) full rewrites each run (~0.4s per
  100k rows; fine for years at current volume).
- `--from-unsloth` renders with session-shaped columns (blank SESSION); a
  dedicated event column set is unclaimed polish (`--from-runs` has its own
  RUNS_COLUMNS). Displayed tables have no COST column by default; opt-in
  `--cost` adds a REFERENCE estimate column — distinct from the stored real
  cost, which stays `--json`-only.
- Sessions whose source model JSON was null/malformed group under `MODEL="/"`
  in per-model reports (empty provider + empty model_id joined by `/`).
  Honest display — any fix belongs in ingestion labeling (invariant 3), not
  display filtering.
- Deferred polish from the `--total` review rounds (all verified safe): no
  CLI-level pin for `--from-unsloth --total <pattern>` or bare `--model` on
  `reconcile`; flag-conflict guard tests assert exit 2 but not message text;
  no-COST-header test skips the `--from-unsloth` tables. Guards (all modes) run after implicit
  ingestion — side effects are idempotent no-op writes to our own store.

## NOT DONE (decisions left to the human)

- ~~Hourly cron~~ **INSTALLED 2026-11-13** for user `elih` (`crontab -l`):
  hourly `record` + `import-unsloth`, output appended to `record.log`
  (gitignored). Verified end-to-end under cron's minimal environment
  (`env -i HOME=... PATH=/usr/bin:/bin`). Cron daemon is systemd-enabled,
  so it survives reboots. If history ever looks stale, check `record.log`.
- No git remote; no PR workflow. If one is wanted, follow the parent
  workspace's GitHub App auth notes in `/home/elih/messinaround/AGENTS.md`
  (that setup is for the openmw repo — this repo has no bot access yet).
