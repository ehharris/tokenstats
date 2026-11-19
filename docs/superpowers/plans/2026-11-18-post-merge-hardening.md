# Post-Merge Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The controller's recommendation for THIS plan is **executing-plans (inline)**: eight small items, all in one file, each already pinned by an existing test class, so the per-task dispatch overhead of subagent-driven development buys nothing here.

**Goal:** Close the eight hardening items the whole-branch review of the deployment-readiness increment deferred to a later pass — two real robustness gaps in `_upsert` and in the migration rollback, one unwritten contract, five test-strength gaps — with no change to any user-visible output.

**Architecture:** No new structure. Every item lands inside code that already exists: `_upsert` (the one monotonic-max ingest engine), `default_store()` (the one-time state migration), the two aggregate engines' docstrings, and four existing test classes (`UpsertEngineTests`, `MigrationTests`, `FilterHelperTests`, `DefaultPathsTests`, plus `ReviewFixTests2`).

**Tech Stack:** Python 3 (stdlib only), `unittest`, SQLite (`PRAGMA table_info`), `unittest.mock`.

**Authority:** no new spec. The binding list is the whole-branch final review's fix-after-merge items, recorded in priority order in `.superpowers/sdd/2026-11-18-deployment-readiness/progress.md` (and triaged from `deferred-minors.md` in the same folder). The deployment-readiness spec (`docs/superpowers/specs/2026-11-18-deployment-readiness-design.md`) stays binding where it touches: state layout, migration order, hidden flags, import purity.

## Global Constraints

- Stdlib only; the tool stays one file; no install step.
- `python3 -m unittest test_tokenstat` passes (427 tests today, 435 after this
  plan — 427 + 5 from Task 1 + 1 from Task 2 + 2 from Task 4), and
  `python3 -W error::ResourceWarning -m unittest test_tokenstat` passes with no warnings.
- `golden/` stays byte-identical: run the suite **without** `TOKENSTAT_UPDATE_GOLDEN`. If a golden diff appears, the change is wrong — do not re-pin.
- `--help` output stays byte-identical (nothing in this plan touches argparse).
- The invariants from `AGENTS.md` hold unchanged: monotonic-max upserts, never an increment (1); labels never shrink (3); date windows local-time and until-day inclusive via `_day_end_ms` (4); sources read-only (5).
- Importing `tokenstat` must stay side-effect-free — no `mkdir`, no migration, no file moves at import. Task 4 adds the test that pins this rather than assuming it.
- `default_store()` stays the only function that creates directories on its own, and `main()` its only caller.
- The behavior-preservation constraint that bound the previous increment is **lifted for the two ingest items only** (Task 1): the review explicitly ruled that widening `_upsert`'s per-row isolation is now allowed. It is still lifted only in the direction of "a row that used to abort the family is now skipped" — no counting rule, column class, or displayed number changes.
- Work on a branch off `master`: `git checkout -b post-merge-hardening master` (the repo has no remote, so there is no `origin/master` to branch from). Commit per task; verify before each commit. No push — there is nowhere to push to.
- Do not touch the live store at `~/.tokenstat/usage.db` or the hourly cron line; nothing in this plan needs either.

## Review Focus

- **A typo in a column class.** Expect: `ValueError` naming the table, the column and the class, raised before a single row is written — not a token column silently demoted to the label class, where `new or old` can shrink it.
- **A source whose numeric column holds text.** Expect: that row bumps `skipped` and its siblings still land, exactly as `import_opencode_steps` already isolates its bad rows. The two ingest paths must not differ in isolation width.
- **A migration that loses the store move to a rival run.** Expect: the `-wal`/`-shm` siblings we already moved stay with the winner at the state dir. Pulling them back to a legacy path that now holds nothing strands them permanently — the next run sees both paths occupied and never retries.
- **The validation running inside the per-row `try`.** It must not: a `ValueError` caught there would be counted `skipped` and the whole family would disappear one row at a time. This is the one ordering mistake this plan is easy to make.
- **Tests that pass for the wrong reason.** `test_reconcile_rejects_unsupported_flags` must prove it never opened a real database, not merely that it exited 2; the sibling-move pin must prove the files arrived with their contents, not only that the source is empty; the import-purity pin must fail if a future path helper calls `os.makedirs`.
- **Any displayed byte moving.** `golden/` plus the strict `-W error::ResourceWarning` run are the check.

---

### Task 1: Make `_upsert`'s column-class contract enforceable

Two coupled changes to one function, in this order: validate the classes first, then widen the per-row isolation. The order is not cosmetic — the widened `except` would swallow a validation `ValueError` as a `skipped` row.

**Files:**
- Modify: `tokenstat.py:275-334` (`_upsert`: docstring, a validation block before the row loop, the `except` at :330)
- Test: `test_tokenstat.py` (`UpsertEngineTests`, `tokenstat.py:98` in the test file — append after `test_a_malformed_row_bumps_skipped_and_the_rest_survive`)

**Interfaces:**
- Consumes: `dest` and `table` as already passed; `PRAGMA table_info(<table>)` (column name is field index 1).
- Produces: no new public names. `_upsert`'s signature is unchanged; it now raises `ValueError` for a class name that is not a column of `table`, is the primary key, or appears in two classes. Callers (`record`, `import_unsloth`, `import_unsloth_runs`) already pass only real columns — verified against `ensure_schema` — so no caller changes.

- [ ] **Step 1: Write the failing validation tests**

Append to `UpsertEngineTests` (the fixture table is `t (k TEXT PRIMARY KEY, model TEXT, n INTEGER, "end" INTEGER, seen INTEGER)`):

```python
    def test_a_class_name_that_is_not_a_column_is_a_hard_error(self):
        # The silent failure this prevents: a typo in numeric= leaves the real
        # column in the label class, where `new or old` can shrink a token
        # count — invariant 1, broken with no error anywhere.
        with self.assertRaises(ValueError) as cm:
            tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 1}],
                              numeric=["nn"])
        self.assertIn("nn", str(cm.exception))
        self.assertIsNone(self.stored("a"))   # and nothing was written

    def test_a_column_in_two_classes_is_a_hard_error(self):
        # Overlapping classes resolve in numeric's favor today: a mistake in
        # the caller reads like a design choice.
        with self.assertRaises(ValueError) as cm:
            tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 1}],
                              numeric=["n"], overwrite=["n"])
        self.assertIn("n", str(cm.exception))

    def test_the_primary_key_is_not_a_column_class(self):
        with self.assertRaises(ValueError):
            tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 1}],
                              numeric=["k"])

    def test_a_missing_table_is_a_hard_error_not_an_empty_run(self):
        # PRAGMA on a table that is not there reports no columns, so every
        # class name fails: a mis-wired ingest says so instead of quietly
        # counting every row skipped.
        with self.assertRaises(ValueError):
            tokenstat._upsert(self.dest, "no_such_table", "k",
                              [{"k": "a", "n": 1}], numeric=["n"])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m unittest test_tokenstat.UpsertEngineTests -v`
Expected: the four new tests FAIL with `AssertionError: ValueError not raised` (today a bad class name is silently accepted; a missing table is counted `skipped`).

- [ ] **Step 3: Implement the validation**

Insert this block at the very top of `_upsert`'s body — **before** `counts = Counter(...)` and the `if not rows: return counts` early exit, so a mis-wired ingest fails even when the source returned zero rows:

```python
    # The column classes are a contract with the table, and every way to break
    # it is silent: an unknown name in `numeric` demotes a real token column to
    # the label class, where `new or old` can shrink it (invariant 1), and a
    # column in two classes resolves in `numeric`'s favor. Checked once, up
    # front, before the first row: inside the per-row try a ValueError would
    # only count as `skipped` and the whole family would vanish one row at a
    # time. `table` is interpolated into the PRAGMA because it is a literal from
    # this module, like every other fragment this engine interpolates.
    known = {row[1] for row in dest.execute(f"PRAGMA table_info({table})")}
    if pk not in known:
        raise ValueError(f"{table} has no column {pk!r}")
    by_class = {}
    for cls, names in (("numeric", numeric), ("overwrite", overwrite),
                       ("insert_only", insert_only)):
        for name in names:
            if name not in known:
                raise ValueError(f"{table} has no column {name!r} "
                                 f"(named in the {cls} class)")
            if name == pk or name in by_class:
                raise ValueError(f"{table}: {name!r} is in both the "
                                 f"{by_class.get(name, 'primary-key')} and "
                                 f"{cls} classes")
            by_class[name] = cls
```

Then state the contract in the docstring, after the paragraph that ends "The caller owns the commit, so a family's rows land in one transaction.":

```
    Every name in `numeric`, `overwrite` and `insert_only` must be a real
    column of `table` and none of them may be `pk`; a name that is not raises
    `ValueError` before any row is written, because the alternative is silent:
    an unknown `numeric` name leaves that column in the label class, where
    invariant 1 is not protected. A payload key that is not a column is a
    different kind of mistake and lands as `skipped`, one row at a time.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.UpsertEngineTests -v`
Expected: all `UpsertEngineTests` pass, including the four pre-existing ones.

- [ ] **Step 5: Run the whole suite and commit**

```bash
python3 -m unittest test_tokenstat 2>&1 | tail -3
git add tokenstat.py test_tokenstat.py
git commit -m "fix: reject column-class names the table cannot hold"
```

Expected: `OK` (431 tests). If any ingest test now raises `ValueError`, a class name really is wrong — read it, do not delete the check.

- [ ] **Step 6: Write the failing isolation test**

Append to `UpsertEngineTests`:

```python
    def test_a_text_numeric_source_value_is_isolated_not_fatal(self):
        # max(5, "x") raises TypeError, not sqlite3.Error. import_opencode_steps
        # already isolates exactly this; the two ingest paths must not differ in
        # how wide one bad row reaches. Good row first, so the bad row is not
        # the only one covered (the malformed-row pin above covers that side).
        tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 5}],
                          numeric=["n"])
        counts = tokenstat._upsert(
            self.dest, "t", "k",
            [{"k": "a", "n": "not-a-number"}, {"k": "b", "n": 4}],
            numeric=["n"])
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(self.stored("a"), (None, 5, None, None))  # untouched
        self.assertEqual(self.stored("b"), (None, 4, None, None))
```

- [ ] **Step 7: Run it to verify it fails**

Run: `python3 -m unittest test_tokenstat.UpsertEngineTests.test_a_text_numeric_source_value_is_isolated_not_fatal -v`
Expected: ERROR — `TypeError: '>' not supported between instances of 'str' and 'int'` escapes `_upsert`.

- [ ] **Step 8: Widen the per-row guard**

Replace the `except` clause at the end of the row loop (currently `except sqlite3.Error:` with the "One malformed source row must not abort (or roll back) the rest." comment):

```python
        except (sqlite3.Error, TypeError, ValueError):
            # One malformed source row must not abort (or roll back) the rest:
            # sqlite3 refuses a value it cannot bind, and a text-typed numeric
            # column makes the max() merge compare str to int. Same width as
            # import_opencode_steps' per-row guard, for the same reason. The
            # column-class check above runs before this loop precisely so a
            # ValueError from a programming mistake is never counted here.
            counts["skipped"] += 1
```

- [ ] **Step 9: Run the full suite, both variants, and commit**

```bash
python3 -m unittest test_tokenstat 2>&1 | tail -3
python3 -W error::ResourceWarning -m unittest test_tokenstat 2>&1 | tail -3
git status --porcelain golden/
git add tokenstat.py test_tokenstat.py
git commit -m "fix: isolate a bad _upsert row as widely as import_opencode_steps does"
```

Expected: `OK` twice (432 tests), `golden/` clean.

---

### Task 2: Never strand a WAL sibling when the store move is lost

`default_store()` moves `-wal` and `-shm` before `usage.db`, and rolls the moved files back when a move fails. If the move failed because a **rival run already moved the store**, rolling back pulls our siblings back to a legacy path that now holds nothing — orphaned forever, because the next run sees both paths occupied and never retries. The gate is one `if`. The task also strengthens the sibling pin that made this gap invisible.

**Files:**
- Modify: `tokenstat.py:117-126` (the `except OSError` rollback block inside `default_store`)
- Test: `test_tokenstat.py` (`MigrationTests`, around :4285)

**Interfaces:**
- Consumes: nothing new; `os.path.exists(old_db)` is already the arbiter two lines below the rollback.
- Produces: no signature change. `default_store(state=None, legacy=None)` still returns the path to use and still decides "by what is on disk, not by what was attempted".

- [ ] **Step 1: Strengthen the existing sibling pin (no code change yet)**

Replace `test_wal_and_shm_siblings_move_too` in `MigrationTests` with a version that says what actually moved, where it landed, and keeps stderr out of the suite's output:

```python
    def test_wal_and_shm_siblings_move_too(self):
        for suffix in ("", "-wal", "-shm"):
            with open(os.path.join(self.legacy, "usage.db" + suffix), "w") as fh:
                fh.write(suffix)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, os.path.join(self.state, "usage.db"))
        self.assertIn("migrated store:", err.getvalue())
        for suffix in ("", "-wal", "-shm"):
            self.assertFalse(os.path.exists(
                os.path.join(self.legacy, "usage.db" + suffix)))
            # Arrived with its bytes, not merely with its name: an empty file
            # where a WAL was is how a corrupt store starts.
            self.assertEqual(read_text(
                os.path.join(self.state, "usage.db" + suffix)), suffix)
```

Run: `python3 -m unittest test_tokenstat.MigrationTests.test_wal_and_shm_siblings_move_too -v`
Expected: PASS (this is a pin-strengthening change, not a bug fix — it also removes the `migrated store:` line the suite printed on every run).

```bash
python3 -m unittest test_tokenstat 2>&1 | tail -3
git add test_tokenstat.py
git commit -m "test: say what the sibling-move pin actually proves"
```

Expected: `OK` (427 tests, unchanged count).

- [ ] **Step 2: Write the failing lost-race test**

Append to `MigrationTests`:

```python
    def test_a_lost_race_leaves_the_siblings_with_the_winner(self):
        # A rival run moves usage.db while we are mid-migration, so our own
        # store move finds nothing left to move. The siblings we already moved
        # belong with the winner at the state dir; pulling them back would
        # strand them beside a legacy path that now holds nothing, and the next
        # run — seeing both paths occupied — would never try again.
        for suffix in ("", "-wal", "-shm"):
            with open(os.path.join(self.legacy, "usage.db" + suffix),
                      "w") as fh:
                fh.write(suffix)
        old_db = os.path.join(self.legacy, "usage.db")
        real = os.replace

        def rival_wins(src, dst):
            if src == old_db:          # the store move: we just lost the race
                real(src, os.path.join(self.state, "usage.db"))
                raise OSError("no such file or directory")
            return real(src, dst)

        with unittest.mock.patch.object(tokenstat.os, "replace",
                                        side_effect=rival_wins):
            with contextlib.redirect_stderr(io.StringIO()):
                used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, os.path.join(self.state, "usage.db"))
        self.assertEqual(read_text(os.path.join(self.state, "usage.db")), "")
        for suffix in ("-wal", "-shm"):
            self.assertFalse(os.path.exists(old_db + suffix))
            self.assertTrue(os.path.exists(
                os.path.join(self.state, "usage.db" + suffix)))
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python3 -m unittest test_tokenstat.MigrationTests.test_a_lost_race_leaves_the_siblings_with_the_winner -v`
Expected: FAIL — `usage.db-wal` is back in the legacy directory (the rollback ran although the store was gone).

- [ ] **Step 4: Gate the rollback**

In `default_store`'s `except OSError as exc:` block, wrap the rollback loop:

```python
        except OSError as exc:
            # Pull the siblings back only while the legacy store is still
            # there. If it is gone, a rival run moved it: our files belong
            # with it at the state dir, and moving them back would orphan them
            # beside a path that holds nothing — permanently, because the next
            # run sees both paths occupied and never retries. This is also
            # what a double fault (the forward move failed AND the reverse one
            # does) leaves behind, so one gate closes both.
            if os.path.exists(old_db):
                for suffix in reversed(moved):
                    try:
                        os.replace(new_db + suffix, old_db + suffix)
                    except OSError:
                        pass
```

Leave the two lines after it (`used = old_db if os.path.exists(old_db) else new_db` and the warning) exactly as they are — they already decide by what is on disk.

- [ ] **Step 5: Run the migration tests, then the suite, and commit**

```bash
python3 -m unittest test_tokenstat.MigrationTests -v
python3 -m unittest test_tokenstat 2>&1 | tail -3
python3 -W error::ResourceWarning -m unittest test_tokenstat 2>&1 | tail -3
git add tokenstat.py test_tokenstat.py
git commit -m "fix: leave migrated WAL siblings with the run that won the store"
```

Expected: `MigrationTests` all pass, `OK` twice (433 tests), no `migrated store:` noise in the suite output.

---

### Task 3: Write down the aggregate engines' trusted-fragment contract

`_grouped_aggregates` and `_total_aggregates` build SQL by interpolation. There is no injection path today — every caller passes a module literal and user text arrives only through `where`/`params` — but the contract is unwritten, so the next caller cannot see it. Docstring only.

**Files:**
- Modify: `tokenstat.py:786-812` (`_grouped_aggregates`) and `tokenstat.py:813-833` (`_total_aggregates`)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing callable. No signature, no SQL, no row shape changes.

- [ ] **Step 1: Add the contract to `_grouped_aggregates`**

Append this paragraph to its docstring, after the paragraph ending "Rows come back in query order.":

```
    `table`, `key_sql` and `sums` are SQL fragments, interpolated into the
    statement rather than bound into it, so they are trusted internal values
    only: every caller passes a literal from this module (or a builder built
    from one). User input reaches a query through `where` and `params`, which
    are parameterized — nothing derived from a command line may be passed
    here. Same contract for `_run_rate_groups` and `_session_rate_groups`.
```

- [ ] **Step 2: Add it to `_total_aggregates`**

Append to its docstring, after "whose rate group is `groups.get("(all)", [])`":

```
    `table`, `sums` and `group_key_sql` are the same trusted internal SQL
    fragments _grouped_aggregates documents, and are subject to the same rule.
```

- [ ] **Step 3: Verify nothing but docstrings moved, and commit**

```bash
git diff --stat
python3 -m unittest test_tokenstat 2>&1 | tail -3
git add tokenstat.py
git commit -m "docs: state the trusted-fragment contract on the aggregate engines"
```

Expected: `git diff --stat` shows only `tokenstat.py` with a handful of added lines and zero deletions outside the two docstrings; `OK`.

---

### Task 4: Make three test promises real

Three test-only gaps: no test exercises an until-only date window (an `elif` regression in `_date_clauses` would pass the suite); `test_reconcile_rejects_unsupported_flags` still opens the live OpenCode and Studio databases before the guard it is testing fires; and the import-purity rule — importing `tokenstat` must not touch the filesystem — has no pin at all.

**Files:**
- Test: `test_tokenstat.py` (`FilterHelperTests` :4550, `ReviewFixTests2` :1879, `DefaultPathsTests` :4204)
- No change to `tokenstat.py`.

**Interfaces:**
- Consumes: `tokenstat._date_clauses`, `tokenstat._connect_src`, `tokenstat.__file__`, the `CliHarness`-style flag names `--src` / `--unsloth-db`.
- Produces: nothing.

- [ ] **Step 1: Add the until-only window assertion**

Append to `FilterHelperTests`:

```python
    def test_until_alone_is_still_a_half_open_window(self):
        # The other half of the window, which nothing exercised: `until` alone
        # must still give the exclusive day-end bound and nothing else. The
        # both-bounds pin above cannot cover this side — verified by mutation,
        # this is the only test that fails when the until clause is made to
        # depend on `since` being present.
        until = ms(2026, 11, 3)
        where, params = tokenstat._date_clauses("created_at", None, until)
        self.assertEqual(where, ["created_at < ?"])
        self.assertEqual(params, [tokenstat._day_end_ms(until)])
```

Run: `python3 -m unittest test_tokenstat.FilterHelperTests -v`
Expected: PASS (a coverage gap closed, not a bug).

- [ ] **Step 2: Prove the reconcile guard test never opens a real database**

Replace `test_reconcile_rejects_unsupported_flags` in `ReviewFixTests2`:

```python
    def test_reconcile_rejects_unsupported_flags(self):
        # Hermetic on every path. An explicit --db keeps the default store
        # (~/.tokenstat, which only exists once the migration has run) out of
        # it, and --src/--unsloth-db keep reconcile's implicit ingestion out
        # of the live OpenCode and Studio databases: the guard fires after
        # that ingestion, so without these the test reads real user data
        # before it ever reaches the exit 2 it is asserting.
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            real = tokenstat._connect_src

            def spy(path):
                seen.append(path)
                return real(path)

            with unittest.mock.patch.object(tokenstat, "_connect_src", spy):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        main(["--db", os.path.join(tmp, "usage.db"),
                              "--src", os.path.join(tmp, "none.db"),
                              "--unsloth-db", os.path.join(tmp, "none.db"),
                              "reconcile", "--since", "2026-11-01"])
        self.assertEqual(cm.exception.code, 2)
        # The proof, not just the exit code: every source this run opened was
        # inside the temp dir.
        self.assertTrue(seen)
        for path in seen:
            self.assertTrue(path.startswith(tmp), path)
```

Run: `python3 -m unittest test_tokenstat.ReviewFixTests2.test_reconcile_rejects_unsupported_flags -v`
Expected: PASS. If it fails on the last assertion, the flags are not reaching `_ingest` — check that `--src`/`--unsloth-db` were placed before the `reconcile` positional (they are global options, so they must be).

- [ ] **Step 3: Add the import-purity pin**

Append to `DefaultPathsTests`, next to `test_module_defaults_point_at_the_state_dir` (same fresh-exec harness, same local `import importlib.util` style):

```python
    def test_importing_creates_nothing(self):
        # Import purity, pinned instead of assumed: the migration is
        # default_store()'s job and only main() calls it, so a fresh import
        # under an empty $HOME must leave that $HOME empty. A stray
        # os.makedirs in a path helper — or at module scope — shows up here as
        # a directory nobody asked for.
        import importlib.util
        with tempfile.TemporaryDirectory() as home:
            spec = importlib.util.spec_from_file_location(
                "tokenstat_pure", tokenstat.__file__)
            fresh = importlib.util.module_from_spec(spec)
            with unittest.mock.patch.dict(os.environ, {"HOME": home,
                                                       "XDG_DATA_HOME": ""}):
                spec.loader.exec_module(fresh)
            self.assertEqual(os.listdir(home), [])
            self.assertEqual(fresh.STATE_DIR, home + "/.tokenstat")
```

Run: `python3 -m unittest test_tokenstat.DefaultPathsTests -v`
Expected: PASS (a pin for a rule that currently holds).

- [ ] **Step 4: Run the suite, both variants, and commit**

```bash
python3 -m unittest test_tokenstat 2>&1 | tail -3
python3 -W error::ResourceWarning -m unittest test_tokenstat 2>&1 | tail -3
git status --porcelain golden/
git add test_tokenstat.py
git commit -m "test: pin the until-only window, a hermetic reconcile guard, import purity"
```

Expected: `OK` twice (435 tests), `golden/` clean.

---

### Task 5: Record the pass

**Files:**
- Modify: `AGENTS.md` (the deployment-readiness increment paragraph)

- [ ] **Step 1: Add one sentence where `_upsert` and the migration are described**

In the "Deployment-readiness increment (2026-11-18)" paragraph, after the sentence describing `_upsert`'s column classes, add:

```
A later hardening pass (same day, plan
`docs/superpowers/plans/2026-11-18-post-merge-hardening.md`) made `_upsert`
reject a column-class name the table does not have — before writing any row, so
a typo cannot demote a token column to the label class — widened its per-row
isolation to `(sqlite3.Error, TypeError, ValueError)` to match
`import_opencode_steps`, and gated the migration rollback on the legacy store
still being there, so a move lost to a rival run leaves the `-wal`/`-shm`
siblings with the winner instead of stranding them.
```

- [ ] **Step 2: Final verification sweep**

```bash
python3 -m unittest test_tokenstat 2>&1 | tail -3
python3 -W error::ResourceWarning -m unittest test_tokenstat 2>&1 | tail -3
git status --porcelain golden/
python3 tokenstat.py --help | head -3
python3 tokenstat.py --total | tail -3
```

Expected: `OK` twice (435 tests), `golden/` clean, `-h` unchanged, and a real report from the live store with exit 0 (this run is read-only against `~/.tokenstat/usage.db` apart from the usual idempotent snapshot).

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: record the post-merge hardening pass"
```

---

## After the plan

Verify on `master` after the fast-forward merge: `python3 -m unittest test_tokenstat` (435 OK), the `-W error::ResourceWarning` variant, `git status --porcelain golden/` empty, and `git log --oneline master ^post-merge-hardening` empty. Then delete the branch. Update `.superpowers/sdd/` with a short ledger entry for this increment (gitignored — no commit).
