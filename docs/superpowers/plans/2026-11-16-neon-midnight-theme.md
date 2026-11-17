# Neon Midnight Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace tokenstat's sunset color scheme with the approved "Neon Midnight" palette, adding `ttotal` (TOTAL column + hero grand total), `tshare` (SHARE column + hero row-count stat), and theme-derived gradient bar endpoints.

**Architecture:** Presentation-only change inside the existing hand-rolled ANSI display layer of the single file `tokenstat.py`: rename/repaint the one palette dict, extend `_PAINT_CELLS` with two cells, paint two more spots in `report_header`, and make `_token_rgb` derive from (and round-trip) the theme endpoints. Tests in `test_tokenstat.py` are updated pins plus new pins; no other files change except an AGENTS.md handoff note.

**Tech Stack:** Python 3 stdlib only (`colorsys`, `re`), `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-16-neon-midnight-theme-design.md`

## Global Constraints

- Stdlib-only, single file — no new dependencies, no new modules.
- `_color_enabled` (tty + `NO_COLOR`) remains the single color switch; piped / `NO_COLOR` output byte-identical to today's plain text.
- No displayed *number* changes; plain fallback renderer untouched.
- Palette values verbatim from the spec, as `(r, g, b)`: border `(76,58,120)` · track `(30,23,48)` · label `(169,156,201)` · num `(244,240,255)` · accent `(232,121,249)` · tin `(96,165,250)` · tout `(244,114,182)` · tcach `(163,230,53)` · ttotal `(167,139,250)` · tshare `(94,234,212)`.
- Work on branch `neon-midnight-theme` off `master`; repo is local-only (no push). Full suite must stay green and warning-clean: `python3 -W error::ResourceWarning -m unittest test_tokenstat`.

## Review Focus

- Piped / `NO_COLOR` runs with share views and painted totals — expected: zero escape bytes anywhere; painting stays behind the existing `color=` gate (Task 3).
- Table alignment when SHARE and TOTAL are painted — expected: column widths computed escape-aware (`_visible`), panel lines keep equal visible width (Task 3).
- Gradient bars under the saturation lerp, with a non-saturated endpoint (`tout`) — expected: first/last glyph colors exactly `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` (Task 2).
- Hero edge cases while painted: singular unit ("1 session"), rows with no dates (span line omitted), `cost_est=None` — expected: same text as today, only color added (Task 4).

---

### Task 1: Rename SUNSET → NEON and add the two new roles

**Files:**
- Modify: `tokenstat.py` (`SUNSET` dict ~line 745; references at `_PAINT_CELLS` area, `panel`/`paint_cells`/`report_header` default args, direct `SUNSET["label"]` uses in `_table_panel` and the second site near the file's main flow)
- Test: `test_tokenstat.py` (import line; `SUNSET[...]` references — find with `grep -n "SUNSET" test_tokenstat.py`)

**Interfaces:**
- Produces: module-level dict `NEON` with roles `border, accent, label, num, tin, tout, tcach, track, ttotal, tshare` (each a 3-int RGB tuple); `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` still importable (values unchanged in this task).

- [ ] **Step 1: Update the test-side references and add the shape pin**

In `test_tokenstat.py`, rename every `SUNSET` reference to `NEON` (import + all uses). Add to the theme tests class:

```python
def test_neon_dict_shape(self):
    self.assertEqual(
        set(NEON), {"border", "accent", "label", "num", "tin", "tout",
                    "tcach", "track", "ttotal", "tshare"})
    for key, rgb in NEON.items():
        self.assertEqual(len(rgb), 3, key)
        self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in rgb), key)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest test_tokenstat -v 2>&1 | tail -5`
Expected: ImportError/failure — `NEON` not defined.

- [ ] **Step 3: Rename and extend the dict in `tokenstat.py`**

Rename `SUNSET` → `NEON` everywhere (including the `theme=NEON` default args and both direct `NEON["label"]` uses). Keep all existing values unchanged; add `"ttotal": (167, 139, 250)` and `"tshare": (94, 234, 212)`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -W error::ResourceWarning -m unittest test_tokenstat`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git checkout -b neon-midnight-theme
git add docs/superpowers/specs/2026-11-16-neon-midnight-theme-design.md docs/superpowers/plans/2026-11-16-neon-midnight-theme.md tokenstat.py test_tokenstat.py
git commit -m "refactor: rename sunset theme to neon, add ttotal/tshare roles"
```

### Task 2: Repaint NEON and make gradient endpoints follow the theme

**Files:**
- Modify: `tokenstat.py` (`NEON` values; `TOKEN_IN_RGB`/`TOKEN_OUT_RGB`; `_HUE_*`/`_LUM_*`/`_SAT*` derivation; `_token_rgb`)
- Test: `test_tokenstat.py` (literal escape pins — find with `grep -n "38;2;" test_tokenstat.py`)

**Interfaces:**
- Consumes: `NEON` from Task 1.
- Produces: `TOKEN_IN_RGB == NEON["tin"]`, `TOKEN_OUT_RGB == NEON["tout"]`; `_token_rgb(t)` interpolating hue (UP sweep), lightness, AND saturation between the endpoints, exact at t=0 and t=1.

- [ ] **Step 1: Write the failing endpoint-derivation test**

```python
def test_gradient_endpoints_follow_theme(self):
    self.assertEqual(TOKEN_IN_RGB, NEON["tin"])
    self.assertEqual(TOKEN_OUT_RGB, NEON["tout"])
```

- [ ] **Step 2: Run to verify it fails** (old hardcoded endpoints ≠ new tin/tout)

- [ ] **Step 3: Repaint and rewire**

Set `NEON` values per Global Constraints. Replace the two `TOKEN_*_RGB` literals with assignments from `NEON["tin"]` / `NEON["tout"]`. In `_token_rgb`, replace the hardcoded `1.0` saturation with a lerp between `_SAT` (from `TOKEN_IN_RGB`) and a new `_SAT_OUT` (from `TOKEN_OUT_RGB`), mirroring how `_LUM_*` are derived so both endpoints round-trip exactly.

- [ ] **Step 4: Update remaining literal escape pins**

Grep for old-palette escapes (`80;168;255`, `255;136;90`, `251;146;60`, `250;204;21`, and sunset `num`/`label` literals). Where a pin names a theme role, derive the expected escape from `NEON` (e.g. `"38;2;{};{};{}m".format(*NEON["tin"])`); keep the gradient endpoint round-trip test asserting `TOKEN_IN_RGB`/`TOKEN_OUT_RGB`.

- [ ] **Step 5: Run the full suite** — expected all pass (the existing endpoints-exact test now proves the saturation lerp).

- [ ] **Step 6: Commit** — `git commit -am "feat: neon midnight palette; gradient derives from theme"`

### Task 3: Paint TOTAL violet and SHARE teal in tables

**Files:**
- Modify: `tokenstat.py` (`_PAINT_CELLS`)
- Test: `test_tokenstat.py`

**Interfaces:**
- Consumes: `NEON["ttotal"]`, `NEON["tshare"]` from Task 1; existing `paint_cells(rows, theme=NEON)` behavior (strings pass through unpainted-by-kind; ints formatted with thousands separators).
- Produces: `TOTAL` cells painted bold violet via role `ttotal`; `share_str` cells painted teal via role `tshare`.

- [ ] **Step 1: Write the failing tests**

```python
def test_total_column_painted_ttotal(self):
    rows = paint_cells([{"total": 7300000, "tokens_input": None}])
    self.assertIn("1;38;2;{};{};{}m".format(*NEON["ttotal"]), rows[0]["total"])

def test_share_column_painted_tshare(self):
    rows = paint_cells([{"total": 7300000, "share_str": "58.7%"}])
    self.assertIn("38;2;{};{};{}m".format(*NEON["tshare"]), rows[0]["share_str"])
```

- [ ] **Step 2: Run to verify both fail**

- [ ] **Step 3: Extend `_PAINT_CELLS`** — change `"total"` role from `num` to `ttotal` (keep bold) and add `"share_str": ("text", "tshare", False)`; `render()` already passes pre-rendered strings through unchanged.

- [ ] **Step 4: Run the full suite** — expected all pass, including existing alignment/visible-width tests (painted cells must not change column widths). If a panel-width test fails, the painting leaked escape bytes into width math — fix before proceeding (Review Focus #2).

- [ ] **Step 5: Pin piped output stays plain** — add one assertion to the existing no-color CLI test set (or a small new test): run `--total --model` with stdout piped (`subprocess`, as existing CLI tests do) and assert `"\x1b" not in out`.

- [ ] **Step 6: Commit** — `git commit -am "feat: violet TOTAL and teal SHARE columns"`

### Task 4: Paint the hero grand total and row-count stat

**Files:**
- Modify: `tokenstat.py` (`report_header`)
- Test: `test_tokenstat.py`

**Interfaces:**
- Consumes: `NEON["ttotal"]`, `NEON["tshare"]`.
- Produces: in `report_header`, the grand-total token count painted `ttotal` bold; the `{n_rows} {unit1}` span painted `tshare`; everything else (in/out spans, cost tail, date span) unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_hero_grand_total_painted_ttotal(self):
    rows = [{"total": 7300000, "tokens_input": 5900000, "tokens_output": 1400000}]
    hero = "\n".join(report_header("all models", rows))
    self.assertIn("1;38;2;{};{};{}m7.30M".format(*NEON["ttotal"]), hero)

def test_hero_row_count_painted_tshare(self):
    rows = [{"total": 1, "tokens_input": 1, "tokens_output": 0}]
    hero = "\n".join(report_header("all models", rows, unit="models"))
    self.assertIn("38;2;{};{};{}m1 model".format(*NEON["tshare"]), hero)
```

- [ ] **Step 2: Run to verify both fail**

- [ ] **Step 3: Wrap the two spans in `report_header`** with the same escape format already used for the in/out numbers (bold prefix for the grand total). Keep the singularization of `unit1` inside the painted span.

- [ ] **Step 4: Run the full suite** — expected all pass; existing hero tests (in/out spans, cost tail, missing dates) prove Review Focus #4.

- [ ] **Step 5: Commit** — `git commit -am "feat: neon hero — violet grand total, teal row-count stat"`

### Task 5: Verify end-to-end and update handoff notes

**Files:**
- Modify: `AGENTS.md` (handoff/state section)

- [ ] **Step 1: Full strict suite** — `python3 -W error::ResourceWarning -m unittest test_tokenstat`; expected green.

- [ ] **Step 2: Eyeball live output** — run `python3 tokenstat.py --total --model` and `python3 tokenstat.py` in a terminal; confirm violet totals, teal share/count, blue→violet→pink block bars, and that `python3 tokenstat.py --model | cat` is plain.

- [ ] **Step 3: Update `AGENTS.md`** — the bar-color/palette notes: sunset palette → NEON (roles incl. `ttotal`/`tshare`), gradient endpoints derived from theme tin/tout with H/L/S lerp, hero and SHARE painting.

- [ ] **Step 4: Commit** — `git commit -am "docs: AGENTS.md handoff for neon midnight theme"`
