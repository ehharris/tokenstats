# Sort share views by share descending — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-model totals tables (share views) list the biggest-share model first instead of alphabetically.

**Architecture:** One stable sort on `share` descending applied in `main()` right after `add_shares(rows)` in the `want_total` branch, before the `--json`/table split, so both renderings share the order. Ties keep the incoming alphabetical-by-model order (stable sort). No query, store, or non-share-view changes.

**Tech Stack:** stdlib Python; unittest suite in `test_tokenstat.py`.

**Spec:** `docs/superpowers/specs/2026-11-15-sort-by-share-desc-design.md`

## Global Constraints

- Stdlib only; everything runs from `/home/elih/messinaround/token_counter`.
- Tests: `python3 -m unittest test_tokenstat -v` (~0.2s, no deps); suite must stay warning-clean under `-W error::ResourceWarning`.
- Session/daily/reconcile ordering unchanged; numbers unchanged — display order only.

## Review Focus

- Tied shares across different models: expected order is alphabetical by model among the tie (stable sort over SQL's `ORDER BY model ASC`) — pinned in Task 1 Step 1's tie test.
- Empty per-model result with `--total --model`: must still print `no sessions match` and not crash — sorting `[]` is a no-op; covered by existing empty-result tests, re-run in Task 1 Step 4's full-suite run.
- Grand-total single row (`--total` bare): unaffected by the sort — pinned by existing `test_total_grand_row_shares_full`, re-run in Task 1 Step 4.

---

### Task 1: Share-descending order for share views

**Files:**
- Modify: `tokenstat.py` (in `main()`, the `want_total` branch, immediately after `rows = add_shares(rows)`)
- Test: `test_tokenstat.py` (`CliShareTests`)

**Interfaces:**
- Consumes: `add_shares(rows)` → rows carrying float `"share"` and `"total"` (unchanged).
- Produces: share-view `rows` list ordered by `share` desc, ties alphabetical; same order reaches table and `--json`.

- [ ] **Step 1: Write the failing tests** — add to `CliShareTests`:

```python
    def test_per_model_rows_sort_by_share_desc(self):
        # fixture models: unsloth-studio total 10k (66.7%) vs lemonade 5k
        # (33.3%); alphabetical order would put lemonade first, so assert
        # the unsloth line precedes the lemonade line
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        u = next(i for i, ln in enumerate(lines) if "unsloth-studio" in ln)
        l = next(i for i, ln in enumerate(lines) if "lemonade" in ln)
        self.assertLess(u, l)

    def test_share_json_sorted_desc_ties_alphabetical(self):
        # own fixture: three models, one big (50%) and a tie at 12.5% each;
        # expected order: zzz/zzz, then the tied pair alphabetically
        src = os.path.join(self.tmp.name, "src3.db")
        aaa_one = '{"id":"one","providerID":"aaa"}'
        aaa_two = '{"id":"two","providerID":"aaa"}'
        zzz = '{"id":"zzz","providerID":"zzz"}'
        self.addCleanup(make_file_db(src, [
            src_row(sid="t1", model=zzz, tin=9000, tout=1000),
            src_row(sid="t2", model=aaa_one, tin=2250, tout=250),
            src_row(sid="t3", model=aaa_two, tin=2250, tout=250),
        ]).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "usage3.db"),
             "--src", src, "--unsloth-db", self.no_udb,
             "--total", "--model", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["model"] for r in rows],
                         ["zzz/zzz", "aaa/one", "aaa/two"])

```

Note: `src_row` defaults carry `tcr=7000, tcw=100, treas=50` — totals are `tokens_input + tokens_output` only, so the tie holds regardless. JSON rows carry `"model": provider || '/' || model_id` (verified against live output).

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_tokenstat.CliShareTests -v`
Expected: the two new tests FAIL (alphabetical order first); all existing pass.

- [ ] **Step 3: Implement** — in `tokenstat.py` `main()`, insert directly after `rows = add_shares(rows)`:

```python
            rows.sort(key=lambda r: -r["share"])  # biggest share first;
```

plus a one-line comment noting ties keep SQL's alphabetical-by-model order via stable sort. (Equivalent expression is fine as long as ties stay alphabetical and non-share views are untouched.)

- [ ] **Step 4: Run the full suite, strict variant included**

Run: `python3 -m unittest test_tokenstat` then
`python3 -W error::ResourceWarning -m unittest test_tokenstat`
Expected: all PASS (143 tests), warning-free.

- [ ] **Step 5: Commit**

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: sort share views by share desc (ties alphabetical)"
```

### Task 2: Docs — record the new default order

**Files:**
- Modify: `README.md` (wherever per-model/`--total --model` output is shown or described, note biggest-share-first)
- Modify: `AGENTS.md` (`State at handoff` paragraph: append one sentence; and if any "Known gotchas" line describes share tables as alphabetical, correct it)

**Interfaces:**
- Consumes: shipped behavior from Task 1.
- Produces: nothing (docs only).

- [ ] **Step 1: Update README.md and AGENTS.md** — one sentence each: share views (`--total --model`, bare `--model`) sort by SHARE descending, ties alphabetical; session/daily views unchanged. Match the surrounding prose style.
- [ ] **Step 2: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: note share-desc default order for share views"
```
