# Sunset Visual Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render tokenstat's displayed reports in the "sunset" theme — rounded panels, a hero header panel, per-class colored numbers, and truecolor background-block bars replacing gradient glyph bars — while leaving piped, `NO_COLOR`, and `--json` output byte-identical.

**Architecture:** All new display code is pure functions added to `tokenstat.py` beside the existing render helpers; `main()`'s table branch gains a color path gated by `_color_enabled(sys.stdout, os.environ)`. The plain path (piped / NO_COLOR / json) keeps running today's code untouched. Cell coloring happens in row dicts *before* `render_table`, whose `_visible()` width math already tolerates ANSI in cells — the renderer itself is not modified.

**Tech Stack:** stdlib-only Python (argparse, colorsys, sqlite3, unittest). No new dependencies. Hand-rolled truecolor SGR sequences.

**Spec:** `docs/superpowers/specs/2026-11-15-visual-redesign-sunset-design.md`

Work on a branch: `git checkout -b visual-redesign-sunset master` (repo-local bot identity; ff-merge back to master after review, per AGENTS.md workflow).

## Global Constraints

- Single file, stdlib only — no third-party imports.
- Piped output, `NO_COLOR` output, and `--json` must be **byte-identical** to today: every new code path is behind the color gate; the existing plain renderer and `paint_bars(rows, color=False)` stay untouched.
- `_color_enabled(out, environ)` remains the single on/off switch (tty AND no `NO_COLOR`).
- No displayed *number* changes; this is presentation only. No SQL changes.
- Tests run with `python3 -m unittest test_tokenstat` and must stay warning-clean under `-W error::ResourceWarning`.
- Palette values used verbatim from the spec's Design decisions section.

**Resolved tension (executor note):** the spec's "new stacked IN/OUT block bars" on daily rows would change piped table shape, violating byte-identity. Deferred: this increment themes only bars that already exist (the share-view BAR column). Do not add bar columns to session/daily views.

---

## Review Focus

Each line gets its pinning test in the named task.

- **Empty result set under color** — `render_table` returns `"no sessions match\n"`; a colored run must print exactly that, no panels, no crash (Task 6).
- **Zero-total rows in block bars** — expect an all-track bar of exactly `width` cells, never a stray IN/OUT cell (Task 2).
- **Tiny nonzero rows** — a row that rounds to 0 cells still shows at least one IN-or-OUT cell (Task 2).
- **`--cost` / `--cache` overlays under color** — COST renders in the neutral number color, CACHE in amber; flag-off output unchanged (Tasks 4, 6).
- **Runs view under color** — run rows carry no real OUT (`tokens_output` absent/0); bars and cells must not crash or paint a blue/orange split from missing keys (Tasks 2, 4, 6).

---

### Task 1: Theme constants and `compact()`

**Files:**
- Modify: `tokenstat.py` (add after `TOKEN_OUT_RGB`, ~line 690)
- Test: `test_tokenstat.py` (new class `SunsetThemeTests`)

**Interfaces:**
- Consumes: nothing.
- Produces: `SUNSET: dict[str, tuple[int,int,int]]` with keys `border, accent, label, num, tin, tout, tcach, track`; `compact(n: int) -> str`.

- [ ] **Step 1: Write the failing tests** in `test_tokenstat.py`:

```python
class SunsetThemeTests(unittest.TestCase):
    def test_sunset_palette_has_all_roles(self):
        for key in ("border", "accent", "label", "num",
                    "tin", "tout", "tcach", "track"):
            self.assertEqual(len(SUNSET[key]), 3)
            self.assertTrue(all(0 <= v <= 255 for v in SUNSET[key]))

    def test_compact_formats_millions_thousands_plain(self):
        self.assertEqual(compact(7_770_000), "7.77M")
        self.assertEqual(compact(12_340), "12.3k")
        self.assertEqual(compact(999), "999")
        self.assertEqual(compact(0), "0")
```

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.SunsetThemeTests -v` → NameError/fail.
- [ ] **Step 3: Implement** in `tokenstat.py`: the spec-pinned palette and compact formatter.

```python
SUNSET = {
    "border": (87, 69, 62), "accent": (251, 146, 60),
    "label": (181, 153, 138), "num": (245, 235, 229),
    "tin": (96, 165, 250), "tout": (251, 146, 60),
    "tcach": (250, 204, 21), "track": (41, 34, 30),
}
```

`compact(n)`: `>= 1_000_000` → `f"{n/1e6:.2f}M"`, `>= 1_000` → `f"{n/1e3:.1f}k"`, else `str(n)`.

- [ ] **Step 4: Run to verify pass** — same command, all green.
- [ ] **Step 5: Commit** — `git add tokenstat.py test_tokenstat.py && git commit -m "feat: sunset theme constants and compact number format"`

### Task 2: `block_bar()` background-block primitive

**Files:**
- Modify: `tokenstat.py` (add near `paint_bars`)
- Test: `test_tokenstat.py` (new class `BlockBarTests`)

**Interfaces:**
- Consumes: `SUNSET`.
- Produces: `block_bar(total: int, tin: int, scale_total: int, width: int, theme: dict = SUNSET) -> str` — exactly `width` visible cells as truecolor background runs (IN-colored then OUT-colored segments over a track fill), no padding.

- [ ] **Step 1: Write the failing tests**:

```python
class BlockBarTests(unittest.TestCase):
    def test_full_width_row_splits_at_in_ratio(self):
        bar = block_bar(100, 60, 100, 20)              # 60% in
        self.assertEqual(_visible(bar), 20)            # exactly width cells
        bg = re.findall(r"\x1b\[48;2;\d+;\d+;\d+m", bar)
        runs = [c for c in bg if c == f"\x1b[48;2;{SUNSET['tin'][0]};{SUNSET['tin'][1]};{SUNSET['tin'][2]}m"]
        self.assertEqual(len(runs), 12)                # round(20 * 0.6)

    def test_zero_total_is_all_track(self):
        bar = block_bar(0, 0, 100, 10)
        self.assertEqual(_visible(bar), 10)
        track = f"\x1b[48;2;{SUNSET['track'][0]};{SUNSET['track'][1]};{SUNSET['track'][2]}m"
        self.assertEqual(bar, track * 10 + "\x1b[0m")

    def test_tiny_nonzero_row_gets_at_least_one_cell(self):
        bar = block_bar(1, 1, 1_000_000, 20)
        self.assertEqual(_visible(bar), 20)
        tin_seq = f"\x1b[48;2;{SUNSET['tin'][0]};{SUNSET['tin'][1]};{SUNSET['tin'][2]}m"
        self.assertIn(tin_seq, bar)                      # never all-track

    def test_scale_zero_safety(self):
        self.assertEqual(_visible(block_bar(5, 3, 0, 8)), 8)
```

Replace the placeholder assertion in `test_tiny_nonzero_row_gets_at_least_one_cell` with a real one: assert the bar contains at least one cell painted with `SUNSET['tin']` and that visible length is 20.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.BlockBarTests -v`.
- [ ] **Step 3: Implement** `block_bar`: `n = round(width * total / scale_total) if scale_total else 0`, clamped to `min 1` when `total > 0`; `n_in = round(n * tin / total)` when `total` else 0; emit `<bg(tin)>spaces*n_in<bg(tout)>spaces*(n-n_in)<bg(track)>spaces*(width-n)` then `\x1b[0m`. Runs view passes `tin=total` (all-OUT split) at the call site (Task 6), not here.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: block_bar truecolor background-block bar primitive"`

### Task 3: `panel()` framing primitive

**Files:**
- Modify: `tokenstat.py` (add near `render_table`)
- Test: `test_tokenstat.py` (new class `PanelTests`)

**Interfaces:**
- Consumes: `_visible`, `SUNSET`.
- Produces: `panel(title: str, lines: list[str], theme: dict = SUNSET) -> list[str]` — rounded border (`╭ ─ ╮ │ ╰ ╯`) around `lines`; title strip reads ` tokenstat · {title}` with accent/dim roles; every returned line has identical visible width = widest content + 2.

- [ ] **Step 1: Write the failing tests**:

```python
class PanelTests(unittest.TestCase):
    def test_all_lines_share_visible_width(self):
        lines = panel("sessions", ["plain", "\x1b[38;2;96;165;250mcolored\x1b[0m"])
        widths = {_visible(ln) for ln in lines}
        self.assertEqual(len(widths), 1)

    def test_border_glyphs_present(self):
        lines = panel("t", ["x"])
        self.assertTrue(lines[0].lstrip().startswith("╭"))
        self.assertTrue(lines[-1].lstrip().startswith("╰"))
        self.assertIn("tokenstat · t", _ANSI_RE.sub("", lines[0]))

    def test_empty_lines_list_still_frames(self):
        lines = panel("t", [])
        self.assertEqual(len(lines), 2)   # top + bottom only
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `panel`: width = max `_visible` of content lines (0 if none); top line = border-colored `╭` + accent-bold `" tokenstat · "` + label-colored title + `─` fill + `╮`; each content line padded to width with the `_visible`-based pattern already used in `render_table`; bottom = `╰` + `─` × (width + 2) + `╯`.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: panel() rounded frame primitive"`

### Task 4: `paint_cells()` per-class number coloring

**Files:**
- Modify: `tokenstat.py` (add near `paint_bars`)
- Test: `test_tokenstat.py` (new class `PaintCellsTests`)

**Interfaces:**
- Consumes: `SUNSET`, `_visible`.
- Produces: `paint_cells(rows: list[dict], columns, theme: dict = SUNSET) -> list[dict]` — copy of rows with the *rendered string* of these keys wrapped in fg colors: `tokens_input`→tin, `tokens_output`→tout, `cache`→tcach, `cost_est`→num, `total`→bold num. Missing keys skipped; None/empty cells untouched; other keys untouched.

- [ ] **Step 1: Write the failing tests**:

```python
class PaintCellsTests(unittest.TestCase):
    def test_class_colors_applied(self):
        rows = paint_cells([{"tokens_input": "10", "tokens_output": "5",
                             "total": "15"}], SESSION_COLUMNS)
        self.assertIn("38;2;96;165;250", rows[0]["tokens_input"])   # tin
        self.assertIn("38;2;251;146;60", rows[0]["tokens_output"])  # tout
        self.assertIn("38;2;245;235;229", rows[0]["total"])         # num

    def test_untouched_keys_and_missing_keys(self):
        rows = paint_cells([{"model": "m", "date": "d"}], SESSION_COLUMNS)
        self.assertEqual(rows[0], {"model": "m", "date": "d"})

    def test_original_rows_not_mutated(self):
        src = [{"total": "15"}]
        paint_cells(src, TOTAL_COLUMNS)
        self.assertEqual(src[0]["total"], "15")

    def test_table_alignment_survives_coloring(self):
        rows = [{"date": "2026-11-15", "model": "m", "tokens_input": "100",
                 "tokens_output": "50", "total": "150", "tok_s": None,
                 "title": ""}]
        table = render_table(paint_cells(rows, SESSION_COLUMNS),
                             SESSION_COLUMNS)
        plain = render_table(rows, SESSION_COLUMNS)
        self.assertEqual([_visible(l) for l in table.splitlines()],
                         [_visible(l) for l in plain.splitlines()])
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `paint_cells` — note the input row values are raw ints here, so format first exactly as `render_table.fmt` would (int → comma-grouped; rate → one decimal; None/absent → skip) then wrap in `\x1b[38;2;r;g;bm...\x1b[0m` (`\x1b[1m` additionally for `total`). Keep it display-only: never mutate input rows.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: paint_cells per-class number coloring for themed tables"`

### Task 5: `report_header()` hero panel builder

**Files:**
- Modify: `tokenstat.py` (add near `panel`)
- Test: `test_tokenstat.py` (new class `ReportHeaderTests`)

**Interfaces:**
- Consumes: `panel`, `compact`, `SUNSET`.
- Produces: `report_header(title: str, rows: list[dict], theme: dict = SUNSET) -> list[str]` — a panel whose hero line shows `{compact(sum(total))} tokens · N rows · {compact(sum(tokens_input or 0))} in · {compact(sum(tokens_output or 0))} out` and whose dim sub-line shows the date range from the min/max `"date"` keys present (omitted when no row carries `date`).

- [ ] **Step 1: Write the failing tests**:

```python
class ReportHeaderTests(unittest.TestCase):
    def test_hero_line_totals_and_row_count(self):
        rows = [{"total": 5_000_000, "tokens_input": 4_000_000,
                 "tokens_output": 1_000_000, "date": "2026-11-15"},
                {"total": 3_000_000, "tokens_input": 2_000_000,
                 "tokens_output": 1_000_000, "date": "2026-11-06"}]
        text = _ANSI_RE.sub("", "\n".join(report_header("sessions", rows)))
        self.assertIn("8.00M tokens", text)
        self.assertIn("2 rows", text)
        self.assertIn("2026-11-06", text) and self.assertIn("2026-11-15", text)

    def test_runs_rows_without_input_keys_are_safe(self):
        rows = [{"total": 10, "date": None}]
        text = _ANSI_RE.sub("", "\n".join(report_header("runs", rows)))
        self.assertIn("10 tokens", text)      # compact() falls through to str

    def test_empty_rows_still_frames(self):
        lines = report_header("x", [])
        self.assertTrue(lines[0].lstrip().startswith("╭"))
```

(Adjust the hero format string in implementation so these asserted substrings appear exactly; keep one line for hero, one dim sub-line.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** `report_header` using `.get("tokens_input", 0)` / `.get("tokens_output", 0)` throughout (runs rows may lack them; `date` may be None — filter Nones out of the range).
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit** — `git commit -am "feat: report_header hero panel with grand totals"`

### Task 6: Wire the color path into `main()`

**Files:**
- Modify: `tokenstat.py` (`main()`, output branch at lines ~1057–1067)
- Test: `test_tokenstat.py` (new class `CliSunsetTests`)

**Interfaces:**
- Consumes: everything above; existing `_color_enabled`, `render_table`, `paint_bars`.
- Produces: colored CLI output = `report_header(...)` panel + table wrapped in `panel(title, ...)`; share-view BAR cells become `f" {block_bar(total, tin_or_total, biggest, 30)} "` instead of gradient glyphs. Plain path untouched.

Title mapping (exact strings): sessions → `"sessions"`; daily → `"daily"`; total/share → `"usage by model · sorted by share"` when per-model rows else `"totals"`; `--from-unsloth` → `"unsloth events"`; `--from-runs` → `"studio runs"`; reconcile → `"reconcile"`. (Derive from the same branch flags that pick `columns`; a daily share view cannot occur.)

- [ ] **Step 1: Write the failing tests** — copy the `setUp`/`run_main` harness style from `CliColorTests`, and force color by patching `_color_enabled`:

```python
class CliSunsetTests(unittest.TestCase):
    # setUp/run_main identical in spirit to CliColorTests (temp src db, --db
    # tmp store, --unsloth-db missing path); add:
    def colored_main(self, argv):
        with mock.patch.object(tokenstat, "_color_enabled",
                               return_value=True):
            ...  # same redirect_stdout capture as run_main

    def test_share_view_uses_block_bars_not_glyphs(self):
        code, out, _ = self.colored_main(["--total", "--model", ...])
        self.assertEqual(code, 0)
        self.assertIn("48;2;", out)                       # background blocks
        self.assertNotIn("█", out)                        # glyph bars gone
        self.assertIn("tokenstat · usage by model", out)  # panel title

    def test_cache_overlay_column_is_amber_under_color(self):
        _, out, _ = self.colored_main(["--total", "--model", "--cache", ...])
        self.assertIn("38;2;250;204;21", out)   # tcach on the CACHE column

    def test_header_panel_prints(self):
        _, out, _ = self.colored_main(["--total", "--model", ...])
        self.assertIn("tokens", out.splitlines()[0] + out.splitlines()[1])
        self.assertIn("╭", out)

    def test_empty_result_plain_even_when_color_enabled(self):
        _, out, _ = self.colored_main(["--model", "zzz-nothing", ...])
        self.assertEqual(out, "no sessions match\n")      # Review Focus pin

    def test_piped_cli_still_byte_identical(self):
        _, out, _ = self.run_main(["--total", "--model", ...])   # no patch
        self.assertNotIn("\x1b", out)
        self.assertIn("█", out)                            # glyphs remain plain-path
```

Note: `main()` must call `_color_enabled` via the module global (it already does — `tokenstat._color_enabled(...)` resolves at call time from within `main`, so patching works only if main calls it unqualified *and* tests patch the module attribute; verify with a quick failing run).

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** in `main()`'s non-json branch, replacing lines ~1063–1067. Colorize rows *before* `render_table`. Structure:

```python
color = _color_enabled(sys.stdout, os.environ)
if share_view:
    if color:
        biggest = max((r["total"] for r in rows), default=0)
        rows = [{**r, "bar": f" {block_bar(
            r['total'], r.get('tokens_input', r.get('progress_tokens', 0)),
            biggest, 30)} "} for r in rows]
    else:
        rows = paint_bars(rows, color=False)   # padding only — today's look
table = render_table(paint_cells(rows, columns) if color else rows, columns)
if color and table != "no sessions match\n":
    lines = table.splitlines()
    lines[0] = dim-wrap(lines[0])              # label-colored header row
    print("\n".join(report_header(title, rows) + panel(title, lines)))
else:
    print(table, end="")
```

The empty-rows case takes the `else` (plain `"no sessions match\n"`, Review Focus line 1). For runs views, `block_bar` receives `tin = total` so the bar is all OUT-orange per the spec. Compute `title` from the branch flags that already select `columns`, using the mapping above. Ensure the test module imports `unittest.mock`.

- [ ] **Step 4: Run the full suite** — `python3 -m unittest test_tokenstat -v` → all green (124+ existing tests unchanged in behavior), then `python3 -W error::ResourceWarning -m unittest test_tokenstat` → warning-clean.
- [ ] **Step 5: Manual eyeball** — run against the real store both ways and confirm: tty-style colored output matches the approved sunset mockup (no glyph bars, blocks + panels + header), and `python3 tokenstat.py --total --model | cat` is unchanged from before this branch (`git stash`-diff or compare against `master` output).
- [ ] **Step 6: Commit** — `git commit -am "feat: sunset-themed panels, hero header, and block bars on tty output"`

### Task 7: README update

**Files:**
- Modify: `README.md` (the bar/color paragraph at lines ~77–93)

- [ ] **Step 1: Update the prose** — describe the new look: sunset theme, header panel, rounded panels, background-block share bars with IN-blue/OUT-orange split over a dark track; gradient glyph bars remain only on plain (piped) output; `NO_COLOR` still opts out. Keep it factual and same tone as surrounding text.
- [ ] **Step 2: Full suite once more** — `python3 -m unittest test_tokenstat`.
- [ ] **Step 3: Commit** — `git commit -am "docs: describe sunset themed tty rendering in README"`

---

## Post-plan (not a task): ff-merge `visual-redesign-sunset` into `master` after clean review, per repo workflow.
