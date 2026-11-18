# Custom themes — `--theme` / `--theme-default`

Date: 2026-11-17 · Status: approved 2026-11-17 · Predecessor: `2026-11-16-neon-midnight-theme-design.md`
(that increment retired the sunset palette and made `NEON` the single built-in theme;
this one makes the theme selectable and adds ten to choose from)

## Intent

The display layer has exactly one palette. The human partner wants to pick among
several: `--theme=<theme>` for a run, `--theme-default` to save a pick so later runs
wear it, roughly ten themes to choose from built in, and a `themes/` subfolder of one
`.json` per theme that the app scans on every run — so a new file dropped in is
immediately selectable, no code change.

Why: the tool is looked at daily in a terminal, and terminals come in many colors.
A palette that reads well on indigo-on-black may be unreadable on a light theme.
Being able to switch — and to keep your own file — is the difference between a
decoration and something usable.

## Goals

- `--theme=<slug>` paints this run's terminal output with that theme.
- `--theme-default` saves a default in `themes/config.json`; later runs use it.
- 10 built-in themes, including two for light terminals, all in the existing role
  scheme so every role (`tin`/`tout`/`tcach`/`ttotal`/`tshare`/`accent`/`label`/
  `num`/`border`) is filled deliberately.
- `themes/` is scanned at run: one `.json` per theme, filename stem = slug, and a
  file that shadows a built-in slug overrides it.
- The share-bar gradient follows the theme (its endpoints are `tin`/`tout`), so bars,
  columns, hero, header rule, and panel borders all move together.
- `--theme` with no value lists the available themes.

## Non-goals / anti-goals

- No change to any displayed number, to filtering, aggregation, ingestion, or exit
  codes beyond the new flags' own actions.
- `--json` output is unchanged (it has never carried color), and piped / `NO_COLOR`
  output stays byte-plain. `_color_enabled` remains the single switch: a theme is
  only ever applied where color was already applied.
- No background/foreground terminal writes (no escape codes that repaint the
  terminal itself), no italics/underline, no per-user theme editor UI.
- No `TOKENSTAT_THEME` environment variable, no per-directory themes. One default,
  one flag.
- Neon Midnight stays the default and stays byte-identical: existing tests that pin
  its exact RGBs and its role key set must pass untouched.

## Constraints

- stdlib-only Python, single file, no install step; TDD via
  `python3 -m unittest test_tokenstat`; suite stays green and warning-clean under
  `-W error::ResourceWarning`.
- Flag-off output byte-identical to today (existing pins).
- `themes/` must be optional: a lone `tokenstat.py` with no folder runs with the
  built-ins and falls back to Neon Midnight when there is no saved default.
- README (including its stale "sunset theme" paragraph), `--help`, and AGENTS.md
  updated to match.

## Approaches considered

**Where the built-ins live.**

- **A — built-ins in code, `themes/` for user themes (chosen).** The 10 palettes are
  dicts beside `NEON`; the folder is scanned at run for additional `.json` themes,
  and a file whose stem matches a built-in slug overrides it. Trades: nothing is
  duplicated, a copied-out single file keeps all 10 themes, git never sees personal
  themes; editing a built-in means editing code.
- **B — all 10 as files in `themes/`, code keeps only a Neon Midnight fallback.**
  Everything is a file and any theme is editable without touching code. Rejected:
  Neon Midnight would exist twice (fallback dict + `neon-midnight.json`) with a
  byte-identical-output requirement between them, and a single-file copy loses 9
  of its 10 themes.
- **C — one `themes.json` holding every theme.** Rejected: contradicts the partner's
  "a `.json` for each theme", and one shared file is worse to hand-edit.

**How the theme reaches the paint code.**

- **Explicit threading (chosen).** `main` resolves one theme and passes it to
  `report_header`, `panel`, `_table_panel`, `paint_cells`, `paint_bars`, and
  `paint_header_block` (the last three currently default to `NEON` or read it
  directly). Gradient stops are computed from the theme instead of import-time
  module globals. No mutable module state; every paint function stays testable in
  isolation.
- **Module-level "active theme" set once in `main`** (rebinding `NEON` and the
  gradient globals). Smaller diff, but mutable global state, tests that depend on
  import order, and a second theme leaking into a later call in-process.

**Where the saved default lives.** Repo-local `themes/config.json` — the human
partner's pick over `~/.config/tokenstat/`, so all of the tool's state stays next to
the code and `usage.db`.

## Design

### Theme file

`themes/<slug>.json`:

```json
{
  "name": "Tokyo Night",
  "colors": {
    "border": "#414868", "label": "#7982A9", "num": "#C0CAF5",
    "accent": "#F7768E", "tin": "#7AA2F7", "tout": "#FF9E64",
    "tcach": "#9ECE6A", "ttotal": "#BB9AF7", "tshare": "#7DCFFF"
  }
}
```

- Required: `name` (display string) and `colors` with exactly the nine roles above.
  Colors are `#rrggbb` (case-insensitive); the loader normalizes to `(r, g, b)`.
- `track` is dropped from the schema — it is defined in `NEON` but read by nothing.
  It stays in `NEON` untouched (a test pins the key set) and is documented as legacy.
- Extra keys are ignored, so a file can carry `track` or future roles without
  breaking.
- Slug = filename stem. A folder file shadows a built-in of the same slug.
- A file that fails to parse or validate is skipped with a one-line stderr warning
  during a scan (one broken file must not break every run); a theme named
  explicitly by `--theme=` reports the failure and exits 2.

### Built-in themes (10)

Palettes are the well-known schemes, mapped onto the nine roles so IN/OUT stay the
most distant pair on the line and `ttotal`/`tshare` stay distinct from them, the
cache color, and the accent. Exact hexes are the builder's call, verified against
each scheme's published values; the set:

1. `neon-midnight` — the current `NEON`, unchanged, still the fallback default
2. `tokyo-night`
3. `dracula`
4. `nord`
5. `gruvbox-dark`
6. `solarized-dark`
7. `monokai`
8. `one-dark`
9. `solarized-light` — dark text, for light terminals
10. `paper` — neutral light palette, dark text

### CLI

- `--theme=<slug>` — use for this run. Unknown slug → `parser.error` (exit 2) with
  the message naming the slug and listing the available ones.
- `--theme` (no value) — print the available themes (slug, display name, marked
  default) and exit 0.
- `--theme-default=<slug>` — save that slug as the default, print
  `default theme: <slug> (<name>)`, exit 0.
- `--theme-default` (no value) — save the `--theme=<slug>` given in the same
  invocation (the partner's phrasing: "`--theme-default` saves it"); an error if no
  `--theme` was given.
- Precedence: `--theme` > `themes/config.json` > Neon Midnight.
- The two actions short-circuit before implicit ingestion: they write one small file
  or print a list, they never touch the store.
- `--theme` is a display overlay like `--cost`/`--cache`, so the bare-dashboard
  trigger stays a truly bare invocation: `tokenstat.py --theme=nord` still renders
  the dashboard, now in Nord.
- `themes/config.json` holds `{"default": "<slug>"}`. Missing or unreadable config →
  Neon Midnight, with a stderr warning only when the file exists but cannot be used.
  Saving creates `themes/` if needed.
- `.gitignore` gains `themes/config.json` and `themes/*.json`; `themes/README.md`
  documents the format and ships tracked, so the folder exists in a fresh clone.

### Paint layer

- `resolve_theme(name, config_path)` returns a role→`(r,g,b)` dict; `main` calls it
  once and threads the result through every display call.
- Gradient: `gradient_stops(theme)` derives the HLS endpoints and hue span from the
  theme's `tin`/`tout` (the existing UP-sweep lerp of hue, lightness and saturation,
  endpoints round-tripping exactly); `_token_rgb` takes the stops instead of reading
  module globals. `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` remain as Neon Midnight's endpoints
  (pinned by tests) and are now derived from `NEON` exactly as before.
- `paint_header_block` takes the theme (header text `label`, rule `border`) instead
  of reading `NEON`.
- Color gating is untouched: no theme is applied when `_color_enabled` is false.

## What's left to the builder

- The exact hexes for the nine non-default themes, checked against each scheme's
  published palette, keeping the distinctness rule above (IN vs OUT vs TOTAL vs
  SHARE vs CACHE vs accent must be tellable apart at a glance, including on the
  light themes).
- Test coverage: every built-in has exactly the nine roles with 0–255 int triples;
  loader parses hex and rejects bad hex, a missing role, and bad JSON; unknown
  `--theme` exits 2 and lists; bare `--theme` lists and marks the default;
  `--theme=<slug>` changes the painted bytes (assert a theme-specific triple) and
  moves the bar gradient endpoints; `--theme-default` writes `themes/config.json`
  and a later run picks it up without the flag; `--theme-default` with no `--theme`
  errors; a folder file shadows a built-in; a broken file warns and is skipped;
  `--json`, piped, and `NO_COLOR` output stay plain under any theme; default output
  stays byte-identical (existing pins untouched); `themes/config.json` is gitignored.
- README: replace the stale "sunset theme" paragraph with the theme system, list the
  ten themes, document both flags and the file format.
