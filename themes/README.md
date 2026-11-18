# `themes/` — your own palettes

`tokenstat` ships ten built-in themes. Anything you drop in this folder joins
that list on the next run — no code change, no restart. One `.json` per theme,
and the filename stem is the slug you type.

## Format

`themes/copper-dusk.json` is theme `copper-dusk`, displayed as `Copper Dusk`:

```json
{
  "name": "Copper Dusk",
  "colors": {
    "border": "#5A4A42", "accent": "#F2A65A",
    "label": "#9A8B80",  "num": "#F5EDE4",
    "tin": "#6FB3D2",    "tout": "#E8747C",
    "tcach": "#A9C46C",  "ttotal": "#C792EA",
    "tshare": "#5FD3C4"
  }
}
```

- `name` is a non-empty display string. `colors` must hold **all nine roles**
  below; a missing role is a validation error. Extra keys are ignored, so a file
  can carry `track` or a future role without breaking.
- Colors are `#rrggbb` strings, any case — no `#rgb`, no color names, no
  `rgb()`, no alpha. Files are read as UTF-8.
- A file that fails to parse or validate is **skipped** with a one-line warning
  on stderr: one broken file never breaks a run. Its slug then does not exist,
  so `--theme=<that slug>` reports `unknown theme` — the warning that `--theme`
  (bare) prints is where you find out why.
- A file whose stem matches a built-in slug **shadows** it: `themes/dracula.json`
  replaces the built-in Dracula.
- `track` is a legacy role — it still lives in the built-in Neon Midnight dict,
  nothing reads it, and it is not part of this schema.

### The nine roles

| role | what it paints |
| --- | --- |
| `border` | the panel frame: corners, side rails, and the rule under the table header |
| `accent` | the `tokenstat ·` half of every panel title, and every TOK/S number (hero and table) |
| `label` | the view name in a panel title (`tokenstat · sessions`), the hero's date span, the table's column names |
| `num` | money: the `--cost` and `--real-cost` columns and the hero's `$` tails |
| `tin` | the `IN` column and the hero's in-count; the left end of the SHARE bar gradient |
| `tout` | the `OUT` column and the hero's out-count; the right end of that gradient |
| `tcach` | the `--cache` column |
| `ttotal` | the `TOTAL` column (bold) and the hero's grand total |
| `tshare` | the `SHARE %` and `SESSIONS` columns and the hero's row count |

A theme paints color and nothing else: no number, glyph, or width moves, and no
color is applied at all when the output is piped, `NO_COLOR` is set, or `--json`
is asked for.

## Commands

Run these from the folder that holds `tokenstat.py` (this folder's parent):

```bash
python3 tokenstat.py --theme                       # list slugs + names, mark the default
python3 tokenstat.py --theme=nord --total          # paint this run (overlay, like --cost)
python3 tokenstat.py --theme-default=nord          # save the default for later runs
python3 tokenstat.py --theme=nord --theme-default  # ...or save this run's --theme
```

Precedence: `--theme` > `themes/config.json` > Neon Midnight. An unknown
`--theme=SLUG` exits 2 and lists the available slugs; a saved default that no
longer resolves warns naming `themes/config.json` and renders in Neon Midnight
rather than costing you the report.

`themes/config.json` is that saved default (`{"default": "<slug>"}`), written by
`--theme-default` — it is **not** a theme, and the scan skips a file with exactly
that name. It and your `*.json` themes are gitignored: this folder holds personal
state, so a fresh clone contains only this README, and the ten built-ins live in
`tokenstat.py` itself.
