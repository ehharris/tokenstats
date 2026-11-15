# Visual redesign — "sunset" theme with background-block bars

Date: 2026-11-15 · Status: awaiting approval · Predecessors: `2026-11-13-token-counter-design.md` and successors (share, tok/s+cost, cache increments)

## Intent

Make tokenstat's displayed reports look colorful-modern instead of plain
monochrome text. The human partner picked the direction by reacting to a
live ANSI mockup (`/tmp/opencode/tokenstat-mockup/mockup.py`, throwaway):
the **sunset** palette (the existing blue→orange bar identity expanded to
the whole UI) and **truecolor background-block bars everywhere** (in place
of gradient glyph bars, for consistency).

## Goals

- Sunset-themed rendering of every displayed table view: sessions, daily,
  `--total`, share views (`--total --model` / bare `--model`), `--from-unsloth`,
  `--from-runs`, and `reconcile`.
- Rounded-border panels around each report section (title strip with mode +
  active filters).
- A header panel above the table: hero grand-total tokens, session count,
  model count, in/out split, date range, sources.
- Background-block bars everywhere a bar appears: share-bar column and any
  per-row visual, plus new stacked IN/OUT block bars where they fit; solid
  track color behind each bar scaled to the largest row.
- Per-class color language throughout: IN blue (#60a5fa), OUT orange
  (#fb923c), CACHE amber (#facc15), matching numbers and bars.

## Non-goals (this increment)

- Interactive TUI mode (`tokenstat ui`) — approved as a possible later
  phase over the same data layer; not designed here.
- Any change to `--json`, piped output, filtering, aggregation, ingestion,
  or exit-code behavior.
- Theme switching / config files (single built-in sunset theme for now).

## Anti-goals

- Breaking "single Python file, stdlib only" — no third-party deps (no Rich;
  the mockup proved hand-rolled ANSI is sufficient).
- Changing any displayed *number* — this is presentation only.
- Regressions in cron/piped contexts: `record`, `import-unsloth`, and piped
  reports must stay exactly as plain text.

## Constraints (from AGENTS.md, reaffirmed)

- `_color_enabled` tty-gating stays the single switch; `NO_COLOR` respected.
  Piped output byte-identical to today's plain tables.
- All existing tests pinning plain/piped output must pass unchanged; new
  rendering gets its own tests (pure functions: theme constants, panel
  builder, block-bar builder — escape-aware width math).
- Store invariants untouched (this touches no SQL beyond what views already run).

## Approaches considered

1. **Rich-based restyle** — polished tables cheaply; rejected: adds a
   dependency and breaks the stdlib-only identity for effects we already
   proved we can hand-roll.
2. **Full Textual TUI now** — biggest wow; rejected as first step: new dep,
   event loop, different test story; deferred as optional phase 2.
3. **Hand-rolled ANSI display layer (chosen)** — theme constants + panel and
   block-bar primitives beside the existing `render_table`/`paint_bars`;
   tty-gated, JSON/piped contracts untouched. Chosen: keeps every invariant,
   zero installs, mockup already validated the look.

## Design decisions (as made with the human partner)

- Palette: **sunset** — border `(87,69,62)`, accent/title orange
  `(251,146,60)`, label dim `(181,153,138)`, numbers `(245,235,229)`,
  IN `(96,165,250)`, OUT `(251,146,60)`, CACHE `(250,204,21)`, track
  `(41,34,30)`. (Mockup constants; exact values are the builder's to tune.)
- Bars: background-block style everywhere — `n = round(width * total /
  max_total)` cells, IN-colored then OUT-colored run for stacked views,
  remainder track-colored; one space padding either side. Gradient glyph
  bars (`paint_bars`) retire from displayed output.
- On-by-default when stdout is a tty (same rule today's bar color uses);
  plain fallback keeps the current table renderer as-is.

## Left to the builder

- Exact panel widths / responsive behavior (fixed ~104 cols is fine; no
  hard requirement), which columns get per-class color in each view, and
  header-panel line composition per report mode.
- Whether `--from-runs` bars are single-hue (runs have no in/out split) —
  suggested: OUT orange throughout with track.
