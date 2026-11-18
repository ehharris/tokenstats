# Custom Themes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick the terminal palette with `--theme=<slug>`, save a default with `--theme-default`, choose among 10 built-in themes, and drop their own `.json` themes into a `themes/` folder that is scanned on every run.

**Architecture:** Themes are plain role→`(r, g, b)` dicts, exactly like the existing `NEON`. `main` resolves one theme (flag → `themes/config.json` → Neon Midnight) and threads it explicitly through every paint function; the share-bar gradient derives its endpoints from the resolved theme instead of import-time globals. Built-in palettes live in `tokenstat.py`; `themes/` holds user files and the saved default.

**Tech Stack:** stdlib-only Python 3 (argparse, json, colorsys, os), unittest, no install step.

**Spec:** `docs/superpowers/specs/2026-11-17-custom-themes-design.md`

## Global Constraints

- stdlib-only Python, single file `tokenstat.py`, no new dependencies, no install step.
- TDD: `python3 -m unittest test_tokenstat` from `/home/elih/messinaround/token_counter`; suite stays green and warning-clean under `python3 -W error::ResourceWarning -m unittest test_tokenstat`.
- Flag-off output stays **byte-identical** to today. Do not edit existing color pins (`NEON` RGBs, the `set(NEON)` key-set assertion at `test_tokenstat.py:3085`, `TOKEN_IN_RGB`/`TOKEN_OUT_RGB`).
- Color is applied only where `_color_enabled(out, environ)` is already the gate: piped output, `NO_COLOR`, and `--json` stay byte-plain under every theme.
- `themes/` must be optional: a lone `tokenstat.py` with no folder runs with the built-ins.
- Nine theme roles, exactly: `border`, `accent`, `label`, `num`, `tin`, `tout`, `tcach`, `ttotal`, `tshare`. `track` is legacy: it stays in `NEON`, is not part of the schema, and is read by nothing.
- Every commit message: `feat:`/`test:`/`docs:` prefix, one task per commit.

## Review Focus

Each line is an input class no task's happy-path tests cover by default; each gets a pinning test in the named task.

- **`--theme <word>` swallowing a command word** (the known `--cost` quirk): `tokenstat.py --theme reconcile` must exit 2 naming the bad slug and listing the slugs, not blame the word as the command. (Task 6)
- **Theme actions with no usable store**: `--theme` (list) and `--theme-default` (save) never open SQLite, so they succeed even when `--db` points at an unwritable path. (Task 6)
- **A `themes/` folder that is missing, empty, or holds a broken file**: every run still renders with the built-ins; a broken file warns on stderr and is skipped, it never aborts a report. (Tasks 2, 5)
- **Piped / `NO_COLOR` / `--json` output under a non-default theme**: no escape bytes anywhere, `--json` shape unchanged. (Task 6)
- **A theme file that is valid JSON but wrong-shaped** (top level a list, `colors` not a dict, a 4-tuple rgb, `name` not a string, hex out of range): a clear `ThemeError` message, never a traceback. (Task 2)

---

### Task 1: Theme registry (10 built-in palettes in code)

**Files:**
- Modify: `tokenstat.py` (insert after the `NEON` dict, before the gradient block at ~line 1044)
- Test: `test_tokenstat.py` (new class `ThemeRegistryTests`, place next to `NeonThemeTests` ~line 3082)

**Interfaces:**
- Consumes: `NEON` (existing).
- Produces:
  - `THEME_ROLES: tuple[str, ...]` — the nine roles, in the order listed in Global Constraints.
  - `TOKYO_NIGHT`, `DRACULA`, `NORD`, `GRUVBOX_DARK`, `SOLARIZED_DARK`, `MONOKAI`, `ONE_DARK`, `SOLARIZED_LIGHT`, `PAPER`: `dict[str, tuple[int, int, int]]`.
  - `THEMES: dict[str, tuple[str, dict]]` — slug → `(display name, colors dict)`, ten entries, `"neon-midnight"` mapping to `("Neon Midnight", NEON)`.

- [ ] **Step 1: Write the failing tests** in `ThemeRegistryTests`

```python
BUILTIN_SLUGS = {"neon-midnight", "tokyo-night", "dracula", "nord",
                 "gruvbox-dark", "solarized-dark", "monokai", "one-dark",
                 "solarized-light", "paper"}

def test_builtin_slugs_and_display_names(self):
    self.assertEqual(set(THEMES), BUILTIN_SLUGS)
    for slug, (name, colors) in THEMES.items():
        self.assertTrue(name and name[0].isupper(), slug)

def test_every_theme_has_exactly_the_nine_roles(self):
    for slug, (_, colors) in THEMES.items():
        self.assertEqual(set(colors), set(THEME_ROLES), slug)
        for role, rgb in colors.items():
            self.assertEqual(len(rgb), 3, f"{slug}/{role}")
            self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255
                                for v in rgb), f"{slug}/{role}")

def test_neon_midnight_is_the_existing_palette_unchanged(self):
    self.assertEqual(THEMES["neon-midnight"], ("Neon Midnight", NEON))

def test_data_colors_are_pairwise_distinct(self):
    # IN / OUT / CACHE / TOTAL / SHARE / accent must be tellable apart: no two
    # equal, and no two within a 60-unit Manhattan distance on the same line.
    for slug, (_, c) in THEMES.items():
        picks = [c[r] for r in ("tin", "tout", "tcach", "ttotal", "tshare", "accent")]
        for i, a in enumerate(picks):
            for b in picks[i + 1:]:
                self.assertGreater(sum(abs(x - y) for x, y in zip(a, b)), 60,
                                   f"{slug}: {picks[i]} vs {b}")
```

Add `THEMES`, `THEME_ROLES` to the `from tokenstat import (...)` block at the top of the test file.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.ThemeRegistryTests -v`
  Expected: FAIL/ERROR with `ImportError: cannot import name 'THEMES'`.

- [ ] **Step 3: Implement the registry** in `tokenstat.py`, immediately after `NEON`.

`THEME_ROLES` is the nine-role tuple. Each new palette is a dict literal of the nine roles with these exact hexes converted to `(r, g, b)` triples (write them as a small `_hex("#414868")` helper local to the block, or as literal triples — either is fine, the values are what is pinned):

| slug (display name) | border | label | num | accent | tin | tout | tcach | ttotal | tshare |
|---|---|---|---|---|---|---|---|---|---|
| `tokyo-night` (Tokyo Night) | `#414868` | `#7982A9` | `#C0CAF5` | `#F7768E` | `#7AA2F7` | `#FF9E64` | `#9ECE6A` | `#BB9AF7` | `#2ACBCF` |
| `dracula` (Dracula) | `#6272A4` | `#8A94C0` | `#F8F8F2` | `#FF79C6` | `#8BE9FD` | `#FFB86C` | `#50FA7B` | `#BD93F9` | `#F1FA8C` |
| `nord` (Nord) | `#4C566A` | `#7B88A1` | `#ECEFF4` | `#BF616A` | `#88C0D0` | `#D08770` | `#A3BE8C` | `#B48EAD` | `#EBCB8B` |
| `gruvbox-dark` (Gruvbox Dark) | `#665C54` | `#A89984` | `#EBDBB2` | `#FE8019` | `#83A598` | `#FB4934` | `#B8BB26` | `#D38696` | `#8EC07C` |
| `solarized-dark` (Solarized Dark) | `#073642` | `#839496` | `#EEE8D5` | `#D33682` | `#268BD2` | `#CB4B16` | `#859900` | `#6C71C4` | `#2AA198` |
| `monokai` (Monokai) | `#75715E` | `#9B9277` | `#F8F8F2` | `#F92672` | `#66D9EF` | `#FD971F` | `#A6E22E` | `#AE81FF` | `#F4BF75` |
| `one-dark` (One Dark) | `#4B5263` | `#7F8695` | `#ABB2BF` | `#C678DD` | `#61AFEF` | `#E06C75` | `#98C379` | `#E5C07B` | `#56B6C2` |
| `solarized-light` (Solarized Light) | `#93A1A1` | `#657B83` | `#073642` | `#D33682` | `#268BD2` | `#CB4B16` | `#859900` | `#6C71C4` | `#2AA198` |
| `paper` (Paper) | `#B0B7C3` | `#6B7280` | `#1F2328` | `#BE185D` | `#1D4ED8` | `#C2410C` | `#15803D` | `#6D28D9` | `#0E7490` |

`solarized-light` and `paper` are the light-terminal pair: `num` is dark, `border`/`label` mid-grey. Comment-derived `label` values on `dracula`/`nord`/`monokai` are lightened from each scheme's comment color so the hero's date span stays readable — say so in a one-line comment above the table.

- [ ] **Step 4: Run to verify pass** — `python3 -m unittest test_tokenstat.ThemeRegistryTests -v`, then the whole suite.
- [ ] **Step 5: Commit** — `git add tokenstat.py test_tokenstat.py && git commit -m "feat: ten built-in theme palettes + THEME_ROLES/THEMES registry"`

---

### Task 2: Theme files — parse, validate, scan the folder

**Files:**
- Modify: `tokenstat.py` (after the registry)
- Test: `test_tokenstat.py` (new class `ThemeFileTests`)

**Interfaces:**
- Consumes: `THEMES`, `THEME_ROLES`.
- Produces:
  - `class ThemeError(Exception)` — message is user-facing (goes to `parser.error` / stderr verbatim).
  - `THEMES_DIR: str` — `os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes")`.
  - `load_theme_file(path) -> tuple[str, dict]` — `(display name, colors)`; raises `ThemeError`.
  - `scan_themes(dir=THEMES_DIR, warn=None) -> dict[str, tuple[str, dict]]` — built-ins merged with folder files (folder wins on slug collision); `warn` is a `callable(str)` for skipped files, default silent.

- [ ] **Step 1: Write the failing tests** in `ThemeFileTests`

```python
GOOD = ('{"name": "My Theme", "colors": {"border": "#101010", "accent": "#202020",'
        ' "label": "#303030", "num": "#F0F0F0", "tin": "#0000FF", "tout": "#FF0000",'
        ' "tcach": "#00FF00", "ttotal": "#FF00FF", "tshare": "#00FFFF"}}')

def write_theme(self, slug, text):   # helper: themes dir in a tmp folder
    ...

def test_parses_hex_case_insensitively(self):
    name, colors = load_theme_file(self.write_theme("mine", GOOD))
    self.assertEqual((name, colors["border"], colors["tin"]),
                     ("My Theme", (16, 16, 16), (0, 0, 255)))

def test_extra_keys_are_ignored(self):        # adds "track" + "author" to GOOD
def test_missing_role_raises(self):           # ThemeError naming the role
def test_bad_hex_raises(self):                # "colors": {... "tin": "blue"} → ThemeError naming "tin"
def test_rgb_out_of_range_raises(self):       # "#100" (3-digit) and a 4-tuple both rejected
def test_non_dict_shapes_raise(self):         # top level a list; "colors" a list; "name" a number
def test_bad_json_raises(self):               # "{oops" → ThemeError naming the file
def test_scan_merges_and_shadows(self):       # themes/nord.json replaces built-in nord
def test_scan_without_folder_is_builtin_only(self):   # dir that does not exist
def test_scan_skips_broken_file_with_warning(self):
    # one valid + one broken file: scan returns the valid one, warn() got a
    # message naming the broken file, and the built-ins are all still present
```

Write each of these as a real test body (one assertion cluster each) using the helpers; the names and the parenthetical expectations above are the contract.

- [ ] **Step 2: Run to verify failure** — `python3 -m unittest test_tokenstat.ThemeFileTests -v` → ImportError on `ThemeError`.
- [ ] **Step 3: Implement** in `tokenstat.py`:

```python
class ThemeError(Exception):
    """A theme file or slug could not be used."""

def _theme_from_obj(obj, origin) -> tuple[str, dict]:
    """Validate one parsed theme object: str `name`, `colors` dict with exactly
    THEME_ROLES, each value '#rrggbb'. `origin` names the file in messages."""

def load_theme_file(path) -> tuple[str, dict]:
    """json.load + _theme_from_obj; json.JSONDecodeError -> ThemeError naming path."""

def scan_themes(dir=THEMES_DIR, warn=None) -> dict:
    """dict(THEMES) updated with {stem: load_theme_file(p) for *.json in dir};
    a file that raises ThemeError is skipped via warn(f"warning: {exc}");
    a missing dir is a no-op."""
```

`_theme_from_obj` is where every shape rule lives (Review Focus line 5): reject a non-dict top level, a non-str `name`, a missing/non-dict `colors`, a missing or extra role, and any color that is not a 7-char `#rrggbb` string.

- [ ] **Step 4: Run to verify pass** — `python3 -m unittest test_tokenstat.ThemeFileTests -v`, then the whole suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: themes/ folder scan — one .json per theme, validated"`

---

### Task 3: Gradient endpoints follow the theme

**Files:**
- Modify: `tokenstat.py:1044-1102` (`TOKEN_IN_RGB`/`TOKEN_OUT_RGB`, `_HUE_*`/`_LUM_*`/`_SAT*`/`_HUE_SPAN`, `_token_rgb`, `_paint_gradient`, `paint_bars`)
- Test: `test_tokenstat.py` (extend the existing gradient test class ~line 2890)

**Interfaces:**
- Consumes: any theme dict (role → `(r, g, b)`).
- Produces:
  - `gradient_stops(theme) -> tuple[float, ...]` — the seven precomputed HLS values `(hue_in, lum_in, sat_in, hue_out, lum_out, sat_out, hue_span)`.
  - `_token_rgb(t, stops) -> tuple[int, int, int]` — **signature change**: stops are passed in, no globals read.
  - `_paint_gradient(bar, frac_in, stops) -> str` — same change.
  - `paint_bars(rows, color, theme=NEON)` — **new keyword**, default keeps today's behavior.
  - `TOKEN_IN_RGB` / `TOKEN_OUT_RGB` stay module constants derived from `NEON` (pinned by existing tests).

- [ ] **Step 1: Write the failing tests**

```python
def test_gradient_endpoints_follow_an_arbitrary_theme(self):
    theme = dict(NEON, tin=(10, 200, 190), tout=(250, 30, 90))
    rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 8,
             "tokens_input": 5, "tokens_output": 5}]
    colors = rgbs(paint_bars(rows, color=True, theme=theme)[0]["bar"])
    self.assertEqual(colors[0], (10, 200, 190))   # left end = theme tin
    self.assertEqual(colors[-1], (250, 30, 90))   # right end = theme tout

def test_gradient_endpoints_round_trip_for_every_theme(self):
    for slug, (_, theme) in THEMES.items():
        stops = gradient_stops(theme)
        self.assertEqual(_token_rgb(0.0, stops), theme["tin"], slug)
        self.assertEqual(_token_rgb(1.0, stops), theme["tout"], slug)

def test_default_paint_bars_unchanged_without_theme_arg(self):
    # existing pins: colors[0] == TOKEN_IN_RGB, colors[-1] == TOKEN_OUT_RGB
```

`rgbs` is the local helper already used at `test_tokenstat.py:2912`.

- [ ] **Step 2: Run to verify failure** — `TypeError: paint_bars() got an unexpected keyword argument 'theme'`.
- [ ] **Step 3: Implement** — replace the six import-time globals with `gradient_stops(theme)` (same math: `colorsys.rgb_to_hls` on each endpoint, `_HUE_SPAN = (hue_out - hue_in) % 1.0` so the sweep goes UP), have `_token_rgb`/`_paint_gradient` take `stops`, and have `paint_bars` compute `stops = gradient_stops(theme)` once per call when `color` is true. Keep `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` as the `NEON`-derived constants and update the comment above them to say the endpoints now come from the resolved theme.
- [ ] **Step 4: Run to verify pass** — the gradient tests, then the whole suite (the existing endpoint pins must still pass).
- [ ] **Step 5: Commit** — `git commit -m "feat: share-bar gradient derives from the theme, not import-time globals"`

---

### Task 4: Thread the theme through the paint layer

**Files:**
- Modify: `tokenstat.py` — `paint_cells` (1128), `panel` (1165), `report_header` (1196), `_table_panel` (1254), `paint_header_block` (1271)
- Test: `test_tokenstat.py` (extend `CliNeonTests` neighbors with a `ThemePaintTests` class)

**Interfaces:**
- Consumes: any theme dict.
- Produces:
  - `paint_cells(rows, theme=NEON)` — unchanged.
  - `panel(title, lines, theme=NEON, color=True)` — unchanged.
  - `report_header(title, rows, theme=NEON, cost_est=None, real_cost=None, unit="rows", color=True)` — unchanged.
  - `paint_header_block(lines, columns, theme=NEON)` — **new keyword**; must stop reading `NEON` directly.
  - `_table_panel(title, rows, columns, share_view, color, theme=NEON)` — **new keyword**; passes the theme to `paint_bars`, `paint_cells`, `paint_header_block`, `panel`.

- [ ] **Step 1: Write the failing tests** in `ThemePaintTests`

```python
def test_panel_uses_the_given_theme(self):
    theme = dict(NEON, border=(1, 2, 3), label=(4, 5, 6))
    out = "\n".join(panel("t", ["x"], theme=theme))
    self.assertIn("38;2;1;2;3m", out)      # border hue
    self.assertIn("38;2;4;5;6m", out)      # title label hue
    self.assertNotIn("38;2;{};{};{}m".format(*NEON["border"]), out)

def test_header_block_uses_the_given_theme(self):
    theme = dict(NEON, label=(7, 7, 7), border=(8, 8, 8))
    lines = render_table([{"model": "m", "total": 1}], SESSION_COLUMNS).splitlines()
    paint_header_block(lines, SESSION_COLUMNS, theme=theme)
    self.assertIn("38;2;7;7;7m", lines[0])   # header text = label role
    self.assertIn("38;2;8;8;8m", lines[1])   # rule = border role

def test_report_header_uses_the_given_theme(self):
    theme = dict(NEON, ttotal=(9, 9, 9), tshare=(11, 11, 11))
    rows = [{"total": 7_300_000, "tokens_input": 1, "tokens_output": 1}]
    hero = "\n".join(report_header("t", rows, theme=theme, unit="models"))
    self.assertIn("38;2;9;9;9m", hero)       # grand total = ttotal
    self.assertIn("38;2;11;11;11m", hero)    # row count = tshare
```

- [ ] **Step 2: Run to verify failure** — `TypeError: paint_header_block() got an unexpected keyword argument 'theme'`.
- [ ] **Step 3: Implement** — add the `theme=NEON` keyword to `paint_header_block` and `_table_panel`, replace `paint_header_block`'s two `NEON[...]` reads with `theme[...]`, and pass `theme` down inside `_table_panel` to `paint_bars`, `paint_cells`, `paint_header_block`, and `panel`. Do not touch `main` yet.
- [ ] **Step 4: Run to verify pass** — `ThemePaintTests`, then the whole suite (default output must be byte-identical).
- [ ] **Step 5: Commit** — `git commit -m "feat: every paint function takes the theme explicitly"`

---

### Task 5: Saved default — `themes/config.json` + resolution

**Files:**
- Modify: `tokenstat.py` (after `scan_themes`)
- Test: `test_tokenstat.py` (new class `ThemeConfigTests`)

**Interfaces:**
- Consumes: `scan_themes`, `THEMES`, `NEON`.
- Produces:
  - `THEME_CONFIG: str` — `os.path.join(THEMES_DIR, "config.json")`.
  - `read_default_theme(path=THEME_CONFIG, warn=None) -> str | None` — the `"default"` slug, or `None`; warns (via `warn`) only when the file exists but cannot be used.
  - `write_default_theme(path, slug) -> None` — creates the parent dir, writes `{"default": slug}` with a trailing newline.
  - `resolve_theme(slug, dir=THEMES_DIR) -> dict` — `None` → `NEON`; a slug → that theme's colors from `scan_themes(dir)`; unknown → `ThemeError` naming the slug and listing the available slugs.

- [ ] **Step 1: Write the failing tests**

```python
def test_read_missing_config_is_none_and_silent(self):
def test_read_returns_the_slug(self):            # {"default": "nord"} → "nord"
def test_read_broken_config_warns_and_is_none(self):
def test_read_config_without_default_key_is_none(self):
def test_write_creates_folder_and_file(self):    # parent dir absent beforehand
def test_resolve_none_is_neon(self):
def test_resolve_slug(self):                     # "gruvbox-dark" → GRUVBOX_DARK
def test_resolve_unknown_lists_slugs(self):      # ThemeError names "nope" + "nord"
def test_resolve_sees_a_folder_theme(self):      # tmp dir with one .json
```

- [ ] **Step 2: Run to verify failure** — ImportError on `resolve_theme`.
- [ ] **Step 3: Implement** the three functions. `read_default_theme` swallows `OSError`/`json.JSONDecodeError`/shape problems into `None` (with `warn` when the file exists); `resolve_theme` builds its map with `scan_themes(dir)` so folder themes are resolvable by slug.
- [ ] **Step 4: Run to verify pass**, then the whole suite.
- [ ] **Step 5: Commit** — `git commit -m "feat: resolve_theme + themes/config.json default read/write"`

---

### Task 6: CLI — `--theme` / `--theme-default`

**Files:**
- Modify: `tokenstat.py` — `main()` argparse block (~1489, after `--cache`) and the wiring just before `dest = None` (~1495) plus the two render paths (~1595-1601, ~1754-1771)
- Modify: `.gitignore` — add `themes/*.json` (covers `themes/config.json` and user themes; this task writes that file, so it owns the rule)
- Test: `test_tokenstat.py` (new class `CliThemeTests`, modeled on `CliNeonTests` at line 3140)

**Interfaces:**
- Consumes: `resolve_theme`, `scan_themes`, `read_default_theme`, `write_default_theme`, `THEMES_DIR`, `THEME_CONFIG`, `THEMES`.
- Produces:
  - `THEME_LIST = object()` / `THEME_SAVE = object()` sentinels (the `SESSIONS_BARE` pattern).
  - `--theme` (`nargs="?"`, `const=THEME_LIST`, `metavar="SLUG"`), `--theme-default` (`nargs="?"`, `const=THEME_SAVE`, `dest="theme_default"`, `metavar="SLUG"`).
  - In `main`, after the `--cost-tier` shim check and **before** `dest = None`: bare `--theme` prints the theme list and returns 0; `--theme-default` saves and returns 0; otherwise `theme = resolve_theme(args.theme or read_default_theme(THEME_CONFIG), THEMES_DIR)` inside `try/except ThemeError → parser.error(str(exc))`.
  - The resolved `theme` is passed to `report_header`, `_table_panel` (dashboard path) and to `paint_bars`, `paint_cells`, `paint_header_block`, `report_header`, `panel` (flagged-view path).

- [ ] **Step 1: Write the failing tests** in `CliThemeTests` (reuse `CliNeonTests`'s `run_main` / `colored_main` helpers and its `src_row` fixture)

```python
def test_unknown_theme_exits_2_and_lists(self):
    with self.assertRaises(SystemExit) as cm:   # stderr captured, as line 2228
        self.run_main(["--db", self.db, "--theme", "nope"])
    self.assertEqual(cm.exception.code, 2)      # stderr names "nope" and "nord"

def test_theme_swallowing_a_command_is_a_theme_error(self):
    # Review Focus line 1: `--theme reconcile` → exit 2 about the theme, not
    # about the command word.
def test_bare_theme_lists_themes_and_marks_default(self):
    # exit 0; one line per slug; the config default line ends with " (default)"
def test_theme_actions_work_without_a_store(self):
    # Review Focus line 2: --theme-default=nord and bare --theme with
    # --db /nonexistent-dir-xyz/usage.db still exit 0.
def test_theme_flag_paints_that_palette(self):
    # colored_main(["--db", ..., "--theme", "gruvbox-dark"]) →
    # "38;2;131;165;152m" (GRUVBOX_DARK tin) present, NEON tin absent
def test_theme_flag_moves_the_bar_gradient(self):
    # share view: first bar glyph == GRUVBOX_DARK tin, last == tout
def test_theme_default_saves_and_later_runs_use_it(self):
    # patch tokenstat.THEME_CONFIG and tokenstat.THEMES_DIR to tmp paths:
    # --theme-default=nord writes the file; a second run with no --theme paints nord
def test_bare_theme_default_saves_this_runs_theme(self):
    # ["--theme", "nord", "--theme-default"] saves nord
def test_theme_default_without_a_theme_errors(self):
    # ["--theme-default"] alone → SystemExit 2
def test_theme_alone_still_renders_the_dashboard(self):
    # bare dashboard panels appear with --theme=nord (overlay, not a view)
def test_theme_never_colors_piped_or_json_output(self):
    # Review Focus line 4: no "--theme" run through the plain (unpatched)
    # path emits "\x1b"; --json --theme=nord is byte-identical to --json
def test_theme_default_is_gitignored(self):
    # subprocess: git check-ignore themes/config.json → exit 0
```

- [ ] **Step 2: Run to verify failure** — argparse rejects the unknown flags (SystemExit 2) / assertions fail.
- [ ] **Step 3: Implement** the two `add_argument` calls with help text that says to prefer the `=` form (copy the tone of `--cost`'s help: "Prefer --theme=SLUG: the space form can swallow a following command"), add `themes/*.json` to `.gitignore`, then the `main` wiring and the two render paths. The list action prints `f"{slug} — {name}"` per theme, sorted, with `" (default)"` on the saved default (and on Neon Midnight when nothing is saved).
- [ ] **Step 4: Run to verify pass** — `CliThemeTests`, then the whole suite, then the strict variant `python3 -W error::ResourceWarning -m unittest test_tokenstat`.
- [ ] **Step 5: Manual smoke** — `python3 tokenstat.py --theme` (list), `python3 tokenstat.py --theme=paper`, `python3 tokenstat.py --theme=nord --theme-default`, `python3 tokenstat.py`, then `python3 tokenstat.py --theme=neon-midnight --theme-default` to leave the default as it was.
- [ ] **Step 6: Commit** — `git commit -m "feat: --theme=<slug> and --theme-default with a themes/ scan"`

---

### Task 7: Docs and ignore rules

**Files:**
- Create: `themes/README.md`
- Modify: `README.md` (the "Terminal output also wears the sunset theme" paragraph, ~line 99, and the `--help` sample if present), `AGENTS.md` (append a `Custom themes increment (2026-11-17)` paragraph)

**Interfaces:**
- Consumes: the shipped flags and file format from Tasks 2-6.
- Produces: nothing consumed by code.

- [ ] **Step 1: Check the ignore rules Task 6 installed** — `git check-ignore themes/config.json` exits 0, and `git check-ignore themes/README.md` prints nothing (this task's doc must stay tracked).
- [ ] **Step 2: `themes/README.md`** — the file format (the JSON shape from the spec, the nine roles and what each paints, hex-only colors, slug = filename stem, folder shadows a built-in, broken files are skipped with a warning), plus one complete example theme and the two commands (`--theme`, `--theme=<slug>`, `--theme-default=<slug>`).
- [ ] **Step 3: `README.md`** — replace the stale sunset paragraph (it still describes the retired palette and a "blue→orange" gradient) with the theme system: the ten slugs, the two flags, the precedence order, the file folder, and the unchanged guarantees (piped / `NO_COLOR` / `--json` stay plain).
- [ ] **Step 4: `AGENTS.md`** — one increment paragraph in the style of the existing ones: what was added, the `themes/` + `config.json` layout, that `track` is legacy, that Neon Midnight remains the default and byte-identical, and pointers to this spec and plan.
- [ ] **Step 5: Verify** — `git check-ignore themes/config.json` exits 0; `python3 tokenstat.py --help` shows both flags; whole suite green.
- [ ] **Step 6: Commit** — `git commit -m "docs: theme system — themes/ format, README, AGENTS increment"`
