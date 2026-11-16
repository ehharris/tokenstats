# Default Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bare `python3 tokenstat.py` renders a three-panel dashboard: clean hero, top-5 models by share, and the 5 most recent sessions (newest first).

**Architecture:** One isolated compact-mode branch in `main()`'s report path guarded by a "truly bare invocation" check; a module-level `_table_panel` helper renders each table panel; `panel()` gains empty-title support for the hero strip. All flagged views stay byte-identical.

**Tech Stack:** stdlib-only Python (`tokenstat.py`), `unittest` (`test_tokenstat.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-15-default-dashboard-design.md`

## Global Constraints

- stdlib only; no install step; file stays runnable as `python3 tokenstat.py`.
- Table copy uses the exact panel titles: hero title is empty (strip renders `╭ tokenstat ...`, no `·`), model panel `usage by model · top 5 by share`, sessions panel `sessions · 5 most recent`.
- Compact mode triggers only when `args.command is None` AND all of these are at defaults: `args.model is None`, `not args.daily`, `not args.total`, `not args.from_unsloth`, `not args.from_runs`, `args.since is None`, `args.until is None`, `args.min_in is None`, `args.min_out is None`. `--cost`/`--cache` overlays still apply.
- `--json` output is completely unchanged (bare `--json` = all session rows, flat array).
- SHARE over ALL models, then truncate to 5 rows (`add_shares` before slicing; sort by `-share`, stable so ties keep alphabetical-by-model order — same convention as the existing share view).
- Sessions panel: reverse of `query_sessions` (oldest-first) sliced to first 5 → newest first.
- Empty store (no session rows): print plain `no sessions match` and return 0, no panels.
- The full test suite must pass, including `python3 -W error::ResourceWarning -m unittest test_tokenstat`.

## Review Focus

- **Empty store, bare run** → plain `no sessions match`, no panels (Task 2 test).
- **Fewer than 5 sessions or models** → show them all, no crash, no filler rows (Task 2 test).
- **`--cost` and `--cache` together in compact mode** → CACHE before TOTAL, COST last, on both table panels (Task 2 test).
- **Piped (non-tty) output** → compact renders fine without color; tests run under `redirect_stdout` which is already non-tty, so every Task 2 test pins this implicitly.

---

### Task 1: `panel()` empty-title support

**Files:**
- Modify: `tokenstat.py` — `panel()` (the function starting `def panel(title, lines, theme=SUNSET):`)
- Test: `test_tokenstat.py` — new class appended at end of file

**Interfaces:**
- Consumes: nothing new.
- Produces: `panel("", lines)` returns a frame whose top strip is `╭ tokenstat ` + border fill, with no `·`; same uniform-visible-width rule as before. `report_header("", rows)` therefore renders a clean hero strip (it just forwards its title to `panel`).

- [ ] **Step 1: Write the failing tests** — append to `test_tokenstat.py` (`_ANSI_RE`, `_visible`, `panel`, `report_header` are already imported at the top):

```python
class PanelEmptyTitleTests(unittest.TestCase):
    def test_empty_title_drops_the_separator(self):
        first = _ANSI_RE.sub("", panel("", ["content here"])[0])
        self.assertTrue(first.startswith("\u256d tokenstat "))
        self.assertNotIn("\u00b7", first)

    def test_empty_title_keeps_uniform_width(self):
        lines = panel("", ["a", "longer content line"])
        self.assertEqual(len({_visible(ln) for ln in lines}), 1)

    def test_report_header_forwards_empty_title(self):
        rows = [{"total": 100, "tokens_input": 60, "tokens_output": 40,
                 "date": "2026-11-15"}]
        lines = report_header("", rows)
        self.assertNotIn("\u00b7", _ANSI_RE.sub("", lines[0]))
        self.assertIn("100 tokens", _ANSI_RE.sub("", lines[1]))
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_tokenstat.PanelEmptyTitleTests -v` (from `/home/elih/messinaround/token_counter`)
Expected: FAIL — first line contains `tokenstat · ` (the current unconditional strip).

- [ ] **Step 3: Implement in `panel()`** — replace the two lines

```python
    head = " tokenstat · "
    strip = f"{head}{title} "
```

with a conditional: when `title` is non-empty keep both lines exactly as today; when empty, `head = strip = " tokenstat "` (one space each side). In the `top = ...` line, the middle piece `paint(title + " ", "label")` must render as `""` for an empty title (guard with `if title else ""` — note `paint(" ")` is NOT falsy). Everything else in `panel()` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.PanelEmptyTitleTests -v` → PASS
Run: `python3 -m unittest test_tokenstat` → all green (no existing behavior changed; non-empty titles take the untouched branch).

- [ ] **Step 5: Commit**

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: panel() renders a clean 'tokenstat' strip for an empty title"
```

---

### Task 2: Compact default dashboard

**Files:**
- Modify: `tokenstat.py` — new module-level `_table_panel()` placed immediately after `report_header()`; compact branch in `main()` inserted after the three implicit `_ingest(...)` calls and **before** the `model_any = args.model is MODEL_ANY` line.
- Test: `test_tokenstat.py` — new class `DefaultDashboardTests` appended at end of file.

**Interfaces:**
- Consumes: Task 1's clean empty-title hero via `report_header("", ...)`; existing `query_sessions`, `total_totals`, `add_shares`, `paint_bars`, `paint_cells`, `render_table`, `panel`, `annotate_cost`, `annotate_cache`, `with_cache`, `SHARE_COLUMNS`, `SESSION_COLUMNS`, `COST_COLUMN`, `_color_enabled`, `SUNSET`.
- Produces: `_table_panel(title, rows, columns, share_view, color) -> list[str]` — panel lines of the rendered table; pads bars when `share_view`; dims the header row only when `color`. (Only used by the compact branch in this plan.)

- [ ] **Step 1: Write the failing tests** — append to `test_tokenstat.py`. Reuse the module's existing helpers (`src_row`, `make_file_db`, `UNSLOTH_JSON`, `LEMONADE_JSON`, `ms`):

```python
class DefaultDashboardTests(unittest.TestCase):
    """Bare tokenstat run: hero + top-5 models + 5 most recent sessions."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")
        other = '{"id":"m3","providerID":"prov3"}'
        # 7 sessions, 3 models, increasing time_created (s1 oldest).
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", title="t-s1", created=ms(2026, 11, 1)),
            src_row(sid="s2", title="t-s2", model=LEMONADE_JSON,
                    created=ms(2026, 11, 2)),
            src_row(sid="s3", title="t-s3", created=ms(2026, 11, 3)),
            src_row(sid="s4", title="t-s4", model=other, tin=5_000_000,
                    tout=900_000, tcr=0, created=ms(2026, 11, 4)),
            src_row(sid="s5", title="t-s5", created=ms(2026, 11, 5)),
            src_row(sid="s6", title="t-s6", model=LEMONADE_JSON,
                    created=ms(2026, 11, 6)),
            src_row(sid="s7", title="t-s7", created=ms(2026, 11, 7)),
        ]).close)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def bare(self, extra=()):
        return self.run_main(["--db", self.db, "--src", self.src,
                              "--unsloth-db", self.no_udb] + list(extra))

    def test_bare_shows_both_panels_model_first(self):
        code, out, _ = self.bare()
        self.assertEqual(code, 0)
        self.assertIn("usage by model \u00b7 top 5 by share", out)
        self.assertIn("sessions \u00b7 5 most recent", out)
        self.assertLess(out.index("usage by model"), out.index("sessions \u00b7"))

    def test_bare_hero_has_no_section_name(self):
        code, out, _ = self.bare()
        first = _ANSI_RE.sub("", out.splitlines()[0])
        self.assertTrue(first.startswith("\u256d tokenstat "))
        self.assertNotIn("\u00b7", first)

    def test_sessions_capped_at_five_newest_first(self):
        code, out, _ = self.bare()
        for gone in ("t-s1", "t-s2"):
            self.assertNotIn(gone, out)
        for kept in ("t-s3", "t-s4", "t-s5", "t-s6", "t-s7"):
            self.assertIn(kept, out)
        self.assertLess(out.index("t-s7"), out.index("t-s3"))

    def test_model_panel_top_five_by_share(self):
        code, out, _ = self.bare()
        self.assertIn("SHARE", out)
        # 3 models only exist here: all show, unsloth-studio dominates.
        self.assertIn(f"unsloth-studio/{UNSLOTH_MODEL_ID}", out)
        self.assertIn("prov3/m3", out)
        self.assertIn("lemonade/gpt-oss-120b", out)

    def test_model_panel_truncates_to_five(self):
        six = os.path.join(self.tmp.name, "six.db")
        rows = [src_row(sid=f"m{i}", title=f"t-m{i}",
                        model='{"id":"mod%d","providerID":"p"}' % i,
                        tin=1000 * (i + 2), tcr=0,
                        created=ms(2026, 11, i + 1)) for i in range(6)]
        self.addCleanup(make_file_db(six, rows).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u4.db"),
             "--src", six, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        # mod0 has the smallest total (2000+200=2200 vs larger tin): dropped.
        self.assertNotIn("p/mod0", out)
        for i in range(1, 6):
            self.assertIn(f"p/mod{i}", out)

    def test_bare_json_unchanged_all_sessions(self):
        code, out, _ = self.bare(["--json"])
        rows = json.loads(out)
        self.assertEqual(len(rows), 7)
        self.assertNotIn("SHARE", out)

    def test_flagged_views_keep_full_tables(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src,
             "--unsloth-db", self.no_udb, "--since", "2026-11-01"])
        self.assertEqual(code, 0)
        self.assertNotIn("5 most recent", out)
        self.assertIn("t-s1", out)  # full (unfiltered-by-cap) table

    def test_cost_and_cache_overlays_on_both_panels(self):
        code, out, _ = self.bare(["--cost", "--cache"])
        self.assertEqual(code, 0)
        plain = _ANSI_RE.sub("", out)
        self.assertGreaterEqual(plain.count("CACHE"), 2)
        self.assertGreaterEqual(plain.count("COST"), 2)

    def test_fewer_than_five_shows_all(self):
        small = os.path.join(self.tmp.name, "small.db")
        self.addCleanup(make_file_db(small, [
            src_row(sid="only", title="t-only")]).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u2.db"),
             "--src", small, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        self.assertIn("t-only", out)

    def test_empty_store_plain_no_match(self):
        empty = os.path.join(self.tmp.name, "empty-src.db")
        self.addCleanup(make_file_db(empty, []).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u3.db"),
             "--src", empty, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no sessions match")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test_tokenstat.DefaultDashboardTests -v`
Expected: FAIL — bare output is today's single full sessions table (no panel titles, all 7 sessions).

- [ ] **Step 3: Implement `_table_panel` in `tokenstat.py`, immediately after `report_header()`:**

```python
def _table_panel(title, rows, columns, share_view, color):
    """Panel of the rendered table; share views get padded (and colored)
    bars first; the header row dims to the label role only in color."""
    if share_view:
        rows = paint_bars(rows, color=color)
    table = render_table(paint_cells(rows) if color else rows, columns)
    lines = table.splitlines()
    if color:
        r, g, b = SUNSET["label"]
        lines[0] = f"\x1b[38;2;{r};{g};{b}m{lines[0]}\x1b[0m"
    return panel(title, lines)
```

- [ ] **Step 4: Implement the compact branch in `main()`**, inserted after the three implicit `_ingest(...)` report-mode calls and before `model_any = args.model is MODEL_ANY`:

```python
        # Compact default dashboard (design 2026-11-15): a truly bare run —
        # no command, no view/filter flags (--cost/--cache are overlays, not
        # views) — renders hero + top-5 models + 5 most recent sessions.
        # --json keeps the full flat array (display-only limits).
        compact_dash = (args.command is None and args.model is None
                        and not args.daily and not args.total
                        and not args.from_unsloth and not args.from_runs
                        and args.since is None and args.until is None
                        and args.min_in is None and args.min_out is None)
        if compact_dash and not args.as_json:
            all_rows = query_sessions(dest)
            if not all_rows:
                print("no sessions match")
                return 0
            model_rows = add_shares(total_totals(dest, by_model=True))
            model_rows.sort(key=lambda r: -r["share"])  # stable: ties keep
            # SQL's alphabetical-by-model order (mirrors the share view).
            sess_rows = list(reversed(all_rows))[:5]
            model_rows = model_rows[:5]
            color = _color_enabled(sys.stdout, os.environ)
            m_cols, s_cols = SHARE_COLUMNS, SESSION_COLUMNS
            if args.cost:
                model_rows = annotate_cost(model_rows, args.cost_tier)
                sess_rows = annotate_cost(sess_rows, args.cost_tier)
                m_cols, s_cols = m_cols + [COST_COLUMN], s_cols + [COST_COLUMN]
            if args.cache:
                model_rows = annotate_cache(model_rows)
                sess_rows = annotate_cache(sess_rows)
                m_cols, s_cols = with_cache(m_cols), with_cache(s_cols)
            lines = report_header("", all_rows)
            lines += _table_panel("usage by model \u00b7 top 5 by share",
                                  model_rows, m_cols, True, color)
            lines += _table_panel("sessions \u00b7 5 most recent",
                                  sess_rows, s_cols, False, color)
            print("\n".join(lines))
            return 0
```

Note: `annotate_cost`/`with_cache` ordering mirrors the existing tail (COST appended last, CACHE inserted before TOTAL). The hero totals/span use `all_rows` (every session), not the truncated tables.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest test_tokenstat.DefaultDashboardTests -v` → PASS
Run: `python3 -m unittest test_tokenstat` → all green (flagged views untouched).
Run: `python3 -W error::ResourceWarning -m unittest test_tokenstat` → warning-free.

- [ ] **Step 6: Manual eyeball**

Run: `python3 tokenstat.py` — expect hero panel, then top-5 model panel with bars, then 5 newest sessions; also check `python3 tokenstat.py | cat` (no color) and `NO_COLOR=1 python3 tokenstat.py`.

- [ ] **Step 7: Commit**

```bash
git add tokenstat.py test_tokenstat.py
git commit -m "feat: bare tokenstat shows top-5 models and 5 most recent sessions"
```
