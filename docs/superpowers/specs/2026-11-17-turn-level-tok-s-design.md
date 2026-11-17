# Turn-level tok/s from `opencode_steps`

## Intent, and why

Every tok/s tokenstat shows should answer one question: **how fast does this
model generate tokens.** The session-family views answer a different question
today — tokens per minute the session happened to exist — so they disagree with
OpenCode's own per-prompt numbers by ~5x. Session
`ses_e14c64b0bffe3hKJ8qniK45xCS` renders 13.1 tok/s while OpenCode reported
63.9 / 70.6 / 62.8 / 77.5 / 68.6 / 76.5 / 70.1 / 74.8 across its 8 prompts.

The goal is to reproduce OpenCode's numbers, not approximate them. Verified
against the live source: a faithful port of OpenCode's own turn logic yields
those 8 values to 0.0 and a session mean of exactly **70.6**.

## Goals

- That session renders **70.6 tok/s**, matching OpenCode exactly.
- Every tok/s means *generated tokens ÷ time spent generating them*, within
  that row's scope — the basis `--from-runs` already uses.
- Ingest **the same way existing data does**: inside `record()`, so report-mode
  implicit ingestion and the hourly cron pick it up with no new call sites;
  read-only source; monotonic max; per-row error isolation; re-running is a
  provable no-op.
- Backfill all 117 stored sessions on the next run, no manual step.

## Non-goals

- No per-turn view rendered in the CLI. The new table makes one possible later;
  building it now is scope the request doesn't need.
- No change to `--from-unsloth` per-event rows (no duration exists to use).
- `TOTAL` keeps meaning IN+OUT. `--cost` is untouched.
- `reconcile` stays excepted from TOK/S, as it is by design.

## Anti-goals

- A number that looks like OpenCode's but is still built from wall-clock.
- Wall-clock and decode-time rates mixed inside one column, or one weighted
  mean folding both together.
- Two sources of truth for decode time.

## What the source actually looks like (verified)

OpenCode's rate lives in `@opencode/util/session-turn.js`, minified into the
binary as `Gu()`. Three parts matter:

```js
// 1. which steps belong to this turn
function yl(msgs, end, legacy) {         // walk back from end-1
  let f = -1;
  for (let g = end - 1; g >= 0; g--) {
    if (msgs[g].type === "idle") return f;              // turn boundary
    if (type !== "user" && type !== "synthetic") continue;
    if (legacy) return g;
    f = g;                                              // remember, keep going
  }
  return f;
}
// legacy = no message in the session has type "idle"

// 2. the rate over the turn's steps
durations = steps.map(s => max(0, s.time.streamed - s.time.firstToken))
output    = Σ (s.tokens.output + s.tokens.reasoning)
// undefined if ANY step lacks `streamed`, or output <= 0, or Σ durations <= 0
rate      = output / (Σ durations / 1000)
```

Three fidelity details that a naive version gets wrong, each confirmed against
the live DB:

- **`synthetic` messages count as turn starts but do not end a turn.** This
  session has one mid-turn (a skill injection); treating it as a boundary adds
  a phantom 9th turn and moves the mean to 69.6.
- **A turn with any never-streamed step has no rate at all** — not a partial
  one. Globally 15 of 4,836 assistant messages lack `streamed`, 25 lack
  `tokens`.
- **The footer renders on every assistant message** with a cumulative
  within-turn rate. The value you read is the turn-final one.

Coverage today: **110 of 117** stored sessions produce a rate; 7 render blank.

## Approaches considered

1. **`decode_ms` column on `usage`.** Cheapest, but it can only ever give a
   *pooled* session rate (71.5 here), because turn boundaries are discarded at
   ingest. Declined: the goal is OpenCode's exact number.
2. **`opencode_steps` table — chosen.** One row per assistant step, so turn
   boundaries survive OpenCode's pruning. Gives 70.6 exactly, and keeps TTFT
   and per-turn drilldowns available later. Costs a second primary key, a
   second ingest pass, and stretches the "one row per source key, SUM over
   distinct keys" invariant to cover steps.
3. **Compute at report time, don't store.** Rejected: breaks the store-as-
   permanent-record model and silently loses history as OpenCode prunes.

## The how

### Store

```sql
CREATE TABLE IF NOT EXISTS opencode_steps (
    message_id       TEXT PRIMARY KEY,   -- session_message.id
    session_id       TEXT NOT NULL,
    turn             INTEGER NOT NULL,   -- 0-based, computed at ingest
    step_ms          INTEGER,            -- NULL = step never streamed
    tokens_generated INTEGER NOT NULL,   -- output + reasoning
    time_created     INTEGER NOT NULL
);
CREATE INDEX idx_opencode_steps_session_turn ON opencode_steps(session_id, turn);
```

No `ALTER TABLE` needed — a new table is just `CREATE TABLE IF NOT EXISTS`.
No `decode_ms` column: the steps table is the single source of truth for
decode time.

Invariant 1 holds per column: `step_ms` and `tokens_generated` are fixed once a
step completes, so `max(stored, source)` covers the streaming→complete
transition and still prevents a pruned source from shrinking history.
`turn` is a deterministic label, overwritten with the recomputed value.
Rows are never deleted, so ingested steps survive OpenCode's pruning forever.

### Ingestion — inside `record()`

`record()` gains a second pass after the `session_v2` loop:

1. Read `session_message` (`type`, `data`, `time_created`) for all sessions,
   ordered by `session_id, time_created`.
2. Replay `yl()` to assign each assistant message its `turn` index. Parse
   `data` in Python — the project avoids SQLite JSON1 (`import_unsloth_runs`
   says so explicitly) — reading only `time.streamed`, `time.firstToken`,
   `time.created`, `tokens.output`, `tokens.reasoning`.
3. `step_ms = max(0, streamed - (firstToken ?? created))`, or NULL when
   `streamed` is absent.
4. Upsert by `message_id` with the same max/insert/update pattern and the same
   `inserted/updated/unchanged` counts; a malformed blob bumps `skipped` and
   the run continues.
5. A source without `session_message` imports as zero steps instead of
   failing, mirroring the `chat_generation_runs` no-op.

Cost: ~4,800 JSON blobs (~28 MB) per run, roughly a second. Acceptable for an
hourly cron; flagged as a known cost rather than hidden.

### The rate — one definition

```
turn rate    = Σ tokens_generated / (Σ step_ms / 1000)
               valid iff the turn has ≥1 step, NO step has NULL step_ms,
               Σ step_ms > 0 and Σ tokens_generated > 0
session rate = unweighted mean of its valid turns' rates, 1 dp
```

Plain SQL, no JSON1:

```sql
SELECT session_id, AVG(rate) FROM (
  SELECT session_id, turn,
         SUM(tokens_generated) * 1000.0 / SUM(step_ms) AS rate
  FROM opencode_steps GROUP BY session_id, turn
  HAVING COUNT(step_ms) = COUNT(*)      -- no never-streamed step in the turn
     AND SUM(step_ms) > 0 AND SUM(tokens_generated) > 0
) GROUP BY session_id
```

A session with no valid turn renders blank and drops out of **both** sides of
any weighted mean, per the existing rule. Mixing bases in one column is the
anti-goal, so there is no wall-clock fallback.

**Property worth stating plainly:** an unweighted mean of turns treats a 10-second
turn the same as a 400-second one. That is exactly what OpenCode's per-prompt
display does, which is why it matches — and the token-weighted aggregates below
compensate at the group level.

### Weight — my call, flagged for approval

Aggregate weight changes from `tokens_input + tokens_output` to the row's
**generated tokens** (`Σ opencode_steps.tokens_generated`, `progress_tokens`
for runs). The weight should be the numerator of what you are averaging; under
the old weight a session with a 200k prompt and 500 output tokens dominates a
*speed* average it barely participated in. Runs already weight by
`progress_tokens`, their generated tokens, so this makes the two sources
consistent rather than running two conventions.

Mechanically: rate-bearing row dicts gain a `rate_weight` key and
`report_header` reads `r.get("rate_weight", r.get("total") or 0)`, so views
without the key behave exactly as before.

### Every tok/s site, after the change

| View | numerator | denominator | weight |
|---|---|---|---|
| session rows | mean of turn rates | | — |
| `--daily` / `--total` (sessions) | weighted mean of session rates | | generated tokens |
| hero, session views | weighted mean of displayed rows | | generated tokens |
| `--from-runs` rows | `progress_tokens` | run started→completed | progress |
| `--from-runs` daily/total | weighted mean of run rates | | progress |
| `--from-unsloth` rows | — blank, no duration exists | | |
| `--from-unsloth` daily/total | prompt + completion | `created_at` span | |

The event aggregates are the one documented exception: per-event rows carry no
duration, so there is nothing to align them to. Their docstrings already say
this and stay accurate.

`_aggregate_rate` stays as the per-row helper for runs and event aggregates;
`_session_rate_groups` switches to the steps-derived rate.

### Docs to update

- `AGENTS.md`: replace the "session rows show a WALL-CLOCK average" sentence,
  record the increment, and extend invariant 1 to name `opencode_steps`.
- `README.md`: the TOK/S description.

## Expected result

```
$ python3 tokenstat.py            # this session's row
...  manta-ray/halogen-qwen3.8-flash-next  ...  70.6 tok/s
```

## Left to the builder

Test names and placement; helper naming; how the `session_message` scan is
shaped, as long as it stays one pass and keeps error isolation.
