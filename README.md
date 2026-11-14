# tokenstat — permanent AI token usage record for this machine

`tokenstat` snapshots per-session AI token usage from OpenCode's database
(`~/.local/share/opencode/opencode.db`, read-only) into its own SQLite store
(`usage.db` next to the script), and reports it filtered by model, date, and
token counts. OpenCode prunes its own history over time; your copy in
`usage.db` never shrinks, so this machine's full token history survives.

Single Python file, stdlib only — no install step:

```bash
python3 tokenstat.py [filters]      # record + report
python3 tokenstat.py record         # record only
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

# Machine-readable (combine with --daily for daily aggregates):
python3 tokenstat.py --json | jq '.[].total'

# Snapshot without querying:
python3 tokenstat.py record
```

Every query records first, so reports are always current. If OpenCode's
database is missing or locked mid-write, `tokenstat` prints a warning and
reports from your existing store instead of failing.

## No double counting

Each OpenCode session occupies exactly one row in the store, keyed by its
session id, and every token column holds the *latest observed total* for
that session — never an increment. Re-recording identical data is a no-op;
a session that grew between snapshots replaces its old totals rather than
adding to them; and if OpenCode ever rewrites a row smaller, the stored
values only ever go up. `record` reports `inserted=N updated=N unchanged=N`
so you can see this happen: repeated runs show everything `unchanged`.

## Keeping history complete

Because queries record implicitly, using the tool keeps history current for
free. To guarantee snapshots even in weeks without a query, schedule an
hourly run: add to crontab (`crontab -e`):
`0 * * * * /usr/bin/python3 $HOME/messinaround/token_counter/tokenstat.py record >> $HOME/messinaround/token_counter/record.log 2>&1`

## Tests

```bash
python3 -m unittest test_tokenstat -v
```
