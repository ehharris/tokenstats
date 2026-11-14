# Design — `--total` all-time aggregate view

Date: 2026-11-13. Status: approved by the human (chose option (a); proposed
details uncorrected). Small change to `tokenstat.py`.

## Intent and why

Today every report mode emits per-session or per-day rows, so there is no
single answer to "how many tokens have I ever used on this machine." The
human wants an aggregate view: `--total` for one grand total across all
time, and `--total --model <pattern>` for a per-model breakdown of all-time
totals.

## Goals

- `tokenstat.py --total` → exactly one row: totals summed over every stored
  session (or event, with `--from-unsloth`).
- `tokenstat.py --total --model X` → one row per model matching X
  (same substring match as other modes), each holding that model's totals.
- Works in both data paths: OpenCode sessions and `--from-unsloth` events.
- `--json` emits the same rows as JSON; `--db/--src/--unsloth-db` unchanged.

## Non-goals / anti-goals

- No new columns beyond what daily mode already reports (sessions/requests,
  IN, OUT, TOTAL, COST).
- No change to ingestion, double-counting guarantees, or existing modes.

## Approaches considered

- **(a) `--total` → single grand-total row; add `--model` → one row per
  matching model.** Chosen: matches "show all time *by model*" literally and
  subsumes filtering.
- (b) `--model` stays a pure filter, so `--total --model X` is still one
  filtered number. Rejected: adds nothing conceptually over `--total` and
  can't show the per-model breakdown that was asked for.

## Decisions (agent's calls, offered for correction; none made)

- Existing filters (`--since/--until/--min-in/--min-out`) still apply to
  the rows being summed — "all time" is the default when no date flags are
  given, consistent with every other mode. Thresholds pre-filter
  sessions/events (same semantics as daily mode).
- `--total --daily` is rejected via `parser.error`, listing unsupported
  flags, like the existing reconcile guard. `--total` with the `reconcile`
  command is likewise unsupported there.
- Row shape mirrors daily rows plus a MODEL column:
  `MODEL  SESSIONS  IN  OUT  TOTAL  COST`. The single grand-total row uses
  `(all)` as its MODEL value; unsloth events label the count column the
  same way daily mode does (they already reuse DAILY_COLUMNS).
- A matching model set is grouped by `provider || '/' || model_id` in
  session mode (so identically named models under different providers stay
  separate) and by the event's own `model` string in event mode.
- No extra TOTAL footer row when several models match — keep output minimal.

## What's left to the builder

Standard implementation via TDD against the existing test suite
(`test_tokenstat.py`, stdlib unittest, temp stores). Update README.md and
AGENTS.md command lists; module docstring/`--help` staleness noted in
AGENTS.md is out of scope except where `--total` text belongs.
