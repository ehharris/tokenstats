# Design: `--cost` rework — single tier option, multi-vendor pricing, `--real-cost`

Date: 2026-11-17
Status: draft — awaiting human approval
Size: small change (short design)

## Intent / why

Rework the cost overlay so one flag says "show cost, at this vendor's prices":
`--cost=<tier>` replaces the `--cost` + `--cost-tier` pair, the price table
covers popular non-Claude models, the COST column header names the model the
pricing is pinned to (so staleness is visible), and a new `--real-cost`
surfaces the stored real USD OpenCode reported as a table column (today it is
`--json`-only).

## Goals

- **G1** `--cost=<tier>`: one option, still defaults to Claude Sonnet when
  bare; `--cost-tier` is removed.
- **G2** Price table gains Codex, DeepSeek, and Qwen models alongside the
  Claude tiers (Claude tiers re-pinned to current models), keyed by specific
  model slug; a family name (`--cost=qwen`) resolves to the provider's
  mid-tier model.
- **G3** COST column header names the pricing model, e.g.
  `COST · Claude Sonnet 5 pricing`.
- **G4** `--real-cost` adds a `REAL COST` column from the stored `cost`
  field (real OpenCode data), coexisting with `--cost`.
- **G5** Flag-less default output stays byte-identical; overlays stay
  display/JSON-only, nothing stored.

## Non-goals

- No per-row model-aware pricing (human chose stored-cost semantics for
  `--real-cost`).
- No `--cost=real` pseudo-tier (kept as its own flag, per the request).
- No changes to ingestion, the stored schema, or the two-source invariants.
- `reconcile` stays excluded from cost overlays (existing reject list).

## Anti-goals

- Do not present the reference estimate as a bill (unchanged wording).
- Do not render a `REAL COST` column on views whose stored cost is always
  0.0 (`--from-unsloth`, `--from-runs`) — reject the flag there instead.

## Approaches considered

- **A — single `--cost` with `nargs="?"` (chosen).** `const="sonnet-5"`,
  `choices=<model slugs + family aliases>`, `default=None` (off) — the
  pattern `--sessions` already uses. Human confirmed.
- **B — fold real cost into the same dial** (`--cost=real`). Fewer flags, but
  one option would mean two different data sources (estimate vs stored); the
  human asked for `--real-cost` as its own flag. Dropped.
- **C — per-row model-aware pricing for `--real-cost`.** Rejected by the
  human in favor of the stored-cost column.

Forks, human-confirmed: `--cost` and `--real-cost` coexist as two columns;
default re-pinned to Claude Sonnet 5; cost columns render **2 decimals**
(was 4); a family name resolves to the provider's **mid-tier model**
(this supersedes the earlier family-slug idea — slugs are model names,
families are aliases).

## The design

### CLI

- `--cost` → `nargs="?"`, `const="sonnet-5"`,
  `choices=sorted(PRICES) + sorted(FAMILY_ALIASES)`, `default=None`,
  `dest="cost"`. Truthiness (`if args.cost`) keeps working (str vs None).
  `--cost-tier` deleted. Resolution happens once in `main`:
  `tier = FAMILY_ALIASES.get(args.cost, args.cost)`, so every downstream
  user (`annotate_cost`, header text) sees a concrete model slug.
  Caveat: the space form `--cost opus-5` works, but `--cost reconcile` would
  swallow the command as the tier value (invalid-choice error); help text
  steers to the `--cost=<slug>` form.
- `--real-cost` → `action="store_true"`, `dest="real_cost"`.
- Rejects (parser.error, existing guard style): `reconcile` rejects both
  (extend its reject list); `--real-cost` is rejected with `--from-unsloth`
  and `--from-runs` (their stored cost is always 0.0), like `--cache` is.
- Bare-run dashboard trigger unchanged: `--cost`/`--cache`/`--real-cost` are
  overlays, not views.

### Pricing table

`PRICES` is keyed by **specific model slug**; each entry carries a display
`name` plus the four prices, and `annotate_cost`'s math is unchanged
(key-presence dispatch, cache multipliers). `FAMILY_ALIASES` maps a provider
family name to the **mid-tier model** the provider offers (human decision);
with a two-model lineup the standard/flagship model is the mid, not the
light one. Values verified at implementation against vendor list prices
(repo convention); the exact lineup and mid-tier picks are the builder's
call. Approximate anchors:

| slug | name | in | out | cache_read | cache_write |
|---|---|---|---|---|---|
| haiku-4.5 | Claude Haiku 4.5 | 1.00 | 5.00 | 0.1× in | 1.25× in |
| sonnet-5 | Claude Sonnet 5 | 2.00 | 10.00 | 0.1× in | 1.25× in |
| opus-5 | Claude Opus 5 | 5.00 | 25.00 | 0.1× in | 1.25× in |
| gpt-5.1-codex-mini | GPT-5.1-Codex-Mini | 0.25 | 2.00 | 0.1× in | = in |
| gpt-5.1-codex | GPT-5.1-Codex | 1.25 | 10.00 | 0.125 | = in |
| deepseek-v3.2 | DeepSeek V3.2 | 0.28 | 0.40 | 0.028 | = in |
| qwen-turbo | Qwen-Turbo | 0.05 | 0.20 | = in | = in |
| qwen-plus | Qwen-Plus | 0.40 | 1.20 | = in | = in |
| qwen-max | Qwen-Max | 1.60 | 6.40 | = in | = in |

| family alias | resolves to (mid-tier) |
|---|---|
| claude | sonnet-5 |
| codex / openai | gpt-5.1-codex |
| deepseek | deepseek-v3.2 |
| qwen | qwen-plus |

Non-Anthropic vendors bill no cache-write premium → `cache_write = in`.

### Columns and hero

- `COST_COLUMN` constant becomes `cost_column(tier)`, returning
  `(f"COST · {PRICES[tier]['name']} pricing", "cost_est", "cost")`. Both
  call sites (dashboard, report path) build it from the resolved `tier`.
- `REAL_COST_COLUMN = ("REAL COST", "cost", "cost")` — reuses the existing
  `cost` render kind; rows already carry `cost`, so no annotate step is
  needed. Appended after the estimate column when both flags are on.
- **2-decimal rendering (human decision):** the `cost` render kind changes
  from `$%.4f` to `$%.2f`, so both `COST` and `REAL COST` cells read
  `$X.XX` (the hero tail already renders 2 decimals). Known consequence:
  sub-cent rows render `$0.00`.
- Hero (`report_header`): estimate tail keeps `~$X.XX`; `--real-cost` adds a
  `· $X.XX` tail (no tilde — it is real), summed over the same row set the
  estimate hero uses (dashboard: all sessions, not the truncated top-5).

### JSON

`--cost` still adds `cost_est` per row. `--real-cost` adds nothing new —
`cost` is already on every row — this is documented, not changed.

## Constraints

- stdlib-only Python, single file; TDD via `python3 -m unittest
  test_tokenstat`; suite must stay green and warning-clean.
- Flag-off output byte-identical to today (existing pins).
- Existing tests that pin the old `COST` header or use `--cost-tier` are
  updated, not deleted; README "Cost estimates", `--help`, and AGENTS.md
  updated to match.

## What's left to the builder

- Exact verified prices, the full model lineup per family, and the mid-tier
  pick per family, verified at implementation time.
- Test coverage: bare `--cost` == `--cost=sonnet-5`; family alias resolves
  to the mid-tier model (header + math); each model's math; invalid slug
  rejected; header names the model; cost cells render 2 decimals;
  `--real-cost` column values match stored `cost`; both flags coexist;
  reject lists (reconcile, `--from-unsloth`, `--from-runs`); dashboard
  overlays; hero tails; flag-off unchanged.
