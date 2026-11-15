#!/usr/bin/env python3
"""tokenstat — permanent local record of AI token usage on this machine.

Snapshots three tables from two sources (read-only) into our own SQLite
store — OpenCode's per-session totals, plus Unsloth Studio's per-request
events and generation runs — and reports them filtered by model, date, and
token thresholds. Reporting views carry a TOK/S column (reconcile
excepted): sessions and daily/total aggregates show a wall-clock average
over their time span (idle drags it down); --from-runs shows each run's
true generation rate. Opt-in --cost overlays a reference Claude-tier dollar
estimate. See README.md.
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

DEFAULT_SRC = os.path.expanduser("~/.local/share/opencode/opencode.db")
DEFAULT_UNSLOTH_DB = os.path.expanduser("~/.unsloth/studio/studio.db")
MODEL_ANY = object()  # --model with no value (valid only with --total)
DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage.db")

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
        """CREATE TABLE IF NOT EXISTS unsloth_events (
               event_id TEXT PRIMARY KEY,
               model TEXT NOT NULL DEFAULT '',
               prompt_tokens INTEGER NOT NULL DEFAULT 0,
               completion_tokens INTEGER NOT NULL DEFAULT 0,
               total_tokens INTEGER NOT NULL DEFAULT 0,
               created_at INTEGER NOT NULL)"""
    )
    dest.execute(
        """CREATE TABLE IF NOT EXISTS unsloth_runs (
               run_id TEXT PRIMARY KEY,
               model TEXT NOT NULL DEFAULT '',
               progress_tokens INTEGER NOT NULL DEFAULT 0,
               started_at INTEGER NOT NULL,
               completed_at INTEGER,
               status TEXT NOT NULL DEFAULT '')"""
    )
    # Hot filter columns: date-windowed queries scan these first.
    dest.execute(
        """CREATE INDEX IF NOT EXISTS idx_unsloth_events_created
               ON unsloth_events(created_at)"""
    )
    dest.execute(
        """CREATE INDEX IF NOT EXISTS idx_usage_time_created
               ON usage(time_created)"""
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


def record(src: sqlite3.Connection, dest: sqlite3.Connection, now_ms: int) -> dict:
    """Upsert every session_v2 row into our usage table.

    One row per OpenCode session id; token/cost columns store the latest
    observed total (max of stored and source), so re-recording can never
    double count and a pruned/rewritten source can never shrink history.
    """
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    rows = src.execute(
        "SELECT id, model, tokens_input, tokens_output, tokens_reasoning,"
        " tokens_cache_read, tokens_cache_write, cost, title, directory,"
        " agent, time_created, time_updated FROM session_v2"
    ).fetchall()
    for (sid, model_raw, tin, tout, treas, tcr, tcw, cost, title, directory,
         agent, created, updated) in rows:
        source_vals = [v or 0 for v in (tin, tout, treas, tcr, tcw, cost)]
        provider, model_id = parse_model(model_raw)
        try:
            existing = dest.execute(
                "SELECT tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write, cost, provider,"
                " model_id, title, directory, agent"
                " FROM usage WHERE session_id = ?", (sid,)).fetchone()
            if existing is None:
                dest.execute(
                    "INSERT INTO usage (session_id, provider, model_id,"
                    " tokens_input, tokens_output, tokens_reasoning,"
                    " tokens_cache_read, tokens_cache_write, cost, title,"
                    " directory, agent, time_created, time_updated,"
                    " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [sid, provider, model_id, *source_vals, title, directory,
                     agent, created, updated, now_ms, now_ms])
                counts["inserted"] += 1
            else:
                merged = [max(e, s) for e, s in zip(existing[:6], source_vals)]
                changed = merged != list(existing[:6])
                # Labels make the numbers interpretable: like the token
                # columns, they never shrink — a null/empty value from the
                # source keeps the best label we already have.
                kept = [new or old for new, old in
                        zip((provider, model_id, title, directory, agent),
                            existing[6:])]
                dest.execute(
                    "UPDATE usage SET provider = ?, model_id = ?,"
                    " tokens_input = ?, tokens_output = ?, tokens_reasoning = ?,"
                    " tokens_cache_read = ?, tokens_cache_write = ?, cost = ?,"
                    " title = ?, directory = ?, agent = ?, time_updated = ?,"
                    " last_seen = ? WHERE session_id = ?",
                    [kept[0], kept[1], *merged, kept[2], kept[3], kept[4],
                     updated, now_ms, sid])
                counts["updated" if changed else "unchanged"] += 1
        except sqlite3.Error:
            # One malformed source row must not abort (or roll back) the rest.
            counts["skipped"] = counts.get("skipped", 0) + 1
    dest.commit()
    return counts


def import_unsloth(src: sqlite3.Connection, dest: sqlite3.Connection) -> dict:
    """Upsert every api_usage_events row into our unsloth_events table.

    One row per Unsloth event id; values are monotonic max, so re-importing
    can never double count (events themselves are immutable)."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    rows = src.execute(
        "SELECT id, model, prompt_tokens, completion_tokens, total_tokens,"
        " created_at FROM api_usage_events").fetchall()
    for eid, model, p, c, t, ts in rows:
        vals = [v or 0 for v in (p, c, t)]
        try:
            existing = dest.execute(
                "SELECT prompt_tokens, completion_tokens, total_tokens"
                " FROM unsloth_events WHERE event_id = ?", (eid,)).fetchone()
            if existing is None:
                dest.execute(
                    "INSERT INTO unsloth_events (event_id, model,"
                    " prompt_tokens, completion_tokens, total_tokens,"
                    " created_at) VALUES (?,?,?,?,?,?)",
                    [eid, model or "", *vals, ts])
                counts["inserted"] += 1
            else:
                merged = [max(e, v) for e, v in zip(existing, vals)]
                changed = merged != list(existing)
                dest.execute(
                    "UPDATE unsloth_events SET model = ?, prompt_tokens = ?,"
                    " completion_tokens = ?, total_tokens = ?"
                    " WHERE event_id = ?", [model or "", *merged, eid])
                counts["updated" if changed else "unchanged"] += 1
        except sqlite3.Error:
            # One malformed source row must not abort (or roll back) the rest.
            counts["skipped"] = counts.get("skipped", 0) + 1
    dest.commit()
    return counts


def import_unsloth_runs(src: sqlite3.Connection,
                        dest: sqlite3.Connection) -> dict:
    """Upsert every chat_generation_runs row into our unsloth_runs table.

    One row per Studio run id; progress_tokens is a monotonic max, so
    re-importing can never double count. The model lives inside request_json
    (no dedicated column); extract it in Python rather than SQLite JSON1 so
    the query works on any build. A studio.db without the table (older
    installs) reports zero runs instead of failing."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    has_table = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='chat_generation_runs'").fetchone()
    if not has_table:
        return counts
    rows = src.execute(
        "SELECT id, request_json, progress_tokens, started_at, completed_at,"
        " status FROM chat_generation_runs").fetchall()
    for rid, request_json, progress, started, completed, status in rows:
        try:
            try:
                data = json.loads(request_json)
            except (TypeError, ValueError):
                data = None
            # Non-dict JSON is an extraction failure, not a row error
            # (mirrors parse_model): blank model, but the row still inserts.
            model = data.get("model", "") if isinstance(data, dict) else ""
            vals = [progress or 0]
            existing = dest.execute(
                "SELECT progress_tokens FROM unsloth_runs"
                " WHERE run_id = ?", (rid,)).fetchone()
            if existing is None:
                dest.execute(
                    "INSERT INTO unsloth_runs (run_id, model,"
                    " progress_tokens, started_at, completed_at, status)"
                    " VALUES (?,?,?,?,?,?)",
                    [rid, str(model), *vals, started, completed, status or ""])
                counts["inserted"] += 1
            else:
                merged = [max(e, v) for e, v in zip(existing, vals)]
                changed = merged != list(existing)
                dest.execute(
                    "UPDATE unsloth_runs SET model = ?, progress_tokens = ?,"
                    " started_at = ?, completed_at = ?, status = ?"
                    " WHERE run_id = ?",
                    [str(model), *merged, started, completed, status or "", rid])
                counts["updated" if changed else "unchanged"] += 1
        except sqlite3.Error:
            # One malformed source row must not abort (or roll back) the rest.
            counts["skipped"] = counts.get("skipped", 0) + 1
    dest.commit()
    return counts


def _event_filters(model, since_ms, until_ms, min_in, min_out):
    where, params = [], []
    if model:
        pattern = "%" + model.lower().replace("%", "\\%").replace("_", "\\_") + "%"
        # Accept either the event's own model string or the provider-prefixed
        # form shown in session reports (e.g. "unsloth-studio/unsloth/Qwen...").
        where.append("(lower(model) LIKE ? ESCAPE '\\' OR "
                     "lower(? || '/' || model) LIKE ? ESCAPE '\\')")
        params.extend([pattern, UNSLOTH_PROVIDER, pattern])
    if since_ms is not None:
        where.append("created_at >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("created_at < ?")
        params.append(_day_end_ms(until_ms))  # until-day inclusive
    if min_in is not None:
        where.append("prompt_tokens >= ?")
        params.append(min_in)
    if min_out is not None:
        where.append("completion_tokens >= ?")
        params.append(min_out)
    return where, params


def query_events(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None):
    """Imported Unsloth request events matching all filters, oldest first.

    Dict shape matches query_sessions: tokens_input = prompt_tokens (which
    include cache hits), tokens_output = completion_tokens. tok_s is always
    None: a single event carries no duration."""
    where, params = _event_filters(model, since_ms, until_ms, min_in, min_out)
    sql = ("SELECT created_at, model, prompt_tokens, completion_tokens"
           " FROM unsloth_events")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC"
    rows = []
    for ts, model_id, p, c in dest.execute(sql, params):
        date = datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        rows.append({"date": date, "model": model_id,
                     "tokens_input": p, "tokens_output": c, "total": p + c,
                     "cost": 0.0, "title": "", "tok_s": None})
    return rows


def daily_events(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None):
    """Per-day event aggregates (sessions = request count), oldest first.
    tok_s spans the bucket's created_at range."""
    where, params = _event_filters(model, since_ms, until_ms, min_in, min_out)
    sql = ("SELECT date(created_at/1000, 'unixepoch', 'localtime') AS day,"
           " COUNT(*), SUM(prompt_tokens), SUM(completion_tokens),"
           " MIN(created_at), MAX(created_at) FROM unsloth_events")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day ASC"
    return [
        {"date": day, "sessions": n, "tokens_input": p or 0,
         "tokens_output": c or 0, "total": (p or 0) + (c or 0), "cost": 0.0,
         "tok_s": _aggregate_rate((p or 0) + (c or 0), first, last)}
        for day, n, p, c, first, last in dest.execute(sql, params)
    ]


def total_events(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None, by_model=False):
    """All-time event aggregates: one grand-total row; per-event-model rows
    (ordered by it) with a model filter or by_model=True. Empty when no
    events match. Mirrors total_totals; cost is always 0.0 (daily_events);
    tok_s spans each row's created_at range."""
    where, params = _event_filters(model, since_ms, until_ms, min_in, min_out)
    tail = " WHERE " + " AND ".join(where) if where else ""
    sums = (" COUNT(*), SUM(prompt_tokens), SUM(completion_tokens),"
            " MIN(created_at), MAX(created_at)")
    if not model and not by_model:
        n, p, c, first, last = dest.execute(
            "SELECT" + sums + " FROM unsloth_events" + tail, params).fetchone()
        if not n:
            return []
        p, c = p or 0, c or 0
        return [{"model": "(all)", "sessions": n, "tokens_input": p,
                 "tokens_output": c, "total": p + c, "cost": 0.0,
                 "tok_s": _aggregate_rate(p + c, first, last)}]
    return [
        {"model": m, "sessions": n, "tokens_input": p or 0,
         "tokens_output": c or 0, "total": (p or 0) + (c or 0), "cost": 0.0,
         "tok_s": _aggregate_rate((p or 0) + (c or 0), first, last)}
        for m, n, p, c, first, last in dest.execute(
            "SELECT model," + sums + " FROM unsloth_events" + tail
            + " GROUP BY model ORDER BY model ASC", params)
    ]


def _run_filters(model, since_ms, until_ms):
    """WHERE clauses for unsloth_runs queries: `model` is a substring match
    on the raw stored model string (LIKE metacharacters escaped), the date
    window applies to started_at with the until-day inclusive."""
    where, params = [], []
    if model:
        pattern = "%" + model.lower().replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("lower(model) LIKE ? ESCAPE '\\'")
        params.append(pattern)
    if since_ms is not None:
        where.append("started_at >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("started_at < ?")
        params.append(_day_end_ms(until_ms))  # until-day inclusive
    return where, params


def _aggregate_rate(total, first_started, last_end):
    """Runs-aggregate tokens/second over a bucket's whole span; None when
    the span is under a second (rate would be meaningless)."""
    span = (last_end - first_started) / 1000
    return round(total / span, 1) if span >= 1 else None


def query_runs(dest, model=None, since_ms=None, until_ms=None):
    """Imported Unsloth generation runs matching all filters, oldest first.

    tokens_input/total carry progress_tokens (runs have no in/out split);
    tok_s is the run's true generation rate — progress over its wall-clock
    seconds — and None for unfinished or sub-second runs."""
    where, params = _run_filters(model, since_ms, until_ms)
    sql = ("SELECT started_at, model, progress_tokens, completed_at, status"
           " FROM unsloth_runs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at ASC"
    rows = []
    for (started, model_id, progress, completed,
         status) in dest.execute(sql, params):
        date = datetime.datetime.fromtimestamp(started / 1000).strftime(
            "%Y-%m-%d")
        tok_s = None
        # "hasn't completed" means EITHER condition: a failed run that
        # recorded completed_at still renders blank.
        if completed is not None and status == "completed":
            tok_s = _aggregate_rate(progress, started, completed)
        rows.append({"date": date, "model": model_id,
                     "progress_tokens": progress,
                     "tokens_input": progress, "tokens_output": 0,
                     "total": progress, "tok_s": tok_s,
                     "cost": 0.0, "title": ""})
    return rows


def daily_runs(dest, model=None, since_ms=None, until_ms=None):
    """Per-day run aggregates (sessions = run count), keyed on the run's
    started day, oldest first. tok_s spans the bucket from its earliest
    start to its latest end."""
    where, params = _run_filters(model, since_ms, until_ms)
    sql = ("SELECT date(started_at/1000, 'unixepoch', 'localtime') AS day,"
           " COUNT(*), SUM(progress_tokens),"
           " MIN(started_at), MAX(COALESCE(completed_at, started_at))"
           " FROM unsloth_runs")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day ASC"
    return [
        {"date": day, "sessions": n, "tokens_input": p or 0,
         "progress_tokens": p or 0,
         "tokens_output": 0, "total": p or 0,
         "tok_s": _aggregate_rate(p or 0, first, last)}
        for day, n, p, first, last in dest.execute(sql, params)
    ]


def total_runs(dest, model=None, since_ms=None, until_ms=None, by_model=False):
    """All-time run aggregates: one grand-total row; per-run-model rows
    (ordered by the raw stored model string) with a model filter or
    by_model=True. Empty when no runs match. Mirrors total_events."""
    where, params = _run_filters(model, since_ms, until_ms)
    tail = " WHERE " + " AND ".join(where) if where else ""
    sums = (" COUNT(*), SUM(progress_tokens),"
            " MIN(started_at), MAX(COALESCE(completed_at, started_at))")
    if not model and not by_model:
        n, p, first, last = dest.execute(
            "SELECT" + sums + " FROM unsloth_runs" + tail, params).fetchone()
        if not n:
            return []
        p = p or 0
        return [{"model": "(all)", "sessions": n, "tokens_input": p,
                 "progress_tokens": p,
                 "tokens_output": 0, "total": p,
                 "tok_s": _aggregate_rate(p, first, last)}]
    return [
        {"model": m, "sessions": n, "tokens_input": p or 0,
         "progress_tokens": p or 0,
         "tokens_output": 0, "total": p or 0,
         "tok_s": _aggregate_rate(p or 0, first, last)}
        for m, n, p, first, last in dest.execute(
            "SELECT model," + sums + " FROM unsloth_runs" + tail
            + " GROUP BY model ORDER BY model ASC", params)
    ]


UNSLOTH_PROVIDER = "unsloth-studio"  # providerID OpenCode records for Studio traffic


def reconcile(dest, model=None):
    """Per-model totals from both sources; gap = Unsloth-reported minus
    session-attributed (positive gap = usage OpenCode no longer records,
    negative = OpenCode counted tokens Studio's log doesn't contain).

    Portable LEFT JOIN + UNION ALL (no FULL OUTER JOIN, which needs SQLite
    >= 3.39): every model from either side appears, with zeros on the other."""
    sql = """
        WITH u AS (SELECT model_id, COUNT(*) AS sessions,
                          SUM(tokens_input + tokens_cache_read
                              + tokens_cache_write) AS attributed_in,
                          SUM(tokens_output + tokens_reasoning) AS attributed_out
                     FROM usage WHERE provider = ?
                     GROUP BY model_id),
             e AS (SELECT model, COUNT(*) AS requests,
                          SUM(prompt_tokens) AS reported_prompt,
                          SUM(completion_tokens) AS reported_completion
                     FROM unsloth_events GROUP BY model)
        SELECT u.model_id AS model, u.sessions, u.attributed_in,
               u.attributed_out,
               e.requests, e.reported_prompt, e.reported_completion
          FROM u LEFT JOIN e ON u.model_id = e.model
        UNION ALL
        SELECT e.model, NULL, NULL, NULL,
               e.requests, e.reported_prompt, e.reported_completion
          FROM e LEFT JOIN u ON u.model_id = e.model
         WHERE u.model_id IS NULL
         ORDER BY model ASC"""
    rows = []
    for (model_id, sess, ai, ao, reqs, rp, rc) in dest.execute(
            sql, (UNSLOTH_PROVIDER,)):
        if model and model.lower() not in (model_id or "").lower():
            continue
        sess, ai, ao = sess or 0, ai or 0, ao or 0
        reqs, rp, rc = reqs or 0, rp or 0, rc or 0
        rows.append({"model": model_id, "sessions": sess,
                     "attributed_in": ai, "reported_prompt": rp,
                     "gap_prompt": rp - ai, "attributed_out": ao,
                     "reported_completion": rc,
                     "gap_completion": rc - ao})
    return rows


RECON_COLUMNS = [
    ("MODEL", "model", "text"), ("SESS", "sessions", "int"),
    ("ATTR_IN", "attributed_in", "int"), ("REQ", "requests", "int"),
    ("REPORTED_P", "reported_prompt", "int"), ("GAP_P", "gap_prompt", "int"),
    ("ATTR_OUT", "attributed_out", "int"),
    ("REPORTED_C", "reported_completion", "int"),
    ("GAP_C", "gap_completion", "int"),
]


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


def _session_filters(model, since_ms, until_ms, min_in, min_out):
    where, params = [], []
    if model:
        pattern = "%" + model.lower().replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("lower(provider || '/' || model_id) LIKE ? ESCAPE '\\'")
        params.append(pattern)
    if since_ms is not None:
        where.append("time_created >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("time_created < ?")
        params.append(_day_end_ms(until_ms))  # until-day inclusive
    if min_in is not None:
        where.append("tokens_input >= ?")
        params.append(min_in)
    if min_out is not None:
        where.append("tokens_output >= ?")
        params.append(min_out)
    return where, params


def query_sessions(dest, model=None, since_ms=None, until_ms=None,
                   min_in=None, min_out=None):
    """Session rows matching all given filters, oldest first.

    tok_s is the session's wall-clock rate — tokens over created→updated
    seconds; None when time_updated is NULL (span unknown) or under a
    second."""
    sql = ("SELECT time_created, provider, model_id, tokens_input,"
           " tokens_output, cost, title, tokens_cache_read,"
           " tokens_cache_write, time_updated FROM usage")
    where, params = _session_filters(model, since_ms, until_ms, min_in, min_out)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_created ASC"
    rows = []
    for (created, provider, model_id, tin, tout, cost, title, tcr, tcw,
         updated) in dest.execute(sql, params):
        date = datetime.datetime.fromtimestamp(created / 1000).strftime(
            "%Y-%m-%d")
        tok_s = (_aggregate_rate(tin + tout, created, updated)
                 if updated is not None else None)
        rows.append({"date": date, "model": f"{provider}/{model_id}",
                     "tokens_input": tin, "tokens_output": tout,
                     "total": tin + tout, "cost": cost, "title": title or "",
                     "tokens_cache_read": tcr, "tokens_cache_write": tcw,
                     "tok_s": tok_s})
    return rows


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
RUNS_COLUMNS = [
    ("DATE", "date", "text"), ("MODEL", "model", "text"),
    ("IN", "progress_tokens", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("TOK/S", "tok_s", "rate"),
]
SHARE_COLUMNS = TOTAL_COLUMNS + [
    ("SHARE", "share_str", "text"), ("BAR", "bar", "text"),
]
COST_COLUMN = ("COST", "cost_est", "cost")  # opt-in --cost estimate overlay
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


# Reference Claude list prices, USD per million tokens, as of 2026-11-14:
# Sonnet 4.6 / Haiku 4.5 / Opus 4.8 — cache reads at the standard 0.1x and
# cache writes at 1.25x each model's input price.
PRICES = {
    "haiku": {"in": 1.0, "out": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "sonnet": {"in": 3.0, "out": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "opus": {"in": 5.0, "out": 25.0, "cache_read": 0.50, "cache_write": 6.25},
}


def annotate_cost(rows, tier="sonnet"):
    """Copy of `rows` with a reference Claude-tier estimate added as float
    `cost_est` (USD). Display/JSON overlay only: inputs untouched, nothing
    stored. Row shape picks the math: run rows (progress_tokens) price as
    output-only; session-family rows add the cache classes; event rows use
    input+output."""
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


# Bars are a left-to-right color gradient: input tokens blue fading into
# output-token orange, with the transition centered where the input share of
# the row's total ends. Interpolated in HSL sweeping UP from blue through
# purple/magenta (never through gray), so every stop stays fully saturated;
# at these endpoints that reproduces TOKEN_IN/OUT_RGB exactly.
TOKEN_IN_RGB = (80, 168, 255)
TOKEN_OUT_RGB = (255, 136, 90)


_HUE_IN, _LUM_IN, _SAT = colorsys.rgb_to_hls(*(v / 255 for v in TOKEN_IN_RGB))
_HUE_OUT, _LUM_OUT, _ = colorsys.rgb_to_hls(*(v / 255 for v in TOKEN_OUT_RGB))
# sweep UP (blue -> purple -> magenta -> red-orange), wrapping past 1.0;
# the DOWN route would go through green/yellow
_HUE_SPAN = (_HUE_OUT - _HUE_IN) % 1.0


def _token_rgb(t):
    """Fully saturated blend from TOKEN_IN_RGB (t=0) to TOKEN_OUT_RGB (t=1)."""
    r, g, b = colorsys.hls_to_rgb((_HUE_IN + t * _HUE_SPAN) % 1.0,
                                  _LUM_IN + (_LUM_OUT - _LUM_IN) * t, 1.0)
    return (round(r * 255), round(g * 255), round(b * 255))


def _paint_gradient(bar, frac_in):
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
        r, g, b = _token_rgb(t)
        parts.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
    return "".join(parts) + "\x1b[0m"


def _color_enabled(out, environ):
    """True-color output only on a terminal, and never under NO_COLOR."""
    return bool(out.isatty()) and "NO_COLOR" not in environ


def paint_bars(rows, color):
    """Copy of `rows` with BAR cells padded by one space either side; when
    color is true, nonzero bars get a per-glyph blue->orange gradient whose
    transition marks the row's input/output split. Table-only."""
    out = []
    for r in rows:
        bar = r["bar"]
        if color and r["total"]:
            bar = _paint_gradient(bar, r["tokens_input"] / r["total"])
        out.append({**r, "bar": f" {bar} "})
    return out


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # SGR color sequences (painted bars)


def _visible(cell):
    """Display length of a cell: len() ignoring escape-sequence bytes, so
    colored cells neither inflate column widths nor shift padded output."""
    return len(_ANSI_RE.sub("", cell))


def render_table(rows, columns=None):
    """Aligned report. `columns` is a list of (header, key, kind)."""
    if columns is None:
        columns = SESSION_COLUMNS
    if not rows:
        return "no sessions match\n"

    def fmt(value, kind):
        if value is None:
            return ""
        if kind == "rate":
            return f"{value:.1f}"
        if kind == "cost":
            return f"${value:.4f}"
        if kind == "int":
            return f"{value:,}"
        if kind == "title":
            return str(value)[:40]
        return str(value)

    header = [name for name, _, _ in columns]
    body = [[fmt(r.get(key), kind) for _, key, kind in columns] for r in rows]
    widths = []
    for i, (name, _, kind) in enumerate(columns):
        if i == len(columns) - 1:
            widths.append(None)  # last column needs no padding
        else:
            widths.append(max([len(name)] + [_visible(row[i]) for row in body]))
    out = []
    for line in [header] + body:
        parts = []
        for i, cell in enumerate(line):
            if widths[i] is None:
                parts.append(cell)
            else:
                # pad by visible length (ljust would count escape bytes)
                parts.append(cell + " " * (widths[i] - _visible(cell)))
        out.append("  ".join(parts).rstrip())
    return "\n".join(out) + "\n"


def daily_totals(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None):
    """Per-day aggregates matching the filters, oldest day first. tok_s
    spans the bucket from its earliest session creation to its latest
    update (None when no row in it has a time_updated)."""
    sql = ("SELECT date(time_created/1000, 'unixepoch', 'localtime') AS day,"
           " COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost),"
           " SUM(tokens_cache_read), SUM(tokens_cache_write),"
           " MIN(time_created), MAX(time_updated) FROM usage")
    where, params = _session_filters(model, since_ms, until_ms, min_in, min_out)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day ASC"
    rows = []
    for (day, n, tin, tout, cost, tcr, tcw, first, last) in dest.execute(
            sql, params):
        total = (tin or 0) + (tout or 0)
        rows.append({"date": day, "sessions": n, "tokens_input": tin or 0,
                     "tokens_output": tout or 0, "total": total,
                     "cost": cost or 0.0, "tokens_cache_read": tcr or 0,
                     "tokens_cache_write": tcw or 0,
                     "tok_s": _aggregate_rate(total, first, last)
                             if last is not None else None})
    return rows


def total_totals(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None, by_model=False):
    """All-time aggregates: one grand-total row; per-provider/model_id rows
    (ordered by that string) with a model filter or by_model=True. Empty
    when no rows match."""
    where, params = _session_filters(model, since_ms, until_ms, min_in, min_out)
    tail = " WHERE " + " AND ".join(where) if where else ""
    sums = (" COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost),"
            " SUM(tokens_cache_read), SUM(tokens_cache_write),"
            " MIN(time_created), MAX(time_updated)")
    if not model and not by_model:
        n, tin, tout, cost, tcr, tcw, first, last = dest.execute(
            "SELECT" + sums + " FROM usage" + tail, params).fetchone()
        if not n:
            return []
        tin, tout = tin or 0, tout or 0
        return [_total_row("(all)", n, tin, tout, cost, tcr, tcw, first, last)]
    rows = []
    for m, n, tin, tout, cost, tcr, tcw, first, last in dest.execute(
            "SELECT provider || '/' || model_id AS m," + sums +
            " FROM usage" + tail + " GROUP BY m ORDER BY m ASC", params):
        rows.append(_total_row(m, n, tin, tout, cost, tcr, tcw, first, last))
    return rows


def _total_row(model, n, tin, tout, cost, tcr, tcw, first, last):
    """Row dict for total_totals: totals plus cache components and a rate
    over the group's created→updated span (None if unknown or < 1 s)."""
    tin, tout = tin or 0, tout or 0
    return {"model": model, "sessions": n, "tokens_input": tin,
            "tokens_output": tout, "total": tin + tout, "cost": cost or 0.0,
            "tokens_cache_read": tcr or 0, "tokens_cache_write": tcw or 0,
            "tok_s": _aggregate_rate(tin + tout, first, last)
                     if last is not None else None}


def _connect_src(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main(argv):
    parser = argparse.ArgumentParser(
        prog="tokenstat",
        description="Record and report AI token usage from OpenCode and "
                    "Unsloth Studio into a permanent local store.")
    parser.add_argument("command", nargs="?",
                        choices=["record", "import-unsloth", "reconcile"],
                        help="record = snapshot OpenCode now; import-unsloth ="
                             " snapshot Unsloth Studio now (events + runs);"
                             " reconcile = compare both sources per model"
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
                        help="minimum tokens in")
    parser.add_argument("--min-out", type=int, dest="min_out",
                        help="minimum tokens out")
    parser.add_argument("--daily", action="store_true",
                        help="totals per day instead of per session")
    parser.add_argument("--total", action="store_true",
                        help="grand total (with --model: per-model totals) "
                             "instead of per-session rows")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of a table (rows carry"
                             " tok_s; --cost adds cost_est)")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"our store (default: {DEFAULT_DB})")
    parser.add_argument("--src", default=DEFAULT_SRC,
                        help=f"OpenCode database (default: {DEFAULT_SRC})")
    parser.add_argument("--unsloth-db", default=DEFAULT_UNSLOTH_DB, dest="unsloth_db",
                        help=f"Unsloth Studio database (default: {DEFAULT_UNSLOTH_DB})")
    parser.add_argument("--from-unsloth", action="store_true", dest="from_unsloth",
                        help="report imported Unsloth request events instead of"
                             " OpenCode sessions (IN=prompt incl. cache, OUT=completion)")
    parser.add_argument("--from-runs", action="store_true", dest="from_runs",
                        help="report imported Unsloth generation runs (IN ="
                             " progress tokens, no in/out split) with true"
                             " tokens/second")
    parser.add_argument("--cost", action="store_true", dest="cost",
                        help="add a reference Claude-tier cost estimate column"
                             " (--cost-tier) to reports; display-only overlay"
                             " (not with reconcile)")
    parser.add_argument("--cost-tier", choices=["haiku", "sonnet", "opus"],
                        default="sonnet", dest="cost_tier",
                        help="price tier for --cost (default: sonnet)")
    parser.add_argument("--cache", action="store_true", dest="cache",
                        help="add a CACHE column (cache read + write) between"
                             " OUT and TOTAL in session views; informational,"
                             " TOTAL unchanged (not with reconcile,"
                             " --from-unsloth or --from-runs)")
    args = parser.parse_args(argv)

    dest = None
    try:
        try:
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
        if args.command == "import-unsloth":
            ok = _ingest(args.unsloth_db,
                         lambda src: import_unsloth(src, dest), verbose=True)
            runs_ok = _ingest(
                args.unsloth_db, lambda src: import_unsloth_runs(src, dest),
                verbose=True)
            return 0 if (ok and runs_ok) else 1

        # Report modes: snapshot both sources first, degrade gracefully if
        # either is missing or locked.
        _ingest(args.src, lambda src: record(src, dest, now_ms), verbose=False)
        _ingest(args.unsloth_db, lambda src: import_unsloth(src, dest),
                verbose=False)
        _ingest(args.unsloth_db, lambda src: import_unsloth_runs(src, dest),
                verbose=False)

        model_any = args.model is MODEL_ANY
        # Bare --model is a shortcut for `--total --model`: totals per model.
        # (With --daily it just means "all models", as before; reconcile keeps
        # its own flag handling.)
        want_total = args.total or (model_any and not args.daily)
        model = None if model_any else args.model

        if args.from_runs and (args.min_in is not None or args.min_out is not None):
            parser.error("--from-runs does not support --min-in/--min-out"
                         " (runs store only progress tokens)")
        if args.cache and (args.from_unsloth or args.from_runs):
            parser.error("--cache requires OpenCode session rows; Unsloth"
                         " event/run sources fold cache hits into prompt"
                         " tokens with no split")
        if args.command == "reconcile":
            unsupported = [flag for flag, val in (
                ("--since", args.since), ("--until", args.until),
                ("--min-in", args.min_in), ("--min-out", args.min_out))
                if val is not None] + (["--daily"] if args.daily else []) \
                + (["--from-unsloth"] if args.from_unsloth else []) \
                + (["--from-runs"] if args.from_runs else []) \
                + (["--total"] if args.total else []) \
                + (["--cost"] if args.cost else []) \
                + (["--cache"] if args.cache else [])
            if unsupported:
                parser.error("reconcile supports only --model; unsupported:"
                             f" {', '.join(unsupported)}")
            rows = reconcile(dest, model=model)
            columns = RECON_COLUMNS
        elif want_total and args.daily:
            parser.error("--total cannot be combined with --daily")
        elif want_total:
            rows = (total_runs(dest, model=model, since_ms=args.since,
                               until_ms=args.until, by_model=model_any)
                    if args.from_runs else
                    total_events(dest, model=model, since_ms=args.since,
                                 until_ms=args.until, min_in=args.min_in,
                                 min_out=args.min_out, by_model=model_any)
                    if args.from_unsloth else
                    total_totals(dest, model=model, since_ms=args.since,
                                 until_ms=args.until, min_in=args.min_in,
                                 min_out=args.min_out, by_model=model_any))
            rows = add_shares(rows)
            rows.sort(key=lambda r: -r["share"])  # biggest share first;
            # ties keep SQL's alphabetical-by-model order (stable sort)
            columns = SHARE_COLUMNS
        elif args.from_runs:
            rows = (daily_runs(dest, model=model, since_ms=args.since,
                               until_ms=args.until) if args.daily else
                    query_runs(dest, model=model, since_ms=args.since,
                               until_ms=args.until))
            columns = DAILY_COLUMNS if args.daily else RUNS_COLUMNS
        elif args.from_unsloth:
            rows = (daily_events(dest, model=model, since_ms=args.since,
                                 until_ms=args.until, min_in=args.min_in,
                                 min_out=args.min_out) if args.daily else
                    query_events(dest, model=model, since_ms=args.since,
                                 until_ms=args.until, min_in=args.min_in,
                                 min_out=args.min_out))
            columns = DAILY_COLUMNS if args.daily else SESSION_COLUMNS
        else:
            rows = (daily_totals(dest, model=model, since_ms=args.since,
                                 until_ms=args.until, min_in=args.min_in,
                                 min_out=args.min_out) if args.daily else
                    query_sessions(dest, model=model, since_ms=args.since,
                                   until_ms=args.until, min_in=args.min_in,
                                   min_out=args.min_out))
            columns = DAILY_COLUMNS if args.daily else SESSION_COLUMNS
        # Opt-in --cost overlay: reference pricing on row copies, never stored
        # data; reconcile keeps its own reject list above. The share-view flag
        # is captured BEFORE appending COST_COLUMN — the append creates a new
        # list, and paint_bars below must still recognize the share view.
        share_view = columns is SHARE_COLUMNS
        if args.cost and columns is not RECON_COLUMNS:
            rows = annotate_cost(rows, args.cost_tier)
            columns = columns + [COST_COLUMN]
        # Opt-in --cache overlay: combined cache classes as one informational
        # column between OUT and TOTAL; TOTAL stays IN + OUT. The guard above
        # keeps it off the cache-blind Unsloth views; reconcile rejects it.
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
            if share_view:
                rows = paint_bars(rows, color=_color_enabled(sys.stdout,
                                                             os.environ))
            print(render_table(rows, columns), end="")
        return 0
    finally:
        if dest is not None:
            dest.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
