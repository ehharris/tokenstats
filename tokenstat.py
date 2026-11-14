#!/usr/bin/env python3
"""tokenstat — permanent local record of AI token usage on this machine.

Reads OpenCode's session database (read-only), snapshots per-session token
usage into our own SQLite store, and reports it filtered by model, date,
and token thresholds. See README.md.
"""
import json
import os
import sqlite3

DEFAULT_SRC = os.path.expanduser("~/.local/share/opencode/opencode.db")

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
