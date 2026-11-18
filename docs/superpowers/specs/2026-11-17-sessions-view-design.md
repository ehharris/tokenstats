# `--sessions` cap — design

Date: 2026-11-17. Status: awaiting approval.
Builds on: `docs/superpowers/specs/2026-11-17-turn-level-tok-s-design.md`
(session TOK/S is the mean of its turn rates — unchanged by this design).

## Intent and why

A way to say "the N most recent sessions" and have it mean it wherever it
applies. `--sessions` lists the 20 most recent sessions; `--sessions=50`
lists 50; and the same cap narrows the session set that `--daily` and
`--total` aggregate over, so "what have the last 10 sessions done" is one
command rather than a manual cross-reference.

`--total` on its own already means "everything", which is the `--sessions=0`
case: the unlimited end of the same dial.

The bare-run dashboard keeps its 5-session panel exactly as it is. The flag
coexists with it rather than replacing it: the dashboard is a glance, 20
rows makes it a scroll.

## Goals

- `--sessions` with no value means 20 in the list view and all sessions in
  the aggregate views; `--sessions=N` means N everywhere.
- `--sessions=0` means all matching sessions in any view.
- "Most recent" is by session creation time, newest first — the same order
  the dashboard's `sessions · 5 most recent` panel uses.
- Used alone it **lists** those sessions, newest first.
- With `--daily` or `--total` (including `--total --model`) it **caps the
  sessions being aggregated**, so the aggregate describes exactly those
  sessions and nothing else.
- Existing filters (`--model`, `--since`, `--until`, `--min-in`,
  `--min-out`) narrow the candidate set first; the cap then takes the N
  most recent of what survives.
- Existing overlays work: `--cost`, `--cache`.
- `--json` emits the displayed rows, newest first for the list view.
- In the list view the hero summarises the rows actually displayed. In the
  aggregate views it behaves as it does today.

## Non-goals

- Changing the bare-run dashboard's 5-session panel.
- A per-turn drill-down view.
- Capping Unsloth event or run views — those are not sessions.
- Sorting by anything other than recency.
- Any change to how TOK/S is computed.

## Anti-goals

- The cap applying to displayed rows in one view and to the underlying
  session set in another. It means one thing: which sessions are in scope.
- An aggregate whose hero reproduces a total over a different session set
  than the rows on screen.
- The dashboard panel and the new list view drifting apart in ordering,
  columns, or title wording. They are the same thing with a different N.
- A flag that silently changes what a bare run prints.

## Constraints

- stdlib only; no new dependencies; no SQLite JSON1.
- Sources stay read-only; the store is opened as today.
- Flag rejection follows the existing pattern: an explicit unsupported-flag
  list with `parser.error(...)`, checked after implicit ingestion.
- Connection hygiene and the `-W error::ResourceWarning` clean suite hold.
- Colour stays tty-gated via `_color_enabled`; piped output stays plain.

## Approaches considered

1. **Flag as a list view only.** `--sessions` renders the N most recent
   sessions; rejected with `--daily`/`--total`. Smallest diff. Trades: it
   cannot answer "what did my last 10 sessions cost", which is the reason
   the cap is worth having at all.
2. **Cap the session set feeding any session-family view, sharing the
   recency logic with the dashboard.** The cap becomes one more predicate
   built by `_session_filters`, which every session-family query already
   funnels through — `query_sessions`, `daily_totals`, `total_totals`, and
   `_session_rate_groups` alike. The list view is then just the capped
   rows rendered newest-first. **Chosen**: one choke point means the rows,
   the aggregates, and the rate groups that feed the hero cannot disagree
   about which sessions are in scope, and the dashboard's panel and the new
   view share the recency step rather than duplicating it.
3. **A `sessions` subcommand.** Matches `record`/`reconcile`, but in this
   repo commands are actions and views are flags, and a subcommand would
   not compose with `--model`/`--since`. Rejected on convention.

## Decided by the human partner

- `--sessions` lists the 20 most recent by default, overridable as
  `--sessions=50`.
- It coexists with the bare-run dashboard; that panel is unchanged.
- It works with `--daily` and `--total`; `--total` is the `--sessions=0`
  case.
- **`--sessions` with no value alongside `--total` means all sessions ever
  recorded**, not 20.
- Approach 2.

## Controller's calls (correctable, not agreed)

- Extending the partner's ruling by symmetry: a valueless `--sessions`
  means **all** in either aggregate view (`--daily` as well as `--total`),
  and 20 in the list view. The reasoning is that an aggregate is a summary
  of everything by default and only a list needs a bound; an explicit
  `--sessions=10` caps every view the same way.
- `--total` with no `--sessions` stays all sessions, exactly as today —
  nothing about current output changes.
- `--sessions=0` means all matching sessions in any view; a negative N is
  a `parser.error`, not a silent empty table.
- Rejected with `--from-unsloth`, `--from-runs`, and the `reconcile`
  command — none of them are sessions.
- The cap is a predicate on the session set (`session_id IN (SELECT
  session_id FROM usage <filters> ORDER BY time_created DESC LIMIT ?)`),
  added inside `_session_filters`, so every consumer sees the same scope.
  `LIMIT -1` is SQLite's no-limit, so N=0 maps to that rather than to a
  second code path.
- The shared piece between the dashboard and the list view is the recency
  step and the title text — `sessions · N most recent`, with N shown as
  `all` when 0 — not the whole renderer: the dashboard draws a panel, the
  views draw hero-plus-table.
- In the list view the hero covers the displayed rows; the aggregate views
  keep today's behaviour, where the hero reproduces the pooled total over
  the capped set.

## Left to the builder

Exact helper and test-class names, whether the list view truncates in
Python or relies solely on the SQL cap, and how the title reads for a
capped aggregate (`daily · last 10 sessions` is the obvious shape). The
existing `query_sessions` signature should not change.
