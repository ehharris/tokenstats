# token_counter — working notes for agents

**What this is:** a permanent local record of all AI token usage on this
machine. `tokenstat.py` (stdlib-only Python, no install step) snapshots two
sources into its own SQLite store and reports them filtered by model, date,
and token counts. Built 2026-11-13 via spec → plan → TDD; design doc in
`docs/superpowers/specs/`, implementation plan in `docs/superpowers/plans/`.

**State at handoff:** everything merged to `master` (no remote configured —
local-only repo, no push). 41 tests green. Store `usage.db` already holds
full recoverable history: ~53 OpenCode sessions and ~1,300 Unsloth request
events back to 2026-11-06.

## Commands (run from this directory)

```bash
python3 tokenstat.py                                   # snapshot both + report sessions
python3 tokenstat.py --model unsloth --since 2026-11-01 --until 2026-11-13
python3 tokenstat.py --min-in 10000 --daily            # thresholds apply to daily mode too
python3 tokenstat.py --total                           # all-time grand total, one row
python3 tokenstat.py --total --model unsloth           # all-time totals, one row per model
python3 tokenstat.py --from-unsloth [--daily|--json]   # Unsloth's own per-request log
python3 tokenstat.py reconcile                         # lost-history view per model
python3 tokenstat.py record | import-unsloth           # explicit snapshots (exit 1 if source unreadable)
python3 -m unittest test_tokenstat -v                  # tests (~0.05s, no deps)
```

## Data sources (verified live)

- **OpenCode:** `~/.local/share/opencode/opencode.db`, table `session_v2` —
  per-session totals only (no per-request data). OpenCode **prunes** this
  table; import promptly or history is lost. Model column is JSON
  (`providerID` + `id`). Providers seen: `unsloth-studio`, `lemonade`,
  `opencode`. Timestamps are epoch **ms**.
- **Unsloth Studio:** `~/.unsloth/studio/studio.db`, table
  `api_usage_events` — per-request prompt/completion tokens, model, ms
  timestamp. Survives OpenCode pruning. `prompt_tokens` includes KV-cache
  hits (maps to OpenCode's input+cache_read, NOT input alone). Studio also
  has Prometheus metrics on the llama-server port and richer event payloads,
  deliberately not imported (ephemeral / UI-only — see conversation history
  in plan docs).

## Invariants — do not break these

1. **No double counting.** One row per source key (`session_id` /
   `event_id`); every numeric column stores the latest observed total via
   monotonic `max`, never an increment; rows are never deleted. Re-running
   any import is a provable no-op. Reports always SUM over distinct keys.
2. **The two sources describe the same traffic** — never sum them together.
   `reconcile` exists to compare them; positive gap = OpenCode lost it,
   negative gap = Studio log missing it (rotation/retry).
3. **Labels never shrink:** `record()` keeps the best existing
   provider/model/title when a source nulls them.
4. **Date windows are local-time and until-day inclusive**, computed via
   `_day_end_ms` (calendar next-midnight, DST-safe — do not replace with
   `+86_400_000`; this machine observes MST/MDT).
5. **Sources open read-only** (`file:...?mode=ro`) — never write to
   OpenCode's or Studio's databases; they belong to running services.
6. Query modes warn-and-continue when a source is missing/locked (exit 0);
   explicit snapshot commands exit 1 on failure.
7. Our store uses WAL + busy_timeout so cron and interactive runs overlap
   safely. Keep `.gitignore` covering `usage.db*` and `record.log`.

## Known gotchas / deferred work (from two review rounds)

- Dates in session mode bucket by **session creation day**; 7 of 53 real
  sessions span multiple days, so per-day session numbers differ from
  Unsloth's request-dated daily numbers. Totals agree; attribution differs.
- `--model` does not escape LIKE `%`/`_` metacharacters (both paths).
- One bad source row in the OpenCode path aborts that run (`record()` lacks
  per-row isolation; the Unsloth import has it — mirror it if you touch this).
- Per-event/per-session SELECT+UPDATE loops are O(N) full rewrites each run
  (~0.4s per 100k rows; fine for years at current volume).
- `--from-unsloth` renders with session-shaped columns (cost shows 0.0000,
  blank SESSION); a dedicated event column set is unclaimed polish.
- Module docstring/`--help` still describe OpenCode as the sole source;
  base spec says the same — both stale since the Unsloth increment.
- No index on `unsloth_events(created_at)` yet (~70k rows/year expected).

## NOT DONE (decisions left to the human)

- ~~Hourly cron~~ **INSTALLED 2026-11-13** for user `elih` (`crontab -l`):
  hourly `record` + `import-unsloth`, output appended to `record.log`
  (gitignored). Verified end-to-end under cron's minimal environment
  (`env -i HOME=... PATH=/usr/bin:/bin`). Cron daemon is systemd-enabled,
  so it survives reboots. If history ever looks stale, check `record.log`.
- No git remote; no PR workflow. If one is wanted, follow the parent
  workspace's GitHub App auth notes in `/home/elih/messinaround/AGENTS.md`
  (that setup is for the openmw repo — this repo has no bot access yet).
