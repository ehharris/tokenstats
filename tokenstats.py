#!/usr/bin/env python3
"""tokenstats — permanent local record of AI token usage on this machine.

Snapshots OpenCode's per-session totals (read-only) into our own SQLite store
and reports them filtered by model, date, and token thresholds. Reporting
views carry a TOK/S column: each session row is the mean of its per-turn
decode rates (matching OpenCode's per-prompt number), and aggregate rows are
token-weighted by generated tokens. Opt-in --cost[=<slug>] overlays a
reference dollar estimate priced at one chosen model (family names resolve to
the provider's mid-tier); opt-in --real-cost shows the stored real USD OpenCode
reported. See README.md.
"""
import argparse
import colorsys
import datetime
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter


def state_dir(home=None):
    """The one directory tokenstats owns: <home>/.tokenstats (pure path math)."""
    if home is None:
        home = os.path.expanduser("~")
    return os.path.join(home, ".tokenstats")


def default_db(home=None):
    """Our store: <home>/.tokenstats/usage.db."""
    return os.path.join(state_dir(home), "usage.db")


def themes_dir(home=None):
    """User theme files and the saved default: <home>/.tokenstats/themes."""
    return os.path.join(state_dir(home), "themes")


def theme_config(home=None):
    """The saved default theme file: <home>/.tokenstats/themes/config.json."""
    return os.path.join(themes_dir(home), "config.json")


def default_src(home=None):
    """OpenCode's database, resolved the way it can be found on each OS. Every
    candidate is tried in order and the first that exists wins; with nothing on
    disk the XDG-style default is returned so the caller still has a real path
    to report. Order: $OPENCODE_DB (absolute) → $XDG_DATA_HOME/opencode/
    opencode.db (absolute) → <home>/.local/share → macOS ~/Library/Application
    Support → Windows %APPDATA% → legacy <home>/.opencode."""
    if home is None:
        home = os.path.expanduser("~")
    local_default = os.path.join(home, ".local", "share", "opencode",
                                 "opencode.db")
    candidates = []
    env_db = os.environ.get("OPENCODE_DB", "")
    if os.path.isabs(env_db):
        candidates.append(env_db)
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if os.path.isabs(xdg):
        candidates.append(os.path.join(xdg, "opencode", "opencode.db"))
    candidates.append(local_default)
    if sys.platform == "darwin":
        candidates.append(os.path.join(home, "Library", "Application Support",
                                       "opencode", "opencode.db"))
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if os.path.isabs(appdata):
            candidates.append(os.path.join(appdata, "opencode", "opencode.db"))
    candidates.append(os.path.join(home, ".opencode", "opencode.db"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return local_default


STATE_DIR = state_dir()
LEGACY_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = default_db()
DEFAULT_SRC = default_src()
THEMES_DIR = themes_dir()
THEME_CONFIG = theme_config()


def default_store(state=None, legacy=None):
    """The store path main() opens when --db was not given: <state>/usage.db,
    after moving any state still sitting beside the script into <state>.

    The path it returns always holds the history: a move that fails keeps using
    the legacy file rather than trading it for an empty report, and a move lost
    to a rival run lands on the store that run migrated.

    The only function here that creates directories or moves files on its own
    (`write_default_theme` also writes, but only because `--theme-default`
    asked it to), and main() is its only caller: importing tokenstats stays
    side-effect-free, and so is every run that names its own --db. `state` and
    `legacy` take their defaults from the module globals at CALL time (the
    same reason main() reads THEMES_DIR there) so a caller that repoints the
    globals steers the migration.
    """
    state = STATE_DIR if state is None else state
    legacy = LEGACY_DIR if legacy is None else legacy
    old_db = os.path.join(legacy, "usage.db")
    new_db = os.path.join(state, "usage.db")
    try:
        os.makedirs(state, exist_ok=True)
    except OSError as exc:
        print(f"warning: could not create {state}: {exc}; using {old_db}"
              " instead", file=sys.stderr)
        return old_db

    # The siblings travel first and the store itself LAST: history is the one
    # thing a migration may not lose, so the file that decides where the store
    # lives is the last one to move. Anything that fails before it (a second
    # filesystem, a permission, a live connection on the -wal, a rival run that
    # won the race) leaves the store exactly where it was, and the files that
    # did move are pulled back. A half-moved store is the failure this avoids:
    # main() would open an empty database at the path it is handed and report
    # nothing while the real history sat at the other one, and the next run —
    # seeing both paths occupied — would never try again.
    used = new_db
    if os.path.exists(old_db) and not os.path.exists(new_db):
        moved = []
        try:
            for suffix in ("-wal", "-shm", ""):
                if os.path.exists(old_db + suffix):
                    os.replace(old_db + suffix, new_db + suffix)
                    moved.append(suffix)
        except OSError as exc:
            # Pull the siblings back only while the legacy store is still
            # there. If it is gone, a rival run moved it: our files belong
            # with it at the state dir, and moving them back would orphan them
            # beside a path that holds nothing — permanently, because the next
            # run sees both paths occupied and never retries. This is also
            # what a double fault (the forward move failed AND the reverse one
            # does) leaves behind, so one gate closes both.
            if os.path.exists(old_db):
                for suffix in reversed(moved):
                    try:
                        os.replace(new_db + suffix, old_db + suffix)
                    except OSError:
                        pass
            # Decide by what is on disk, not by what was attempted: a failed
            # move leaves the legacy store in place, and a lost race leaves it
            # gone — moved by the winner, whose store is the one to open.
            used = old_db if os.path.exists(old_db) else new_db
            if used == old_db:
                print(f"warning: could not migrate {old_db}: {exc}; using it"
                      " in place", file=sys.stderr)
        else:
            print(f"migrated store: {old_db} -> {new_db}", file=sys.stderr)

    # Themes travel with it, but only as long as the state dir has none of its
    # own: a theme is cosmetic, so a file that will not move is a warning and a
    # skipped file, never a lost report. Only top-level *.json files move —
    # README.md and examples/ are shipped documentation, not personal state.
    old_themes = os.path.join(legacy, "themes")
    new_themes = os.path.join(state, "themes")
    if os.path.isdir(old_themes) and not os.path.isdir(new_themes):
        try:
            os.makedirs(new_themes)
            names = sorted(os.listdir(old_themes))
        except OSError as exc:
            print(f"warning: could not migrate {old_themes}: {exc}; leaving"
                  " theme files in place", file=sys.stderr)
            names = []
        for name in names:
            if not name.endswith(".json"):
                continue
            old_file = os.path.join(old_themes, name)
            new_file = os.path.join(new_themes, name)
            if not os.path.isfile(old_file):
                continue
            try:
                os.replace(old_file, new_file)
            except OSError as exc:
                print(f"warning: could not migrate {old_file}: {exc}; using"
                      " it in place", file=sys.stderr)
                continue
            print(f"migrated theme file: {old_file} -> {new_file}",
                  file=sys.stderr)
    return used


MODEL_ANY = object()  # --model with no value (valid only with --total)
SESSIONS_BARE = object()  # --sessions with no value; argparse skips type on
# a non-str const, so the sentinel distinguishes the valueless flag from
# --sessions=0 (a plain string const would be run through type=int).
THEME_LIST = object()   # --theme with no value: list the themes and exit
THEME_SAVE = object()   # --theme-default with no value: save this run's theme

TOKEN_COLUMNS = (
    "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "cost",
)


def ensure_schema(dest: sqlite3.Connection) -> None:
    dest.execute(
        """CREATE TABLE IF NOT EXISTS usage (
               session_id TEXT PRIMARY KEY,
               provider TEXT NOT NULL DEFAULT '',
               model_id TEXT NOT NULL DEFAULT '',
               tokens_input INTEGER NOT NULL DEFAULT 0,
               tokens_output INTEGER NOT NULL DEFAULT 0,
               tokens_reasoning INTEGER NOT NULL DEFAULT 0,
               tokens_cache_read INTEGER NOT NULL DEFAULT 0,
               tokens_cache_write INTEGER NOT NULL DEFAULT 0,
               cost REAL NOT NULL DEFAULT 0,
               title TEXT,
               directory TEXT,
               agent TEXT,
               time_created INTEGER NOT NULL,
               time_updated INTEGER,
               first_seen INTEGER NOT NULL,
               last_seen INTEGER NOT NULL)"""
    )
    dest.execute(
        """CREATE TABLE IF NOT EXISTS opencode_steps (
               message_id TEXT PRIMARY KEY,
               session_id TEXT NOT NULL,
               turn INTEGER NOT NULL DEFAULT 0,
               step_ms INTEGER,
               tokens_generated INTEGER NOT NULL DEFAULT 0,
               time_created INTEGER NOT NULL)"""
    )
    # Hot filter columns: date-windowed queries scan these first.
    dest.execute(
        """CREATE INDEX IF NOT EXISTS idx_usage_time_created
               ON usage(time_created)"""
    )
    dest.execute(
        """CREATE INDEX IF NOT EXISTS idx_opencode_steps_session_turn
               ON opencode_steps(session_id, turn)"""
    )


def parse_model(raw):
    """Source `model` JSON -> (provider, model_id); ("", "") for NULL/invalid."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ("", "")
    if not isinstance(data, dict):
        return ("", "")
    return (str(data.get("providerID", "")), str(data.get("id", "")))


def _turn_index(types: list[str], end: int, legacy: bool) -> int:
    """Index of the input message opening the turn ending at `end` (exclusive).

    Port of OpenCode's inputIndex: walk back from `end`, remembering the last
    user/synthetic message, and stop at the first `idle` — so a mid-turn
    `synthetic` never splits a turn. legacy (a session with no `idle` rows at
    all) returns the nearest input instead of walking past one. -1 when the
    session has no input message before `end`."""
    found = -1
    for i in range(end - 1, -1, -1):
        kind = types[i]
        if kind == "idle":
            return found
        if kind not in ("user", "synthetic"):
            continue
        if legacy:
            return i
        found = i
    return found


def _upsert(dest, table, pk, rows, numeric, overwrite=(), insert_only=()):
    """Fold `rows` into `table` with one monotonic-max upsert per row.

    `rows` is a list of dicts carrying every column to write, `pk` among them;
    the column order is the first row's key order. Each column belongs to one
    class, and the classes are what keeps the ingest invariants:

      numeric      — stored as max(old, new), never an increment: a source that
                     repeats or shrinks can neither double count nor erase
                     history (invariant 1).
      overwrite    — takes the new value unconditionally, even NULL, so a run
                     that finished stops looking unfinished.
      insert_only  — written on insert and never touched again (our own
                     provenance columns, e.g. first_seen, and source
                     timestamps that are immutable upstream).
      everything else — a label: `new or old`, so a source that nulls a
                     provider/model/title keeps the best we already have
                     (invariant 3).

    A row counts `updated` when its NUMERIC merge moved and `unchanged`
    otherwise: a label-only change is not a number moving, which is what these
    counters have always meant. One malformed row fails inside its own
    statement and bumps `skipped` without aborting (or rolling back) the rest.
    The caller owns the commit, so a family's rows land in one transaction.

    Every name in `numeric`, `overwrite` and `insert_only` must be a real
    column of `table` and none of them may be `pk`; a name that is not raises
    `ValueError` before any row is written, because the alternative is silent:
    an unknown `numeric` name leaves that column in the label class, where
    invariant 1 is not protected. A payload key that is not a column is a
    different kind of mistake and lands as `skipped`, one row at a time.
    """
    # The column classes are a contract with the table, and every way to break
    # it is silent: an unknown name in `numeric` demotes a real token column to
    # the label class, where `new or old` can shrink it (invariant 1), and a
    # column in two classes resolves in `numeric`'s favor. Checked once, up
    # front, before the first row: inside the per-row try a ValueError would
    # only count as `skipped` and the whole family would vanish one row at a
    # time. `table` is interpolated into the PRAGMA because it is a literal from
    # this module, like every other fragment this engine interpolates.
    known = {row[1] for row in dest.execute(f"PRAGMA table_info({table})")}
    if pk not in known:
        raise ValueError(f"{table} has no column {pk!r}")
    by_class = {}
    for cls, names in (("numeric", numeric), ("overwrite", overwrite),
                       ("insert_only", insert_only)):
        for name in names:
            if name not in known:
                raise ValueError(f"{table} has no column {name!r} "
                                 f"(named in the {cls} class)")
            if name == pk or name in by_class:
                raise ValueError(f"{table}: {name!r} is in both the "
                                 f"{by_class.get(name, 'primary-key')} and "
                                 f"{cls} classes")
            by_class[name] = cls
    counts = Counter(inserted=0, updated=0, unchanged=0)
    if not rows:
        return counts
    cols = list(rows[0])
    labels = [c for c in cols if c != pk and c not in numeric
              and c not in overwrite and c not in insert_only]
    # Only the numeric and label columns need the stored row: an overwrite
    # column comes from the source alone, an insert_only one is never read.
    probe = [c for c in cols if c != pk and c not in overwrite
             and c not in insert_only]
    updates = [c for c in cols if c != pk and c not in insert_only]
    select = f"SELECT {', '.join(probe)} FROM {table} WHERE {pk} = ?"
    insert = (f"INSERT INTO {table} ({', '.join(cols)})"
              f" VALUES ({', '.join('?' * len(cols))})")
    update = (f"UPDATE {table} SET "
              f"{', '.join(f'{c} = ?' for c in updates)} WHERE {pk} = ?")
    for row in rows:
        try:
            existing = dest.execute(select, (row[pk],)).fetchone()
            if existing is None:
                dest.execute(insert, [row[c] for c in cols])
                counts["inserted"] += 1
                continue
            old = dict(zip(probe, existing))
            merged = {c: max(old[c], row[c]) for c in numeric}
            changed = merged != {c: old[c] for c in numeric}
            values = {**{c: row[c] for c in overwrite}, **merged,
                      **{c: row[c] or old[c] for c in labels}}
            dest.execute(update, [values[c] for c in updates] + [row[pk]])
            counts["updated" if changed else "unchanged"] += 1
        except (sqlite3.Error, TypeError, ValueError):
            # One malformed source row must not abort (or roll back) the rest:
            # sqlite3 refuses a value it cannot bind, and a text-typed numeric
            # column makes the max() merge compare str to int. Same width as
            # import_opencode_steps' per-row guard, for the same reason. The
            # column-class check above runs before this loop precisely so a
            # ValueError from a programming mistake is never counted here.
            counts["skipped"] += 1
    return counts


def record(src: sqlite3.Connection, dest: sqlite3.Connection, now_ms: int) -> dict:
    """Upsert every session_v2 row into our usage table.

    One row per OpenCode session id; token/cost columns store the latest
    observed total (max of stored and source), so re-recording can never
    double count and a pruned/rewritten source can never shrink history.
    time_updated and last_seen follow the source (last_seen is the one field
    that moves on an unchanged session), first_seen and the source's own
    time_created are written once — a creation stamp is immutable, so a
    source that ever rewrote one must not move the stored value — and the
    labels — provider, model_id, title, directory and agent — keep the best
    we already have (invariant 3).
    """
    rows = src.execute(
        "SELECT id, model, tokens_input, tokens_output, tokens_reasoning,"
        " tokens_cache_read, tokens_cache_write, cost, title, directory,"
        " agent, time_created, time_updated FROM session_v2"
    ).fetchall()
    payload = []
    for (sid, model_raw, tin, tout, treas, tcr, tcw, cost, title, directory,
         agent, created, updated) in rows:
        provider, model_id = parse_model(model_raw)
        payload.append({
            "session_id": sid, "provider": provider, "model_id": model_id,
            "tokens_input": tin or 0, "tokens_output": tout or 0,
            "tokens_reasoning": treas or 0, "tokens_cache_read": tcr or 0,
            "tokens_cache_write": tcw or 0, "cost": cost or 0,
            "title": title, "directory": directory, "agent": agent,
            "time_created": created, "time_updated": updated,
            "first_seen": now_ms, "last_seen": now_ms,
        })
    counts = _upsert(dest, "usage", "session_id", payload,
                     numeric=TOKEN_COLUMNS,
                     overwrite=("time_updated", "last_seen"),
                     insert_only=("first_seen", "time_created"))
    counts.update(import_opencode_steps(src, dest))
    dest.commit()
    return counts


def import_opencode_steps(src: sqlite3.Connection,
                          dest: sqlite3.Connection) -> dict:
    """Snapshot OpenCode assistant steps into our opencode_steps table.

    One row per session_message.id; step_ms and tokens_generated are monotonic
    max, so re-recording can never double count and a pruned source can never
    shrink history. A source without session_message imports as zero steps.

    This family keeps its own loop instead of folding into `_upsert`: the dense
    turn ordinals and the rule that a NULL step_ms never maxes against a real
    duration are semantics, not duplication.

    `step_ms` is a duration, not a running total: NULL means "no information"
    (the step never streamed), so it is deliberately NOT folded into max() as
    a zero — unlike the other numeric columns. Keep the stored value when the
    new reading is NULL, take the new value when none was stored. The spec's
    turn rule requires this: a never-streamed step contributes no duration.

    Turn numbers are dense 0-based ORDINALS, not raw message indices. The
    boundary walk (_turn_index) returns the index of the input message opening
    the turn; distinct boundary indices are numbered in first-appearance order
    so the first turn's steps get 0, the next turn's get 1, and so on. This
    keeps _turn_index's raw-index contract intact (Task 1 pins it) while the
    stored turn stays a small, stable label. A skipped row never claims an
    ordinal, so stored turns still start at 0.

    Turns are recomputed for every LIVE session, so growth and the legacy ->
    idle flip still regroup turns. A session is frozen ONLY when the source has
    lost a step we already stored (a stored message_id missing from the current
    assistant messages): pruning would otherwise move boundaries and silently
    rewrite historical rates. Frozen rows keep their stored turn; new rows in a
    pruned session take dense ordinals continuing from that session's stored
    maximum, so they cannot collide with a frozen one.

    Per-row isolation covers the whole body — ordinal lookup, blob parse,
    arithmetic and upsert — against sqlite3.Error, TypeError and ValueError.
    A well-formed blob with non-numeric timing/token fields raises TypeError
    in the arithmetic; per the spec's error-isolation constraint it bumps
    `skipped` instead of aborting the snapshot.
    """
    counts = Counter(inserted=0, updated=0, unchanged=0)
    has_table = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='session_message'").fetchone()
    if not has_table:
        return counts
    rows = src.execute(
        "SELECT id, session_id, type, time_created, data FROM session_message"
        " ORDER BY session_id, time_created").fetchall()
    # One pass over our own store: {session_id: {message_id: stored turn}}.
    # This detects pruning (a stored step gone from the source) and supplies
    # the high-water mark for new rows in a pruned session, without an N+1
    # SELECT per session or per row beyond the existing per-row upsert probe.
    stored_by_session = {}
    for sid, mid, turn in dest.execute(
            "SELECT session_id, message_id, turn FROM opencode_steps"):
        stored_by_session.setdefault(sid, {})[mid] = turn
    # ORDER BY keeps each session's messages contiguous and in time order.
    sessions = {}
    for row in rows:
        sessions.setdefault(row[1], []).append(row)
    for sid, msgs in sessions.items():
        stored = stored_by_session.get(sid, {})
        present_ids = {m[0] for m in msgs if m[2] == "assistant"}
        pruned = bool(stored.keys() - present_ids)
        stored_max = max(stored.values(), default=-1)
        types = [m[2] for m in msgs]
        legacy = not any(kind == "idle" for kind in types)
        turn_ordinals = {}      # live session: recomputed dense ordinals
        new_turn_ordinals = {}  # pruned session: dense ordinals for NEW rows
        for i, (mid, sid, kind, created, data) in enumerate(msgs):
            if kind != "assistant":
                continue
            try:
                boundary = _turn_index(types, i + 1, legacy)
                data_obj = json.loads(data)
                # Non-dict JSON is an extraction failure, not a row error
                # (mirrors parse_model): the step still inserts, with no
                # duration and no generated tokens.
                if not isinstance(data_obj, dict):
                    data_obj = {}
                timing = data_obj.get("time")
                tokens = data_obj.get("tokens")
                timing = timing if isinstance(timing, dict) else {}
                tokens = tokens if isinstance(tokens, dict) else {}
                streamed = timing.get("streamed")
                if streamed is None:
                    step_ms = None
                else:
                    first = timing.get("firstToken")
                    start = first if first is not None else created
                    step_ms = max(0, streamed - start)
                tokens_generated = ((tokens.get("output") or 0)
                                    + (tokens.get("reasoning") or 0))
                existing = dest.execute(
                    "SELECT step_ms, tokens_generated FROM opencode_steps"
                    " WHERE message_id = ?", (mid,)).fetchone()
                is_new = existing is None
                # Peek at the ordinal without claiming it: a row that fails
                # anywhere below must not consume a turn number. In a pruned
                # session existing rows keep their stored turn (the UPDATE
                # below omits `turn`); new rows continue the stored sequence
                # so they cannot collide with a frozen ordinal.
                if not pruned:
                    turn = turn_ordinals.get(boundary)
                    if turn is None:
                        turn = len(turn_ordinals)
                elif is_new:
                    turn = new_turn_ordinals.get(boundary)
                    if turn is None:
                        turn = stored_max + 1 + len(new_turn_ordinals)
                else:
                    turn = None  # frozen row: leave its stored turn alone
                if is_new:
                    dest.execute(
                        "INSERT INTO opencode_steps (message_id, session_id,"
                        " turn, step_ms, tokens_generated, time_created)"
                        " VALUES (?,?,?,?,?,?)",
                        [mid, sid, turn, step_ms, tokens_generated, created])
                    outcome = "inserted"
                else:
                    old_step, old_tokens = existing
                    # NULL is "no information", never a zero to max against.
                    if step_ms is None:
                        merged_step = old_step
                    elif old_step is None:
                        merged_step = step_ms
                    else:
                        merged_step = max(old_step, step_ms)
                    merged = [merged_step, max(old_tokens, tokens_generated)]
                    changed = merged != list(existing)
                    if pruned:
                        dest.execute(
                            "UPDATE opencode_steps SET step_ms = ?,"
                            " tokens_generated = ?, time_created = ?"
                            " WHERE message_id = ?",
                            [*merged, created, mid])
                    else:
                        dest.execute(
                            "UPDATE opencode_steps SET turn = ?, step_ms = ?,"
                            " tokens_generated = ?, time_created = ?"
                            " WHERE message_id = ?",
                            [turn, *merged, created, mid])
                    outcome = "updated" if changed else "unchanged"
                # The upsert succeeded — now the ordinal is official.
                if not pruned:
                    turn_ordinals[boundary] = turn
                elif is_new:
                    new_turn_ordinals[boundary] = turn
                counts[outcome] += 1
            except (sqlite3.Error, TypeError, ValueError):
                # One malformed source row must not abort (or roll back) the
                # rest, and must not consume a turn ordinal.
                counts["skipped"] += 1
    dest.commit()
    return counts


def _like_pattern(model):
    """Lowered substring LIKE pattern for `model`, with the LIKE metacharacters
    `%` and `_` escaped so user text always matches literally (pairs with
    ESCAPE '\\')."""
    return "%" + model.lower().replace("%", "\\%").replace("_", "\\_") + "%"


def _date_clauses(column, since_ms, until_ms):
    """(where, params) for an inclusive date window over `column`: since is a
    lower bound, until is exclusive at the end of the until-day (via
    _day_end_ms, local-time and DST-safe). Empty lists when unbounded."""
    where, params = [], []
    if since_ms is not None:
        where.append(column + " >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append(column + " < ?")
        params.append(_day_end_ms(until_ms))  # until-day inclusive
    return where, params


def _weighted_mean_rate_and_weight(pairs):
    """(token-weighted mean rate, Σ weight of the rows that averaged into it).

    Rows with no rate or no weight drop out of BOTH sides; `den` is exactly the
    denominator the mean divided by, exposed so an aggregate row can carry the
    weight it actually used rather than a sum over rows it ignored."""
    num = den = 0
    for rate, weight in pairs:
        if rate is not None and weight:
            num += rate * weight
            den += weight
    return (round(num / den, 1) if den else None), den


def _weighted_mean_rate(pairs):
    """Token-weighted mean of `pairs`, a group's (rate, tokens moved) pairs,
    rounded to one decimal like every other rate; None when nothing is left.

    Each row's rate counts in proportion to the tokens it moved, so a session
    that carried 200k tokens speaks 200x one that carried 1k. Rows with no
    rate (session with no valid turn, unfinished or failed run) and rows with
    no tokens are dropped from BOTH sides: they cannot be averaged and they
    carry no weight. The alternative — dividing the group's token sum by its
    calendar span — counted every idle second between and inside sessions, so
    one session left open overnight sank a model's average below every row
    above it."""
    return _weighted_mean_rate_and_weight(pairs)[0]


def _session_rate_groups(dest, key_sql, where, params):
    """{group key: [(per-session turn-mean rate, generated tokens)]} for
    sessions matching `where`, using query_sessions' own turn-derived rate and
    generated-token weight so an aggregate stays reproducible from the rates
    the sessions view displays. A rate is None when the session has no valid
    turn. `key_sql` is the SQL expression to group on."""
    sql = ("SELECT " + key_sql + ", " + _TURN_RATE_SQL + ", " + _GENERATED_SQL
           + " FROM usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    groups = {}
    for key, rate, generated in dest.execute(sql, params):
        groups.setdefault(key, []).append((rate, generated or 0))
    return groups


def _grouped_aggregates(dest, table, key_sql, sums, where, params, groups,
                        row_builder):
    """One engine for every grouped aggregate view: SELECT key + `sums`,
    GROUP BY key ORDER BY key ASC, one row dict per group from
    row_builder(key, counts, rate, weight).

    `counts` is the aggregate tuple (the `sums` columns of the row). `groups`
    maps a key to the (rate, weight) pairs to average for that row; None
    means the family has no per-row rate to weight, and rate/weight arrive
    None. Rows come back in query order.

    `table`, `key_sql` and `sums` are SQL fragments, interpolated into the
    statement rather than bound into it, so they are trusted internal values
    only: every caller passes a literal from this module (or a builder built
    from one). User input reaches a query through `where` and `params`, which
    are parameterized — nothing derived from a command line may be passed
    here. Same contract for `_run_rate_groups` and `_session_rate_groups`."""
    sql = "SELECT " + key_sql + " AS key," + sums + " FROM " + table
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY key ORDER BY key ASC"
    rows = []
    for row in dest.execute(sql, params):
        key, counts = row[0], tuple(row[1:])
        if groups is None:
            rate = weight = None
        else:
            rate, weight = _weighted_mean_rate_and_weight(groups.get(key, []))
        rows.append(row_builder(key, counts, rate, weight))
    return rows


def _total_aggregates(dest, table, sums, where, params, groups, row_builder,
                      group_key_sql=None):
    """All-time aggregates: with `group_key_sql`, the same grouped rows
    _grouped_aggregates would build; without it, one grand-total row keyed
    "(all)" (empty when nothing matches — COUNT(*) is 0), whose rate group is
    groups.get("(all)", []).

    `table`, `sums` and `group_key_sql` are the same trusted internal SQL
    fragments _grouped_aggregates documents, and the same rule applies."""
    if group_key_sql is not None:
        return _grouped_aggregates(dest, table, group_key_sql, sums, where,
                                   params, groups, row_builder)
    sql = "SELECT" + sums + " FROM " + table
    if where:
        sql += " WHERE " + " AND ".join(where)
    found = dest.execute(sql, params).fetchone()
    if not found[0]:
        return []
    if groups is None:
        rate = weight = None
    else:
        rate, weight = _weighted_mean_rate_and_weight(groups.get("(all)", []))
    return [row_builder("(all)", tuple(found), rate, weight)]


DAY_MS = 86_400_000


def _day_end_ms(until_ms):
    """Exclusive upper bound for an inclusive until-day: local midnight of
    the next calendar day (DST-safe, unlike until_ms + DAY_MS)."""
    start = datetime.datetime.fromtimestamp(until_ms / 1000)
    return int((start + datetime.timedelta(days=1)).timestamp() * 1000)


def parse_date(s: str) -> int:
    """YYYY-MM-DD -> epoch ms at local midnight."""
    try:
        dt = datetime.datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r}, expected YYYY-MM-DD") from None
    return int(dt.timestamp() * 1000)


def _session_filters(model, since_ms, until_ms, min_in, min_out, limit=None):
    where, params = [], []
    if model:
        where.append("lower(provider || '/' || model_id) LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(model))
    date_where, date_params = _date_clauses("time_created", since_ms, until_ms)
    where.extend(date_where)
    params.extend(date_params)
    if min_in is not None:
        where.append("tokens_input >= ?")
        params.append(min_in)
    if min_out is not None:
        where.append("tokens_output >= ?")
        params.append(min_out)
    if limit:  # None and 0 both mean "no cap"
        inner = list(params)
        where.append("session_id IN (SELECT session_id FROM usage"
                     + (" WHERE " + " AND ".join(where) if where else "")
                     + " ORDER BY time_created DESC, session_id LIMIT ?)")
        params = inner + inner + [int(limit)]
    return where, params


# Scalar subqueries correlated on usage.session_id, reused by the aggregate
# views (Task 4). _TURN_RATE_SQL is the unweighted mean of the session's turn
# rates; a turn is excluded when ANY step never streamed (COUNT(step_ms) <>
# COUNT(*)), when it moved no tokens, or when its pooled span is sub-second.
# _GENERATED_SQL is the session's total generated tokens (output+reasoning).
_TURN_RATE_SQL = (
    "(SELECT AVG(rate) FROM (SELECT SUM(tokens_generated) * 1000.0"
    " / SUM(step_ms) AS rate FROM opencode_steps"
    " WHERE opencode_steps.session_id = usage.session_id GROUP BY turn"
    " HAVING COUNT(step_ms) = COUNT(*) AND SUM(step_ms) >= 1000"
    " AND SUM(tokens_generated) > 0))")
_GENERATED_SQL = (
    "(SELECT SUM(tokens_generated) FROM opencode_steps"
    " WHERE opencode_steps.session_id = usage.session_id)")


def query_sessions(dest, model=None, since_ms=None, until_ms=None,
                   min_in=None, min_out=None, limit=None):
    """Session rows matching all given filters, oldest first.

    tok_s is the unweighted mean of the session's per-turn rates — the number
    OpenCode shows per prompt: Σ(output+reasoning) / Σ(streamed - firstToken)
    per turn, then averaged across turns. None when the session has no valid
    turn. rate_weight is the session's total generated tokens. There is
    deliberately no wall-clock fallback: mixing the two bases is the bug this
    replaces. limit=N keeps only the N most recent matching sessions
    (None/0 = uncapped)."""
    sql = ("SELECT time_created, provider, model_id, tokens_input,"
           " tokens_output, cost, title, tokens_cache_read,"
           " tokens_cache_write, " + _TURN_RATE_SQL + ", " + _GENERATED_SQL
           + " FROM usage")
    where, params = _session_filters(model, since_ms, until_ms, min_in,
                                     min_out, limit)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_created ASC"
    rows = []
    for (created, provider, model_id, tin, tout, cost, title, tcr, tcw,
         rate, generated) in dest.execute(sql, params):
        date = datetime.datetime.fromtimestamp(created / 1000).strftime(
            "%Y-%m-%d")
        rows.append({"date": date, "model": f"{provider}/{model_id}",
                     "tokens_input": tin, "tokens_output": tout,
                     "total": tin + tout, "cost": cost, "title": title or "",
                     "tokens_cache_read": tcr, "tokens_cache_write": tcw,
                     "tok_s": round(rate, 1) if rate is not None else None,
                     "rate_weight": generated or 0})
    return rows


def most_recent(rows, n):
    """`rows` oldest-first in, newest-first out, truncated to `n`;
    n <= 0 keeps all. The one recency step shared by the default
    dashboard's session panel and the --sessions list view."""
    rev = list(reversed(rows))
    return rev[:n] if n > 0 else rev


def _sessions_limit(value, daily, total):
    """Resolve --sessions into the `limit=` the query functions take
    (None = flag absent, 0 = uncapped, N = N most recent).

    A valueless --sessions lists the 20 most recent sessions, but means ALL
    sessions in the aggregate views: with --daily/--total the view is about
    coverage, not recency. `total` is the EFFECTIVE total flag — bare
    --model is the --total shortcut and counts, so a valueless --sessions
    with `--sessions --model` also means all sessions."""
    if value is None:
        return None
    if value is SESSIONS_BARE:
        return 0 if (daily or total) else 20
    return value


SESSION_COLUMNS = [
    ("DATE", "date", "text"), ("MODEL", "model", "text"),
    ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("TOK/S", "tok_s", "rate"),
    ("SESSION", "title", "title"),
]
DAILY_COLUMNS = [
    ("DATE", "date", "text"), ("SESSIONS", "sessions", "int"),
    ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("TOK/S", "tok_s", "rate"),
]
TOTAL_COLUMNS = [
    ("MODEL", "model", "text"), ("SESSIONS", "sessions", "int"),
    ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("TOK/S", "tok_s", "rate"),
]
SHARE_COLUMNS = TOTAL_COLUMNS + [
    # share pair (human decision): the percent column is "SHARE %", the bar
    # column "SHARE" — labeled so the visual reads at a glance.
    ("SHARE %", "share_str", "text"), ("SHARE", "bar", "text"),
]


def cost_column(tier):
    """(header, key, kind) for the opt-in --cost overlay; the header names the
    model the pricing is pinned to, so a stale price table is visible.
    SINGLE line (human decision): "COST · <model> Prices" — compactness wins
    over column width; the whole head stays one line tall."""
    return (f"COST \u00b7 {PRICES[tier]['name']} Prices", "cost_est", "cost")


# --real-cost overlay: the stored real cost (real OpenCode data); every
# session-family row and aggregate already carries `cost`, display-only.
REAL_COST_COLUMN = ("REAL COST", "cost", "cost")
CACHE_COLUMN = ("CACHE", "cache", "int")  # opt-in --cache display overlay


def annotate_cache(rows):
    """Copy of `rows` with the two cache classes combined into int `cache`.
    Display-only overlay: inputs untouched, nothing stored, TOTAL unchanged."""
    return [{**r, "cache": r.get("tokens_cache_read", 0)
             + r.get("tokens_cache_write", 0)} for r in rows]


def with_cache(columns):
    """Copy of `columns` with CACHE_COLUMN inserted before the TOTAL entry
    (OUT CACHE TOTAL reads as the token classes, CACHE outside the total)."""
    out = list(columns)
    i = next(i for i, (header, _, _) in enumerate(out) if header == "TOTAL")
    out.insert(i, CACHE_COLUMN)
    return out


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


def annotate_cost(rows, tier="sonnet-5"):
    """Copy of `rows` with a reference estimate added as float `cost_est`
    (USD), priced at `tier`'s entry in PRICES. Display/JSON overlay only:
    inputs untouched, nothing stored. Row shape picks the math: run rows
    (progress_tokens) price as output-only; session-family rows add the
    cache classes; event rows use input+output."""
    p = PRICES[tier]
    out = []
    for r in rows:
        if "progress_tokens" in r:
            est = r["progress_tokens"] * p["out"] / 1e6
        elif "tokens_cache_read" in r:
            est = (r["tokens_input"] * p["in"] + r["tokens_output"] * p["out"]
                   + r["tokens_cache_read"] * p["cache_read"]
                   + r["tokens_cache_write"] * p["cache_write"]) / 1e6
        else:
            est = (r["tokens_input"] * p["in"]
                   + r["tokens_output"] * p["out"]) / 1e6
        out.append({**r, "cost_est": est})
    return out


BAR_FULL = "█"  # U+2588; fractions use the full-cell shades ░▒▓ (U+2591..2593).
# Eighth-block glyphs (U+2581..2587) match fill height better but sit on a
# different vertical metric than U+2588 in many fonts, which rags the bar's
# bottom edge — full-cell shades avoid that entirely.
BAR_SHADES = ("░", "▒", "▓")  # light / medium / dark shade


def _bar(value, biggest, width):
    """Smooth text bar: full blocks plus at most one shade block for the
    fraction, length proportional to value/biggest; nonzero never renders
    empty."""
    if value <= 0 or biggest <= 0:
        return ""
    exact = width * value / biggest
    full = int(exact)
    eighths = round((exact - full) * 8)
    if eighths == 8:  # fraction rounded up to a whole block
        full, eighths = full + 1, 0
    shade = ""
    if eighths:
        shade = BAR_SHADES[0 if eighths <= 2 else 1 if eighths <= 4 else 2]
    bar = BAR_FULL * full + shade
    return bar or BAR_SHADES[0]  # nonzero never renders empty


def add_shares(rows, bar_width=30):
    """Copy of `rows` with per-row share of the overall (summed) total plus a
    smooth Unicode bar scaled to the largest row. Pure display enrichment;
    inputs untouched."""
    overall = sum(r["total"] for r in rows)
    biggest = max((r["total"] for r in rows), default=0)
    out = []
    for r in rows:
        share = 100.0 * r["total"] / overall if overall else 0.0
        out.append({**r, "share": share, "share_str": f"{share:.1f}%",
                    "bar": _bar(r["total"], biggest, bar_width)})
    return out


# Neon Midnight palette for the display layer: near-black indigo chrome,
# a mirrored input-blue / output-pink pair for the token columns, lime for
# cache reads, violet totals (the gradient midpoint), teal derived stats.
# Every value is a true-color (r, g, b) triple.
NEON = {
    "border": (76, 58, 120), "accent": (232, 121, 249),
    "label": (169, 156, 201), "num": (244, 240, 255),
    "tin": (96, 165, 250), "tout": (244, 114, 182),
    "tcach": (163, 230, 53), "track": (30, 23, 48),
    "ttotal": (167, 139, 250), "tshare": (94, 234, 212),
}

# Nine roles shared by every palette; `track` is legacy and lives in NEON only.
THEME_ROLES = ("border", "accent", "label", "num", "tin", "tout",
               "tcach", "ttotal", "tshare")


def _hex(h):
    """#RRGGBB -> (r, g, b) triple, so the palette hexes read as written."""
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


# dracula/nord/monokai `label` values are lifted from each scheme's comment color so the hero's date span stays readable.
TOKYO_NIGHT = {
    "border": _hex("#414868"), "accent": _hex("#F7768E"),
    "label": _hex("#7982A9"), "num": _hex("#C0CAF5"),
    "tin": _hex("#7AA2F7"), "tout": _hex("#FF9E64"),
    "tcach": _hex("#9ECE6A"), "ttotal": _hex("#BB9AF7"),
    "tshare": _hex("#2ACBCF"),
}
DRACULA = {
    "border": _hex("#6272A4"), "accent": _hex("#FF79C6"),
    "label": _hex("#8A94C0"), "num": _hex("#F8F8F2"),
    "tin": _hex("#8BE9FD"), "tout": _hex("#FFB86C"),
    "tcach": _hex("#50FA7B"), "ttotal": _hex("#BD93F9"),
    "tshare": _hex("#F1FA8C"),
}
NORD = {
    "border": _hex("#4C566A"), "accent": _hex("#BF616A"),
    "label": _hex("#7B88A1"), "num": _hex("#ECEFF4"),
    "tin": _hex("#88C0D0"), "tout": _hex("#D08770"),
    "tcach": _hex("#A3BE8C"), "ttotal": _hex("#B48EAD"),
    "tshare": _hex("#EBCB8B"),
}
GRUVBOX_DARK = {
    "border": _hex("#665C54"), "accent": _hex("#FE8019"),
    "label": _hex("#A89984"), "num": _hex("#EBDBB2"),
    "tin": _hex("#83A598"), "tout": _hex("#FB4934"),
    "tcach": _hex("#B8BB26"), "ttotal": _hex("#D38696"),
    "tshare": _hex("#8EC07C"),
}
SOLARIZED_DARK = {
    "border": _hex("#073642"), "accent": _hex("#D33682"),
    "label": _hex("#839496"), "num": _hex("#EEE8D5"),
    "tin": _hex("#268BD2"), "tout": _hex("#CB4B16"),
    "tcach": _hex("#859900"), "ttotal": _hex("#6C71C4"),
    "tshare": _hex("#2AA198"),
}
MONOKAI = {
    "border": _hex("#75715E"), "accent": _hex("#F92672"),
    "label": _hex("#9B9277"), "num": _hex("#F8F8F2"),
    "tin": _hex("#66D9EF"), "tout": _hex("#FD971F"),
    "tcach": _hex("#A6E22E"), "ttotal": _hex("#AE81FF"),
    "tshare": _hex("#F4BF75"),
}
ONE_DARK = {
    "border": _hex("#4B5263"), "accent": _hex("#C678DD"),
    "label": _hex("#7F8695"), "num": _hex("#ABB2BF"),
    "tin": _hex("#61AFEF"), "tout": _hex("#E06C75"),
    "tcach": _hex("#98C379"), "ttotal": _hex("#E5C07B"),
    "tshare": _hex("#56B6C2"),
}
SOLARIZED_LIGHT = {
    "border": _hex("#93A1A1"), "accent": _hex("#D33682"),
    "label": _hex("#657B83"), "num": _hex("#073642"),
    "tin": _hex("#268BD2"), "tout": _hex("#CB4B16"),
    "tcach": _hex("#859900"), "ttotal": _hex("#6C71C4"),
    "tshare": _hex("#2AA198"),
}
PAPER = {
    "border": _hex("#B0B7C3"), "accent": _hex("#BE185D"),
    "label": _hex("#6B7280"), "num": _hex("#1F2328"),
    "tin": _hex("#1D4ED8"), "tout": _hex("#C2410C"),
    "tcach": _hex("#15803D"), "ttotal": _hex("#6D28D9"),
    "tshare": _hex("#0E7490"),
}
THEMES = {
    "neon-midnight": ("Neon Midnight", {role: NEON[role] for role in THEME_ROLES}),
    "tokyo-night": ("Tokyo Night", TOKYO_NIGHT),
    "dracula": ("Dracula", DRACULA),
    "nord": ("Nord", NORD),
    "gruvbox-dark": ("Gruvbox Dark", GRUVBOX_DARK),
    "solarized-dark": ("Solarized Dark", SOLARIZED_DARK),
    "monokai": ("Monokai", MONOKAI),
    "one-dark": ("One Dark", ONE_DARK),
    "solarized-light": ("Solarized Light", SOLARIZED_LIGHT),
    "paper": ("Paper", PAPER),
}

# Strict hex for user-supplied files: exactly '#rrggbb', digits or a-f, any
# case. (The _hex() helper above is deliberately NOT reused — it is permissive
# and only for the built-in palettes.)
_THEME_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")


class ThemeError(Exception):
    """A theme file or slug could not be used."""


def _theme_from_obj(obj, origin):
    """Validate one parsed theme object: a str `name` and a `colors` dict
    holding every THEME_ROLES key (extra keys ignored), each value a 7-char
    '#rrggbb' string. Returns (display name, colors) with (r, g, b) triples;
    `origin` names the file in the user-facing messages."""
    if not isinstance(obj, dict):
        raise ThemeError(f"{origin}: theme must be a JSON object with "
                         f"'name' and 'colors'")
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ThemeError(f"{origin}: theme 'name' must be a non-empty string")
    raw = obj.get("colors")
    if not isinstance(raw, dict):
        raise ThemeError(f"{origin}: theme 'colors' must be a JSON object")
    missing = [role for role in THEME_ROLES if role not in raw]
    if missing:
        raise ThemeError(f"{origin}: theme colors missing {', '.join(missing)}")
    colors = {}
    for role in THEME_ROLES:
        hexstr = raw[role]
        if not isinstance(hexstr, str) or not _THEME_HEX_RE.match(hexstr):
            raise ThemeError(f"{origin}: role '{role}' is not a #rrggbb "
                             f"color: {hexstr!r}")
        colors[role] = (int(hexstr[1:3], 16), int(hexstr[3:5], 16),
                        int(hexstr[5:7], 16))
    return name, colors


def load_theme_file(path):
    """Read + validate one theme JSON file; returns (display name, colors)."""
    try:
        # encoding pinned so a theme file parses identically in every locale;
        # undecodable bytes (UTF-16-authored, binary misnamed .json) are a
        # ThemeError like any other parse failure, never a traceback.
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ThemeError(f"{path}: {exc}") from exc
    return _theme_from_obj(obj, path)


def scan_themes(dir=THEMES_DIR, warn=None):
    """Built-in THEMES merged with every *.json in `dir` (folder wins on slug
    collision), except the saved-default file `config.json` (see THEME_CONFIG):
    it shares this folder but is not a theme, so a scan never reads it, warns
    about it, or lists "config" as a slug. A file that raises ThemeError is
    skipped, reported through warn(f"warning: ...") when given; a missing dir
    is a no-op."""
    found = dict(THEMES)
    if os.path.isdir(dir):
        for fname in sorted(os.listdir(dir)):
            if not fname.endswith(".json"):
                continue
            # config.json is the saved-default file that lives in this same
            # folder (see THEME_CONFIG), not a theme: skip it so a scan neither
            # warns about it nor lists "config" as an available slug.
            if fname == "config.json":
                continue
            try:
                found[os.path.splitext(fname)[0]] = \
                    load_theme_file(os.path.join(dir, fname))
            except ThemeError as exc:
                if warn is not None:
                    warn(f"warning: {exc}")
    return found


# The saved default theme lives in THEME_CONFIG (see the globals block at the
# top) as {"default": "<slug>"}.
# It is read whenever a run resolves a theme without an explicit --theme=SLUG
# (and by the bare --theme list) and written by --theme-default; a missing or
# unusable file is not an error, it simply means "no default saved".


def read_default_theme(path=THEME_CONFIG, warn=None):
    """The saved default theme slug from `path`, or None. A missing file is a
    silent None; a file that exists but cannot be read, decoded, or parsed, is
    not a JSON object, or has no usable string "default" is reported through
    warn(f"warning: ...") and yields None — an unusable config must not let a
    saved theme silently stop applying with no clue why."""
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if warn is not None:
            warn(f"warning: {path}: {exc}")
        return None
    if not isinstance(obj, dict):
        if warn is not None:
            warn(f"warning: {path}: config must be a JSON object")
        return None
    slug = obj.get("default")
    if not isinstance(slug, str) or not slug:
        if warn is not None:
            warn(f"warning: {path}: no usable 'default' theme slug found")
        return None
    return slug


def write_default_theme(path, slug):
    """Persist {"default": slug} to `path`, creating the parent folder if needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"default": slug}) + "\n")


def resolve_theme(slug, dir=THEMES_DIR, warn=None):
    """The role -> (r, g, b) colors for `slug`: None means the Neon Midnight
    default; a slug is looked up in scan_themes(dir) so folder themes resolve by
    slug too; an unknown slug raises ThemeError naming it and listing the
    available slugs. `warn`, when given, goes to that scan, so a theme file that
    is present but invalid reports its real reason instead of only reading as an
    unknown slug."""
    if slug is None:
        return NEON
    found = scan_themes(dir, warn=warn)
    if slug in found:
        return found[slug][1]
    raise ThemeError(f"unknown theme '{slug}'; available: "
                     f"{', '.join(sorted(found))}")


# Bars are a left-to-right color gradient: input tokens blue fading into
# output-token pink, with the transition centered where the input share of
# the row's total ends. Interpolated in HSL sweeping UP from blue through
# violet (never through gray); hue, lightness AND saturation are lerped
# between the endpoints so both ends reproduce them exactly. Those endpoints
# now come from the resolved theme paint_bars is handed (its `tin`/`tout`
# roles), not from import-time globals; the two constants below are the default
# theme's (NEON) endpoints and no production caller reads them any more — they
# are kept as the public pin on that palette (NeonThemeTests pins them to
# NEON["tin"]/NEON["tout"]).
TOKEN_IN_RGB = NEON["tin"]
TOKEN_OUT_RGB = NEON["tout"]


def gradient_stops(theme):
    """The seven precomputed HLS values a theme's gradient runs on:
    (hue_in, lum_in, sat_in, hue_out, lum_out, sat_out, hue_span)."""
    hue_in, lum_in, sat_in = colorsys.rgb_to_hls(*(v / 255 for v in theme["tin"]))
    hue_out, lum_out, sat_out = colorsys.rgb_to_hls(*(v / 255 for v in theme["tout"]))
    # sweep UP (blue -> violet -> pink), wrapping past 1.0 when needed; for
    # the default pair it is the short arc
    return (hue_in, lum_in, sat_in, hue_out, lum_out, sat_out,
            (hue_out - hue_in) % 1.0)


def _token_rgb(t, stops):
    """Blend from the theme's tin (t=0) to its tout (t=1), lerping hue
    (UP sweep), lightness, and saturation; endpoints round-trip exactly."""
    hue_in, lum_in, sat_in, hue_out, lum_out, sat_out, hue_span = stops
    r, g, b = colorsys.hls_to_rgb((hue_in + t * hue_span) % 1.0,
                                  lum_in + (lum_out - lum_in) * t,
                                  sat_in + (sat_out - sat_in) * t)
    return (round(r * 255), round(g * 255), round(b * 255))


def _paint_gradient(bar, frac_in, stops):
    """Per-glyph true-color sweep; the IN->OUT midpoint lands at frac_in."""
    n = len(bar)
    parts = []
    for i, ch in enumerate(bar):
        if frac_in <= 0:
            t = 1.0
        elif frac_in >= 1:
            t = 0.0
        else:
            p = i / (n - 1) if n > 1 else 0.5
            t = 0.5 * p / frac_in if p <= frac_in \
                else 0.5 + 0.5 * (p - frac_in) / (1 - frac_in)
        r, g, b = _token_rgb(t, stops)
        parts.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
    return "".join(parts) + "\x1b[0m"


def _color_enabled(out, environ):
    """True-color output only on a terminal, and never under NO_COLOR."""
    return bool(out.isatty()) and "NO_COLOR" not in environ


def paint_bars(rows, color, theme=NEON):
    """Copy of `rows` with BAR cells padded by one space either side; when
    color is true, nonzero bars get a per-glyph gradient from `theme`'s tin to
    its tout whose transition marks the row's input/output split. Defaults to
    the Neon Midnight palette, so callers that pass no theme are unchanged.
    Table-only."""
    stops = gradient_stops(theme) if color else None
    out = []
    for r in rows:
        bar = r["bar"]
        if color and r["total"]:
            bar = _paint_gradient(bar, r["tokens_input"] / r["total"], stops)
        out.append({**r, "bar": f" {bar} "})
    return out


def compact(n):
    """Token count in report form: 7.77M, 12.3k, 999."""
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)


# per painted key: (cell kind as in render_table.fmt, color role, bold)
_PAINT_CELLS = {
    "tokens_input": ("int", "tin", False),
    "tokens_output": ("int", "tout", False),
    "cache": ("int", "tcach", False),
    "cost_est": ("cost", "num", False),
    "cost": ("cost", "num", False),         # --real-cost's stored value
    "total": ("int", "ttotal", True),
    "share_str": ("text", "tshare", False),
    "sessions": ("int", "tshare", False),  # row count: the hero's other stat
    "tok_s": ("rate", "accent", False),   # same hue as the hero's TOK/S stat
}


def paint_cells(rows, theme=NEON):
    """Copy of `rows` whose token/cost/rate cells are wrapped in their class
    color. Values arrive raw (ints) or already rendered (strings); numbers are
    formatted exactly as `render_table.fmt` would, so painting never changes
    a cell's visible width. The input rows are left untouched."""
    def render(value, kind):
        if isinstance(value, str):
            return value                     # already a rendered cell
        if kind == "int":
            return f"{value:,}"
        if kind == "rate":
            return f"{value:.1f}"
        return f"${value:.2f}"               # cost_est / cost

    out = []
    for row in rows:
        painted = dict(row)                  # shallow copy: never mutate input
        for key, (kind, role, bold) in _PAINT_CELLS.items():
            value = painted.get(key)
            if value is None or value == "":
                continue
            r, g, b = theme[role]
            painted[key] = (f"\x1b[{'1;' if bold else ''}38;2;{r};{g};{b}m"
                            f"{render(value, kind)}\x1b[0m")
        out.append(painted)
    return out


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # SGR color sequences (painted bars)


def _visible(cell):
    """Display length of a cell: len() ignoring escape-sequence bytes, so
    colored cells neither inflate column widths nor shift padded output."""
    return len(_ANSI_RE.sub("", cell))


def panel(title, lines, theme=NEON, color=True):
    """Rounded frame around `lines`, headed by a ` tokenstats · {title} ` strip;
    an empty title renders the plain ` tokenstats ` strip (no separator).
    `color=False` emits the same frame characters with no escape bytes.
    Every returned line shares one visible width: the widest content line or
    the title strip, whichever is longer, plus the two border cells."""
    def paint(text, role, bold=False):
        if not text:
            return ""
        if not color:
            return text
        r, g, b = theme[role]
        return f"\x1b[{'1;' if bold else ''}38;2;{r};{g};{b}m{text}\x1b[0m"

    if title:
        head = " tokenstats · "
        strip = f"{head}{title} "
    else:
        head = strip = " tokenstats "
    widest = max((_visible(ln) for ln in lines), default=0)
    # one space inset either side, but never narrower than the title strip
    inner = max(len(strip), widest + 2)
    top = ("╭" + paint(head, "accent", bold=True)
           + (paint(title + " ", "label") if title else "")
           + paint("─" * (inner - len(strip)), "border") + paint("╮", "border"))
    body = [paint("│", "border") + " " + ln + " " * (inner - 1 - _visible(ln))
            + paint("│", "border") for ln in lines]
    bottom = "╰" + paint("─" * inner, "border") + paint("╯", "border")
    return [top] + body + [bottom]


def report_header(title, rows, theme=NEON, cost_est=None, real_cost=None,
                  unit="rows", color=True):
    """Hero panel for a report: one grand-total line over a dim date span.
    Safe for rows that carry no tokens_input/tokens_output keys and for rows
    whose date is missing or None (the span is omitted when none is dated).
    `cost_est` (float USD, precomputed by the caller) appends a `~$X.XX`
    reference-estimate tail; omitted means no cost shown. `real_cost` (float
    USD) appends a `· $X.XX` tail after it — no tilde, it is the stored
    real cost. `unit` names what one row means in this view (the dashboard
    passes "sessions").
    Rows carrying a `tok_s` add a token-weighted TOK/S stat; when none of
    them has a rate the segment is omitted entirely.
    `color=False` renders the same text with no escape bytes."""
    def sum_of(key):
        return sum(r.get(key) or 0 for r in rows)

    def paint(text, rgb, bold=False):
        if not color:
            return text
        r, g, b = rgb
        return (f"\x1b[{'1;' if bold else ''}38;2;{r};{g};{b}m"
                f"{text}\x1b[0m")

    # "1 sessions" would read wrong: singularize a count of one.
    n_rows = len(rows)
    unit1 = unit[:-1] if n_rows == 1 and unit.endswith("s") else unit
    # Same token-weighted mean the aggregate tables use, over exactly the rows
    # this hero summarizes, so the hero and the table under it never disagree.
    # A view whose rows carry no rate gets no TOK/S segment at all.
    rate = _weighted_mean_rate([(r.get("tok_s"),
                                 r.get("rate_weight", r.get("total") or 0))
                                for r in rows])
    # IN/OUT tinted like the table columns; the grand total glows at the
    # gradient midpoint (bold); the row-count is a derived stat like SHARE,
    # so it wears the same teal; TOK/S takes the accent hue, the token-column
    # hues being claimed by the tables. Only the NUMBERS are painted — every
    # unit word ("tokens", "in", "out", " tok/s", the row-count unit) stays in
    # the terminal's own color. panel() measures visible width, so the escape
    # bytes never rag the frame.
    hero = (paint(compact(sum_of('total')), theme["ttotal"], bold=True)
            + " tokens · " + paint(str(n_rows), theme["tshare"]) + " " + unit1
            + " · " + paint(compact(sum_of('tokens_input')), theme["tin"]) + " in"
            + " · " + paint(compact(sum_of('tokens_output')), theme["tout"]) + " out"
            + (" · " + paint(f"{rate:.1f}", theme["accent"]) + " tok/s"
               if rate is not None else "")
            + (" · " + paint(f"~${cost_est:,.2f}", theme["num"])
               if cost_est is not None else "")
            + (" · " + paint(f"${real_cost:,.2f}", theme["num"])
               if real_cost is not None else ""))
    lines = [hero]
    dates = sorted(d for d in (r.get("date") for r in rows) if d)
    if dates:
        span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} → {dates[-1]}"
        lines.append(paint(span, theme["label"]))
    return panel(title, lines, theme, color=color)


def _table_panel(title, rows, columns, share_view, color, theme=NEON):
    """Panel of the rendered table; share views get padded (and colored)
    bars first; the header block and its rule dim to their roles in color.
    Every painter below gets `theme`, so one theme owns the whole panel."""
    if share_view:
        rows = paint_bars(rows, color=color, theme=theme)
    table = render_table(paint_cells(rows, theme) if color else rows, columns)
    lines = table.splitlines()
    if color and table != "no sessions match\n":
        paint_header_block(lines, columns, theme=theme)
    return panel(title, lines, theme, color=color)


def header_height(columns):
    """Lines the header block occupies: 1, plus one per "\\n" wrap in a name."""
    return max(name.count("\n") for name, _, _ in columns) + 1


def paint_header_block(lines, columns, theme=NEON):
    """In place: header lines wear the label role (column names recede behind
    the painted numbers below them) and the rule under them the border hue.
    Color mode only; `lines` are render_table output for `columns`."""
    h_h = header_height(columns)
    r, g, b = theme["label"]
    for i in range(h_h):
        lines[i] = f"\x1b[38;2;{r};{g};{b}m{lines[i]}\x1b[0m"
    r, g, b = theme["border"]
    lines[h_h] = f"\x1b[38;2;{r};{g};{b}m{lines[h_h]}\x1b[0m"


def render_table(rows, columns=None):
    """Aligned report. `columns` is a list of (header, key, kind). A header
    may contain "\\n" to wrap; header text bottom-left aligns and a rule
    separates the header block from the data."""
    if columns is None:
        columns = SESSION_COLUMNS
    if not rows:
        return "no sessions match\n"

    def fmt(value, kind):
        if value is None:
            return ""
        if isinstance(value, str) and kind in ("int", "cost", "rate"):
            return value          # already numeric-formatted (paint_cells)
        if kind == "rate":
            return f"{value:.1f}"
        if kind == "cost":
            return f"${value:.2f}"
        if kind == "int":
            return f"{value:,}"
        if kind == "title":
            return str(value)[:40]
        return str(value)

    header = [name.split("\n") for name, _, _ in columns]
    h_h = max(len(h) for h in header)
    body = [[fmt(r.get(key), kind) for _, key, kind in columns] for r in rows]
    widths = []
    for i, (name, _, kind) in enumerate(columns):
        if i == len(columns) - 1:
            widths.append(None)  # last column needs no padding
        else:
            widths.append(max([max(_visible(l) for l in header[i])]
                              + [_visible(row[i]) for row in body]))

    def line_out(cells):
        parts = []
        for i, cell in enumerate(cells):
            if widths[i] is None:
                parts.append(cell)
            else:
                # pad by visible length (ljust would count escape bytes)
                parts.append(cell + " " * (widths[i] - _visible(cell)))
        return "  ".join(parts).rstrip()

    # Header text BOTTOM-aligns (human decision): a wrapped column (the
    # --cost header) floats its first lines above, and every column's last
    # line sits on the rule that separates the head from the data.
    out = []
    for j in range(h_h):
        out.append(line_out([h[j - h_h] if len(h) + j >= h_h else ""
                             for h in header]))
    for row in body:
        out.append(line_out(row))
    out.insert(h_h, "\u2500" * max(_visible(ln) for ln in out))
    return "\n".join(out) + "\n"


def daily_totals(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None, limit=None):
    """Per-day aggregates matching the filters, oldest day first. TOK/S is the
    token-weighted mean of the day's per-session rates. limit=N aggregates
    only the N most recent matching sessions (None/0 = uncapped)."""
    where, params = _session_filters(model, since_ms, until_ms, min_in,
                                     min_out, limit)
    day_sql = "date(time_created/1000, 'unixepoch', 'localtime')"
    return _grouped_aggregates(
        dest, "usage", day_sql,
        " COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost),"
        " SUM(tokens_cache_read), SUM(tokens_cache_write)",
        where, params, _session_rate_groups(dest, day_sql, where, params),
        lambda day, counts, rate, weight: {
            "date": day, "sessions": counts[0], "tokens_input": counts[1] or 0,
            "tokens_output": counts[2] or 0,
            "total": (counts[1] or 0) + (counts[2] or 0),
            "cost": counts[3] or 0.0, "tokens_cache_read": counts[4] or 0,
            "tokens_cache_write": counts[5] or 0,
            "tok_s": rate, "rate_weight": weight})


def total_totals(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None, by_model=False, limit=None):
    """All-time aggregates: one grand-total row; per-provider/model_id rows
    (ordered by that string) with a model filter or by_model=True. Empty
    when no rows match. TOK/S is the token-weighted mean of the group's
    per-session rates. limit=N aggregates only the N most recent matching
    sessions, in both the grand-total and per-model branches
    (None/0 = uncapped)."""
    where, params = _session_filters(model, since_ms, until_ms, min_in,
                                     min_out, limit)
    sums = (" COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost),"
            " SUM(tokens_cache_read), SUM(tokens_cache_write)")
    builder = lambda m, counts, rate, weight: _total_row(
        m, *counts, rate, weight)
    if not model and not by_model:
        return _total_aggregates(
            dest, "usage", sums, where, params,
            _session_rate_groups(dest, "'(all)'", where, params), builder)
    return _total_aggregates(
        dest, "usage", sums, where, params,
        _session_rate_groups(dest, "provider || '/' || model_id", where,
                             params), builder,
        group_key_sql="provider || '/' || model_id")


def _total_row(model, n, tin, tout, cost, tcr, tcw, rate, weight):
    """Row dict for total_totals: totals plus cache components, the
    token-weighted mean of the group's per-session rates (None if none of them
    carries weight), and the Σ weight that produced it."""
    tin, tout = tin or 0, tout or 0
    return {"model": model, "sessions": n, "tokens_input": tin,
            "tokens_output": tout, "total": tin + tout, "cost": cost or 0.0,
            "tokens_cache_read": tcr or 0, "tokens_cache_write": tcw or 0,
            "tok_s": rate, "rate_weight": weight}


def _connect_src(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _view_rows(dest, args, model, model_any, want_total, limit):
    """(rows, columns) for the view the flags ask for.

    The total family ends with SHARE and the share-descending sort (ties keep
    SQL's alphabetical-by-model order, because the sort is stable); the session
    list renders newest first when a --sessions cap is in force. main has
    already rejected --total x --daily by the time this runs."""
    if want_total:
        rows = total_totals(dest, model=model, since_ms=args.since,
                            until_ms=args.until, min_in=args.min_in,
                            min_out=args.min_out, by_model=model_any,
                            limit=limit)
        rows = add_shares(rows)
        rows.sort(key=lambda r: -r["share"])  # biggest share first
        return rows, SHARE_COLUMNS
    rows = (daily_totals(dest, model=model, since_ms=args.since,
                         until_ms=args.until, min_in=args.min_in,
                         min_out=args.min_out, limit=limit)
            if args.daily else
            query_sessions(dest, model=model, since_ms=args.since,
                           until_ms=args.until, min_in=args.min_in,
                           min_out=args.min_out, limit=limit))
    if not args.daily and args.sessions is not None:
        # The list view renders newest first; --json serialises the same rows,
        # so the reversal happens on the row list itself.
        rows = most_recent(rows, limit)
    return rows, (DAILY_COLUMNS if args.daily else SESSION_COLUMNS)


def _view_title(args, share_view, limit):
    """(panel title, hero row-unit) for the view those flags picked.

    A positive --sessions cap on an aggregate says so in the title
    (` · last N sessions`); the list view names its scope instead
    (`sessions · N most recent`, `sessions · all`), and a 0 cap leaves every
    title byte-identical to the uncapped view."""
    if share_view:
        # Rows here are per-model totals, or the single grand total.
        title = ("usage by model · sorted by share"
                 if args.model is not None else "totals")
        hero_unit = "models" if args.model is not None else "totals"
    elif args.daily:
        title, hero_unit = "daily", "days"
    else:
        title, hero_unit = "sessions", "sessions"
        if args.sessions is not None:
            title = (f"sessions · {limit} most recent" if limit
                     else "sessions · all")
    # Aggregate views fold sessions up, so a positive cap must say so in the
    # title: ` · last N sessions` on --daily and the totals/share views
    # (bare --model counts — it is the --total shortcut). The list view's title
    # already names its scope, and a limit of 0 (all) leaves every title
    # byte-identical to the uncapped view.
    if limit and (share_view or args.daily):
        title = f"{title} · last {limit} sessions"
    return title, hero_unit


def render_report(title, rows, columns, share_view, color, theme, head=()):
    """The exact text main() prints for a table view.

    A terminal with rows gets `head` — the hero lines, empty for the views
    that get no hero — above the framed table, and the frame comes from
    _table_panel, so one path paints the table, the bars and the header block.
    Everything else (piped, NO_COLOR, or the empty view) is the bare
    render_table text: byte-plain, unframed, and printed with no extra newline
    — the golden piped cases compare these bytes exactly. A share view gets its
    bars padded there too: paint_bars' one space either side is layout, not
    color, and the plain table has always had it."""
    if color and rows:
        return "\n".join(list(head) + _table_panel(title, rows, columns,
                                                   share_view, color,
                                                   theme)) + "\n"
    if share_view:
        rows = paint_bars(rows, color=False, theme=theme)
    return render_table(rows, columns)


def _ensure_utf8_streams(out=None, err=None):
    """Best-effort UTF-8 so box-drawing/Unicode never crashes a Windows cp1252
    console. No-op on streams without reconfigure (StringIO in tests, already
    UTF-8 POSIX consoles)."""
    for stream in (sys.stdout if out is None else out,
                   sys.stderr if err is None else err):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def main(argv):
    _ensure_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="tokenstats",
        description="Record and report AI token usage from OpenCode into a "
                    "permanent local store.")
    parser.add_argument("command", nargs="?",
                        choices=["record"],
                        help="record = snapshot OpenCode now"
                             " (default: report)")
    parser.add_argument("--model", nargs="?", const=MODEL_ANY,
                        help="substring match on provider/model — literal,"
                             " %% and _ are not wildcards (omit the value:"
                             " totals for every model)")
    parser.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD",
                        help="first day of usage (inclusive)")
    parser.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                        help="last day of usage (inclusive)")
    parser.add_argument("--min-in", type=int, dest="min_in",
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-out", type=int, dest="min_out",
                        help=argparse.SUPPRESS)
    parser.add_argument("--daily", action="store_true",
                        help="totals per day instead of per session")
    parser.add_argument("--total", action="store_true",
                        help="grand total (with --model: per-model totals) "
                             "instead of per-session rows")
    parser.add_argument("--sessions", nargs="?", const=SESSIONS_BARE,
                        type=int, default=None, metavar="N",
                        help="scope to the N most recent sessions "
                             "(default 20 when listing, all when aggregating)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of a table (rows carry"
                             " tok_s; --cost adds cost_est)")
    # No default to print: the store is created on first run, so --help would
    # otherwise advertise a path that does not exist yet. default=None means
    # "ask default_store()", below. The rest of the plumbing below is hidden
    # for the same reason: fully usable, never advertised.
    parser.add_argument("--db", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--src", default=DEFAULT_SRC,
                        help=argparse.SUPPRESS)
    parser.add_argument("--cost", nargs="?", const="sonnet-5", default=None,
                        metavar="TIER", dest="cost",
                        choices=sorted(PRICES) + sorted(FAMILY_ALIASES),
                        help="add a reference cost estimate column priced at"
                             " TIER (model slug or family name; default:"
                             " sonnet-5); display-only overlay. Prefer"
                             " --cost=TIER: the space form"
                             " can swallow a following command")
    parser.add_argument("--real-cost", action="store_true", dest="real_cost",
                        help="add a REAL COST column with the stored real USD"
                             " OpenCode reported")
    # --cost-tier was removed with the --cost rework; kept as a suppressed
    # shim so stale shell-history invocations get a migration message
    # instead of argparse blaming the word it swallowed as the command.
    parser.add_argument("--cost-tier", nargs="?", const="<removed>",
                        default=None, dest="_cost_tier", metavar="TIER",
                        help=argparse.SUPPRESS)
    parser.add_argument("--cache", action="store_true", dest="cache",
                        help="add a CACHE column (cache read + write) between"
                             " OUT and TOTAL in session views; informational,"
                             " TOTAL unchanged")
    parser.add_argument("--theme", nargs="?", const=THEME_LIST, default=None,
                        metavar="SLUG",
                        help="bare --theme lists all themes and exits; with"
                             " SLUG, paints this run with that theme"
                             " (display-only overlay like --cost, so the"
                             " dashboard still renders). Prefer --theme=SLUG:"
                             " the space form can swallow a following command")
    parser.add_argument("--theme-default", nargs="?", const=THEME_SAVE,
                        default=None, dest="theme_default", metavar="SLUG",
                        help="save SLUG as the default theme in"
                             " ~/.tokenstats/themes/config.json (bare"
                             " --theme-default saves this run's theme). Prefer"
                             " --theme-default=SLUG: the space form can"
                             " swallow a following command")
    args = parser.parse_args(argv)
    # `--theme=` and `--theme-default=` (an explicit empty value) mean "no
    # value", not a slug named '': normalize each to what its bare form means,
    # so an empty --theme leaves the saved default in charge and an empty
    # --theme-default hits the same "name a --theme=SLUG" error as the bare
    # flag instead of failing on the empty slug.
    if args.theme == "":
        args.theme = None
    if args.theme_default == "":
        args.theme_default = THEME_SAVE
    if args._cost_tier is not None:
        parser.error("--cost-tier was removed; use --cost=<slug>"
                     " (e.g. --cost=opus-5); see --help")
    cost_tier = resolve_cost_tier(args.cost) if args.cost else None

    # Theme: resolved here, before the store opens, and passed explicitly to
    # every paint call below. Both actions short-circuit here too — listing or
    # saving touches themes/ only, never SQLite. THEMES_DIR and THEME_CONFIG
    # are read now (not as resolve_theme's def-time default arguments) so a
    # caller that repoints the module globals steers this run.
    def theme_warn(msg):
        print(msg, file=sys.stderr)

    if args.theme_default is not None:
        # bare --theme-default saves the --theme=SLUG of the same invocation.
        slug = (args.theme_default if isinstance(args.theme_default, str)
                else args.theme if isinstance(args.theme, str) else None)
        if slug is None:
            parser.error("--theme-default with no value saves the theme given"
                         " by --theme=SLUG in the same invocation; none was"
                         " given — add --theme=SLUG, or save directly as"
                         " --theme-default=SLUG")
        # write_default_theme does not validate: resolve first, so a typo in
        # the slug never lands in ~/.tokenstats/themes/config.json.
        try:
            resolve_theme(slug, THEMES_DIR)
        except ThemeError as exc:
            parser.error(str(exc))
        name = scan_themes(THEMES_DIR)[slug][0]   # safe: it just resolved
        try:
            write_default_theme(THEME_CONFIG, slug)
        except OSError as exc:
            # a read-only (or otherwise unwritable) themes/ is a CLI failure
            # like every other one here: tokenstats: error: ..., never a
            # traceback.
            parser.error(f"could not write {THEME_CONFIG}: {exc}")
        print(f"default theme: {slug} ({name})")
        return 0
    if args.theme is THEME_LIST:
        # The list is where a user looks for the theme they just wrote, so a
        # file that failed validation says why here: scan_themes warns.
        found = scan_themes(THEMES_DIR, warn=theme_warn)
        default = read_default_theme(THEME_CONFIG, warn=theme_warn) \
            or "neon-midnight"
        for slug in sorted(found):
            print(f"{slug} \u2014 {found[slug][0]}"
                  + (" (default)" if slug == default else ""))
        if default not in found:
            # A saved default that is not in this list marks nothing on stdout,
            # which reads as a silent bug from the one place the user came to
            # diagnose it: say where the dead slug came from. stdout stays the
            # plain list.
            theme_warn(f"warning: {THEME_CONFIG}: saved default theme"
                       f" '{default}' is not available; runs render in Neon"
                       f" Midnight until --theme-default=SLUG saves another")
        return 0
    # Precedence: --theme=SLUG > ~/.tokenstats/themes/config.json > Neon
    # Midnight. The two sources fail differently on purpose: a slug typed for
    # this run is the user's own request, so an unknown one is a hard exit 2; a
    # *saved* default that cannot be resolved is a config that cannot be used —
    # warn naming the file and render with Neon Midnight, because a cosmetic
    # default must not cost the user a report whose data is fine. Only this
    # saved-default path passes warn= down: a file that is present but invalid
    # then reports its real reason from the scan, above the config line,
    # instead of reading as a plain "unknown theme" for a file the user can see
    # on disk.
    if args.theme:
        try:
            theme = resolve_theme(args.theme, THEMES_DIR)
        except ThemeError as exc:
            parser.error(str(exc))
    else:
        try:
            theme = resolve_theme(read_default_theme(THEME_CONFIG,
                                                     warn=theme_warn),
                                  THEMES_DIR, warn=theme_warn)
        except ThemeError as exc:
            theme_warn(f"warning: {THEME_CONFIG}: {exc}; using Neon Midnight")
            theme = NEON

    dest = None
    try:
        try:
            # Only a run that was not told where the store lives has a state
            # dir to create (and possibly a store to migrate): this is the
            # module's only call to default_store(), so every run that passes
            # --db — and every test — never touches $HOME.
            if args.db is None:
                args.db = default_store()
            dest = sqlite3.connect(args.db)
            # WAL + busy_timeout so a scheduled `record` and an interactive
            # query can overlap without either failing.
            dest.execute("PRAGMA journal_mode=WAL")
            dest.execute("PRAGMA busy_timeout=5000")
            ensure_schema(dest)
        except sqlite3.Error as exc:
            print(f"warning: could not open our store {args.db}: {exc}",
                  file=sys.stderr)
            return 1
        now_ms = int(time.time() * 1000)

        def _ingest(path, fn, verbose):
            """Open `path` read-only, run one ingestion step on it, and always
            close the source; warn-and-continue on sqlite errors."""
            try:
                src = _connect_src(path)
            except sqlite3.Error as exc:
                print(f"warning: could not read {path}: {exc}", file=sys.stderr)
                return False
            try:
                counts = fn(src)
                if verbose:
                    line = (f"inserted={counts['inserted']}"
                            f" updated={counts['updated']}"
                            f" unchanged={counts['unchanged']}")
                    if counts.get("skipped"):
                        line += f" skipped={counts['skipped']}"
                    print(line)
                return True
            except sqlite3.Error as exc:
                print(f"warning: could not read {path}: {exc}", file=sys.stderr)
                return False
            finally:
                src.close()

        if args.command == "record":
            ok = _ingest(args.src, lambda src: record(src, dest, now_ms),
                         verbose=True)
            return 0 if ok else 1

        # Report mode: snapshot OpenCode first, degrade gracefully if the
        # source is missing or locked.
        _ingest(args.src, lambda src: record(src, dest, now_ms), verbose=False)

        # Compact default dashboard (design 2026-11-15): a truly bare run —
        # no command, no view/filter flags (--cost/--cache are overlays, not
        # views) — renders hero + top-5 models + 5 most recent sessions.
        # --json keeps the full flat array (display-only limits).
        compact_dash = (args.command is None and args.model is None
                        and not args.daily and not args.total
                        and args.sessions is None
                        and args.since is None and args.until is None
                        and args.min_in is None and args.min_out is None)
        if compact_dash and not args.as_json:
            all_rows = query_sessions(dest)
            if not all_rows:
                print("no sessions match")
                return 0
            model_rows = add_shares(total_totals(dest, by_model=True))
            model_rows.sort(key=lambda r: -r["share"])  # stable: ties keep
            # SQL's alphabetical-by-model order (mirrors the share view).
            sess_rows = most_recent(all_rows, 5)
            model_rows = model_rows[:5]
            color = _color_enabled(sys.stdout, os.environ)
            m_cols, s_cols = SHARE_COLUMNS, SESSION_COLUMNS
            hero_cost = None
            if args.cost:
                model_rows = annotate_cost(model_rows, cost_tier)
                sess_rows = annotate_cost(sess_rows, cost_tier)
                cc = cost_column(cost_tier)
                m_cols, s_cols = m_cols + [cc], s_cols + [cc]
                # Hero total spans ALL sessions — the model table truncates
                # to five rows, so its COST column can't sum to the truth.
                hero_cost = sum(r["cost_est"]
                                for r in annotate_cost(all_rows, cost_tier))
            hero_real = None
            if args.real_cost:
                hero_real = sum(r["cost"] for r in all_rows)
                m_cols = m_cols + [REAL_COST_COLUMN]
                s_cols = s_cols + [REAL_COST_COLUMN]
            if args.cache:
                model_rows = annotate_cache(model_rows)
                sess_rows = annotate_cache(sess_rows)
                m_cols, s_cols = with_cache(m_cols), with_cache(s_cols)
            lines = report_header("", all_rows, theme, cost_est=hero_cost,
                                  real_cost=hero_real, unit="sessions",
                                  color=color)
            lines += _table_panel("usage by model \u00b7 top 5 by share",
                                  model_rows, m_cols, True, color, theme)
            lines += _table_panel("sessions \u00b7 5 most recent",
                                  sess_rows, s_cols, False, color, theme)
            print("\n".join(lines))
            return 0

        model_any = args.model is MODEL_ANY
        # Bare --model is a shortcut for `--total --model`: totals per model.
        # (With --daily it just means "all models", as before.)
        want_total = args.total or (model_any and not args.daily)
        model = None if model_any else args.model

        # The SQL layer can't guard this: a negative LIMIT silently
        # degenerates to no cap, so the CLI is the only rejection point.
        limit = _sessions_limit(args.sessions, args.daily, want_total)
        if limit is not None and limit < 0:
            parser.error("--sessions must be a non-negative number of"
                         " sessions (0 = all)")
        if want_total and args.daily:
            parser.error("--total cannot be combined with --daily")
        rows, columns = _view_rows(dest, args, model, model_any, want_total,
                                   limit)
        # Opt-in --cost overlay: reference pricing on row copies, never stored
        # data. The share-view flag is captured BEFORE appending cost_column —
        # the append creates a new list, and render_report must still recognize
        # the share view.
        share_view = columns is SHARE_COLUMNS
        # Panel title, derived from the same flags that picked `columns`
        # (a daily share view cannot occur: --total rejects --daily).
        title, hero_unit = _view_title(args, share_view, limit)
        if args.cost:
            rows = annotate_cost(rows, cost_tier)
            columns = columns + [cost_column(cost_tier)]
        if args.real_cost:
            columns = columns + [REAL_COST_COLUMN]
        # Opt-in --cache overlay: combined cache classes as one informational
        # column between OUT and TOTAL; TOTAL stays IN + OUT.
        if args.cache:
            rows = annotate_cache(rows)
            columns = with_cache(columns)
        if args.as_json:
            # share travels in JSON; the painted bar is table-only decoration;
            # `cache` is a render-time join of fields JSON already carries
            print(json.dumps([{k: v for k, v in r.items()
                               if k not in ("bar", "share_str", "cache")}
                              for r in rows], indent=2))
        else:
            color = _color_enabled(sys.stdout, os.environ)
            # A total view narrowed to a single aggregate row already shows the
            # grand total in-table — the hero would be a pure echo.
            head = ([] if (want_total and len(rows) == 1)
                    else report_header(title, rows, theme, unit=hero_unit))
            print(render_report(title, rows, columns, share_view, color, theme,
                                head=head), end="")
        return 0
    finally:
        if dest is not None:
            dest.close()


def cli_main():
    """Console-script entry point: argv from sys.argv, exit code from main()."""
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
