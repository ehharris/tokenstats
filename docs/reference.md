# tokenstats — reference

Everything the [README](../README.md) leaves out: where state lives, how the
OpenCode database is found, every option (including the hidden ones), how each
number is computed, and what the tool guarantees.

## Where things live

Everything tokenstats owns sits in one directory, `~/.tokenstats/`:

```
~/.tokenstats/usage.db             the store (plus its -wal / -shm siblings)
~/.tokenstats/themes/              your theme files, read by --theme
~/.tokenstats/themes/config.json   the default saved by --theme-default
~/.tokenstats/record.log           the scheduler log (see "Scheduled snapshots")
```

Nothing is created until a run needs the store, so the script itself can sit
anywhere — including a read-only directory. It reads one database it does not
own (OpenCode's), opened read-only. A store or theme files left beside an older
copy of the script are moved into `~/.tokenstats/` on the first run, which
says so on stderr (`migrated store: <old> -> <new>`).

## Finding OpenCode's database

`tokenstats` probes these locations in order and uses the first that exists,
so you rarely pass a path:

1. `$OPENCODE_DB` (when it is an absolute path)
2. `$XDG_DATA_HOME/opencode/opencode.db` (when `XDG_DATA_HOME` is absolute)
3. `~/.local/share/opencode/opencode.db` (the XDG default, on every OS)
4. macOS — `~/Library/Application Support/opencode/opencode.db`
5. Windows — `%APPDATA%\opencode\opencode.db`
6. legacy `~/.opencode/opencode.db`

With none present it reports the `.local/share` default. `--src PATH`
overrides all of it.

## Full option list

| Option | Effect |
| --- | --- |
| `record` (command) | snapshot OpenCode now and exit; exit 1 if the source is unreadable |
| `--model [PATTERN]` | substring match on `provider/model` — literal (`%`/`_` are not wildcards). Bare `--model` means every model, and alone it is the shortcut for `--total --model` |
| `--since YYYY-MM-DD` / `--until YYYY-MM-DD` | date window, both days inclusive, bucketed by session creation day (local time, DST-safe) |
| `--daily` | per-day totals instead of per-session rows |
| `--total` | one all-time grand-total row; with `--model`, one row per model |
| `--sessions[=N]` | scope to the N most recent sessions (see below) |
| `--json` | machine-readable rows instead of a table |
| `--cost[=TIER]` | reference dollar overlay column (see below) |
| `--real-cost` | stored real USD column (see below) |
| `--cache` | combined cache-read + cache-write column (see below) |
| `--theme[=SLUG]` | bare lists themes; with SLUG paints this run |
| `--theme-default[=SLUG]` | save the default theme; bare saves this run's `--theme` |
| `--min-in N` / `--min-out N` | *(hidden)* threshold filters on session IN / OUT |
| `--db PATH` / `--src PATH` | *(hidden)* store and OpenCode source overrides |

The four hidden options are fully functional but deliberately left out of
`--help`, which lists only what a normal run needs. (A fifth, the retired
`--cost-tier`, only points at `--cost=<slug>`.) Prefer the `=` form for every
optional-value flag: `--cost record` swallows the command and exits 2.

## Views

- **Bare run → dashboard.** A truly bare invocation (no command, no view or
  filter flags; `--cost`/`--cache`/`--theme` overlays are allowed) renders a
  hero panel with grand totals, a `usage by model · top 5 by share` panel
  (shares computed over ALL models before truncating to five), and a
  `sessions · 5 most recent` panel. Any view/filter flag restores the ordinary
  full table; `--json` always emits every session row.
- **Sessions** (default table): one row per session, oldest first, with a
  `SESSION` title column.
- **`--daily`**: per-day aggregates, oldest day first.
- **`--total`**: one grand-total row; add `--model` (or use bare `--model`,
  the shortcut) for one row per model with SHARE columns.
- `--total` and `--daily` cannot be combined (exit 2).

Hero and tables share one stat order (human decision 2026-11-18): grand
total, TOK/S, row count, IN, OUT — then the opt-in `CACHE` (right after OUT)
and, last, the SHARE pair and cost columns. Row-label columns (`DATE`,
`MODEL`, `SESSION` title) stay at the edges.

### Session scope (`--sessions`)

`--sessions` lists the **20 most recent sessions**, newest first;
`--sessions=50` lists 50. With `--daily` or `--total` (bare `--model` counts —
it is the `--total` shortcut unless `--daily` is set) the same cap instead
narrows the session set the aggregate covers, so the aggregate describes
exactly those sessions and nothing else — the table title says so
(`daily · last 3 sessions`, `totals · last 3 sessions`). A valueless
`--sessions` means **all** sessions in the aggregate views — an aggregate is a
summary by default — and `--sessions=0` means all matching sessions in any
view. Existing filters (`--model`, `--since`, `--until`, `--min-in`,
`--min-out`) narrow the candidate set first; the cap then takes the N most
recent of what survives. `--json` emits the displayed rows, newest first in
the list view. A negative N is rejected (exit 2). The bare-run dashboard is
unchanged.

## How the numbers are computed

### Share columns

Per-model `--total` reports end with two extra columns: `SHARE %`, the row's
percent of the overall total across the **displayed** rows (so with
`--since`/`--until`/threshold filters it means "% of the filtered total"),
and `SHARE`, a smooth Unicode bar (full blocks `█` plus one shade block
`░▒▓` for the fraction — deliberately no eighth-block glyphs like `▁`, whose
different vertical metrics rag the bottom edge in many fonts) padded with a
space on either side and scaled to the largest row so the mix is visible at a
glance (the bar's length is the row against the LARGEST row, not the percent
tick). On a terminal the same glyph bar is additionally colored truecolor, one
color per glyph: a gradient from the theme's `tin` into its `tout` whose
transition sits at the row's input/output split. Piped output, `NO_COLOR`, and
`--json` render the same bar without the coloring. Rows sort by `SHARE`
descending — biggest share first, ties alphabetical by model — in both the
table and `--json`; session and daily views keep their chronological order.

### TOK/S

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
over the rows it summarizes (its second segment, right after the grand total:
`· 70.2 tok/s ·`), so hero and table always agree; it
is omitted when none of those rows has a rate. In `--json` the value is the
float `tok_s` (null when blank); session-family rows also carry `rate_weight`,
the sum of the generated tokens of the rows that contributed to their rate.

### Cost estimate (`--cost`)

`--cost[=TIER]` adds a `COST` column (formatted `$X.XX`) and, under `--json`,
a `cost_est` field to any report. Bare `--cost` means `--cost=sonnet-5`. The
column header names the model the pricing is pinned to —
`COST · Claude Sonnet 5 Prices` — so a stale price table is visible. `TIER` is
a model slug or a family name that resolves to the provider's mid-tier model
(`claude`→sonnet-5, `codex`/`openai`→gpt-5.1-codex, `deepseek`→deepseek-v3.2,
`qwen`→qwen-plus). Reference list prices, USD per million tokens, verified
2026-11-17:

| slug | model | in | out | cache read | cache write |
| --- | --- | ---: | ---: | ---: | ---: |
| `haiku-4.5` | Claude Haiku 4.5 | 1.00 | 5.00 | 0.10 | 1.25 |
| `sonnet-5` | Claude Sonnet 5 | 2.00 | 10.00 | 0.20 | 2.50 |
| `opus-5` | Claude Opus 5 | 5.00 | 25.00 | 0.50 | 6.25 |
| `gpt-5.1-codex-mini` | GPT-5.1-Codex-Mini | 0.25 | 2.00 | 0.025 | 0.25 |
| `gpt-5.1-codex` | GPT-5.1-Codex | 1.25 | 10.00 | 0.125 | 1.25 |
| `deepseek-v3.2` | DeepSeek V3.2 | 0.28 | 0.40 | 0.028 | 0.28 |
| `qwen-turbo` | Qwen-Turbo | 0.05 | 0.20 | 0.05 | 0.05 |
| `qwen-plus` | Qwen-Plus | 0.40 | 1.20 | 0.40 | 0.40 |
| `qwen-max` | Qwen-Max | 1.60 | 6.40 | 1.60 | 1.60 |

Anthropic bills cache reads at 0.1x and cache writes at 1.25x input; the other
vendors bill no cache-write premium. The number is a **reference estimate**,
not money spent: all tracked traffic is priced at one model's list prices, and
most tracked models are not the pinned one — read it as "what this volume
would cost at this tier," never as a bill. Session rows price all four token
classes (input + output + cache read/write). The overlay is display/JSON only
— nothing stored changes.

### Real cost (`--real-cost`)

`--real-cost` adds a `REAL COST` column with the **stored** `cost` — the real
USD OpenCode reported for that traffic — as opposed to `--cost`'s reference
estimate. The two coexist (estimate column first, real column after), and the
dashboard hero gains a `· $X.XX` tail (no tilde: it is real). The value is the
one `--json` has always carried, so `--json` output is unchanged. Only OpenCode
sessions record real cost. Free-tier traffic honestly renders `$0.00`.

### Cache tokens (`--cache`)

`--cache` adds one `CACHE` column after `OUT` in the OpenCode
session views (sessions, `--daily`, `--total`): the combined cache-read +
cache-write tokens OpenCode records per session, and a matching `· X cache`
hero segment after the `out` stat. It is **informational** —
`TOTAL` stays `IN + OUT`, so every existing number is unchanged — and it
explains why `IN` alone undercounts an agentic session: those re-send their
whole history every request, those re-sends land in `CACHE`, and `IN + CACHE`
is the true prompt volume. `--json` already carries the two classes separately
(`tokens_cache_read`, `tokens_cache_write`) and gains no new field.

### JSON shape

`--json` prints an array of the displayed rows. Session rows carry `date`,
`model`, `tokens_input`, `tokens_output`, `total`, `cost`, `title`,
`tokens_cache_read`, `tokens_cache_write`, `tok_s`, `rate_weight`; aggregate
rows carry `sessions` and `share` instead of `title`; `--cost` adds
`cost_est`. The painted bar and its text mirrors (`bar`, `share_str`,
`cache`) are table-only and never appear in JSON.

## Guarantees

- **No double counting.** Each OpenCode session occupies exactly one row in
  the store, keyed by its session id, and every token column holds the *latest
  observed total* for that session — never an increment. Re-recording identical
  data is a no-op; a session that grew between snapshots replaces its old
  totals rather than adding to them; and if OpenCode ever rewrites a row
  smaller, the stored values only ever go up. `record` reports
  `inserted=N updated=N unchanged=N` so you can see this happen: repeated runs
  show everything `unchanged`. The same guarantee applies to the per-message
  `opencode_steps` snapshot (keyed by `message_id`; `step_ms`/
  `tokens_generated` are monotonic maxes and rows are never deleted, so
  ingested steps survive OpenCode pruning its own messages).
- **Labels never shrink.** A source that nulls a provider/model/title keeps
  the best value already stored.
- **The source opens read-only** (`file:...?mode=ro`) — tokenstats never
  writes to OpenCode's database; it belongs to a running service.
- **Graceful degradation.** Every query snapshots OpenCode first, so reports
  are always current. If the database is missing or locked, `tokenstats`
  prints a warning and reports from your existing store instead of failing
  (exit 0). The explicit `record` command exits 1 on failure.
- **Concurrency.** The store uses WAL + a 5 s busy timeout, so a scheduled
  `record` and an interactive query can overlap safely.
- **Plain output is plain.** Piped output, `NO_COLOR`, and `--json` are
  byte-plain under every theme and overlay combination; color is decoration
  applied only to a terminal.

## Themes — resolution details

Precedence is `--theme=SLUG` > `~/.tokenstats/themes/config.json` > Neon
Midnight. The two failure modes split on purpose: an unknown `--theme=SLUG`
exits 2 and lists what is available, while a saved default that no longer
resolves warns naming `~/.tokenstats/themes/config.json` and renders in Neon
Midnight rather than costing you the report. `~/.tokenstats/themes/` is
scanned whenever a theme is resolved or listed: one `.json` per theme,
filename stem = slug, a file shadows a built-in of the same slug, a file named
exactly `config.json` is skipped (it is the saved default, not a theme), and a
file that fails to parse or validate is skipped with a one-line stderr
warning. The repo's `themes/` folder is documentation only — its format
README and `examples/` — and is not read at runtime. The ten built-in palettes
live in `tokenstats.py` itself; `neon-midnight` (the default) is indigo
chrome, blue `IN` / pink `OUT`, lime cache, violet totals, teal derived
stats.

## Scheduled snapshots (details)

Because queries snapshot OpenCode implicitly, using the tool keeps history
current for free — a scheduler is **not** needed for up-to-date stats. It
protects a different failure: weeks without a single query, during which
OpenCode prunes its own tables and never-recorded sessions are lost. An
hourly `record`, logging into the state directory, guarantees snapshots
regardless of query habits:

**Linux / macOS** — add to `crontab -e` (point the path at wherever the script
lives; if you installed with pipx, use `tokenstats` instead of the python
line):

```cron
0 * * * * /usr/bin/python3 $HOME/path/to/tokenstats.py record >> $HOME/.tokenstats/record.log 2>&1
```

**Windows** — create an hourly task with Task Scheduler (`schtasks`), again
pointing at the real script path:

```bat
schtasks /Create /SC HOURLY /TN tokenstats /TR "python %USERPROFILE%\path\to\tokenstats.py record"
```

## Cross-platform notes

- **Database discovery** is per-OS (see "Finding OpenCode's database").
- **Console encoding**: every run best-effort reconfigures stdout/stderr to
  UTF-8, so the box-drawing frame never crashes a Windows cp1252 console.
- **State migration**: a store or theme files left beside an older copy of the
  script (the pre-1.0 layout) travel into `~/.tokenstats/` on the first run
  that needs the store; the migration cannot lose history and says what it
  moved on stderr.
- **Dates** are local-time throughout; the `--until` day is inclusive via
  calendar next-midnight, which stays correct across DST transitions.

## Development

```bash
python3 -m unittest test_tokenstats -v                     # ~400 tests, no deps, ~0.5 s
python3 -W error::ResourceWarning -m unittest test_tokenstats  # strict: must stay warning-clean
TOKENSTATS_UPDATE_GOLDEN=1 python3 -m unittest test_tokenstats  # re-pin golden/ only AFTER reading the diff
```

`golden/` holds exact stdout for 15 invocations × {piped, tty};
`ByteIdentityGoldenTests` compares them so decoration changes can never leak
into plain output. Design docs and implementation plans for every feature sit
under `docs/superpowers/`.
