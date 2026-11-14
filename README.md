# tokenstat — permanent AI token usage record for this machine

`tokenstat` snapshots per-session AI token usage from OpenCode's database
(`~/.local/share/opencode/opencode.db`, read-only) into its own SQLite store
(`usage.db` next to the script), and reports it filtered by model, date, and
token counts. OpenCode prunes its own history over time; your copy in
`usage.db` never shrinks, so this machine's full token history survives.

Single Python file, stdlib only — no install step:

```bash
python3 tokenstat.py [filters]      # snapshot both sources + report
python3 tokenstat.py record         # snapshot OpenCode only
python3 tokenstat.py import-unsloth # snapshot Unsloth Studio only
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

# Machine-readable (combine with --daily for daily aggregates):
python3 tokenstat.py --json | jq '.[].total'

# Snapshot without querying:
python3 tokenstat.py record

# Unsloth's own per-request log (IN = prompt incl. cache hits, OUT = completion):
python3 tokenstat.py --from-unsloth --model unsloth --since 2026-11-01
python3 tokenstat.py --from-unsloth --daily

# Lost-history view: what Unsloth recorded that OpenCode no longer can:
python3 tokenstat.py reconcile
```

The two reports (session mode and `--from-unsloth`) describe the *same*
traffic from two angles — never add them together. In `reconcile`, a
positive gap means usage only Unsloth still records (OpenCode pruned it);
a negative gap means OpenCode counted tokens Studio's log doesn't contain
(e.g. Studio rotated its log, or a request was retried client-side).

Every query snapshots both sources first (OpenCode's database and Unsloth
Studio's `api_usage_events`), so reports are always current. If either
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
The same guarantee applies to imported Unsloth events, keyed by their own
event ids.

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
