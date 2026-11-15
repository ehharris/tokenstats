# CACHE column (`--cache`) — design (approved 2026-11-15)

## Intent

The session views hide the KV-cache token classes, which is why a
`--total --model` lifetime figure (~3.7M for Qwen3.8-Flash-Next) reads so
far under Unsloth's per-request counters (~4.5M for 50 requests): OpenCode
tracks cached prompt tokens in `tokens_cache_read`, separate from IN, and
agentic sessions re-send the whole history every request. Surfacing CACHE
next to IN/OUT makes `IN + CACHE ≈ Unsloth prompt_tokens` visible without
leaving the session view.

## Approach considered

- **A (chosen): display-only overlay flag `--cache`, one combined column.**
  The store already ingests and the row dicts already carry
  `tokens_cache_read`/`tokens_cache_write`; nothing is stored or fetched new.
  Exactly the shape of the existing `--cost` overlay, so it inherits that
  pattern's tests and guard placement.
- B: always-on CACHE column — rejected: changes flag-less output for every
  user habit and widens tables where cache is irrelevant.
- C: two columns CACHE_R / CACHE_W — rejected by the human in review:
  writes are ~0 on this machine; `--json` already carries both split.

## Design

1. New flag `--cache`: adds one `CACHE` column = `tokens_cache_read +
   tokens_cache_write`, an int, inserted **between OUT and TOTAL** in the
   session (`SESSION_COLUMNS`), daily (`DAILY_COLUMNS`) and share/total
   (`SHARE_COLUMNS`) renderings. Runs views have their own column set and
   are not touched.
2. `TOTAL` stays `IN + OUT` — CACHE is informational only, never summed
   into TOTAL, so stored semantics, reconcile, and every existing number
   are unchanged.
3. Rejected combinations (argparse error, exit 2), because those sources
   carry no cache split: `reconcile`, `--from-unsloth`, `--from-runs`.
   Message names the reason (`prompt_tokens` includes cache hits but not a
   split). Guard sits with the existing flag guards, before view dispatch.
4. `--json` unchanged (rows already carry both cache fields); no new JSON
   key.
5. Off by default; flag-less output byte-identical to today.

## Left to the builder

Column tuple placement details; test names; README/AGENTS.md wording.
