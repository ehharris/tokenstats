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
            " tokens_cache_read, tokens_cache_write, cost"
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
            merged = [max(e, s) for e, s in zip(existing, source_vals)]
            changed = merged != list(existing)
            dest.execute(
                "UPDATE usage SET provider = ?, model_id = ?,"
                " tokens_input = ?, tokens_output = ?, tokens_reasoning = ?,"
                " tokens_cache_read = ?, tokens_cache_write = ?, cost = ?,"
                " title = ?, directory = ?, agent = ?, time_updated = ?,"
                " last_seen = ? WHERE session_id = ?",
                [provider, model_id, *merged, title, directory, agent,
                 updated, now_ms, sid])
            counts["updated" if changed else "unchanged"] += 1
    dest.commit()
    return counts


DAY_MS = 86_400_000


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
        params.append(until_ms + DAY_MS)  # until-day inclusive
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


def render_table(rows):
    """Aligned report: DATE MODEL IN OUT TOTAL COST SESSION."""
    if not rows:
        return "no sessions match\n"
    header = ["DATE", "MODEL", "IN", "OUT", "TOTAL", "COST", "SESSION"]
    lines = [header]
    for r in rows:
        title = (r["title"] or "")[:40]
        lines.append([r["date"], r["model"], f"{r['tokens_input']:,}",
                      f"{r['tokens_output']:,}", f"{r['total']:,}",
                      f"{r['cost']:.4f}", title])
    body = lines[1:]
    widths = []
    for i, name in enumerate(header):
        if name in ("IN", "OUT", "TOTAL", "COST"):
            widths.append(max(len(name), *(len(l[i + 1]) for l in body)))
        elif name == "SESSION":
            widths.append(None)  # last column: no padding needed
        else:
            widths.append(max(len(name), *(len(l[i + 1]) for l in body)))
    out = []
    for line in lines:
        parts = []
        for i, cell in enumerate(line):
            if widths[i] is None:
                parts.append(cell)
            elif header[i] in ("IN", "OUT", "TOTAL", "COST"):
                parts.append(cell.rjust(widths[i]))
            else:
                parts.append(cell.ljust(widths[i]))
        out.append("  ".join(parts).rstrip())
    return "\n".join(out) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(
        prog="tokenstat", description=__doc__.splitlines()[1])
    parser.add_argument("--model", help="substring match on provider/model")
    parser.add_argument("--since", type=parse_date, metavar="YYYY-MM-DD",
                        help="first day of usage (inclusive)")
    parser.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                        help="last day of usage (inclusive)")
    parser.add_argument("--min-in", type=int, dest="min_in",
                        help="minimum tokens in")
    parser.add_argument("--min-out", type=int, dest="min_out",
                        help="minimum tokens out")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"our store (default: {DEFAULT_DB})")
    args = parser.parse_args(argv)

    dest = sqlite3.connect(args.db)
    ensure_schema(dest)
    print(render_table(query_sessions(
        dest, model=args.model, since_ms=args.since, until_ms=args.until,
        min_in=args.min_in, min_out=args.min_out)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
