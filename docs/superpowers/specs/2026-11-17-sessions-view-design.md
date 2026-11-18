# `--sessions` view — design

Date: 2026-11-17. Status: awaiting approval.
Builds on: `docs/superpowers/specs/2026-11-17-turn-level-tok-s-design.md`
(session TOK/S is the mean of its turn rates — unchanged by this design).

## Intent and why

A quick way to list sessions, newest first, with a count the operator
chooses: `--sessions` lists the 20 most recent sessions, `--sessions=50`
lists the 50 most recent. Today the only way to see recent sessions is a
bare run, which shows exactly 5 and no way to ask for more, or the plain
session view, which lists *every* matching session oldest-first.

The bare-run dashboard keeps its 5-session panel exactly as it is. The new
flag coexists with it rather than replacing it: the dashboard is a glance,
20 rows makes it a scroll.

## Goals

- `--sessions` (no value) lists the 20 most recent sessions.
- `--sessions=N` lists the N most recent. `--sessions=0` lists all
  matching sessions.
- Newest first, so the most recent session is the first row.
- Existing filters narrow the candidate set before the cut: `--model`,
  `--since`, `--until`, `--min-in`, `--min-out`.
- Existing overlays work: `--cost`, `--cache`.
- `--json` emits the displayed rows, newest first.
- The hero summarises the rows actually displayed (the N), not everything.

## Non-goals

- Changing the bare-run dashboard's 5-session panel.
- A per-turn drill-down view.
- Sorting by anything other than recency; no `--sessions` + `--daily` or
  `--total` combination.
- Any change to how TOK/S is computed.

## Anti-goals

- The dashboard panel and the new view drifting apart in ordering, columns,
  or title wording. They are the same thing with a different N.
- A flag that silently changes what a bare run prints.

## Constraints

- stdlib only; no new dependencies; no SQLite JSON1.
- Sources stay read-only; the store is opened as today.
- Flag rejection follows the existing pattern: an explicit unsupported-flag
  list with `parser.error(...)`, checked after implicit ingestion.
- Connection hygiene and the `-W error::ResourceWarning` clean suite hold.
- Colour stays tty-gated via `_color_enabled`; piped output stays plain.

## Approaches considered

1. **Flag as a new view, rendered directly.** `--sessions` with
   `nargs="?"`, `const=20`, `type=int`; a new branch calls
   `query_sessions(...)`, reverses, truncates, renders. Smallest diff.
   Trades: the dashboard panel and the new view render the same thing by
   two paths, which is how they drift.
2. **Flag as a new view, sharing the recency logic.** Same flag shape, but
   the "newest-first, first N" step and the panel title live in one helper
   the dashboard (N=5) and the new view (N=given) both call. Slightly
   bigger diff, touches working dashboard code. **Chosen** — the request
   is the dashboard panel with a bigger N, so it should be one code path.
   Drift is the bug class this repo just spent a review round removing.
3. **A `sessions` subcommand.** Matches `record`/`reconcile`, but in this
   repo commands are actions and views are flags, and a subcommand would
   not compose with `--model`/`--since`. Rejected on convention.

## Decided by the human partner

- The feature exists as `--sessions`, defaulting to 20, overridable as
  `--sessions=50`.
- It coexists with the bare-run dashboard; the dashboard's 5-row panel is
  unchanged.
- Approach 2.

## Controller's calls (correctable, not agreed)

- `--sessions` with no value means 20; `--sessions=0` means all matching.
- Rejected with `--daily`, `--total`, `--from-unsloth`, `--from-runs`, and
  the `reconcile` command — none of them list sessions.
- The hero covers the displayed N rows, unlike the dashboard, whose hero
  deliberately spans all sessions because its panel is a sample.
- A negative N is a `parser.error`, not a silent empty table.
- The shared helper returns rows newest-first truncated to N (N=0 → all);
  the dashboard passes 5. Title text is shared: `sessions · N most
  recent`, with N rendered as `all` when 0.

## Left to the builder

Exact helper name and placement, test class and case names, and whether
the truncation happens in Python after `query_sessions` or in SQL. The
existing `query_sessions` signature should not change.
