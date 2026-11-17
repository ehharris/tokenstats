# Token-weighted TOK/S everywhere, plus a hero TOK/S

Date: 2026-11-17
Status: approved by the human partner (both decisions were theirs, below)

## Intent and why

`TOK/S` aggregates should answer "how fast did this group deliver its
tokens", not "what was the average of its rows' rates". The human partner
asked for a **token-weighted mean in every place TOK/S is available**, and for
the dashboard/report **hero to gain a TOK/S stat** computed the same way.

They chose this knowing the consequence I measured on the live store: for
`manta-ray/halogen-qwen3.8-flash-next` the simple mean reads 197.9 while the
token-weighted mean reads 49.2, because the model's biggest session (229k
tokens) is also its slowest (1.8 tok/s) and carries 66% of the weight. They
want the weighted number anyway, in the tables and in the hero.

## Goals

- Every aggregate TOK/S is the token-weighted mean of the rates of the rows
  it folds up: `total_totals`, `daily_totals`, `total_runs`, `daily_runs`.
- The hero (`report_header`) shows the token-weighted mean of the rows it
  summarizes, in every view where those rows carry rates.
- Rows with no rate stay out of the arithmetic entirely.

## Non-goals

- No change to per-row rates: a session row is still its own wall-clock
  average, a run row still its true generation rate.
- No new stored columns; this is derived at report time, as TOK/S always has
  been.
- No TOK/S on `reconcile` (unchanged: it is a comparison view and rejects the
  column by design).

## Constraints

- Event aggregates (`daily_events`, `total_events`) keep the created_at-span
  rate: per-event rows carry no duration, so there is no per-row rate to
  weight. This is the one place TOK/S is "available" but cannot be a weighted
  mean of rows, and it stays documented as the exception.
- Piped / `NO_COLOR` output stays byte-plain (already pinned).
- `panel()` measures visible width, so a colored hero stat never rags the
  frame; the hero line must keep that property.

## Approach considered

1. **Weighted mean computed from the rows the view already has** — chosen.
   Aggregates already fetch their rows' rates; the hero already sums its
   rows' tokens. Adding the weight to the same pass costs nothing and keeps
   one definition of "the rows this number summarizes".
2. *Precompute a weighted rate in SQL with `SUM(rate*tokens)/SUM(tokens)`* —
   would duplicate the rate formula in SQL, splitting it away from
   `query_sessions`/`query_runs`, which is exactly the drift the previous
   increment removed. Rejected.
3. *Store per-row active seconds and derive real throughput* — the honest
   fix, but OpenCode's `session_v2` exposes only created/updated, so the data
   does not exist for non-Studio providers. Out of scope.

## Decisions

- **Weight = the row's total tokens** (`tokens_input + tokens_output` for
  sessions and daily/total rows, `progress_tokens` for runs). Human partner's
  choice: token-weighted everywhere.
- **Rows with no rate are dropped from numerator and denominator**, not
  counted as 0. A group with no rates, or with rates but zero total weight,
  renders blank (`None`).
- **Hero computes its own TOK/S from the rows passed in**, like it already
  sums tokens, so every view gets it for free and it always matches the table
  beneath it. No new parameter.
- **Hero placement and color** (my call): appended after `out` and before the
  `--cost` tail, formatted `157.7 tok/s` (same one-decimal format as the
  table column), painted the hero `accent` magenta because the IN/OUT/TOTAL/
  SHARE hues are already claimed by table columns. The whole ` · NNN.N tok/s`
  segment is omitted when no displayed row has a rate, so `--from-unsloth`
  per-event heroes are unchanged.

```
╭ tokenstat ──────────────────────────────────────────────────────╮
│ 9.86M tokens · 117 sessions · 7.80M in · 2.06M out · 157.7 tok/s │
│ 2026-11-06 → 2026-11-17                                         │
╰─────────────────────────────────────────────────────────────────╯
```

## Left to the builder

Naming of the helper, and how the rate-group helpers carry weights alongside
rates, as long as the rate formula stays in one place and the observable
behavior above holds.
