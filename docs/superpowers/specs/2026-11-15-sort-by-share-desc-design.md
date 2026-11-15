# Sort share views by share descending — design (approved 2026-11-15)

## Intent and why

The per-model totals tables (`--total --model`, bare `--model`, including the
`--from-unsloth`/`--from-runs` variants) currently list models alphabetically,
so the interesting rows — the heavy-traffic models — sit wherever the alphabet
puts them. The human partner wants the biggest-share models to lead the table.

## Goals

- Share views render rows by `share` descending (heaviest first).
- Deterministic: ties keep the existing alphabetical-by-model order.
- `--json` share-view rows carry the same order as the table.

## Non-goals

- Session and daily views are unchanged (oldest-first / date order; they have
  no share column).
- `reconcile` is unchanged.
- No user-facing sort flag — this is a new fixed default.

## Anti-goals

- No change to stored data, query results, or the numbers themselves — pure
  display ordering.

## Constraints

- `share` is computed in Python by `add_shares` after the SQL query (it needs
  the overall total across displayed rows), so it cannot be ordered by SQL.

## Approaches

1. **Sort in `main()` after `add_shares`** — one statement at the point where
   share views are already identified; keeps `add_shares` a pure computation
   and covers all three sources (sessions/events/runs) in one place.
2. Sort inside `add_shares` — rejected: couples row enrichment with ordering,
   surprising for any future caller that wants input order.
3. SQL-side ORDER BY — rejected outright: share does not exist there.

Chosen: 1. Python's sort is stable, so ties fall back to the incoming
alphabetical-by-model order for free.

## Design

In `main()`, in the `want_total` branch immediately after
`rows = add_shares(rows)`:

```python
rows.sort(key=lambda r: -r["share"])
```

(or equivalent `sorted(..., reverse=True)` on a tuple — builder's choice as
long as ties stay alphabetical). Applies before the `--json`/table split so
both render in share order. Grand-total single-row output is unaffected.

## Left to the builder

Exact sort expression; test placement (a CLI-level pin plus/or an ordering
unit test) per existing conventions in `test_tokenstat.py`.
