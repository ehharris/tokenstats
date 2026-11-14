#!/usr/bin/env python3
"""tokenstat — permanent local record of AI token usage on this machine.

Reads OpenCode's session database (read-only), snapshots per-session token
usage into our own SQLite store, and reports it filtered by model, date,
and token thresholds. See README.md.
"""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

DEFAULT_SRC = os.path.expanduser("~/.local/share/opencode/opencode.db")
DEFAULT_UNSLOTH_DB = os.path.expanduser("~/.unsloth/studio/studio.db")
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
        existing = dest.execute(
            "SELECT tokens_input, tokens_output, tokens_reasoning,"
            " tokens_cache_read, tokens_cache_write, cost, provider,"
            " model_id, title, directory, agent"
            " FROM usage WHERE session_id = ?", (sid,)).fetchone()
        if existing is None:
            dest.execute(
                "INSERT INTO usage (session_id, provider, model_id,"
                " tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write, cost, title, directory,"
                " agent, time_created, time_updated, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [sid, provider, model_id, *source_vals, title, directory,
                 agent, created, updated, now_ms, now_ms])
            counts["inserted"] += 1
        else:
            merged = [max(e, s) for e, s in zip(existing[:6], source_vals)]
            changed = merged != list(existing[:6])
            # Labels make the numbers interpretable: like the token columns,
            # they never shrink — a null/empty value from the source keeps
            # the best label we already have.
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


def _event_filters(model, since_ms, until_ms, min_in, min_out):
    where, params = [], []
    if model:
        pattern = f"%{model.lower()}%"
        # Accept either the event's own model string or the provider-prefixed
        # form shown in session reports (e.g. "unsloth-studio/unsloth/Qwen...").
        where.append("(lower(model) LIKE ? OR lower(? || '/' || model) LIKE ?)")
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
    include cache hits), tokens_output = completion_tokens."""
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
                     "cost": 0.0, "title": ""})
    return rows


def daily_events(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None):
    """Per-day event aggregates (sessions = request count), oldest first."""
    where, params = _event_filters(model, since_ms, until_ms, min_in, min_out)
    sql = ("SELECT date(created_at/1000, 'unixepoch', 'localtime') AS day,"
           " COUNT(*), SUM(prompt_tokens), SUM(completion_tokens)"
           " FROM unsloth_events")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day ASC"
    return [
        {"date": day, "sessions": n, "tokens_input": p or 0,
         "tokens_output": c or 0, "total": (p or 0) + (c or 0), "cost": 0.0}
        for day, n, p, c in dest.execute(sql, params)
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


def query_sessions(dest, model=None, since_ms=None, until_ms=None,
                   min_in=None, min_out=None):
    """Session rows matching all given filters, oldest first."""
    sql = ("SELECT time_created, provider, model_id, tokens_input,"
           " tokens_output, cost, title FROM usage")
    where, params = [], []
    if model:
        where.append("lower(provider || '/' || model_id) LIKE ?")
        params.append(f"%{model.lower()}%")
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
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY time_created ASC"
    rows = []
    for created, provider, model_id, tin, tout, cost, title in dest.execute(
            sql, params):
        date = datetime.datetime.fromtimestamp(created / 1000).strftime(
            "%Y-%m-%d")
        rows.append({"date": date, "model": f"{provider}/{model_id}",
                     "tokens_input": tin, "tokens_output": tout,
                     "total": tin + tout, "cost": cost, "title": title or ""})
    return rows


SESSION_COLUMNS = [
    ("DATE", "date", "text"), ("MODEL", "model", "text"),
    ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("COST", "cost", "cost"),
    ("SESSION", "title", "title"),
]
DAILY_COLUMNS = [
    ("DATE", "date", "text"), ("SESSIONS", "sessions", "int"),
    ("IN", "tokens_input", "int"), ("OUT", "tokens_output", "int"),
    ("TOTAL", "total", "int"), ("COST", "cost", "cost"),
]


def render_table(rows, columns=None):
    """Aligned report. `columns` is a list of (header, key, kind)."""
    if columns is None:
        columns = SESSION_COLUMNS
    if not rows:
        return "no sessions match\n"

    def fmt(value, kind):
        if value is None:
            return ""
        if kind == "int":
            return f"{value:,}"
        if kind == "cost":
            return f"{value:.4f}"
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
            widths.append(max([len(name)] + [len(row[i]) for row in body]))
    out = []
    for line in [header] + body:
        parts = []
        for i, cell in enumerate(line):
            if widths[i] is None:
                parts.append(cell)
            elif columns[i][2] in ("int", "cost"):
                parts.append(cell.rjust(widths[i]))
            else:
                parts.append(cell.ljust(widths[i]))
        out.append("  ".join(parts).rstrip())
    return "\n".join(out) + "\n"


def daily_totals(dest, model=None, since_ms=None, until_ms=None,
                 min_in=None, min_out=None):
    """Per-day aggregates matching the filters, oldest day first."""
    sql = ("SELECT date(time_created/1000, 'unixepoch', 'localtime') AS day,"
           " COUNT(*), SUM(tokens_input), SUM(tokens_output), SUM(cost)"
           " FROM usage")
    where, params = [], []
    if model:
        where.append("lower(provider || '/' || model_id) LIKE ?")
        params.append(f"%{model.lower()}%")
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
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day ASC"
    return [
        {"date": day, "sessions": n, "tokens_input": tin or 0,
         "tokens_output": tout or 0, "total": (tin or 0) + (tout or 0),
         "cost": cost or 0.0}
        for day, n, tin, tout, cost in dest.execute(sql, params)
    ]


def _connect_src(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def main(argv):
    parser = argparse.ArgumentParser(
        prog="tokenstat",
        description="Record and report AI token usage from OpenCode's "
                    "database into a permanent local store.")
    parser.add_argument("command", nargs="?",
                        choices=["record", "import-unsloth", "reconcile"],
                        help="record = snapshot OpenCode now; import-unsloth ="
                             " snapshot Unsloth Studio now; reconcile ="
                             " compare both sources per model (default: report)")
    parser.add_argument("--model", help="substring match on provider/model")
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
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of a table")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"our store (default: {DEFAULT_DB})")
    parser.add_argument("--src", default=DEFAULT_SRC,
                        help=f"OpenCode database (default: {DEFAULT_SRC})")
    parser.add_argument("--unsloth-db", default=DEFAULT_UNSLOTH_DB, dest="unsloth_db",
                        help=f"Unsloth Studio database (default: {DEFAULT_UNSLOTH_DB})")
    parser.add_argument("--from-unsloth", action="store_true", dest="from_unsloth",
                        help="report imported Unsloth request events instead of"
                             " OpenCode sessions (IN=prompt incl. cache, OUT=completion)")
    args = parser.parse_args(argv)

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

    def _ingest(fn, label, path, verbose):
        """Run one ingestion step; warn-and-continue on sqlite errors."""
        try:
            counts = fn()
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

    if args.command == "record":
        ok = _ingest(lambda: record(_connect_src(args.src), dest, now_ms),
                     "opencode", args.src, verbose=True)
        return 0 if ok else 1
    if args.command == "import-unsloth":
        ok = _ingest(lambda: import_unsloth(_connect_src(args.unsloth_db), dest),
                     "unsloth", args.unsloth_db, verbose=True)
        return 0 if ok else 1

    # Report modes: snapshot both sources first, degrade gracefully if
    # either is missing or locked.
    _ingest(lambda: record(_connect_src(args.src), dest, now_ms),
            "opencode", args.src, verbose=False)
    _ingest(lambda: import_unsloth(_connect_src(args.unsloth_db), dest),
            "unsloth", args.unsloth_db, verbose=False)

    if args.command == "reconcile":
        unsupported = [flag for flag, val in (
            ("--since", args.since), ("--until", args.until),
            ("--min-in", args.min_in), ("--min-out", args.min_out))
            if val is not None] + (["--daily"] if args.daily else []) \
            + (["--from-unsloth"] if args.from_unsloth else [])
        if unsupported:
            parser.error("reconcile supports only --model; unsupported:"
                         f" {', '.join(unsupported)}")
        rows = reconcile(dest, model=args.model)
        columns = RECON_COLUMNS
    elif args.from_unsloth:
        rows = (daily_events(dest, model=args.model, since_ms=args.since,
                             until_ms=args.until, min_in=args.min_in,
                             min_out=args.min_out) if args.daily else
                query_events(dest, model=args.model, since_ms=args.since,
                             until_ms=args.until, min_in=args.min_in,
                             min_out=args.min_out))
        columns = DAILY_COLUMNS if args.daily else SESSION_COLUMNS
    else:
        rows = (daily_totals(dest, model=args.model, since_ms=args.since,
                             until_ms=args.until, min_in=args.min_in,
                             min_out=args.min_out) if args.daily else
                query_sessions(dest, model=args.model, since_ms=args.since,
                               until_ms=args.until, min_in=args.min_in,
                               min_out=args.min_out))
        columns = DAILY_COLUMNS if args.daily else SESSION_COLUMNS
    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_table(rows, columns), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
