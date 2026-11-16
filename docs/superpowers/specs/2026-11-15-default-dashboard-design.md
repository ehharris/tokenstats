# Default dashboard design — 2026-11-15

**Intent.** Bare `python3 tokenstat.py` is the view a human actually glances
at; today it dumps every session oldest-first (so the live session hides at
the bottom — see AGENTS.md "looks stale" note) and shows no per-model
picture without a second run. The dashboard should answer, in one screen:
what's running overall, which models dominate, and what happened most
recently.

## Layout (table mode only)

Three stacked panels, top to bottom:

1. **Hero** — the existing `report_header` grand-total line + date span,
   computed over **all** sessions; title strip renders clean as
   `╭ tokenstat ─...` (no ` · sessions`).
2. **`tokenstat · usage by model · top 5 by share`** — the bare-`--model`
   share view (`SHARE_COLUMNS`, SHARE/BAR, sorted by share desc), truncated
   to the first 5 rows.
3. **`tokenstat · sessions · 5 most recent`** — `SESSION_COLUMNS`, newest
   `time_created` first, max 5 rows.

## Decisions (agreed with the human partner)

- **Trigger:** compact mode only when truly bare — no command and none of
  `--model --daily --total --from-unsloth --from-runs --since --until
  --min-in --min-out`. The pure overlays `--cost`/`--cache` work with it.
  Any view/filter flag keeps today's full-table behavior, so the complete
  session list stays reachable.
- **Share math:** SHARE is computed over **all** models first
  (`add_shares` on the full per-model rows), then truncated to 5 — the
  percentages stay true overall shares (they may sum under 100).
- **`--json` unchanged:** bare `--json` still emits every session row as a
  flat array; the limits are display-only.
- **Empty store:** no session rows → plain `no sessions match`, exactly as
  today (no panels).
- **Fewer than 5:** show what exists; no filler.

## Approaches considered

1. **Dedicated compact branch in `main()`** (chosen) — a small, isolated
   render path guarded by the bare-invocation check; every existing view
   stays byte-identical and test-pinned. Costs a little duplication with
   the single-table render tail.
2. Generalize the renderer into a list of named sections used by all views
   — cleaner long-term, but rewrites the shared path that every view
   renders through; risk not worth it for one view.
3. Always cap sessions / always show the model panel regardless of flags —
   simplest trigger, but removes access to full filtered tables
   (regression).

## Left to the builder

- Where exactly the compact check and helper live inside `tokenstat.py`
  (suggested: module-level `_table_panel(title, rows, columns, share_view,
  color)` next to the render code; compact block after implicit ingestion).
- Test placement in `test_tokenstat.py` (new TestCase classes, following
  existing CLI-test setup patterns with `make_file_db`/`src_row`).
