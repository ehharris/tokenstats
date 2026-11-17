# tokenstat — permanent AI token usage record for this machine

`tokenstat` snapshots AI token usage from two sources into its own SQLite
store (`usage.db` next to the script), and reports it filtered by model,
date, and token counts: OpenCode's session database
(`~/.local/share/opencode/opencode.db`) and Unsloth Studio's per-request
event log plus generation runs (`~/.unsloth/studio/studio.db`), both opened
read-only. OpenCode prunes its own history over time; your copy in
`usage.db` never shrinks, so this machine's full token history survives.

Single Python file, stdlib only — no install step:

```bash
python3 tokenstat.py [filters]      # snapshot both sources + report
python3 tokenstat.py record         # snapshot OpenCode only
python3 tokenstat.py import-unsloth # snapshot Unsloth Studio only (events + runs)
python3 tokenstat.py reconcile      # per-model: sessions vs Unsloth's log, gaps
```

## Examples

```bash
# Everything recorded, newest data included automatically:
python3 tokenstat.py

# Only Unsloth-backed models:
python3 tokenstat.py --model unsloth

# A date range (both days inclusive):
python3 tokenstat.py --since 2026-11-01 --until 2026-11-13

# Heavy sessions only — at least 10k tokens in or 5k out:
python3 tokenstat.py --min-in 10000 --min-out 5000

# Per-day totals instead of per-session rows:
python3 tokenstat.py --daily --model unsloth

# All-time grand total in one row (add --model for a per-model breakdown):
python3 tokenstat.py --total
python3 tokenstat.py --total --model unsloth

# Per-model totals across ALL models (--model with no value; the bare
# --model is also a shortcut that implies --total):
python3 tokenstat.py --total --model
python3 tokenstat.py --model

# Machine-readable (rows carry `tok_s`; with --cost every row also gains
# `cost_est`; run rows and run aggregates carry `progress_tokens`; --total
# rows carry `share`, the row's percent of the overall displayed total):
python3 tokenstat.py --json | jq '.[].total'

# Snapshot without querying:
python3 tokenstat.py record

# Unsloth's own per-request log (IN = prompt incl. cache hits, OUT = completion):
python3 tokenstat.py --from-unsloth --model unsloth --since 2026-11-01
python3 tokenstat.py --from-unsloth --daily

# Unsloth's generation runs — TOK/S here is each run's TRUE generation rate
# (progress tokens over the run's own wall-clock seconds; IN carries the
# progress total and OUT renders 0 — runs have no in/out split):
python3 tokenstat.py --from-runs
python3 tokenstat.py --from-runs --daily
python3 tokenstat.py --from-runs --total        # per-model, with SHARE/BAR

# Opt-in dollar overlay (off by default; see "Cost estimates" below):
python3 tokenstat.py --total --model --cost
python3 tokenstat.py --from-runs --cost --cost-tier opus

# Opt-in cache column between OUT and TOTAL (see "Cache tokens" below):
python3 tokenstat.py --total --model --cache

# Lost-history view: what Unsloth recorded that OpenCode no longer can:
python3 tokenstat.py reconcile
```

Per-model `--total` reports end with two extra columns: `SHARE`, the row's
percent of the overall total across the displayed rows, and `BAR`, a smooth
Unicode bar (full blocks `█` plus one shade block `░▒▓` for the fraction —
deliberately no eighth-block glyphs like `▁`, whose different vertical
metrics rag the bottom edge in many fonts) padded with a space on either
side and scaled to the largest row so the mix is visible at a glance. On a
terminal the same glyph bar is additionally colored truecolor, one color per
glyph: a blue→orange gradient whose transition sits at the row's
input/output split (runs in `--from-runs` have no in/out split, so their
bars ride the pure input-blue end, like any other all-input row). Piped
output, `NO_COLOR`, and `--json` render the same bar without the coloring —
exactly as before. Rows sort by `SHARE` descending —
biggest share first, ties alphabetical by model — in both the table and
`--json`; session and daily views keep their chronological order.

Terminal output also wears the sunset theme. Above the table a hero header
panel summarizes the report — grand total tokens, row count, the in/out
split in compact form (`7.77M`, `12.3k`), and the date range — and the table
itself sits in a rounded border panel titled `tokenstat · <view>` (for
example `tokenstat · sessions` or `tokenstat · daily`). The table header row
is dimmed, and the token columns are colored by class: `IN` blue, `OUT`
orange, `CACHE` amber, `TOTAL` bold, `TOK/S` magenta (the hero's TOK/S stat
wears that same hue). `reconcile` gets the panel but no hero
header — its rows carry no summed token classes. All of this is decoration
only: `NO_COLOR` strips the colors and `--json` stays plain. Flagged views
print a plain table when their output is piped; the bare dashboard keeps its
panel framing even down a pipe (without color).

A bare run renders a compact **dashboard** instead of one table: a hero
panel with grand totals over all sessions, a `usage by model · top 5 by
share` panel (shares computed over ALL models before truncating to five),
and a `sessions · 5 most recent` panel (newest first). Any view/filter flag
restores the ordinary full table; `--json` always emits every session row.

Every report table except `reconcile` also has a `TOK/S` column. A session
row's is a **wall-clock average** — its tokens divided by its own time span
(session creation → last update) — so idle time inside that window drags the
number down; it is blank when the span is unknown or under a second. Daily
and total rows are the **token-weighted mean** of the rates of the rows they
fold up: each row counts in proportion to the tokens it moved, so a session
that carried 200k tokens speaks 200x one that carried 1k, and the idle time
between sessions never enters the number. Rows with no rate, or no tokens,
are left out of both sides of that average, and a group where none has one
renders blank. The hero line carries the same token-weighted TOK/S over the
rows it summarizes (`· 157.1 tok/s`), so hero and table always agree; it is
omitted when none of those rows has a rate. Per-event rows in
`--from-unsloth` carry no duration, so their TOK/S is blank (their daily and
total aggregates rate over the events' created_at range instead). Only
`--from-runs` measures generation speed: there TOK/S is each run's *true*
rate — progress tokens over the run's own wall-clock seconds — and its
aggregates weight those true rates by progress tokens, which is why those
numbers typically sit well above session-mode averages. In `--json` the value
is the float `tok_s` (null when blank).

## Cost estimates (`--cost`)

`--cost` adds a `COST` column (formatted `$X.XXXX`) and, under `--json`, a
`cost_est` field to any report except `reconcile`, which rejects the flag.
The number is a **reference estimate**, not money spent: all tracked traffic
is priced at one Anthropic Claude tier's list prices — Sonnet 4.6 at $3/Mtok
input / $15/Mtok output by default, switchable with `--cost-tier
haiku|sonnet|opus` (Haiku 4.5 $1/$5, Opus 4.8 $5/$25; cache reads at 0.1x
and cache writes at 1.25x each tier's input price) — as of 2026-11-14. Most
tracked models are not Claude, so read it as "what this volume would cost at
this tier," never as a bill. It is deliberately distinct from the `cost`
OpenCode actually records and stores (still stored, still emitted by
`--json`, no longer displayed). Row math follows the row shape: session-family
rows price all four token classes (input + output + cache read/write); event
rows price prompt + completion — since Unsloth's prompt includes KV-cache
hits, that over-estimates the input side; run rows price progress tokens as
output-only. The overlay is display/JSON only — nothing stored changes — and
flag-less output is exactly as before.

## Cache tokens (`--cache`)

`--cache` adds one `CACHE` column between `OUT` and `TOTAL` in the OpenCode
session views (sessions, `--daily`, `--total`): the combined cache-read +
cache-write tokens OpenCode records per session. It is **informational** —
`TOTAL` stays `IN + OUT`, so every existing number is unchanged — and it
explains the gap to Unsloth's counters: agentic sessions re-send their whole
history every request, those re-sends land in `CACHE`, and `IN + CACHE` is
roughly what Unsloth logs as each request's prompt. `--json` already carries
the two classes separately (`tokens_cache_read`, `tokens_cache_write`) and
gains no new field. The flag is rejected (exit 2) for `reconcile`,
`--from-unsloth`, and `--from-runs`: those sources fold cache hits into
prompt totals with no split to show.

The reports (session mode, `--from-unsloth`, `--from-runs`) describe the
*same* traffic from different angles — never add them together. In
`reconcile`, a
positive gap means usage only Unsloth still records (OpenCode pruned it);
a negative gap means OpenCode counted tokens Studio's log doesn't contain
(e.g. Studio rotated its log, or a request was retried client-side).

Every query snapshots both sources first (OpenCode's database and Unsloth
Studio's `api_usage_events` + `chat_generation_runs`), so reports are always
current. If either
database is missing or locked, `tokenstat` prints a warning and reports
from your existing store instead of failing. Override paths with `--src`,
`--unsloth-db`, and `--db`.

## No double counting

Each OpenCode session occupies exactly one row in the store, keyed by its
session id, and every token column holds the *latest observed total* for
that session — never an increment. Re-recording identical data is a no-op;
a session that grew between snapshots replaces its old totals rather than
adding to them; and if OpenCode ever rewrites a row smaller, the stored
values only ever go up. `record` reports `inserted=N updated=N unchanged=N`
so you can see this happen: repeated runs show everything `unchanged`.
The same guarantee applies to imported Unsloth events (keyed by their own
event ids) and generation runs (keyed by run id, with `progress_tokens` a
monotonic max).

One mapping caveat: Unsloth's `prompt_tokens` counts every prompt token it
processed, including KV-cache hits; OpenCode splits those into
`tokens_input` vs `tokens_cache_read`. So compare an Unsloth prompt total to
OpenCode's input + cache columns — which is exactly what `reconcile` does.

## Keeping history complete

Because queries snapshot both sources implicitly, using the tool keeps
history current for free. To guarantee snapshots even in weeks without a
query, schedule an hourly run: add to crontab (`crontab -e`):
`0 * * * * /usr/bin/python3 $HOME/messinaround/token_counter/tokenstat.py record >> $HOME/messinaround/token_counter/record.log 2>&1; /usr/bin/python3 $HOME/messinaround/token_counter/tokenstat.py import-unsloth >> $HOME/messinaround/token_counter/record.log 2>&1`

## Tests

```bash
python3 -m unittest test_tokenstat -v
```
