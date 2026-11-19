# tokenstats — permanent AI token usage record for this machine

`tokenstats` snapshots OpenCode's session database (opened read-only) into its
own SQLite store (`~/.tokenstats/usage.db`) and reports it filtered by model,
date, and token counts. OpenCode prunes its own history over time; your copy in
`usage.db` never shrinks, so this machine's full token history survives.

Single Python file, stdlib only. It runs on Linux, macOS, and Windows and finds
OpenCode's database automatically for your OS.

## Install

```bash
pipx install .         # recommended: a `tokenstats` command on PATH
# or
pip install --user .   # the same command, into your user site
```

Either installs a `tokenstats` console command (Linux, macOS, and Windows).
Installing is optional — the script also runs straight from the file:

```bash
python3 tokenstats.py [filters]   # snapshot OpenCode + report
python3 tokenstats.py record      # snapshot OpenCode only
```

## Where things live

Everything tokenstats owns sits in one directory, `~/.tokenstats/`:

```
~/.tokenstats/usage.db             the store (plus its -wal / -shm siblings)
~/.tokenstats/themes/              your theme files, read by --theme
~/.tokenstats/themes/config.json   the default saved by --theme-default
~/.tokenstats/record.log           the scheduler log (see "Keeping history complete")
```

Nothing is created until a run needs the store, so the script itself can sit
anywhere — including a read-only directory. It reads one database it does not
own (OpenCode's), opened read-only. A store or theme files left beside an older
copy of the script are moved into `~/.tokenstats/` on the first run, which says
so on stderr (`migrated store: <old> -> <new>`).

## Finding OpenCode's database

`tokenstats` probes these locations in order and uses the first that exists, so
you rarely pass a path:

1. `$OPENCODE_DB` (when it is an absolute path)
2. `$XDG_DATA_HOME/opencode/opencode.db` (when `XDG_DATA_HOME` is absolute)
3. `~/.local/share/opencode/opencode.db` (the XDG default, on every OS)
4. macOS — `~/Library/Application Support/opencode/opencode.db`
5. Windows — `%APPDATA%\opencode\opencode.db`
6. legacy `~/.opencode/opencode.db`

With none present it reports the `.local/share` default. `--src PATH` overrides
all of it.

## Examples

```bash
# Everything recorded, newest data included automatically:
python3 tokenstats.py

# Only one model/provider's traffic (substring match on provider/model):
python3 tokenstats.py --model qwen

# A date range (both days inclusive):
python3 tokenstats.py --since 2026-11-01 --until 2026-11-13

# Heavy sessions only — at least 10k tokens in or 5k out:
python3 tokenstats.py --min-in 10000 --min-out 5000

# Per-day totals instead of per-session rows:
python3 tokenstats.py --daily --model qwen

# All-time grand total in one row (add --model for a per-model breakdown):
python3 tokenstats.py --total
python3 tokenstats.py --total --model qwen

# Per-model totals across ALL models (--model with no value; the bare
# --model is also a shortcut that implies --total):
python3 tokenstats.py --total --model
python3 tokenstats.py --model

# The 10 most recent sessions — alone it lists them newest first, and with
# --daily/--total it caps the session set those aggregates cover:
python3 tokenstats.py --sessions=10
python3 tokenstats.py --sessions=10 --total --model

# Machine-readable (rows carry `tok_s`; with --cost every row also gains
# `cost_est`; --total rows carry `share`, the row's percent of the overall
# displayed total):
python3 tokenstats.py --json | jq '.[].total'

# Snapshot without querying:
python3 tokenstats.py record

# Opt-in dollar overlays (off by default; see "Cost estimates" below):
python3 tokenstats.py --total --model --cost
python3 tokenstats.py --total --real-cost        # stored real USD, not an estimate

# Opt-in cache column between OUT and TOTAL (see "Cache tokens" below):
python3 tokenstats.py --total --model --cache

# Paint this run with a theme — one of the ten built-ins or your own
# ~/.tokenstats/themes/SLUG.json (see themes/README.md); bare --theme lists them:
python3 tokenstats.py --theme=nord
python3 tokenstats.py --theme                    # list slugs + mark the default
```

Per-model `--total` reports end with two extra columns: `SHARE %`, the row's
percent of the overall total across the displayed rows, and `SHARE`, a smooth
Unicode bar (full blocks `█` plus one shade block `░▒▓` for the fraction —
deliberately no eighth-block glyphs like `▁`, whose different vertical
metrics rag the bottom edge in many fonts) padded with a space on either
side and scaled to the largest row so the mix is visible at a glance (the
bar's length is the row against the LARGEST row, not the percent tick). On a
terminal the same glyph bar is additionally colored truecolor, one color per
glyph: a gradient from the theme's `tin` into its `tout` whose transition
sits at the row's input/output split. Piped output, `NO_COLOR`, and `--json`
render the same bar without the coloring — exactly as before. Rows sort by
`SHARE` descending — biggest share first, ties alphabetical by model — in
both the table and `--json`; session and daily views keep their chronological
order.

Terminal output is painted by a theme. Above the table a hero header panel
summarizes the report — grand total tokens, row count, the in/out split in
compact form (`7.77M`, `12.3k`), and the date range — and the table itself sits
in a rounded border panel titled `tokenstats · <view>` (for example
`tokenstats · sessions` or `tokenstats · daily`). The table header row is dimmed,
and the token columns are colored by class: `IN` and `OUT` wear the theme's
input/output pair, `CACHE` its cache hue, `TOTAL` its total hue in bold, `TOK/S`
the accent and `SESSIONS` the share hue (the hero's TOK/S and row-count stats
wear those same hues). All of this is decoration only: `NO_COLOR` strips
the colors and `--json` stays plain. Flagged views print a plain table when
their output is piped; the bare dashboard keeps its panel framing even down a
pipe (without color).

Ten themes ship built in: `neon-midnight` (the default — indigo chrome, blue
`IN` / pink `OUT`, lime cache, violet totals, teal derived stats),
`tokyo-night`, `dracula`, `nord`, `gruvbox-dark`, `solarized-dark`, `monokai`,
`one-dark`, and two for light terminals, `solarized-light` and `paper`.
`--theme=SLUG` paints one run — a display overlay like `--cost`, so
`--theme=nord` still renders the dashboard — and bare `--theme` lists the slugs
and marks the default. `--theme-default=SLUG` saves one into
`~/.tokenstats/themes/config.json` for every later run (bare `--theme-default`
saves the `--theme=SLUG` of the same invocation); precedence is `--theme` >
`~/.tokenstats/themes/config.json` > Neon Midnight. An unknown `--theme=SLUG`
exits 2 and lists what is available, while a saved default that no longer
resolves warns naming `~/.tokenstats/themes/config.json` and renders in Neon
Midnight rather than costing you the report. Your own themes are one `.json`
each in `~/.tokenstats/themes/` — filename stem = slug, and a file shadows a
built-in of the same slug; `themes/README.md` is the format reference. The
repo's `themes/` folder is documentation only — its format README and
`examples/` — and is not read at runtime. Color is all a theme touches: piped
output, `NO_COLOR`, and `--json` stay byte-plain under every theme, and
flag-less output is byte-identical to the tool from before themes existed.

A bare run renders a compact **dashboard** instead of one table: a hero
panel with grand totals over all sessions, a `usage by model · top 5 by
share` panel (shares computed over ALL models before truncating to five),
and a `sessions · 5 most recent` panel (newest first). Any view/filter flag
restores the ordinary full table; `--json` always emits every session row.

Every report table has a `TOK/S` column. A session row's is the **mean of its
per-turn rates** — the number OpenCode shows per prompt: for each turn,
Σ(output+reasoning tokens) divided by the turn's total stream time in seconds
(each step's `streamed − firstToken`, or `streamed − created` when `firstToken`
is NULL, in milliseconds), then averaged across the session's turns. A turn is
skipped when any of its steps never streamed, when it generated no tokens, or
when its pooled stream time is under a second; a session with no surviving turn
renders blank. There is deliberately no wall-clock fallback. Daily and total
rows are the **token-weighted mean** of the rates of the rows they fold up,
weighted by **generated tokens** (`tokens_generated` = output+reasoning over a
session's step rows): each row counts in proportion to the tokens it actually
generated, so a session that carried 200k generated tokens speaks 200x one that
carried 1k, and the idle time between sessions never enters the number. Because
the weight matches the rate's own numerator, the hero over any session-family
table reproduces the pooled grand total to 0.1. Rows with no rate, or no
generated tokens, are left out of both sides of that average, and a group where
none has one renders blank. The hero line carries the same token-weighted TOK/S
over the rows it summarizes (`· 70.2 tok/s`), so hero and table always agree; it
is omitted when none of those rows has a rate. In `--json` the value is the
float `tok_s` (null when blank); session-family rows also carry `rate_weight`,
the sum of the generated tokens of the rows that contributed to their rate.

## Session scope (`--sessions`)

`--sessions` lists the **20 most recent sessions**, newest first;
`--sessions=50` lists 50. With `--daily` or `--total` (bare `--model`
counts — it is the `--total` shortcut unless `--daily` is set) the same
cap instead narrows the session set the aggregate covers, so the
aggregate describes exactly those sessions and nothing else — the table
title says so (`daily · last 3 sessions`, `totals · last 3 sessions`,
`usage by model · sorted by share · last 3 sessions`). A valueless
`--sessions` means **all** sessions in the
aggregate views — an aggregate is a summary by default, so `--total` and
`--daily` alone stay the everything views — and `--sessions=0` means all
matching sessions in any view. Existing filters (`--model`, `--since`,
`--until`, `--min-in`, `--min-out`) narrow the candidate set first; the cap
then takes the N most recent of what survives. `--json` emits the displayed
rows, newest first in the list view. A negative N is rejected (exit 2). The
bare-run dashboard (top-5 models, 5 most-recent sessions) is unchanged.

## Cost estimates (`--cost`)

`--cost[=TIER]` adds a `COST` column (formatted `$X.XX`) and, under `--json`,
a `cost_est` field to any report.
Bare `--cost` means `--cost=sonnet-5`. The column header names the model the
pricing is pinned to — `COST · Claude Sonnet 5 Prices` — so a stale price
table is visible. Header text bottom-left aligns and every table separates
its header block from the data with a `─` rule. `TIER` is a model slug
(`haiku-4.5`, `sonnet-5`,
`opus-5`, `gpt-5.1-codex-mini`, `gpt-5.1-codex`, `deepseek-v3.2`,
`qwen-turbo`, `qwen-plus`, `qwen-max`) or a family name that resolves to the
provider's mid-tier model (`claude`→sonnet-5, `codex`/`openai`→gpt-5.1-codex,
`deepseek`→deepseek-v3.2, `qwen`→qwen-plus). Prefer the `--cost=TIER` form:
the space form can swallow a following command.

The number is a **reference estimate**, not money spent: all tracked traffic
is priced at one model's list prices (verified 2026-11-17; Anthropic bills
cache reads at 0.1x and cache writes at 1.25x input, the other vendors bill
no cache-write premium). Most tracked models are not the pinned one, so read
it as "what this volume would cost at this tier," never as a bill. Session
rows price all four token classes (input + output + cache read/write). The
overlay is display/JSON only — nothing stored changes — and flag-less output
is exactly as before.

## Real cost (`--real-cost`)

`--real-cost` adds a `REAL COST` column with the **stored** `cost` — the real
USD OpenCode reported for that traffic — as opposed to `--cost`'s reference
estimate. The two coexist (estimate column first, real column after), and the
dashboard hero gains a `· $X.XX` tail (no tilde: it is real). The value is
the one `--json` has always carried, so `--json` output is unchanged. Only
OpenCode sessions record real cost. Free-tier traffic honestly renders
`$0.00`.

## Cache tokens (`--cache`)

`--cache` adds one `CACHE` column between `OUT` and `TOTAL` in the OpenCode
session views (sessions, `--daily`, `--total`): the combined cache-read +
cache-write tokens OpenCode records per session. It is **informational** —
`TOTAL` stays `IN + OUT`, so every existing number is unchanged — and it
explains why `IN` alone undercounts an agentic session: those re-send their
whole history every request, those re-sends land in `CACHE`, and `IN + CACHE`
is the true prompt volume. `--json` already carries the two classes separately
(`tokens_cache_read`, `tokens_cache_write`) and gains no new field.

Every query snapshots OpenCode first, so reports are always current. If the
database is missing or locked, `tokenstats` prints a warning and reports from
your existing store instead of failing. `--src` and `--db` still override those
paths, and `--min-in` and `--min-out` still work: all four are fully functional
but deliberately left out of `--help`, which lists only what a normal run needs
(a fifth, the retired `--cost-tier`, only points at `--cost=<slug>`).

## No double counting

Each OpenCode session occupies exactly one row in the store, keyed by its
session id, and every token column holds the *latest observed total* for
that session — never an increment. Re-recording identical data is a no-op;
a session that grew between snapshots replaces its old totals rather than
adding to them; and if OpenCode ever rewrites a row smaller, the stored
values only ever go up. `record` reports `inserted=N updated=N unchanged=N`
so you can see this happen: repeated runs show everything `unchanged`.
The same guarantee applies to the per-message `opencode_steps` snapshot
(keyed by `message_id`; `step_ms`/`tokens_generated` are monotonic maxes and
rows are never deleted, so ingested steps survive OpenCode pruning its own
messages).

## Keeping history complete

Because queries snapshot OpenCode implicitly, using the tool keeps history
current for free. To guarantee snapshots even in weeks without a query,
schedule an hourly `record`, logging into the state directory.

**Linux / macOS** — add to `crontab -e` (point the path at wherever the script
lives):

```cron
0 * * * * /usr/bin/python3 $HOME/path/to/tokenstats.py record >> $HOME/.tokenstats/record.log 2>&1
```

**Windows** — create an hourly task with Task Scheduler (`schtasks`), again
pointing at the real script path:

```bat
schtasks /Create /SC HOURLY /TN tokenstats /TR "python %USERPROFILE%\path\to\tokenstats.py record"
```

## Tests

```bash
python3 -m unittest test_tokenstats -v
```
