# token_counter — working notes for agents

**What this is:** a permanent local record of all AI token usage on this
machine. `tokenstat.py` (stdlib-only Python, no install step) snapshots
three tables from two sources (OpenCode sessions; Unsloth Studio request
events and generation runs) into its own SQLite store and reports them
filtered by model, date, and token counts. Built 2026-11-13 via spec →
plan → TDD; design doc in `docs/superpowers/specs/`, implementation plan
in `docs/superpowers/plans/`.

**State at handoff (2026-11-17):** work sits on `master` (no remote
configured — local-only repo, no push; branch workflow used for increments,
fast-forward merged after clean review rounds). 311 tests green. Connection
hygiene (2026-11-15): every sqlite
connection main() opens is now closed — the store via try/finally and each
read-only source by `_ingest` owning its open/close; `NoResourceWarningTests`
subprocess-pins a `-W error::ResourceWarning` CLI run (suite runs warning-free
under the same flag). Increments since the original build: `--total` all-time
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
Share views (`--total --model`, bare `--model`) now sort rows by `SHARE`
descending — ties keep alphabetical-by-model order (stable sort) and
`--json` rows carry the same order — session/daily views unchanged.
Share-titles increment (2026-11-17, human decision): the percent column is
titled `SHARE %` and the bar column `SHARE` (was `SHARE` + unlabeled bar)
so the visual reads at a glance; the bar column is no longer unlabeled.
Tokens-per-second increment (2026-11-14): new store
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
excepted — session rows show the unweighted mean of their per-turn rates,
Σ(output+reasoning)/Σ(streamed−firstToken, or streamed−created when firstToken
is NULL) per turn then averaged across turns
— the number OpenCode shows per prompt — blank when no turn qualifies (no step
rows, a never-streamed step, a sub-second span, or zero generated tokens),
deliberately NO wall-clock fallback; per-event rows have no
duration so their TOK/S is blank while daily/total event aggregates rate
over their created_at span; `--json` rows carry `tok_s`); opt-in `--cost`
overlay plus `--cost-tier haiku|sonnet|opus` (default sonnet) adding a
`COST` column (`$X.XXXX`; SUPERSEDED by the cost-rework increment below —
that flag and these prices no longer exist) and `cost_est` in `--json`, computed from a
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
Cache-column increment (2026-11-15): opt-in `--cache` adds one combined
`CACHE` column (read+write) between OUT and TOTAL in the session-family
views only; informational — TOTAL stays IN+OUT, `--json` gains no field
(both classes already carried), rejected for reconcile/--from-unsloth/
--from-runs (no cache split there); primitives `annotate_cache`/`with_cache`.
Store `usage.db` holds full recoverable history: ~128 OpenCode sessions,
~5,160 `opencode_steps` rows (one per ingested assistant message — see the
turn-level increment below), 2,539 Unsloth request events, and 5 generation
runs back to 2026-11-06 (counts from the 2026-11-17 verification pass; the
source is live and grows between runs).

Default-dashboard increment (2026-11-15): bare run renders hero + top-5-by-share
model panel + 5 most-recent sessions (newest first); trigger is a truly bare
invocation (`--cost`/`--cache` overlays allowed; `--json` and every flagged view
unchanged); SHARE computed over all models then truncated to 5. Two stale pins
updated per spec: CliCacheTests bare+--cache header pin (line-0 → whole-output
regex) and CliShareTests bare leg dropped (bare now shows SHARE by design).

Neon Midnight theme increment (2026-11-17): the sunset palette is retired —
`NEON` dict (renamed from `SUNSET`) is the single theme: near-black indigo
chrome (border `(76,58,120)`, track `(30,23,48)`, label `(169,156,201)`,
num `(244,240,255)`, accent `(232,121,249)`), IN `(96,165,250)`, OUT
`(244,114,182)`, CACHE lime `(163,230,53)`, plus two new roles: `ttotal`
violet `(167,139,250)` — the gradient midpoint — on the TOTAL column (bold)
and the hero grand total, and `tshare` teal `(94,234,212)` on the SHARE
column and the hero row-count stat (derived stats share the hue). Bar
mechanism unchanged (per-glyph `█`+`░▒▓` blocks); `TOKEN_IN_RGB`/`TOKEN_OUT_RGB`
now DERIVE from `NEON["tin"]/["tout"]` and `_token_rgb` lerps saturation as
well as hue (UP sweep, blue→violet→pink) and lightness — forcing S=1 only
round-tripped the old fully-saturated endpoints. Piped/`NO_COLOR` output
remains byte-plain (pinned). Spec + plan in docs/superpowers/{specs,plans}/
2026-11-16-neon-midnight-theme*.

Aggregate TOK/S increment (2026-11-17): daily/total aggregates stopped being
"tokens over the group's calendar span" and became the **token-weighted mean
of the rates of the rows they fold up** (`_weighted_mean_rate` over
`_session_rate_groups` / `_run_rate_groups`, which reuse
`query_sessions`/`query_runs`' own formula, so an aggregate stays reproducible
from the rates displayed above it). The old denominator was
`MIN(created)→MAX(updated)` across the group, which counts every idle second
between and inside sessions: `--model` for a model whose only long session
sat open overnight read 2.4 tok/s while its four session rows read
1.8/595/118/77. Weight = the row's GENERATED tokens (`tokens_generated` =
output+reasoning summed over the session's step rows, or `progress_tokens`
for runs) — revised 2026-11-17 in the turn-level increment from the old
`tokens_input+tokens_output` so the weight matches the rate's own numerator;
as a result the hero over any session-family table reproduces the pooled grand
total to 0.1. The biggest session dominates: that same
model reads 40.6 weighted vs 197.9 as a flat mean — the human partner chose
weighted knowing this. Rows with no rate (session with no valid turn,
unfinished/failed run) or no tokens drop out of BOTH sides of the average; a
group with nothing left renders blank. Applies to
`total_totals`/`daily_totals` and `total_runs`/`daily_runs`, and the hero
(`report_header`) now computes the same weighted mean over the rows it
summarizes and appends `· NNN.N tok/s` before the `--cost` tail — omitted
whole when no displayed row has a rate, so `--from-unsloth` per-event and
`reconcile` heroes are unchanged. Only hero NUMBERS are painted: every unit
word ("tokens", "in", "out", "tok/s", the row-count unit) stays in the
terminal's own color, so the teal row-count and magenta rate read like the
rest. The table TOK/S and SESSIONS columns wear those same accent/teal hues
(`tok_s` and `sessions` joined `_PAINT_CELLS`, the rate with a "rate" kind
formatted exactly as `render_table` would, so column widths never shift). The
event aggregates keep the created_at-span rate: per-event rows carry no
duration to weight (documented in their docstrings). `_aggregate_rate` is now
a per-row helper only — aggregate callers use `_weighted_mean_rate`. Spec:
docs/superpowers/specs/2026-11-17-token-weighted-tok-s-design.md.

Turn-level TOK/S increment (2026-11-17): new store table `opencode_steps`
(message_id PK, session_id, turn, step_ms, tokens_generated, time_created),
filled by `import_opencode_steps` inside `record()` — one row per OpenCode
assistant message. `turn` is a dense 0-based ordinal (first-appearance order of
the input boundary returned by the ported `_turn_index`), `step_ms` is the step
duration (streamed − firstToken, or streamed − created when firstToken is NULL,
NULL when the step never streamed) and `tokens_generated` = output+reasoning.
Session `tok_s` is now the UNWEIGHTED MEAN of the session's per-turn rates —
Σ(tokens_generated)*1000/Σ(step_ms) per turn (a turn is excluded when any step
never streamed, when it moved no tokens, or when its pooled span is < 1 s), then
averaged across the surviving turns — the number OpenCode shows per prompt; the
reference session
`ses_e14c64b0bffe3hKJ8qniK45xCS` reads 70.6 TOK/S (was 13.1 on the old
wall-clock basis). No wall-clock fallback: mixing the two bases is the bug
this replaces. Session and aggregate `rate_weight` is Σ generated tokens (see
the increment above); `--from-runs`/`--from-unsloth` are UNCHANGED — the event
aggregates still rate over their created_at span and runs over their own
wall-clock seconds. Coverage on live data (2026-11-17 verification pass): of
128 sessions in the store, 121 render a rate and 7 render blank — blank means
"no rateable turn / no usable step timing" (older sessions predating ingestion
have no step rows; the rest have step rows whose every turn is never-streamed,
sub-second, or zero-token), NOT zero speed. Verified reference discrepancy:
Σ`tokens_generated` over this session's step rows is 77,696 and Σ over
OpenCode's own 134 assistant message rows is exactly 77,696, while
`session_v2`'s `tokens_output+tokens_reasoning` is 77,951
(255-token delta) — OpenCode's own totals and its own message rows disagree,
so `rate_weight` is faithful to the message rows it is defined over (see Known
gotchas). Spec + plan in docs/superpowers/{specs,plans}/2026-11-17-turn-level-tok-s*.

Sessions-cap increment (2026-11-17): `--sessions[=N]` scopes the OpenCode
session views to the N most recent sessions (by creation time, newest
first). Semantics: a valueless `--sessions` means the 20 most recent in the
list view but ALL sessions in the aggregate views (an aggregate is a
summary by default and only a list needs a bound; `--total` alone already
IS the all-sessions view — the `--sessions=0` end of the same dial);
`--sessions=0` means all matching sessions in any view; an explicit N caps
every view the same way; `--daily`/`--total` without the flag are unchanged
and uncapped. The cap is ONE predicate inside `_session_filters`
(`session_id IN (SELECT session_id FROM usage <same where-params> ORDER BY
time_created DESC, session_id LIMIT ?)` — the cap takes the N most recent
of what survives the existing --model/--since/--until/threshold filters),
which `query_sessions`, `daily_totals`, `total_totals`, and the rate groups
all funnel through, so rows, aggregates, and the hero's TOK/S share one
scope and an aggregate can never total a different session set than the
rows on screen (None and 0 simply add no cap predicate — the uncapped case
is reached by omission, not a second code path). Alone (no other view flag)
it renders the capped rows newest first — the same `most_recent` recency
step the dashboard's panel shares — titled `sessions · N most recent` or
`sessions · all`; with a POSITIVE cap in force the aggregate titles say so
(`daily · last 3 sessions`,
`totals · last 3 sessions`,
`usage by model · sorted by share · last 3 sessions`) and
with no cap or a 0 cap every title stays byte-identical to today;
`hero_unit` values never change. Rejected (exit 2, after implicit
ingestion like the other guards) with `--from-unsloth`, `--from-runs`, and
`reconcile` — none of them are sessions — and for negative N (SQLite
degenerates a negative LIMIT to no-limit, so the CLI is the only rejection
point). The bare-run dashboard is UNCHANGED: its trigger stays a truly
bare invocation and its 5-session panel keeps its own title. Spec in
docs/superpowers/specs/2026-11-17-sessions-view-design.md, plan in
docs/superpowers/plans/2026-11-17-sessions-cap.md.

Cost-rework increment (2026-11-17): `--cost` and `--cost-tier` merged into ONE
option `--cost[=<slug>]` (argparse `nargs="?"`, `const="sonnet-5"` — the
`--sessions` pattern; `--cost-tier` removed; prefer the `=` form because
`--cost reconcile` swallows the command → exit 2). `PRICES` re-keyed to
specific model slugs with display `name`s (haiku-4.5, sonnet-5, opus-5,
gpt-5.1-codex-mini, gpt-5.1-codex, deepseek-v3.2, qwen-turbo, qwen-plus,
qwen-max; list prices verified 2026-11-17 — Claude re-pinned from Sonnet 4.6
to Sonnet 5 $2/$10, Opus 5 $5/$25; non-Anthropic vendors bill no cache-write
premium → `cache_write = in`), plus `FAMILY_ALIASES` mapping a family name to
the provider's MID-TIER model (claude→sonnet-5, codex/openai→gpt-5.1-codex,
deepseek→deepseek-v3.2, qwen→qwen-plus; two-model lineups: the standard model
is the mid), resolved once in `main` via `resolve_cost_tier`. The COST header
is built per-run by `cost_column(tier)` and names the pinned model on ONE
line (`COST · Claude Sonnet 5 Prices`) — a stale price table is visible and
the head stays one line tall (an earlier two-line wrap was reverted: the
floating first line read as a gap above the rule). `render_table` still
splits header names on `"\n"` if a column ever wants a wrap. Table head
increment (human decision, same day):
header text BOTTOM-left aligns and every table gets a `─` rule between the
header block and the data (`header_height`/`paint_header_block`; rule wears
the border hue in color) — flag-less table output gained that one rule line.
Cost
cells render `$X.XX` (2 decimals, human decision; sub-cent rows render
`$0.00`). New `--real-cost` adds a `REAL COST` column from the stored `cost`
field (the real USD OpenCode reported; rows already carry it — pure display,
`--json` shape unchanged), coexists with `--cost` (estimate column first,
real after), and adds a `· $X.XX` hero tail (no tilde — it is real) summed
over ALL sessions on the dashboard; rejected with `--from-unsloth`/
`--from-runs` (their stored cost is always 0.0) and `reconcile`. Spec + plan
in docs/superpowers/{specs,plans}/2026-11-17-cost-rework*.

Custom themes increment (2026-11-17): the one built-in palette became ten
selectable ones — `THEMES` maps slug → (display name, colors) for
`neon-midnight` (`NEON` minus the legacy `track`), `tokyo-night`, `dracula`, `nord`,
`gruvbox-dark`, `solarized-dark`, `monokai`, `one-dark`, `solarized-light` and
`paper` (the last two for light terminals). `THEME_ROLES` is the nine-role schema
(`border`, `accent`, `label`, `num`, `tin`, `tout`, `tcach`, `ttotal`, `tshare`);
`track` is LEGACY — it stays in `NEON` (a test pins the key set), is read by
nothing, and is not in the file schema. `main` resolves ONE theme (`resolve_theme`)
and threads it explicitly through `report_header`/`panel`/`_table_panel`/
`paint_cells`/`paint_bars`/`paint_header_block` — no mutable module state — and
the bar gradient takes its HLS endpoints from the theme's `tin`/`tout`
(`gradient_stops`); `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` are now only Neon Midnight's
endpoints. CLI: `--theme=SLUG` paints one run (a display overlay like `--cost`,
so the bare dashboard still renders), bare `--theme` lists the themes and marks
the default, `--theme-default=SLUG` saves the default into `themes/config.json`
(`{"default": "<slug>"}`, written by `write_default_theme`, read every run by
`read_default_theme`; bare `--theme-default` saves the same run's `--theme=SLUG`,
and a save wins over a list when both flags are given). Both actions
short-circuit before implicit ingestion and never touch SQLite. Precedence:
`--theme` > `themes/config.json` > Neon Midnight, and the two failure modes split
on purpose: an explicit `--theme=<unknown>` is exit 2 naming the slug and
listing the slugs, while a saved default that no longer resolves warns naming
`themes/config.json` and renders in Neon Midnight (a cosmetic default never costs
a report). `themes/` is scanned every run by `scan_themes`: one `.json` per
theme, filename stem = slug, a folder file shadows a built-in of the same slug,
a file named exactly `config.json` is skipped (it is the saved default, not a
theme), and a file that fails to parse or validate is skipped with a one-line
stderr warning — so an explicitly named broken slug reads as an unknown theme,
and `--theme`'s list prints the reason. Layout: `themes/*.json` and
`themes/config.json` are gitignored (personal state); `themes/README.md` is the
tracked format reference, so a fresh clone has the folder holding only that doc
and the ten palettes live in `tokenstat.py`.
Neon Midnight remains the default and flag-off output stays byte-identical
(existing pins untouched); piped/`NO_COLOR`/`--json` output stays byte-plain
under every theme. Spec + plan in docs/superpowers/{specs,plans}/
2026-11-17-custom-themes*.

## Commands (run from this directory)

```bash
python3 tokenstat.py                                   # snapshot both + compact dashboard (top-5 models, 5 newest sessions)
python3 tokenstat.py --model unsloth --since 2026-11-01 --until 2026-11-13
python3 tokenstat.py --min-in 10000 --daily            # thresholds apply to daily mode too
python3 tokenstat.py --total                           # all-time grand total, one row
python3 tokenstat.py --total --model unsloth           # all-time totals, one row per model
python3 tokenstat.py --total --model                   # ...for ALL models (bare --model)
python3 tokenstat.py --model                           # same as --total --model (shortcut)
python3 tokenstat.py --sessions                        # list the 20 most recent sessions, newest first
python3 tokenstat.py --sessions=10 --daily             # daily aggregates over the 10 most recent sessions
python3 tokenstat.py --from-unsloth [--daily|--json]   # Unsloth's own per-request log
python3 tokenstat.py --from-runs [--daily|--total]     # Studio runs, TRUE generation tok/s
python3 tokenstat.py --cost[=<slug>]                   # reference $ overlay (default sonnet-5; slugs + family aliases)
python3 tokenstat.py --total --real-cost               # stored real USD column (session views only)
python3 tokenstat.py --total --model --cache           # combined cache column (session views only)
python3 tokenstat.py --theme[=SLUG]                    # list themes / paint this run (display-only overlay)
python3 tokenstat.py --theme-default=SLUG              # save the default theme in themes/config.json
python3 tokenstat.py reconcile                         # lost-history view per model
python3 tokenstat.py record | import-unsloth           # explicit snapshots (import-unsloth = events + runs; exit 1 if source unreadable)
python3 -m unittest test_tokenstat -v                  # tests (~0.15s, no deps)
python3 -W error::ResourceWarning -m unittest test_tokenstat   # strict variant: must stay warning-clean
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
   monotonic `max`, never an increment; rows are never deleted. Same for the
   step snapshot: `opencode_steps` has one row per `message_id`, with
   `step_ms`/`tokens_generated` kept at the monotonic max and rows never
   deleted, so ingested steps survive OpenCode pruning `session_message`
   (a NULL `step_ms` means "never streamed" and never maxes against a real
   duration). Re-running any import is a provable no-op (the numeric token
   columns and step rows are unchanged; `last_seen` is the one field that
   moves, and it is not a token count). Reports always SUM over distinct keys.
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
- **Source-level discrepancy, not our bug (verified 2026-11-17):** for the
  reference session, Σ`tokens_generated` over our step rows is 77,696 and Σ
  over OpenCode's own assistant message rows is also exactly 77,696, while
  `session_v2`'s `tokens_output+tokens_reasoning` is 77,951 — a 255-token
  delta in OpenCode's own data (its per-session totals disagree with the sum
  of its own message rows). `rate_weight` is faithful to the message rows it
  is defined over; do not "correct" it to the 77,951 total.
- Deferred perf pass (verified safe to postpone): the near-duplicate
  aggregate queries (`total_events`/`total_totals`, `daily_events`/
  `daily_runs`/`daily_totals`) are not deduplicated, and the per-row
  SELECT+UPDATE ingest loops are O(N) full rewrites each run (~0.4s per
  100k rows; fine for years at current volume).
- `--from-unsloth` renders with session-shaped columns (blank SESSION); a
  dedicated event column set is unclaimed polish (`--from-runs` has its own
  RUNS_COLUMNS). Displayed tables have no COST column by default; opt-in
  `--cost` adds a REFERENCE estimate column (header names the pinned model);
  the stored real cost is displayed only by opt-in `--real-cost` (and always
  was in `--json`).
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
