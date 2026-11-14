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
    parser.add_argument("command", nargs="?", choices=["record"],
                        help="record = snapshot usage now and exit")
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

    if args.command == "record":
        try:
            counts = record(_connect_src(args.src), dest, now_ms)
        except sqlite3.Error as exc:
            print(f"warning: could not read {args.src}: {exc}",
                  file=sys.stderr)
            return 1
        print(f"inserted={counts['inserted']} updated={counts['updated']}"
              f" unchanged={counts['unchanged']}")
        return 0

    # Query mode: record implicitly first, degrade gracefully if the source
    # database is missing or locked while OpenCode writes.
    try:
        record(_connect_src(args.src), dest, now_ms)
    except sqlite3.Error as exc:
        print(f"warning: could not read {args.src}: {exc}", file=sys.stderr)

    if args.daily:
        rows = daily_totals(dest, model=args.model, since_ms=args.since,
                            until_ms=args.until, min_in=args.min_in,
                            min_out=args.min_out)
    else:
        rows = query_sessions(dest, model=args.model, since_ms=args.since,
                              until_ms=args.until, min_in=args.min_in,
                              min_out=args.min_out)
    if args.as_json:
        print(json.dumps(rows, indent=2))
    else:
        columns = DAILY_COLUMNS if args.daily else SESSION_COLUMNS
        print(render_table(rows, columns), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
