# Turn-Level tok/s Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every tok/s tokenstat reports mean "generated tokens ÷ time spent generating them," with session rows reproducing OpenCode's per-prompt numbers exactly (70.6 for the reference session, not 13.1).

**Architecture:** A new `opencode_steps` store table holds one row per OpenCode assistant step, including the turn index computed at ingest by replaying OpenCode's own turn-boundary walk. A session's rate is the unweighted mean of its turns' rates; daily/total/hero aggregates stay token-weighted means of the rows they fold, now weighted by generated tokens. `--from-runs` keeps its true generation rate; `--from-unsloth` per-event rows stay blank because no duration exists.

**Tech Stack:** stdlib-only Python 3 (`tokenstat.py`, single module), SQLite via `sqlite3`, `unittest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-17-turn-level-tok-s-design.md`

## Global Constraints

- stdlib only — no third-party imports, no install step.
- **No SQLite JSON1.** Parse source JSON blobs in Python, as `import_unsloth_runs` already does ("so the query works on any build").
- **Invariant 1 — no double counting:** one row per source key; every numeric column stores the latest observed total via monotonic `max`; rows are never deleted; re-running any import is a provable no-op; reports always SUM over distinct keys.
- **Invariant 3 — labels never shrink.**
- **Invariant 5 — sources open read-only** (`file:...?mode=ro`); never write to OpenCode's or Studio's databases.
- Ingestion failures on a missing source table are a **zero-count no-op**, mirroring the `chat_generation_runs` handling.
- Per-row error isolation: one malformed source row bumps `skipped` and the run continues.
- Rates round to **one decimal**, like every other rate in the tool.
- A span under **1 second** yields no rate (`None`), mirroring `_aggregate_rate`.
- The suite must stay green under `python3 -W error::ResourceWarning -m unittest test_tokenstat`.

## Review Focus

Failure modes the spec implies that a happy-path test would not catch, most likely to bite first:

- **A turn with one never-streamed step has no rate at all** — not a partial rate over the steps that did stream. OpenCode returns `undefined` for the whole turn; a partial rate would silently overstate speed. 15 of 4,836 live assistant messages lack `streamed`.
- **A `synthetic` message mid-turn must not split the turn.** The reference session has one (a skill injection); treating it as a boundary invents a phantom 9th turn and moves the session mean from 70.6 to 69.6.
- **A session with no ingested steps renders blank and drops out of both sides of a weighted mean** — it must not contribute a 0.0 rate or a zero weight that dilutes the group.
- **Re-running `record` must be a byte-for-byte no-op for steps, and a pruned source must not shrink stored values** — OpenCode prunes, so this is the mechanism keeping history recoverable.
- **A source DB without a `session_message` table must not fail ingestion** — report modes degrade to zero steps and exit 0, like the `chat_generation_runs` no-op.
- **A session with no `idle` messages uses OpenCode's legacy boundary rule** (return the nearest preceding input, do not walk past it) — older OpenCode builds wrote no `idle` rows.

---

## File Structure

- `tokenstat.py` — modify. All production changes land here: schema (`ensure_schema`), the turn-boundary port, the new ingestion pass, the rate SQL, and the three display sites.
- `test_tokenstat.py` — modify. New test classes plus updates to the wall-clock pins in `SessionTokSTests` and `AggregateWeightedRateTests`.
- `AGENTS.md`, `README.md` — modify. Replace the wall-clock TOK/S description; extend invariant 1 to name `opencode_steps`.

---

## Task 1: `opencode_steps` schema and the turn-boundary port

**Files:**
- Modify: `tokenstat.py` — `ensure_schema` (~line 34), new `_turn_index` above `record`
- Test: `test_tokenstat.py` — new `StepsSchemaTests`, `TurnIndexTests`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - store table `opencode_steps(message_id TEXT PK, session_id TEXT NOT NULL, turn INTEGER NOT NULL, step_ms INTEGER NULL, tokens_generated INTEGER NOT NULL, time_created INTEGER NOT NULL)`
  - `_turn_index(types: list[str], end: int, legacy: bool) -> int` — index of the input message opening the turn that ends at `end`, or `-1`.

- [ ] **Step 1: Write the failing schema test**

```python
class StepsSchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_opencode_steps(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        cols = [r[1] for r in dest.execute("PRAGMA table_info(opencode_steps)")]
        self.assertEqual(
            cols, ["message_id", "session_id", "turn", "step_ms",
                   "tokens_generated", "time_created"])
        idx = [r[1] for r in dest.execute(
            "PRAGMA index_list(opencode_steps)")]
        self.assertIn("idx_opencode_steps_session_turn", idx)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest test_tokenstat.StepsSchemaTests -v`
Expected: FAIL — `table_info` returns no columns.

- [ ] **Step 3: Add the table and index to `ensure_schema`**

Alongside the existing three `CREATE TABLE IF NOT EXISTS` statements, matching their formatting:

```sql
CREATE TABLE IF NOT EXISTS opencode_steps (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn INTEGER NOT NULL DEFAULT 0,
    step_ms INTEGER,
    tokens_generated INTEGER NOT NULL DEFAULT 0,
    time_created INTEGER NOT NULL)
```

plus `CREATE INDEX IF NOT EXISTS idx_opencode_steps_session_turn ON opencode_steps(session_id, turn)` with the other two indexes.

- [ ] **Step 4: Run the schema test to verify it passes**

Run: `python3 -m unittest test_tokenstat.StepsSchemaTests -v`
Expected: PASS

- [ ] **Step 5: Write the failing turn-boundary tests**

These pin the three fidelity rules from the spec. `types` is the session's message-type sequence in `time_created` order; `end` is exclusive.

```python
class TurnIndexTests(unittest.TestCase):
    def test_walks_back_past_synthetic_to_the_opening_user(self):
        # Reference session: a mid-turn synthetic must NOT open the turn.
        t = ["user", "assistant", "synthetic", "assistant", "assistant", "idle"]
        self.assertEqual(_turn_index(t, 5, legacy=False), 0)

    def test_stops_at_the_preceding_idle(self):
        t = ["user", "assistant", "idle", "user", "assistant", "assistant"]
        self.assertEqual(_turn_index(t, 5, legacy=False), 3)

    def test_synthetic_opens_a_turn_when_no_user_precedes(self):
        t = ["user", "assistant", "idle", "synthetic", "assistant"]
        self.assertEqual(_turn_index(t, 4, legacy=False), 3)

    def test_legacy_returns_nearest_input_and_does_not_walk_past_idle(self):
        t = ["user", "assistant", "idle", "user", "assistant"]
        self.assertEqual(_turn_index(t, 4, legacy=True), 3)

    def test_no_input_message_returns_minus_one(self):
        self.assertEqual(_turn_index(["assistant", "assistant"], 2, False), -1)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.TurnIndexTests -v`
Expected: FAIL — `ImportError`/`NameError` for `_turn_index`.

- [ ] **Step 7: Implement `_turn_index` in `tokenstat.py`**

A direct port of OpenCode's `yl()`. The body is the algorithm, not a choice, so it is given:

```python
def _turn_index(types, end, legacy):
    """Index of the input message opening the turn ending at `end` (exclusive).

    Port of OpenCode's inputIndex: walk back from `end`, remembering the last
    user/synthetic message, and stop at the first `idle` — so a mid-turn
    `synthetic` never splits a turn. legacy (a session with no `idle` rows at
    all) returns the nearest input instead of walking past one. -1 when the
    session has no input message before `end`."""
    found = -1
    for i in range(end - 1, -1, -1):
        kind = types[i]
        if kind == "idle":
            return found
        if kind not in ("user", "synthetic"):
            continue
        if legacy:
            return i
        found = i
    return found
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.TurnIndexTests -v`
Expected: PASS

- [ ] **Step 9: Run the full suite and commit**

Run: `python3 -m unittest test_tokenstat` — Expected: all green (existing 218 plus new).

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: add opencode_steps schema and OpenCode turn-boundary port"
```

---

## Task 2: Ingest assistant steps inside `record()`

**Files:**
- Modify: `tokenstat.py` — new `import_opencode_steps` after `record`, and one call at the end of `record`
- Test: `test_tokenstat.py` — new `ImportStepsTests`, plus a `make_step_src`/`step_msg` helper beside `make_src`

**Interfaces:**
- Consumes: `opencode_steps` table and `_turn_index(types, end, legacy)` from Task 1.
- Produces: `import_opencode_steps(src: sqlite3.Connection, dest: sqlite3.Connection) -> dict` returning `{"inserted": int, "updated": int, "unchanged": int}` and optionally `"skipped"`. `record()` returns those counts merged into its own dict.

- [ ] **Step 1: Add the test helpers**

`make_src` builds only `session_v2`; steps need a second source table. Add beside it:

```python
def step_msg(mid="m1", sid="s1", kind="assistant", created=1_764_000_000_000,
             first=None, streamed=None, out=100, reasoning=50, data=None):
    """One session_message row. `data` defaults to a timing+tokens blob."""
    if data is None:
        data = json.dumps({
            "time": {"created": created,
                     **({"firstToken": first} if first is not None else {}),
                     **({"streamed": streamed} if streamed is not None else {})},
            "tokens": {"output": out, "reasoning": reasoning},
        })
    return (mid, sid, kind, created, data)


def make_step_src(session_rows=(), messages=()):
    """Fake OpenCode source with session_v2 AND session_message."""
    src = make_src(session_rows)
    src.execute("""CREATE TABLE session_message (
                       id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
                       time_created INTEGER, data TEXT)""")
    for row in messages:
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)", row)
    return src
```

- [ ] **Step 2: Write the failing ingestion tests**

```python
class ImportStepsTests(unittest.TestCase):
    BASE = 1_764_000_000_000

    def dest_for(self, messages, rows=(src_row(),)):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(list(rows), list(messages))
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def rows(self, dest):
        return dest.execute("SELECT message_id, session_id, turn, step_ms,"
                            " tokens_generated FROM opencode_steps"
                            " ORDER BY message_id").fetchall()

    def test_step_tokens_are_output_plus_reasoning(self): ...
    def test_step_ms_is_streamed_minus_first_token(self): ...
    def test_step_ms_falls_back_to_created_without_first_token(self): ...
    def test_never_streamed_step_stores_null_step_ms(self): ...
    def test_turn_index_matches_opencode_boundaries(self): ...
    def test_non_assistant_messages_are_not_stored(self): ...
    def test_rerun_is_a_noop(self): ...
    def test_pruned_source_cannot_shrink_stored_values(self): ...
    def test_missing_session_message_table_is_a_noop(self): ...
    def test_malformed_blob_bumps_skipped_and_continues(self): ...
    def test_record_counts_include_steps(self): ...
```

Fill each body from these pins, using `self.rows(dest)`:

- `output=100, reasoning=50` → `tokens_generated == 150`.
- `first=BASE+100, streamed=BASE+1100` → `step_ms == 1000`.
- no `firstToken`, `created=BASE`, `streamed=BASE+900` → `step_ms == 900`.
- `streamed=None` → `step_ms is None`.
- `["user", "assistant", "synthetic", "assistant", "idle"]` → both assistants get `turn == 0`; with a second `user` + two more assistants after the `idle`, those get `turn == 1`.
- `user`/`idle`/`synthetic` rows → absent from `opencode_steps`.
- run `record` twice on the same source → second counts are all `updated`/`unchanged`, and `step_ms`/`tokens_generated` are unchanged.
- ingest a step at `step_ms=1000`, then re-ingest a source where that message has `step_ms=500` (or is gone) → stored value stays `1000`.
- `make_src([src_row()])` with no `session_message` table → `record` returns normally, `opencode_steps` is empty.
- one row with `data="{not json"` among two good rows → `counts["skipped"] == 1` and both good rows stored.
- `record` on a source with one session and two steps → `counts["inserted"] == 3`.

- [ ] **Step 3: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.ImportStepsTests -v`
Expected: FAIL — `ImportError` for `import_opencode_steps` (or assertion failures once it exists).

- [ ] **Step 4: Implement `import_opencode_steps`**

Signature: `import_opencode_steps(src, dest) -> dict`. Place it after `record`. Behaviour:

- If `session_message` is absent from `sqlite_master`, return the zero counts immediately (mirror `import_unsloth_runs`).
- Read `session_message` ordered by `session_id, time_created`, selecting `id, session_id, type, time_created, data`.
- Group rows by `session_id` in that order; per session compute `legacy = not any(kind == "idle")`, then for each `assistant` row at position `i` set `turn = _turn_index(types, i + 1, legacy)`.
- Parse `data` with `json.loads`; a `TypeError`/`ValueError` bumps `skipped` and continues (mirror `parse_model`'s tolerance — a non-dict blob is an extraction failure, not a row error, so it stores `step_ms=None, tokens_generated=0`).
- `step_ms = max(0, streamed - (firstToken if firstToken is not None else created))` when `streamed` is present, else `None`. `tokens_generated = (output or 0) + (reasoning or 0)`.
- Upsert by `message_id` with the same `existing is None` / `merged = [max(e, v) ...]` / `changed` pattern as `import_unsloth_runs`. `step_ms` merges as `max(existing, new)` treating `None` as "no information": keep the stored value when the new one is `None`, and take the new value when the stored one is `None`. `turn` and `time_created` are written from the freshly computed value.
- Commit once at the end; wrap each row in `try/except sqlite3.Error` bumping `skipped`.

- [ ] **Step 5: Call it from `record()`**

At the end of `record`, just before `dest.commit()`, merge the step counts into `record`'s own dict so one `record` call reports one honest total:

```python
steps = import_opencode_steps(src, dest)
for key, value in steps.items():
    counts[key] = counts.get(key, 0) + value
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.ImportStepsTests -v`
Expected: PASS

- [ ] **Step 7: Run the full suite and commit**

Run: `python3 -m unittest test_tokenstat` — Expected: all green. Existing `RecordTests` count pins must still pass; a fake `session_v2`-only source makes the steps pass a no-op.

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: ingest OpenCode assistant steps during record"
```

---

## Task 3: Session rows report the mean of their turn rates

**Files:**
- Modify: `tokenstat.py` — module constants `_TURN_RATE_SQL`, `_GENERATED_SQL`; `query_sessions` (~line 597)
- Test: `test_tokenstat.py` — rewrite `SessionTokSTests` rate pins; new `SessionTurnRateTests`

**Interfaces:**
- Consumes: `opencode_steps` from Tasks 1–2.
- Produces:
  - `_TURN_RATE_SQL` — SQL scalar subquery expression, correlated on `usage.session_id`, yielding the session's mean turn rate or `NULL`.
  - `_GENERATED_SQL` — SQL scalar subquery expression, correlated on `usage.session_id`, yielding `Σ tokens_generated` or `NULL`.
  - `query_sessions` rows gain `rate_weight` (int) and `tok_s` now means the turn-mean rate.

- [ ] **Step 1: Write the failing rate tests**

```python
class SessionTurnRateTests(unittest.TestCase):
    """A session's TOK/S is the unweighted mean of its turns' rates, matching
    OpenCode's per-prompt number: Σ(output+reasoning) / Σ(streamed - firstToken)
    per turn, then averaged across turns."""

    BASE = 1_764_000_000_000

    def dest_for(self, messages, rows=(src_row(),)):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(list(rows), list(messages))
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def turn(self, mid, first_off, stream_off, out, reasoning=0, sid="s1"):
        return step_msg(mid, sid=sid, first=self.BASE + first_off,
                        streamed=self.BASE + first_off + stream_off,
                        out=out, reasoning=reasoning)
```

Pins to fill in as tests on `query_sessions(dest)[0]`:

- **Two turns average their rates.** Turn 1: one step, `first_off=0, stream_off=1000, out=100` → 100.0 tok/s. Turn 2 (after an `idle` + `user`): one step, `first_off=0, stream_off=2000, out=100` → 50.0 tok/s. Session `tok_s == 75.0`.
- **Steps inside one turn pool before dividing.** Two steps in one turn, each `stream_off=1000`, `out=100` and `out=300` → `(400 / 2.0) == 200.0`, not the mean of 100.0 and 300.0.
- **A turn with a never-streamed step contributes nothing.** One valid turn at 100.0 plus a second turn containing a `streamed=None` step → `tok_s == 100.0` and the bad turn is dropped, not averaged as 0.
- **A session with no steps has `tok_s is None`.**
- **Sub-second turns are dropped.** A turn whose `Σ step_ms < 1000` → excluded; if it is the only turn, `tok_s is None`.
- **`rate_weight` is the session's generated tokens** — sum of `tokens_generated` across its steps.
- **Reference shape:** eight turns reproducing the reference session's rates `[63.9, 70.6, ...]` is impractical here; instead assert the mean of two turns with non-round values, e.g. turn rates `1000/3` and `1000/7` → `tok_s == round((1000/3 + 1000/7) / 2, 1)`.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.SessionTurnRateTests -v`
Expected: FAIL — rates still computed from wall clock.

- [ ] **Step 3: Add the two SQL constants above `query_sessions`**

No JSON1; plain aggregation over `opencode_steps`. The `HAVING` clause encodes OpenCode's guards: no never-streamed step in the turn (`COUNT(step_ms) = COUNT(*)`), positive tokens, and a turn span of at least one second — the same sub-second floor `_aggregate_rate` applies, per the Global Constraints.

```python
_TURN_RATE_SQL = (
    "(SELECT AVG(rate) FROM (SELECT SUM(tokens_generated) * 1000.0"
    " / SUM(step_ms) AS rate FROM opencode_steps"
    " WHERE opencode_steps.session_id = usage.session_id GROUP BY turn"
    " HAVING COUNT(step_ms) = COUNT(*) AND SUM(step_ms) >= 1000"
    " AND SUM(tokens_generated) > 0))")
_GENERATED_SQL = (
    "(SELECT SUM(tokens_generated) FROM opencode_steps"
    " WHERE opencode_steps.session_id = usage.session_id)")
```

- [ ] **Step 4: Rewrite `query_sessions` to use them**

Append `, " + _TURN_RATE_SQL + ", " + _GENERATED_SQL` to the SELECT list, unpack the two extra values, and set `"tok_s": round(rate, 1) if rate is not None else None` and `"rate_weight": generated or 0`. `time_updated` stays in the query only if still needed by other row fields; drop it from the rate computation. `total` keeps meaning `tokens_input + tokens_output` — the spec is explicit that the new weight does not redefine the TOTAL column. Update the docstring: the rate is the mean of the session's turn rates, `None` when the session has no valid turn.

- [ ] **Step 5: Update the stale wall-clock pins in `SessionTokSTests`**

`test_session_rate_is_tokens_over_wall_clock_span` and `test_session_zero_span_rate_is_none` pin the removed behaviour. Retitle and repoint them at the new rule using `make_step_src`: a session with no steps has `tok_s is None`; a session whose only turn spans under a second has `tok_s is None`. Keep `test_session_rows_carry_cache_components` and `test_event_rows_have_none_rate` untouched.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.SessionTurnRateTests test_tokenstat.SessionTokSTests -v`
Expected: PASS

- [ ] **Step 7: Run the full suite and commit**

Run: `python3 -m unittest test_tokenstat` — Expected: all green.

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: session tok_s is the mean of its turn rates"
```

---

## Task 4: Aggregates and the hero weight on generated tokens

**Files:**
- Modify: `tokenstat.py` — `_session_rate_groups` (~line 383), `report_header` (~line 932), `query_runs` (~line 424)
- Test: `test_tokenstat.py` — update `AggregateWeightedRateTests` and `HeroTokSTests`; new `AggregateTurnWeightTests`

**Interfaces:**
- Consumes: `_TURN_RATE_SQL`, `_GENERATED_SQL` from Task 3.
- Produces: `_session_rate_groups(dest, key_sql, where, params) -> dict[str, list[tuple[float | None, int]]]` with the same signature but turn-derived rates and generated-token weights. Every rate-bearing row dict carries `rate_weight`.

- [ ] **Step 1: Write the failing aggregate tests**

```python
class AggregateTurnWeightTests(unittest.TestCase):
    """Daily/total/hero TOK/S is the token-weighted mean of the session rates
    it folds, weighted by GENERATED tokens (the numerator being averaged), not
    by in+out."""
```

Pins, built with `make_step_src` so each session has real turns:

- **Weight is generated tokens, not in+out.** Session A: `tokens_input=100_000, tokens_output=100`, one turn at 100.0 tok/s. Session B: `tokens_input=1_000, tokens_output=1_000`, one turn at 50.0 tok/s. Under the old weight A dominates; under generated-token weight the mean is `round((100.0*100 + 50.0*1000) / 1100, 1) == 54.5`. Assert `total_totals(dest)[0]["tok_s"] == 54.5`.
- **A session with no rate drops out of both sides.** Add a third session with no steps → the aggregate is unchanged from the two-rate case, not diluted.
- **`daily_totals` matches `total_totals`** when every session falls on one day.
- **The hero matches the table.** `report_header("", total_totals(dest))` contains the same `N.N tok/s` string as the table's TOK/S cell.
- **`--from-runs` is unchanged:** run rows still carry `rate_weight == progress_tokens`, and the runs hero is byte-identical to before the change.

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.AggregateTurnWeightTests -v`
Expected: FAIL — weights still come from `tokens_input + tokens_output`.

- [ ] **Step 3: Rewrite `_session_rate_groups`**

Keep the name and signature. Replace the body's SQL so each matching session contributes `(turn-mean rate, generated tokens)`:

```python
sql = ("SELECT " + key_sql + ", " + _TURN_RATE_SQL + ", " + _GENERATED_SQL
       + " FROM usage")
```

Append `(rate, generated or 0)` per row. Update the docstring: rates are turn-derived and weights are generated tokens, so an aggregate stays reproducible from the rates the sessions view displays.

- [ ] **Step 4: Make the hero use `rate_weight`**

In `report_header`, change the weighted-mean input to prefer the row's own weight:

```python
rate = _weighted_mean_rate([(r.get("tok_s"),
                             r.get("rate_weight", r.get("total") or 0))
                            for r in rows])
```

Views whose rows carry no `rate_weight` keep behaving exactly as before.

- [ ] **Step 5: Give run rows an explicit `rate_weight`**

In `query_runs`, add `"rate_weight": progress` to the row dict. The value equals the existing `total`, so no displayed number moves; the key just makes both sources state their own weight instead of inheriting `total`.

- [ ] **Step 6: Update the stale weight pins**

`AggregateWeightedRateTests` and `HeroTokSTests` assert the in+out weighting. Update their fixtures to add steps (or restate their expected values) so they pin the generated-token weight. Keep their structural assertions — that aggregates are weighted means, not calendar spans, and that rate-less rows drop out of both sides.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.AggregateTurnWeightTests test_tokenstat.AggregateWeightedRateTests test_tokenstat.HeroTokSTests test_tokenstat.RunsQueryTests -v`
Expected: PASS

- [ ] **Step 8: Run the full suite and commit**

Run: `python3 -m unittest test_tokenstat` — Expected: all green.

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: weight tok/s aggregates by generated tokens"
```

---

## Task 5: Live verification and documentation

**Files:**
- Modify: `AGENTS.md`, `README.md`
- Verify against: `usage.db`, `~/.local/share/opencode/opencode.db`

**Interfaces:**
- Consumes: everything above.
- Produces: documentation matching shipped behaviour; a verified reference number.

- [ ] **Step 1: Backfill and check the reference session**

```bash
python3 tokenstat.py record
python3 tokenstat.py --model halogen-qwen3.8-flash-next
```

Expected: the `ses_e14c64b0bffe3hKJ8qniK45xCS` row renders **70.6** in TOK/S (was 13.1). If it differs, stop and reconcile against the spec's turn rules before continuing — do not adjust the test to match the output.

- [ ] **Step 2: Verify coverage and idempotence**

```bash
python3 tokenstat.py record          # second run
python3 tokenstat.py --total --model
python3 tokenstat.py --from-runs
python3 tokenstat.py --from-unsloth --total
```

Expected: the second `record` reports `inserted=0`; per-model TOK/S values are plausible (no zeros, no blanks for models with real traffic); `--from-runs` and `--from-unsloth` output is unchanged from before this branch.

- [ ] **Step 3: Verify the suite is warning-clean**

Run: `python3 -W error::ResourceWarning -m unittest test_tokenstat`
Expected: all green, no `ResourceWarning`.

- [ ] **Step 4: Update `AGENTS.md`**

- In the "Tokens-per-second increment" note, replace "session rows show a WALL-CLOCK average — tokens over their own created→updated span, idle drags it down" with the turn-mean rule.
- In the "Aggregate TOK/S increment" note, record that the weight is now generated tokens and why.
- Extend invariant 1 to name `opencode_steps` (one row per `message_id`; `step_ms`/`tokens_generated` monotonic max; rows never deleted, so ingested steps survive OpenCode's pruning).
- Add the new spec path to the design-doc list.
- Update the "State at handoff" store line to include the step count.

- [ ] **Step 5: Update `README.md`**

Replace the TOK/S column description with: session rows are the mean of their per-turn rates (matching OpenCode's per-prompt number); aggregates are token-weighted means weighted by generated tokens; `--from-runs` is a run's true generation rate; `--from-unsloth` per-event rows are blank.

- [ ] **Step 6: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: document turn-level tok/s and generated-token weighting"
```
