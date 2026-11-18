# `--cost` Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `--cost=<slug>` option (model slugs + family aliases resolving to each provider's mid-tier model) replaces `--cost`/`--cost-tier`, the COST header names the pinned pricing model, cost cells render 2 decimals, and `--real-cost` shows the stored real USD as a column.

**Architecture:** All changes are display/JSON overlays in the single file `tokenstat.py`: a re-keyed `PRICES` table plus `FAMILY_ALIASES` resolved once in `main()`, per-run column tuples, and one new `report_header` tail. No ingestion, schema, or stored-data changes.

**Tech Stack:** stdlib-only Python 3 (argparse, sqlite3), unittest.

**Spec:** `docs/superpowers/specs/2026-11-17-cost-rework-design.md`

## Global Constraints

- stdlib-only Python, single file `tokenstat.py`; no new dependencies, no install step.
- Tests: `python3 -m unittest test_tokenstat` must be green after every task, and the suite must stay warning-clean under `python3 -W error::ResourceWarning -m unittest test_tokenstat`.
- Flag-less default output stays byte-identical (existing pins enforce this — never "fix" them by changing flag-off rendering).
- `reconcile` rejects all cost overlays; `--real-cost` is additionally rejected with `--from-unsloth` / `--from-runs` (their stored cost is always 0.0).
- Prices are vendor LIST prices; before committing Task 1, verify each `PRICES` value against the vendor's pricing page (repo convention) and correct any drift — the header names the pinned model, so table and header must agree.
- Repo convention: work on an increment branch (`cost-rework`), fast-forward merge to `master` only after the whole plan reviews clean.
- Commit after every task with a conventional message (`feat:` / `test:` / `docs:`).

## Review Focus

- `--cost reconcile` (space form): argparse eats `reconcile` as the tier value → must exit 2 with an invalid-choice error, never silently render a dashboard (Task 1 pins).
- `--cost=` (empty value): must exit 2, not fall back to the default tier (Task 1 pins).
- Sub-cent cost cells render `$0.00` under the 2-decimal rule — blank or `$0` would be wrong (Task 2 pins).
- Dashboard hero real-cost total is over ALL sessions, not the truncated top-5 model panel — mirrors the existing estimate-hero decision (Task 3 pins).
- `--real-cost` must not change `--json` shape: `cost` is already on every row; no new key (Task 3 pins).

---

### Task 1: `--cost=<slug>` — pricing table, family aliases, named header

**Files:**
- Modify: `tokenstat.py:928-957` (`PRICES`, `annotate_cost`), `tokenstat.py:908` (`COST_COLUMN`), `tokenstat.py:1397-1403` (argparse), `tokenstat.py:1492-1501` (dashboard), `tokenstat.py:1636-1638` (report path)
- Test: `test_tokenstat.py` — `CostTests` (2195-2247), `CliTotalTests` (2049+), `DefaultDashboardTests` (3218+)

**Interfaces:**
- Produces: `PRICES: dict[str, dict]` keyed by model slug, each entry `{"name": str, "in": float, "out": float, "cache_read": float, "cache_write": float}` (USD per Mtok); `FAMILY_ALIASES: dict[str, str]` (family → mid-tier slug); `resolve_cost_tier(slug: str) -> str`; `annotate_cost(rows, tier="sonnet-5")` (math unchanged, key-presence dispatch); `cost_column(tier: str) -> tuple[str, str, str]`. `args.cost` is now `str | None` (raw slug/alias); `main()` resolves once into a local `cost_tier`. `--cost-tier` and `args.cost_tier` no longer exist.

- [ ] **Step 1: Update `CostTests` to the new slugs/values and add alias tests**

Retarget the existing tests (same structure, new tier strings and expected values — Sonnet 5 is $2/$10, cache-read 0.1×, cache-write 1.25×):

- `test_annotate_cost_session_uses_all_token_classes_sonnet`: `annotate_cost(rows, "sonnet-5")`, expect `14.70` (2 + 10 + 0.2 + 2.5 per Mtok-class); update the comment.
- `test_annotate_cost_tier_changes_number`: `"haiku-4.5"` → 1.0, `"opus-5"` → 5.0.
- `test_annotate_cost_event_uses_prompt_completion`: `"sonnet-5"` → 12.0.
- `test_annotate_cost_run_uses_progress_only`: `"sonnet-5"` → 10.0.
- `test_run_aggregate_rows_carry_progress_tokens`: `"sonnet-5"`, expect `0.005` ((100+400)×10/1e6); update comment.
- `test_annotate_cost_does_not_mutate_input_rows`: `"sonnet-5"`.

Add to `CostTests`:

```python
def test_resolve_cost_tier_alias_maps_to_mid_tier(self):
    from tokenstat import resolve_cost_tier, FAMILY_ALIASES, PRICES
    for alias, target in FAMILY_ALIASES.items():
        self.assertEqual(resolve_cost_tier(alias), target)
        self.assertIn(target, PRICES)
        self.assertTrue(PRICES[target]["name"])

def test_resolve_cost_tier_passes_model_slugs_through(self):
    from tokenstat import resolve_cost_tier
    self.assertEqual(resolve_cost_tier("sonnet-5"), "sonnet-5")
```

- [ ] **Step 2: Run `python3 -m unittest test_tokenstat.CostTests -v`** — expect FAIL (`KeyError: 'sonnet-5'` / `ImportError: resolve_cost_tier`).

- [ ] **Step 3: Replace `PRICES` and add `FAMILY_ALIASES` + `resolve_cost_tier` in `tokenstat.py` (replaces 928-935)**

Verify every value against the vendor pricing page first (Global Constraints); these are the anchors checked 2026-11-17:

```python
# Reference list prices, USD per million tokens, verified 2026-11-17.
# Anthropic bills cache reads at 0.1x and cache writes at 1.25x input;
# the other vendors bill no cache-write premium (cache_write = in).
PRICES = {
    "haiku-4.5":          {"name": "Claude Haiku 4.5",     "in": 1.00, "out": 5.00,  "cache_read": 0.10,  "cache_write": 1.25},
    "sonnet-5":           {"name": "Claude Sonnet 5",      "in": 2.00, "out": 10.00, "cache_read": 0.20,  "cache_write": 2.50},
    "opus-5":             {"name": "Claude Opus 5",        "in": 5.00, "out": 25.00, "cache_read": 0.50,  "cache_write": 6.25},
    "gpt-5.1-codex-mini": {"name": "GPT-5.1-Codex-Mini",   "in": 0.25, "out": 2.00,  "cache_read": 0.025, "cache_write": 0.25},
    "gpt-5.1-codex":      {"name": "GPT-5.1-Codex",        "in": 1.25, "out": 10.00, "cache_read": 0.125, "cache_write": 1.25},
    "deepseek-v3.2":      {"name": "DeepSeek V3.2",        "in": 0.28, "out": 0.40,  "cache_read": 0.028, "cache_write": 0.28},
    "qwen-turbo":         {"name": "Qwen-Turbo",           "in": 0.05, "out": 0.20,  "cache_read": 0.05,  "cache_write": 0.05},
    "qwen-plus":          {"name": "Qwen-Plus",            "in": 0.40, "out": 1.20,  "cache_read": 0.40,  "cache_write": 0.40},
    "qwen-max":           {"name": "Qwen-Max",             "in": 1.60, "out": 6.40,  "cache_read": 1.60,  "cache_write": 1.60},
}

# Family name -> the mid-tier model the provider offers (two-model lineups:
# the standard model is the mid, not the light one).
FAMILY_ALIASES = {"claude": "sonnet-5", "openai": "gpt-5.1-codex",
                  "codex": "gpt-5.1-codex", "deepseek": "deepseek-v3.2",
                  "qwen": "qwen-plus"}


def resolve_cost_tier(slug):
    """Family alias -> mid-tier model slug; model slugs pass through."""
    return FAMILY_ALIASES.get(slug, slug)
```

Change `annotate_cost`'s default to `tier="sonnet-5"` (body unchanged).

- [ ] **Step 4: Merge the CLI into one option (replaces `tokenstat.py:1397-1403`)**

```python
    parser.add_argument("--cost", nargs="?", const="sonnet-5", default=None,
                        metavar="TIER", dest="cost",
                        choices=sorted(PRICES) + sorted(FAMILY_ALIASES),
                        help="add a reference cost estimate column priced at"
                             " TIER (model slug or family name; default:"
                             " sonnet-5); display-only overlay (not with"
                             " reconcile). Prefer --cost=TIER: the space form"
                             " can swallow a following command")
```

Delete the `--cost-tier` argument entirely. Immediately after `args = parser.parse_args(argv)` (line 1409) add the single resolution point:

```python
    cost_tier = resolve_cost_tier(args.cost) if args.cost else None
```

Replace the four `args.cost_tier` uses (dashboard 1494-1501, report path 1637) with `cost_tier`.

- [ ] **Step 5: Replace `COST_COLUMN` with `cost_column(tier)` (`tokenstat.py:908`)**

```python
def cost_column(tier):
    """(header, key, kind) for the --cost overlay; the header names the model
    the pricing is pinned to, so a stale price table is visible."""
    return (f"COST \u00b7 {PRICES[tier]['name']} pricing", "cost_est", "cost")
```

Update both append sites: dashboard `m_cols, s_cols = m_cols + [cost_column(cost_tier)], s_cols + [cost_column(cost_tier)]`; report path `columns = columns + [cost_column(cost_tier)]`.

- [ ] **Step 6: Add CLI tests to `CliTotalTests`** (fixture: two default `src_row`s; reuse its `run_main`)

```python
    def test_cost_bare_header_names_pinned_model(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost"])
        self.assertEqual(code, 0)
        self.assertIn("COST \u00b7 Claude Sonnet 5 pricing", out)

    def test_cost_slug_changes_header(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost=deepseek-v3.2"])
        self.assertEqual(code, 0)
        self.assertIn("COST \u00b7 DeepSeek V3.2 pricing", out)

    def test_cost_family_alias_equals_model_slug(self):
        base = ["--db", self.db, "--src", self.src, "--unsloth-db",
                self.no_udb, "--total"]
        _, alias, _ = self.run_main(base + ["--cost=qwen"])
        _, model, _ = self.run_main(base + ["--cost=qwen-plus"])
        self.assertEqual(alias, model)
        self.assertIn("COST \u00b7 Qwen-Plus pricing", alias)

    def test_cost_invalid_slug_exits_2(self):
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb, "--total", "--cost=bogus"])
        self.assertEqual(cm.exception.code, 2)

    def test_cost_empty_value_exits_2(self):  # review-focus pin
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb, "--total", "--cost="])
        self.assertEqual(cm.exception.code, 2)

    def test_cost_space_form_swallows_command_exits_2(self):  # review-focus pin
        # "--cost reconcile": argparse eats the command as the tier value;
        # the user must get exit 2, never a silent dashboard.
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb, "--cost", "reconcile"])
        self.assertEqual(cm.exception.code, 2)

    def test_cost_tier_flag_removed(self):
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb, "--total",
                  "--cost-tier", "opus"])
        self.assertEqual(cm.exception.code, 2)
```

- [ ] **Step 7: Update `DefaultDashboardTests` pins**

- `test_hero_shows_total_estimate_only_with_cost`: new expected tail `~$19.03` (sonnet-5: 6 default rows × $0.00565 + s4 $19.00025 = $19.034); update the comment's math to the $2/$10 prices.
- `test_cost_and_cache_overlays_on_both_panels`: the table header now ends the line with `pricing` — change `endswith("COST")` to `endswith("pricing")`.
- Add:

```python
    def test_bare_cost_header_names_pinned_model(self):
        code, out, _ = self.bare(["--cost"])
        self.assertEqual(code, 0)
        plain = _ANSI_RE.sub("", out)
        self.assertEqual(plain.count("COST \u00b7 Claude Sonnet 5 pricing"), 2)
```

- [ ] **Step 8: Run the full suite** — `python3 -m unittest test_tokenstat` then the `-W error::ResourceWarning` variant; expect all green.

- [ ] **Step 9: Commit** — `git add tokenstat.py test_tokenstat.py && git commit -m "feat: single --cost=<slug> option with multi-vendor pricing and model-named header"`

---

### Task 2: 2-decimal cost rendering

**Files:**
- Modify: `tokenstat.py:1106` (`paint_cells.render`), `tokenstat.py:1243-1244` (`render_table.fmt`)
- Test: `test_tokenstat.py` — add a `CostFormatTests` class

**Interfaces:**
- Consumes: the `cost` cell kind used by `cost_column` (Task 1).
- Produces: every `cost`-kind cell renders `$X.XX` in both plain and painted paths.

- [ ] **Step 1: Write the failing tests**

```python
class CostFormatTests(unittest.TestCase):
    """cost-kind cells render $X.XX (2 decimals, human decision 2026-11-17)."""

    def test_render_table_cost_two_decimals(self):
        from tokenstat import render_table
        table = render_table([{"cost_est": 14.7}],
                             [("COST", "cost_est", "cost")])
        self.assertIn("$14.70", table)

    def test_render_table_sub_cent_cost_renders_zero(self):  # review-focus pin
        from tokenstat import render_table
        table = render_table([{"cost_est": 0.004}],
                             [("COST", "cost_est", "cost")])
        self.assertIn("$0.00", table)

    def test_paint_cells_cost_two_decimals(self):
        from tokenstat import paint_cells
        (row,) = paint_cells([{"cost_est": 14.7}])
        self.assertIn("$14.70", row["cost_est"])
```

- [ ] **Step 2: Run `python3 -m unittest test_tokenstat.CostFormatTests -v`** — expect FAIL (`$14.7000`).
- [ ] **Step 3: Change both formatters** — `tokenstat.py:1106` and `tokenstat.py:1244`: `f"${value:.4f}"` → `f"${value:.2f}"` (keep the trailing comments; adjust the 1106 comment to `# cost_est / cost`).
- [ ] **Step 4: Run the full suite** — expect green (no existing pin asserts 4 decimals).
- [ ] **Step 5: Commit** — `git commit -am "feat: cost columns render 2 decimals"`

---

### Task 3: `--real-cost` — stored-cost column, hero tail, rejects

**Files:**
- Modify: `tokenstat.py` argparse (after the `--cost` block), `tokenstat.py:908` area (new `REAL_COST_COLUMN`), `tokenstat.py:1162-1207` (`report_header`), `tokenstat.py:1492-1507` (dashboard), `tokenstat.py:1525-1528` + `1539-1551` (guards), `tokenstat.py:1636-1644` (report path)
- Test: `test_tokenstat.py` — new `RealCostTests` class, `HeroTokSTests` (1078+), `DefaultDashboardTests` (3218+)

**Interfaces:**
- Consumes: `cost_column`/`cost_tier` (Task 1); rows already carry the stored `cost` float in every session-family view and aggregate.
- Produces: `REAL_COST_COLUMN = ("REAL COST", "cost", "cost")`; `report_header(..., real_cost=None)` appending a `· $X.XX` tail after the estimate tail; `args.real_cost` bool.

- [ ] **Step 1: Write the failing tests**

New class — copy `setUp`/`tearDown`/`run_main` verbatim from `CliTotalTests` (fixture: two default `src_row`s, each stored `cost=0.01`):

```python
class RealCostTests(unittest.TestCase):
    """--real-cost: the stored real USD as a display column."""

    def test_real_cost_column_shows_stored_cost(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("REAL COST", out)
        self.assertIn("$0.02", out)          # grand total 2 x $0.01

    def test_real_cost_coexists_with_estimate(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost", "--real-cost"])
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        self.assertLess(header.index("COST \u00b7"), header.index("REAL COST"))

    def test_real_cost_json_shape_unchanged(self):  # review-focus pin
        base = ["--db", self.db, "--src", self.src, "--unsloth-db",
                self.no_udb, "--total"]
        _, plain, _ = self.run_main(base + ["--json"])
        _, real, _ = self.run_main(base + ["--real-cost", "--json"])
        self.assertEqual(set(json.loads(plain)[0]), set(json.loads(real)[0]))

    def test_real_cost_rejects_unsupported_views(self):
        for extra in (["reconcile"], ["--from-unsloth"], ["--from-runs"]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()):
                main(["--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb] + extra + ["--real-cost"])
            self.assertEqual(cm.exception.code, 2)
```

Add to `HeroTokSTests` (uses its `hero()` helper):

```python
    def test_hero_real_cost_tail_sits_after_estimate(self):
        text = self.hero(cost_est=1.0, real_cost=5.0)
        self.assertIn("$5.00", text)
        self.assertLess(text.index("~$1.00"), text.index("$5.00"))
```

Add to `DefaultDashboardTests`:

```python
    def test_dashboard_hero_real_total_over_all_sessions(self):
        # review-focus pin: 7 sessions x stored $0.01 = $0.07, not the
        # truncated top-5 panel's sum.
        code, out, _ = self.bare(["--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("$0.07", _ANSI_RE.sub("", out))
        self.assertNotIn("~$", out)          # no estimate tail without --cost
```

- [ ] **Step 2: Run the new tests** — expect FAIL (unrecognized `--real-cost` / unexpected keyword `real_cost`).
- [ ] **Step 3: Implement**

- argparse, after `--cost`: `parser.add_argument("--real-cost", action="store_true", dest="real_cost", help="add a REAL COST column with the stored real USD OpenCode reported (session views only; not with reconcile, --from-unsloth or --from-runs)")`.
- Near `cost_column`: `REAL_COST_COLUMN = ("REAL COST", "cost", "cost")` with a one-line comment (stored real cost, display-only; rows already carry `cost`).
- `report_header`: add `real_cost=None` keyword after `cost_est`; append tail `(" · " + paint(f"${real_cost:,.2f}", theme["num"]) if real_cost is not None else "")` after the estimate tail; extend the docstring (no tilde — it is real).
- Guards: extend the `--cache` guard pattern with a sibling — `if args.real_cost and (args.from_unsloth or args.from_runs): parser.error("--real-cost requires OpenCode session rows; Unsloth event/run sources store no real cost")`. Add `+ (["--real-cost"] if args.real_cost else [])` to reconcile's reject list.
- Dashboard block (after the `--cost` block): `hero_real = None`; when `args.real_cost`: `hero_real = sum(r["cost"] for r in all_rows)`, append `REAL_COST_COLUMN` to `m_cols`/`s_cols`, pass `real_cost=hero_real` to `report_header`.
- Report path (after the `--cost` overlay block): `if args.real_cost: columns = columns + [REAL_COST_COLUMN]` (reconcile already exited via its reject list; no row annotation needed).

- [ ] **Step 4: Run the full suite + warning-clean variant** — expect green.
- [ ] **Step 5: Commit** — `git commit -am "feat: --real-cost shows the stored real cost as a column"`

---

### Task 4: Docs — README, AGENTS.md, module docstring

**Files:**
- Modify: `README.md:71-73` (examples) and `README.md:166-183` ("Cost estimates" section; add a "Real cost" subsection)
- Modify: `AGENTS.md` (new increment paragraph; update the `--cost` line in Commands)
- Modify: `tokenstat.py` module docstring (the `Opt-in --cost` line)

**Interfaces:**
- Consumes: final behavior from Tasks 1-3.
- Produces: docs matching reality (repo convention: docs are part of the increment).

- [ ] **Step 1: README** — examples become `python3 tokenstat.py --total --model --cost` and `python3 tokenstat.py --from-runs --cost=opus-5`; add `python3 tokenstat.py --total --real-cost`. Rewrite "Cost estimates": `--cost[=TIER]` (bare = `sonnet-5`), the slug list + family aliases with the mid-tier rule, the model-named header, 2-decimal rendering, and that `--real-cost` displays the stored real cost (which `--json` has always carried) and is rejected with `--from-unsloth`/`--from-runs`/`reconcile`. Keep the "reference estimate, never a bill" wording.
- [ ] **Step 2: AGENTS.md** — one increment paragraph in the style of the existing ones (what changed, where, decisions: stored-cost semantics for `--real-cost`, family→mid-tier, 2-decimal, default re-pinned to Sonnet 5) and update the Commands example line to `--cost[=<slug>]` / `--real-cost`.
- [ ] **Step 3: `tokenstat.py` module docstring** — update the `Opt-in --cost` sentence to the new flag form and mention `--real-cost`.
- [ ] **Step 4: Run the full suite** — expect green (docs-only).
- [ ] **Step 5: Commit** — `git commit -am "docs: document the --cost rework and --real-cost"`
