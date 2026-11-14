# Token Counter — Design

Date: 2026-11-13
Status: awaiting approval

## Intent

Keep a permanent record of every AI token used on this machine for its whole
history, and let that history be queried by model name, tokens in/out, and
date. OpenCode is the sole source of AI traffic here (Unsloth and other
providers all route through it), but OpenCode prunes its own session records
over time — so the data must be copied out before it disappears.

## Goals

- Permanent local history that survives OpenCode deleting or rewriting its
  database.
- Accurate capture of per-session usage: model, tokens in, tokens out,
  reasoning tokens, cache read/write, cost, timestamps.
- Fast filtering by model name (substring), date range, and token-count
  thresholds.
- Light: one Python file, stdlib only, no server, no dependencies.

## Non-goals

- Per-request / per-message granularity (OpenCode stores usage only as
  per-session totals; that is the finest data available).
- Tracking tools other than OpenCode.
- Charts or a web UI.

## Anti-goals

- Losing any recorded data on re-runs.
- Anything that must be remembered to be run manually for history to stay
  complete.

## Data source (verified)

`~/.local/share/opencode/opencode.db` (SQLite), table `session_v2`:
one row per session with `model` (JSON: providerID + id), `tokens_input`,
`tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`,
`cost`, `title`, `directory`, `agent`, `time_created`, `time_updated`.
Verified present for all providers in use, including `unsloth-studio`.

## Approaches considered

- **A. CLI reading OpenCode's DB live** — simple and always accurate, but
  history vanishes when OpenCode prunes. Rejected: fails the core goal.
- **B. Web dashboard** — nicer browsing, heavier, still needs the retention
  layer underneath. Rejected for now; can be added later on the same store.
- **C. Recorder + own SQLite DB + CLI query (chosen)** — snapshots usage
  rows into `token_counter/usage.db` incrementally; queries run against our
  own permanent copy. Slightly more moving parts, but it is the only option
  that meets the retention goal.

## How it works

One program, `tokenstat.py`, two commands:

1. **`tokenstat record`** — opens OpenCode's DB read-only
   (`file:...?mode=ro`), upserts every `session_v2` row into our
   `usage(session_id PRIMARY KEY, ...)` table. Values are monotonic
   (take the max of token counts) so a session growing between snapshots
   updates correctly and a pruned/rewritten source can never shrink our
   history. Records `first_seen` / `last_seen`. Idempotent; safe to run
   any number of times.

   **No double counting — by construction.** A session occupies exactly one
   row keyed by its OpenCode `session_id`, and each column stores the *latest
   observed total* for that session, never an increment. Re-recording the
   same session (every run re-reads everything) therefore changes nothing:
   identical values are no-ops, and a grown session replaces rather than adds
   to its previous totals. Every report is a `SUM` over distinct sessions, so
   repeated snapshots can never inflate counts. The recorder verifies this by
   reporting rows inserted vs. updated vs. unchanged on each run.
2. **`tokenstat [filters]`** — prints the table of sessions matching:
   - `--model <substr>` (matches provider/id, e.g. `unsloth`)
   - `--since YYYY-MM-DD` / `--until YYYY-MM-DD` (date of usage = session
     creation date; sessions are short-lived so this is a good proxy)
   - `--min-in N` / `--min-out N` token thresholds
   - `--daily` groups totals by date instead of listing sessions
   - `--json` machine-readable output

   **Every query run does an implicit `record` first**, so data is always
   current without the user remembering to snapshot.

Retention safety net: a documented cron/systemd line
(`tokenstat record`) for hourly background snapshots, so history stays
complete even if no queries are run for weeks. Installing it is optional;
the one-liner goes in the README (my call).

## Storage

`token_counter/usage.db` — our SQLite file, owned by this tool, never
modified by OpenCode. Backup = copy the file.

## Left to the builder

Exact CLI flag parsing, table formatting, and error handling (e.g. source
DB locked while OpenCode writes — retry or degrade to querying our own
copy).
