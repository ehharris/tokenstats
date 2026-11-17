# Color scheme redo — "Neon Midnight" theme

Date: 2026-11-16 · Status: approved 2026-11-16 · Predecessor: `2026-11-15-visual-redesign-sunset-design.md` (same display layer, new palette)

## Intent

Replace the sunset color scheme with one of five candidates presented as
side-by-side live mockups (browser companion). The human partner picked
**Neon Midnight** and asked for two additions shown in the final mockup:
the TOTAL-color treatment also applied to the hero's grand-total number,
and a dedicated color for the SHARE column.

## Goals

- Swap every displayed-report color from the sunset palette to Neon Midnight.
- New role `ttotal` (violet, the gradient midpoint): bold on the `TOTAL`
  column and on the hero grand-total token count in `report_header`.
- New role `tshare` (teal): the `SHARE` column (`share_str` cells).
- Share-bar gradient endpoints follow the theme's in/out colors instead of
  the standalone hardcoded pair, so bar, columns, and hero share one family.

## Non-goals / anti-goals (unchanged from the sunset design)

- No theme switching or config files — single built-in theme.
- No change to `--json`, piped output, filtering, aggregation, ingestion,
  exit codes, or any displayed *number*. Stdlib-only stays.
- Piped / `NO_COLOR` output byte-identical plain text (`_color_enabled`
  remains the single switch).

## Approaches considered

The five palettes were the real alternatives (mockup-validated with the
human partner): Aurora Boreal, Neon Midnight, Fern & Ember, Deep Sea,
Velvet Ember. Chosen: **Neon Midnight** — their pick from the screens.
Within it, TOTAL could have taken the accent fuchsia or the gradient
midpoint; midpoint chosen so IN→TOTAL→OUT reads as one family (and in
other palettes accent collides with OUT). SHARE color was delegated to
the builder: teal `#5eead4` — pops against indigo, distinct from
in-blue / out-pink / cache-lime / total-violet even when `--cache` and
share views stack.

## Design decisions

- Palette (r,g,b): border `(76,58,120)` · track `(30,23,48)` · label
  `(169,156,201)` · num `(244,240,255)` · accent `(232,121,249)` · tin
  `(96,165,250)` · tout `(244,114,182)` · tcach `(163,230,53)` · ttotal
  `(167,139,250)` · tshare `(94,234,212)`.
- `SUNSET` dict renamed to `NEON` (and the test import); all default
  `theme=` parameters follow.
- Hero: `report_header` grand total (`compact(sum_of('total'))`) painted
  `ttotal`, bold; the row-count stat (`{n} {unit1}` — "3 models", "5
  sessions") painted in the same teal as SHARE (both are derived stats);
  rest of the hero line unchanged. Accent fuchsia was considered for the
  count and rejected: too close to `tout` pink on the same line.
- Table: `_PAINT_CELLS` gains `total → ttotal (bold)` changed from `num`,
  and `share_str → tshare`. TOTAL is bold today via the `num` role; it
  stays bold, just violet now.
- Gradient: bar endpoints derive from the theme's tin/tout (single source
  of truth). Bar MECHANISM is unchanged — per-glyph Unicode blocks (`█`
  full cells + one `░▒▓` shade via eighths rounding, scaled to the largest
  row), each glyph colored at its own gradient stop with the transition
  centered on the row's in/out split. Discrete steps, not a smooth fill;
  the smooth CSS strips in early mockups were a mockup artifact.
  `_token_rgb` must lerp **saturation** between the endpoint saturations
  in addition to hue/lightness — forcing S=1 only round-trips
  fully-saturated colors, and `#f472b6` is not one. Hue still sweeps UP
  (blue→violet→pink is the short arc for this pair).
- Tests: update pins that hardcode sunset/old-gradient escape sequences
  (`80;168;255`, `255;136;90`, `96;165;250` as tout neighbor,
  `251;146;60`, `250;204;21`, `245;235;229` where it names totaled cells)
  to the NEON values; add pins for ttotal on TOTAL + hero grand total and
  tshare on SHARE. Endpoint round-trip test must still pass after the S-lerp fix.

## Left to the builder

- Exact test-pin mechanics (prefer deriving expected escapes from the
  `NEON` dict over new literals where practical).
- Whether `TOKEN_IN_RGB`/`TOKEN_OUT_RGB` stay as module constants assigned
  from `NEON["tin"]/["tout"]` (suggested, keeps imports/tests working) or
  disappear into the theme.
