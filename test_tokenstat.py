"""Tests for tokenstat.py — run with: python3 -m unittest test_tokenstat -v"""
import argparse
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tokenstat
import unittest
import unittest.mock

from tokenstat import (_ANSI_RE, _table_panel, _turn_index, _visible,
                       GRUVBOX_DARK, NEON, NORD, SESSION_COLUMNS,
                       SHARE_COLUMNS, THEMES, THEME_ROLES, ThemeError,
                       add_shares, compact, daily_events, daily_runs,
                       daily_totals, ensure_schema, import_unsloth,
                       import_unsloth_runs, load_theme_file, main, paint_bars,
                       paint_cells, paint_header_block, panel, parse_date,
                       parse_model, query_events, query_runs, query_sessions,
                       read_default_theme, reconcile, record, render_table,
                       report_header, resolve_theme, scan_themes, total_events,
                       total_runs, total_totals, write_default_theme)

LEMONADE_JSON = '{"id":"gpt-oss-120b","providerID":"lemonade"}'
UNSLOTH_MODEL_ID = "unsloth/Qwen3.8-Flash-Next-GGUF"


def ms(y, m, d):
    """Local midnight of the given date, in epoch milliseconds."""
    return int(datetime.datetime(y, m, d).timestamp() * 1000)

UNSLOTH_JSON = (
    '{"id":"unsloth/Qwen3.8-Flash-Next-GGUF","providerID":"unsloth-studio",'
    '"variant":"default"}'
)


def make_src(rows=()):
    """Fake OpenCode source DB with the columns `record` reads from session_v2."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE session_v2 (
               id TEXT PRIMARY KEY, model TEXT,
               tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
               tokens_cache_read INTEGER, tokens_cache_write INTEGER, cost REAL,
               title TEXT, directory TEXT, agent TEXT,
               time_created INTEGER, time_updated INTEGER)"""
    )
    for row in rows:
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    return conn


def step_msg(mid="m1", sid="s1", kind="assistant", created=1_764_000_000_000,
             first=None, streamed=None, out=100, reasoning=50, data=None):
    """One session_message row. `data` defaults to a timing+tokens blob."""
    if data is None:
        data = json.dumps({
            "time": {"created": created,
                     **({"firstToken": first} if first is not None else {}),
                     **({"streamed": streamed} if streamed is not None else {})},
            "tokens": {"output": out, "reasoning": reasoning},
        })
    return (mid, sid, kind, created, data)


def make_step_src(session_rows=(), messages=()):
    """Fake OpenCode source with session_v2 AND session_message."""
    src = make_src(session_rows)
    src.execute("""CREATE TABLE session_message (
                       id TEXT PRIMARY KEY, session_id TEXT, type TEXT,
                       time_created INTEGER, data TEXT)""")
    for row in messages:
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)", row)
    return src


def src_row(sid="s1", model=UNSLOTH_JSON, tin=1000, tout=200, treas=50,
            tcr=7000, tcw=100, cost=0.01, title="a session", directory="/tmp/d",
            agent="build", created=1_764_000_000_000, updated=1_764_000_005_000):
    return (sid, model, tin, tout, treas, tcr, tcw, cost, title, directory,
            agent, created, updated)


def make_dest():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


class UpsertEngineTests(unittest.TestCase):
    """The one monotonic-max upsert every ingest path folds into.

    Fixture table stands in for a store table: `model` is a label, `n` a
    numeric column, `end` an overwrite column (named like a keyword because a
    store column may be one), `seen` an insert-only column."""

    def setUp(self):
        self.dest = sqlite3.connect(":memory:")
        self.addCleanup(self.dest.close)
        self.dest.execute(
            'CREATE TABLE t (k TEXT PRIMARY KEY, model TEXT, n INTEGER,'
            ' "end" INTEGER, seen INTEGER)')

    def stored(self, k="a"):
        return self.dest.execute(
            'SELECT model, n, "end", seen FROM t WHERE k = ?', (k,)).fetchone()

    def test_numeric_columns_fold_to_max_and_labels_keep_best(self):
        rows = [{"k": "a", "model": "m", "n": 5}]
        first = tokenstat._upsert(self.dest, "t", "k", rows, numeric=["n"])
        self.assertEqual(first["inserted"], 1)
        # A clean run carries no `skipped` key at all: the returned Counter has
        # to stay dict-equal to the plain dicts the ingest tests compare whole.
        self.assertEqual(first, {"inserted": 1, "updated": 0, "unchanged": 0})
        again = tokenstat._upsert(self.dest, "t", "k",
                                  [{"k": "a", "model": "", "n": 3}],
                                  numeric=["n"])
        self.assertEqual(again["unchanged"], 1)      # 3 does not shrink 5
        # stored row keeps the label the source nulled and the number it grew
        self.assertEqual(self.stored(), ("m", 5, None, None))
        again = tokenstat._upsert(self.dest, "t", "k",
                                  [{"k": "a", "model": "m2", "n": 9}],
                                  numeric=["n"])
        self.assertEqual(again["updated"], 1)        # and n is now 9, model m2
        self.assertEqual(self.stored(), ("m2", 9, None, None))

    def test_overwrite_columns_take_the_new_value_even_when_falsy(self):
        tokenstat._upsert(self.dest, "t", "k",
                          [{"k": "a", "n": 1, "end": 7}], numeric=["n"],
                          overwrite=["end"])
        tokenstat._upsert(self.dest, "t", "k",
                          [{"k": "a", "n": 1, "end": None}], numeric=["n"],
                          overwrite=["end"])
        # A NULL reading lands as NULL: an overwrite column is never keep-best,
        # or a finished run would look unfinished forever.
        self.assertEqual(self.stored(), (None, 1, None, None))

    def test_insert_only_columns_are_written_once(self):
        tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 1, "seen": 1}],
                          numeric=["n"], insert_only=["seen"])
        tokenstat._upsert(self.dest, "t", "k", [{"k": "a", "n": 2, "seen": 2}],
                          numeric=["n"], insert_only=["seen"])
        # n folded up to 2, seen still the value the first insert wrote.
        self.assertEqual(self.stored(), (None, 2, None, 1))

    def test_a_malformed_row_bumps_skipped_and_the_rest_survive(self):
        # A dict is a type sqlite3 refuses to bind: the bad row is isolated and
        # the good row beside it still lands.
        counts = tokenstat._upsert(
            self.dest, "t", "k",
            [{"k": "bad", "model": "m", "n": {"not": "a number"}},
             {"k": "good", "model": "m", "n": 4}], numeric=["n"])
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["inserted"], 1)
        self.assertIsNone(self.stored("bad"))
        self.assertEqual(self.stored("good"), ("m", 4, None, None))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)

    def stored(self, sid="s1"):
        cur = self.dest.execute(
            "SELECT provider, model_id, tokens_input, tokens_output,"
            " tokens_reasoning, tokens_cache_read, tokens_cache_write, cost"
            " FROM usage WHERE session_id = ?", (sid,))
        return cur.fetchone()

    def test_record_inserts_new_session(self):
        src = make_src([src_row()])
        self.addCleanup(src.close)
        result = record(src, self.dest, now_ms=1)
        self.assertEqual(result, {"inserted": 1, "updated": 0, "unchanged": 0})
        row = self.stored()
        self.assertIsNotNone(row)
        provider, model_id, tin, tout, treas, tcr, tcw, cost = row
        self.assertEqual(provider, "unsloth-studio")
        self.assertEqual(model_id, "unsloth/Qwen3.8-Flash-Next-GGUF")
        self.assertEqual((tin, tout, treas, tcr, tcw), (1000, 200, 50, 7000, 100))
        self.assertAlmostEqual(cost, 0.01)

    def test_record_is_idempotent_no_double_counting(self):
        src = make_src([src_row()])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        result = record(src, self.dest, now_ms=2)
        self.assertEqual(result, {"inserted": 0, "updated": 0, "unchanged": 1})
        self.assertEqual(self.stored()[2], 1000)

    def test_record_updates_grown_session(self):
        src = make_src([src_row(tin=1000, tout=200)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        src.execute(
            "UPDATE session_v2 SET tokens_input = 3000, tokens_output = 600"
            " WHERE id = 's1'")
        result = record(src, self.dest, now_ms=2)
        self.assertEqual(result, {"inserted": 0, "updated": 1, "unchanged": 0})
        row = self.stored()
        self.assertEqual(row[2], 3000)  # replaced, not added (would be 4000)
        self.assertEqual(row[3], 600)

    def test_record_never_shrinks_on_source_regression(self):
        src = make_src([src_row(tin=100)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        src.execute("UPDATE session_v2 SET tokens_input = 50 WHERE id = 's1'")
        record(src, self.dest, now_ms=2)
        self.assertEqual(self.stored()[2], 100)

    def test_record_null_and_malformed_model(self):
        src = make_src([src_row(sid="n1", model=None),
                        src_row(sid="n2", model="not json")])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        for sid in ("n1", "n2"):
            row = self.stored(sid)
            self.assertEqual((row[0], row[1]), ("", ""), f"session {sid}")


class RecordIsolationTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)

    def test_record_skips_bad_row_keeps_good(self):
        # src_row(sid="bad", created=None) -> NULL time_created on insert ->
        # NOT NULL violation; record() must skip it, not abort the run.
        src = make_src([src_row(sid="good"), src_row(sid="bad", created=None)])
        self.addCleanup(src.close)
        counts = record(src, self.dest, now_ms=1)
        self.assertEqual(counts["inserted"], 1)             # good row
        self.assertEqual(counts.get("skipped", 0), 1)       # bad row
        self.assertEqual(self.dest.execute(
            "SELECT tokens_input FROM usage WHERE session_id='good'").fetchone()[0],
            1000)
        self.assertEqual(self.dest.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id='bad'").fetchone()[0], 0)


class ImportStepsTests(unittest.TestCase):
    BASE = 1_764_000_000_000

    def dest_for(self, messages, rows=(src_row(),)):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(list(rows), list(messages))
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def rows(self, dest):
        return dest.execute("SELECT message_id, session_id, turn, step_ms,"
                            " tokens_generated FROM opencode_steps"
                            " ORDER BY message_id").fetchall()

    def test_step_tokens_are_output_plus_reasoning(self):
        dest = self.dest_for([step_msg(out=100, reasoning=50)])
        rows = self.rows(dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 150)

    def test_step_ms_is_streamed_minus_first_token(self):
        dest = self.dest_for([step_msg(first=self.BASE + 100,
                                       streamed=self.BASE + 1100)])
        rows = self.rows(dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 1000)

    def test_step_ms_falls_back_to_created_without_first_token(self):
        dest = self.dest_for([step_msg(created=self.BASE,
                                       streamed=self.BASE + 900)])
        rows = self.rows(dest)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 900)

    def test_never_streamed_step_stores_null_step_ms(self):
        dest = self.dest_for([step_msg(streamed=None)])
        rows = self.rows(dest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0][3])

    def test_turn_index_matches_opencode_boundaries(self):
        messages = [
            step_msg(mid="u1", kind="user", created=self.BASE + 1),
            step_msg(mid="a1", created=self.BASE + 2),
            step_msg(mid="x1", kind="synthetic", created=self.BASE + 3),
            step_msg(mid="a2", created=self.BASE + 4),
            step_msg(mid="i1", kind="idle", created=self.BASE + 5),
            step_msg(mid="u2", kind="user", created=self.BASE + 6),
            step_msg(mid="a3", created=self.BASE + 7),
            step_msg(mid="a4", created=self.BASE + 8),
        ]
        dest = self.dest_for(messages)
        stored = {r[0]: r[2] for r in self.rows(dest)}
        self.assertEqual(stored, {"a1": 0, "a2": 0, "a3": 1, "a4": 1})

    def test_non_assistant_messages_are_not_stored(self):
        messages = [
            step_msg(mid="u1", kind="user"),
            step_msg(mid="i1", kind="idle"),
            step_msg(mid="x1", kind="synthetic"),
        ]
        dest = self.dest_for(messages)
        self.assertEqual(self.rows(dest), [])

    def test_rerun_is_a_noop(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row()],
                            [step_msg(first=self.BASE + 100,
                                      streamed=self.BASE + 1100)])
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        before = self.rows(dest)
        self.assertEqual(before[0][3], 1000)
        self.assertEqual(before[0][4], 150)
        counts = record(src, dest, now_ms=2)
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(counts["unchanged"], 2)  # the session and the step
        self.assertEqual(self.rows(dest), before)

    def test_pruned_source_cannot_shrink_stored_values(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(
            [src_row()],
            [step_msg(first=self.BASE, streamed=self.BASE + 1000)])
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        self.assertEqual(self.rows(dest)[0][3], 1000)
        # Same step id, shorter stream and fewer tokens: a pruned/rewritten
        # source must not shrink either stored total.
        src.execute("DELETE FROM session_message")
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    step_msg(first=self.BASE, streamed=self.BASE + 500,
                             out=10, reasoning=5))
        record(src, dest, now_ms=2)
        row = self.rows(dest)[0]
        self.assertEqual(row[3], 1000)
        self.assertEqual(row[4], 150)

    def test_missing_session_message_table_is_a_noop(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_src([src_row()])  # session_v2 only, no session_message
        self.addCleanup(src.close)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts, {"inserted": 1, "updated": 0, "unchanged": 0})
        self.assertEqual(self.rows(dest), [])

    def test_malformed_blob_bumps_skipped_and_continues(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        messages = [
            step_msg(mid="good1", out=100),
            step_msg(mid="bad", data="{not json"),
            step_msg(mid="good2", out=200),
        ]
        src = make_step_src([src_row()], messages)
        self.addCleanup(src.close)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts["skipped"], 1)
        stored = {r[0]: r[4] for r in self.rows(dest)}
        self.assertEqual(stored, {"good1": 150, "good2": 250})

    def test_record_counts_include_steps(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row()],
                            [step_msg(mid="a1"), step_msg(mid="a2")])
        self.addCleanup(src.close)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts["inserted"], 3)

    def test_non_numeric_blob_values_bump_skipped_and_continue(self):
        # Valid JSON, but the numeric fields are strings: both arithmetic
        # sites (step_ms and tokens_generated) must isolate the row rather
        # than aborting the whole snapshot.
        bad_stream = json.dumps({
            "time": {"streamed": "not-a-number"},
            "tokens": {"output": 100, "reasoning": 50},
        })
        bad_tokens = json.dumps({
            "time": {"streamed": self.BASE + 1000},
            "tokens": {"output": "100", "reasoning": 50},
        })
        messages = [
            step_msg(mid="good1", first=self.BASE,
                     streamed=self.BASE + 1000),
            step_msg(mid="bad_stream", data=bad_stream),
            step_msg(mid="bad_tokens", data=bad_tokens),
            step_msg(mid="good2", out=200),
        ]
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row()], messages)
        self.addCleanup(src.close)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts["skipped"], 2)
        self.assertEqual({r[0]: r[4] for r in self.rows(dest)},
                         {"good1": 150, "good2": 250})

    def test_skipped_first_boundary_row_does_not_consume_an_ordinal(self):
        # The bad assistant at index 1 opens boundary 0 (before the idle);
        # the good assistants open boundary 3. A skipped row must not burn
        # ordinal 0, so the stored turns still start at 0.
        messages = [
            step_msg(mid="u1", kind="user", created=self.BASE + 1),
            step_msg(mid="bad", data="{not json", created=self.BASE + 2),
            step_msg(mid="i1", kind="idle", created=self.BASE + 3),
            step_msg(mid="u2", kind="user", created=self.BASE + 4),
            step_msg(mid="a1", created=self.BASE + 5),
            step_msg(mid="a2", created=self.BASE + 6),
        ]
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row()], messages)
        self.addCleanup(src.close)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual({r[0]: r[2] for r in self.rows(dest)},
                         {"a1": 0, "a2": 0})


class TurnStabilityTests(unittest.TestCase):
    """A session's turn labels are frozen only when the source has lost a
    stored step. A live session keeps recomputing, so the legacy->idle flip
    still improves grouping; a pruned session keeps its historical turns so a
    displayed rate cannot drift without new generation."""

    BASE = 1_764_000_000_000

    def boundary(self, mid, kind, created):
        return step_msg(mid, kind=kind, created=created)

    def assistant(self, mid, created, out=100, span=1000):
        return step_msg(mid, created=created, first=self.BASE,
                        streamed=self.BASE + span, out=out, reasoning=0)

    def make(self, messages):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row()], list(messages))
        self.addCleanup(src.close)
        return dest, src

    def turns(self, dest):
        return dict(dest.execute(
            "SELECT message_id, turn FROM opencode_steps"
            " ORDER BY message_id").fetchall())

    def test_pruning_preserves_stored_turns_and_rate(self):
        messages = [
            self.boundary("u1", "user", self.BASE + 1),
            self.assistant("a0", self.BASE + 2, out=100),   # turn 0: 100 tok/s
            self.boundary("i1", "idle", self.BASE + 3),
            self.boundary("u2", "user", self.BASE + 4),
            self.assistant("a1", self.BASE + 5, out=200),   # turn 1: 200 tok/s
            self.boundary("i2", "idle", self.BASE + 6),
            self.boundary("u3", "user", self.BASE + 7),
            self.assistant("a2", self.BASE + 8, out=300),   # turn 2: 300 tok/s
        ]
        dest, src = self.make(messages)
        record(src, dest, now_ms=1)
        before = self.turns(dest)
        self.assertEqual(before, {"a0": 0, "a1": 1, "a2": 2})
        rate = query_sessions(dest)[0]["tok_s"]
        self.assertEqual(rate, 200.0)

        # OpenCode prunes an idle boundary (i2) and the middle assistant (a1,
        # a stored id — the detectable signal). The recomputed walk would
        # renumber a2 from turn 2 to turn 1 and pool its 300 tok/s into a1's
        # turn — moving the displayed rate with no new generation.
        src.execute("DELETE FROM session_message WHERE id IN ('a1', 'i2')")
        record(src, dest, now_ms=2)
        self.assertEqual(self.turns(dest), before)
        self.assertEqual(query_sessions(dest)[0]["tok_s"], rate)

    def test_new_rows_after_pruning_continue_the_stored_ordinals(self):
        messages = [
            self.boundary("u1", "user", self.BASE + 1),
            self.assistant("a0", self.BASE + 2, out=100),
            self.boundary("i1", "idle", self.BASE + 3),
            self.boundary("u2", "user", self.BASE + 4),
            self.assistant("a1", self.BASE + 5, out=200),
        ]
        dest, src = self.make(messages)
        record(src, dest, now_ms=1)
        self.assertEqual(self.turns(dest), {"a0": 0, "a1": 1})

        src.execute("DELETE FROM session_message WHERE id = 'a1'")
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    self.boundary("i2", "idle", self.BASE + 6))
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    self.boundary("u3", "user", self.BASE + 7))
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    self.assistant("a2", self.BASE + 8, out=300))
        record(src, dest, now_ms=2)
        turns = self.turns(dest)
        # The pruned a1 row still owns turn 1; the new assistant must continue
        # the stored sequence at 2, not collide with the frozen ordinal.
        self.assertEqual(turns["a2"], 2)
        self.assertEqual(turns, {"a0": 0, "a1": 1, "a2": 2})

    def test_legacy_flip_still_regroups_when_nothing_was_pruned(self):
        # No idle rows at first ingest -> legacy walk: each user opens a turn.
        messages = [
            self.boundary("u1", "user", self.BASE + 1),
            self.assistant("a0", self.BASE + 2, out=100),
            self.boundary("u2", "user", self.BASE + 3),
            self.assistant("a1", self.BASE + 4, out=100, span=2000),
        ]
        dest, src = self.make(messages)
        record(src, dest, now_ms=1)
        self.assertEqual(self.turns(dest), {"a0": 0, "a1": 1})

        # No stored id was lost — an idle boundary simply arrived. The live
        # session must regroup: non-legacy walks back past both users to the
        # idle, so a1 moves from turn 1 into turn 0.
        src.execute("INSERT INTO session_message VALUES (?,?,?,?,?)",
                    self.boundary("i0", "idle", self.BASE + 0))
        record(src, dest, now_ms=2)
        self.assertEqual(self.turns(dest), {"a0": 0, "a1": 0})

    def test_unchanged_source_is_a_provable_noop(self):
        messages = [
            self.boundary("u1", "user", self.BASE + 1),
            self.assistant("a0", self.BASE + 2, out=100),
            self.boundary("i1", "idle", self.BASE + 3),
            self.boundary("u2", "user", self.BASE + 4),
            self.assistant("a1", self.BASE + 5, out=200),
        ]
        dest, src = self.make(messages)
        counts = record(src, dest, now_ms=1)
        self.assertEqual(counts["inserted"], 3)  # one session + two steps
        sql = ("SELECT message_id, session_id, turn, step_ms, tokens_generated,"
               " time_created FROM opencode_steps ORDER BY message_id")
        before = dest.execute(sql).fetchall()
        counts = record(src, dest, now_ms=2)
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(dest.execute(sql).fetchall(), before)


class ParseModelTests(unittest.TestCase):
    def test_parse_model_extracts_provider_and_id(self):
        self.assertEqual(
            parse_model(UNSLOTH_JSON),
            ("unsloth-studio", "unsloth/Qwen3.8-Flash-Next-GGUF"))

    def test_parse_model_null_and_invalid(self):
        self.assertEqual(parse_model(None), ("", ""))
        self.assertEqual(parse_model("not json"), ("", ""))


class QueryTests(unittest.TestCase):
    def setUp(self):
        src = make_src([
            src_row(sid="u1", model=UNSLOTH_JSON, tin=5000, tout=900,
                    created=ms(2026, 11, 1), title="unsloth session"),
            src_row(sid="l1", model=LEMONADE_JSON, tin=100, tout=5,
                    created=ms(2026, 11, 5), title="lemonade session"),
        ])
        self.addCleanup(src.close)
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        record(src, self.dest, now_ms=1)

    def test_filter_by_model_substring(self):
        for pattern in ("unsloth", "UNSLOTH"):
            rows = query_sessions(self.dest, model=pattern)
            self.assertEqual(len(rows), 1, pattern)
            self.assertIn("unsloth-studio", rows[0]["model"])

    def test_filter_date_window_inclusive_endpoints(self):
        both = query_sessions(self.dest, since_ms=ms(2026, 11, 1),
                              until_ms=ms(2026, 11, 5))
        self.assertEqual(len(both), 2)
        only_first = query_sessions(self.dest, since_ms=ms(2026, 11, 1),
                                    until_ms=ms(2026, 11, 4))
        self.assertEqual([r["date"] for r in only_first], ["2026-11-01"])

    def test_filter_min_tokens(self):
        rows = query_sessions(self.dest, min_out=100)
        self.assertEqual(len(rows), 1)
        self.assertIn("unsloth-studio", rows[0]["model"])
        rows = query_sessions(self.dest, min_in=1000)
        self.assertEqual(len(rows), 1)
        self.assertIn("unsloth-studio", rows[0]["model"])

    def test_query_returns_expected_dict_keys(self):
        row = query_sessions(self.dest)[0]
        self.assertEqual(
            sorted(row.keys()),
            sorted(["date", "model", "tokens_input", "tokens_output",
                    "total", "cost", "title", "tok_s", "rate_weight",
                    "tokens_cache_read", "tokens_cache_write"]))
        self.assertEqual(row["total"], row["tokens_input"] + row["tokens_output"])


class SessionTokSTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_src([src_row(sid="s1", tin=1000, tout=200,
                                 created=1_764_000_000_000,
                                 updated=1_764_000_005_000)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)

    def test_session_with_no_steps_has_none_rate(self):
        # A session_v2 row may carry a wall-clock span, but with no step data
        # there is no turn rate — and no wall-clock fallback.
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src([src_row(sid="n")], [])
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        rows = query_sessions(dest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["tok_s"])

    def test_session_subsecond_turn_has_none_rate(self):
        # A single 0.5 s turn falls below the one-second floor and is dropped;
        # with no other turn the session has no rate (not a ZeroDivision).
        dest = make_dest()
        self.addCleanup(dest.close)
        base = 1_764_000_000_000
        src = make_step_src(
            [src_row(sid="z")],
            [step_msg(mid="a", sid="z", first=base, streamed=base + 500)])
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        rows = query_sessions(dest)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["tok_s"])

    def test_session_rows_carry_cache_components(self):
        row = query_sessions(self.dest)[0]
        self.assertIn("tokens_cache_read", row)
        self.assertIn("tokens_cache_write", row)

    def test_event_rows_have_none_rate(self):
        # from ImportUnslothTests.setUp style
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_unsloth_src([unsloth_event("e1")])
        self.addCleanup(src.close)
        import_unsloth(src, dest)
        self.assertIsNone(query_events(dest)[0]["tok_s"])


class SessionTurnRateTests(unittest.TestCase):
    """A session's TOK/S is the unweighted mean of its turns' rates, matching
    OpenCode's per-prompt number: Σ(output+reasoning) / Σ(streamed - firstToken)
    per turn, then averaged across turns."""

    BASE = 1_764_000_000_000

    def dest_for(self, messages, rows=(src_row(),)):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(list(rows), list(messages))
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def turn(self, mid, first_off, stream_off, out, reasoning=0, sid="s1",
             created=None):
        # `created` is only ordering metadata: step_ms comes from the blob's
        # firstToken/streamed, so bumping it cannot change the rate math. It
        # lets multi-turn fixtures keep assistant/idle/user messages in a
        # deterministic time order (as ImportStepsTests does).
        return step_msg(mid, sid=sid,
                        created=self.BASE if created is None else created,
                        first=self.BASE + first_off,
                        streamed=self.BASE + first_off + stream_off,
                        out=out, reasoning=reasoning)

    def test_two_turns_average_their_rates(self):
        # turn 0: 100 tokens / 1.0 s = 100.0; turn 1: 100 / 2.0 = 50.0.
        messages = [
            self.turn("a1", 0, 1000, 100),
            step_msg("i1", kind="idle", created=self.BASE + 10),
            step_msg("u1", kind="user", created=self.BASE + 20),
            self.turn("a2", 0, 2000, 100, created=self.BASE + 30),
        ]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertAlmostEqual(row["tok_s"], 75.0, places=2)

    def test_steps_in_one_turn_pool_before_dividing(self):
        # Two steps in ONE turn with unequal spans: (100+300) / (1.0+4.0) =
        # 80.0, NOT mean(100.0, 75.0) = 87.5. Equal 1 s spans would make the
        # two rules coincide, so use 1 s and 4 s to catch a GROUP BY step
        # regression.
        messages = [
            self.turn("a1", 0, 1000, 100),
            self.turn("a2", 0, 4000, 300, created=self.BASE + 10),
        ]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertAlmostEqual(row["tok_s"], 80.0, places=2)

    def test_never_streamed_step_drops_its_turn(self):
        # Turn 0 is valid (100.0). Turn 1 holds a valid step AND a
        # never-streamed step: ANY such step voids the whole turn, so turn 1
        # is dropped rather than averaged in (as 0 or as a pooled rate).
        messages = [
            self.turn("a1", 0, 1000, 100),
            step_msg("i1", kind="idle", created=self.BASE + 10),
            step_msg("u1", kind="user", created=self.BASE + 20),
            self.turn("a2", 0, 1000, 100, created=self.BASE + 30),
            step_msg("bad", first=self.BASE + 40, streamed=None, out=999,
                     created=self.BASE + 40),
        ]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertAlmostEqual(row["tok_s"], 100.0, places=2)

    def test_session_with_no_steps_has_no_rate(self):
        # The session_v2 row has a wall-clock span, but there is no step data
        # and deliberately no wall-clock fallback.
        row = query_sessions(self.dest_for([]))[0]
        self.assertIsNone(row["tok_s"])

    def test_subsecond_turn_is_dropped(self):
        # A 0.5 s turn is excluded by the >= 1000 ms floor; alone -> None.
        messages = [self.turn("a1", 0, 500, 100)]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertIsNone(row["tok_s"])

    def test_rate_weight_is_generated_tokens(self):
        # tokens_generated is output + reasoning per step: 100 + (200+50).
        messages = [
            self.turn("a1", 0, 1000, 100),
            self.turn("a2", 0, 1000, 200, reasoning=50,
                      created=self.BASE + 10),
        ]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertEqual(row["rate_weight"], 350)

    def test_mean_of_nonround_turn_rates(self):
        # Turn rates 1000/3 and 1000/7, averaged then rounded to one decimal.
        messages = [
            self.turn("a1", 0, 3000, 1000),
            step_msg("i1", kind="idle", created=self.BASE + 10),
            step_msg("u1", kind="user", created=self.BASE + 20),
            self.turn("a2", 0, 7000, 1000, created=self.BASE + 30),
        ]
        row = query_sessions(self.dest_for(messages))[0]
        self.assertEqual(row["tok_s"], round((1000 / 3 + 1000 / 7) / 2, 1))


class EventTokSTests(unittest.TestCase):
    """Aggregated event views derive tok_s from their rows' created_at span;
    per-event rows carry no duration and report None."""

    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        self.base = 1_764_000_000_000  # fixed instant; both events share one local day
        src = make_unsloth_src([
            unsloth_event("e1", prompt=100, completion=50, created=self.base),
            unsloth_event("e2", prompt=300, completion=50,
                          created=self.base + 5_000),
        ])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)

    def test_daily_events_rate_from_created_span(self):
        days = daily_events(self.dest)
        self.assertEqual(len(days), 1)
        # (100+50) + (300+50) tokens over a 5 s created_at span = 100.0
        self.assertAlmostEqual(days[0]["tok_s"], 100.0, places=2)
        # sub-second span -> None (same semantics as _aggregate_rate)
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_unsloth_src([
            unsloth_event("f1", prompt=10, created=self.base),
            unsloth_event("f2", prompt=10, created=self.base + 500)])
        self.addCleanup(src.close)
        import_unsloth(src, dest)
        self.assertIsNone(daily_events(dest)[0]["tok_s"])

    def test_total_events_grand_row_rate(self):
        r = total_events(self.dest)[0]
        self.assertAlmostEqual(r["tok_s"], 100.0, places=2)


class AggregateWeightedRateTests(unittest.TestCase):
    """Aggregate TOK/S is the TOKEN-WEIGHTED mean of the rates of the rows it
    folds up — weighted by GENERATED tokens, the very numerator the per-session
    rate is built from. Not the group's calendar span (idle time between and
    inside sessions does not belong in the denominator) and not a flat average
    of row rates (a 17-second session is not as informative as one that moved
    200k tokens)."""

    BASE = 1_764_000_000_000

    def session_dest(self, *rows):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_src(list(rows))
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def step_dest(self, sessions, steps):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(sessions, steps)
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def step(self, mid, sid, generated, span_ms, base=None, first_off=0,
             created=None):
        """One assistant step: `generated` tokens over `span_ms` (a single
        turn at `generated * 1000 / span_ms` tok/s)."""
        base = self.BASE if base is None else base
        return step_msg(mid, sid=sid,
                        created=base if created is None else created,
                        first=base + first_off,
                        streamed=base + first_off + span_ms,
                        out=generated, reasoning=0)

    def test_total_totals_rate_is_token_weighted_mean(self):
        dest = self.step_dest(
            [src_row(sid="s1", tin=1000, tout=200, created=self.BASE,
                     updated=self.BASE + 5_000),        # 240.0, 240 generated
             src_row(sid="s2", model=LEMONADE_JSON, tin=500, tout=50,
                     created=self.BASE + 60_000,
                     updated=self.BASE + 65_000)],      # 110.0, 550 generated
            [self.step("a1", "s1", 240, 1000),
             self.step("a2", "s2", 550, 5000)])
        # (240*240 + 110*550) / 790 = 149.5; a flat mean says 175.0 and
        # in+out weighting (1200/550) says 199.1. Generated tokens are the
        # weights because they are the numerator being averaged.
        self.assertAlmostEqual(total_totals(dest)[0]["tok_s"], 149.5, places=2)

    def test_total_totals_per_model_rate_is_that_models_weighted_mean(self):
        dest = self.step_dest(
            [src_row(sid="s1", tin=1000, tout=200, created=self.BASE,
                     updated=self.BASE + 5_000),        # 240.0, 240 generated
             src_row(sid="s2", tin=500, tout=50, created=self.BASE + 60_000,
                     updated=self.BASE + 65_000),       # 110.0, 550 generated
             src_row(sid="s3", model=LEMONADE_JSON, tin=90, tout=10,
                     created=self.BASE,
                     updated=self.BASE + 10_000),       # 10.0, 100 generated
             src_row(sid="s4", model=LEMONADE_JSON, tin=190, tout=10,
                     created=self.BASE + 30_000,
                     updated=self.BASE + 50_000)],      # 10.0, 200 generated
            [self.step("a1", "s1", 240, 1000),
             self.step("a2", "s2", 550, 5000),
             self.step("b1", "s3", 100, 10000),
             self.step("b2", "s4", 200, 20000)])
        rows = {r["model"]: r["tok_s"]
                for r in total_totals(dest, by_model=True)}
        self.assertAlmostEqual(rows["unsloth-studio/" + UNSLOTH_MODEL_ID],
                               149.5, places=2)
        self.assertAlmostEqual(rows["lemonade/gpt-oss-120b"], 10.0, places=2)

    def test_daily_totals_rate_is_weighted_mean_of_that_days_sessions(self):
        day = ms(2026, 11, 1)
        dest = self.step_dest(
            [src_row(sid="s1", tin=1000, tout=200, created=day + 1_000,
                     updated=day + 6_000),              # 240.0, 240 generated
             src_row(sid="s2", tin=4800, tout=200, created=day + 20_000,
                     updated=day + 70_000)],            # 100.0, 5000 generated
            [self.step("a1", "s1", 240, 1000, base=day),
             self.step("a2", "s2", 5000, 50000, base=day)])
        # (240*240 + 100*5000) / 5240 = 106.4; flat mean says 170.0.
        day_row = daily_totals(dest)[0]
        self.assertAlmostEqual(day_row["tok_s"], 106.4, places=2)
        self.assertEqual(day_row["rate_weight"], 240 + 5000)

    def test_sessions_without_a_rate_are_excluded_from_the_mean(self):
        # A session with no steps has no rate: it must not contribute a 0 that
        # halves the average of the sessions that do have one.
        dest = self.step_dest(
            [src_row(sid="s1", tin=1000, tout=200, created=self.BASE,
                     updated=self.BASE + 5_000),        # 240.0, 240 generated
             src_row(sid="s2", model=LEMONADE_JSON, tin=9999, tout=9999,
                     created=self.BASE + 60_000, updated=None)],
            [self.step("a1", "s1", 240, 1000)])
        row = total_totals(dest)[0]
        self.assertAlmostEqual(row["tok_s"], 240.0, places=2)
        # The rate-less session's 19,998 in+out tokens contribute to neither
        # side of the mean: the only contributing weight is s1's 240 generated.
        self.assertEqual(row["rate_weight"], 240)

    def test_zero_token_rows_carry_no_weight(self):
        # A step that generated no tokens yields no rate, so this row cannot
        # contribute to either side of the mean (no ZeroDivision).
        dest = self.step_dest(
            [src_row(sid="s1", tin=0, tout=0, created=self.BASE,
                     updated=self.BASE + 5_000),
             src_row(sid="s2", model=LEMONADE_JSON, tin=0, tout=0,
                     created=self.BASE + 60_000, updated=None)],
            [self.step("a1", "s1", 0, 5000)])
        self.assertIsNone(total_totals(dest)[0]["tok_s"])

    def test_group_without_any_rate_has_no_rate(self):
        dest = self.session_dest(
            src_row(sid="s1", created=self.BASE, updated=None),
            src_row(sid="s2", model=LEMONADE_JSON, created=self.BASE + 60_000,
                    updated=None))
        self.assertIsNone(total_totals(dest)[0]["tok_s"])
        self.assertIsNone(daily_totals(dest)[0]["tok_s"])

    def test_total_totals_rate_matches_the_sessions_view(self):
        # The grand-total TOK/S must be reproducible from the session rows'
        # own rates. Each session has TWO turns so its turn-mean differs from
        # tokens/(updated - created): a wall-clock-basis aggregate fails this
        # pin. Each session's generated tokens equal its in+out total, so the
        # displayed rows' TOTAL cell is the weight the aggregate must use.
        dest = make_dest()
        self.addCleanup(dest.close)
        sessions = [
            src_row(sid="s1", tin=1000, tout=200, created=self.BASE,
                    updated=self.BASE + 5_000),   # 240.0 wall; 200.0 turns
            src_row(sid="s2", tin=500, tout=50, created=self.BASE + 1_000,
                    updated=self.BASE + 6_000),   # 110.0 wall; 150.0 turns
            src_row(sid="s3", model=LEMONADE_JSON, tin=90, tout=10,
                    created=self.BASE + 90_000,
                    updated=self.BASE + 100_000), # 10.0 both ways
        ]
        steps = [
            self.step("s1a1", "s1", 600, 2000),                    # 300.0
            step_msg("s1i", sid="s1", kind="idle", created=self.BASE + 10),
            step_msg("s1u", sid="s1", kind="user", created=self.BASE + 20),
            self.step("s1a2", "s1", 600, 6000, first_off=10000,
                      created=self.BASE + 30),                     # 100.0
            self.step("s2a1", "s2", 275, 1000,
                      created=self.BASE + 1_000),                  # 275.0
            step_msg("s2i", sid="s2", kind="idle", created=self.BASE + 1_010),
            step_msg("s2u", sid="s2", kind="user", created=self.BASE + 1_020),
            self.step("s2a2", "s2", 275, 11000, first_off=10000,
                      created=self.BASE + 1_030),                  # 25.0
            self.step("s3a1", "s3", 100, 10000,
                      base=self.BASE + 90_000),                    # 10.0
        ]
        src = make_step_src(sessions, steps)
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        rows = query_sessions(dest)
        self.assertEqual(len(rows), 3)
        # (200*1200 + 150*550 + 10*100) / 1850 = 174.9; a wall-clock basis
        # would give 188.9.
        expected = round(sum(r["tok_s"] * r["total"] for r in rows)
                         / sum(r["total"] for r in rows), 1)
        self.assertAlmostEqual(total_totals(dest)[0]["tok_s"], expected,
                               places=2)

    def runs_dest(self, *rows):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_runs_src(list(rows))
        self.addCleanup(src.close)
        import_unsloth_runs(src, dest)
        return dest

    def test_total_runs_rate_is_token_weighted_mean(self):
        dest = self.runs_dest(
            run_row("r1", progress=200, started=self.BASE,
                    completed=self.BASE + 10_000),     # 200 tok / 10 s = 20.0
            run_row("r2", progress=100, started=self.BASE + 30_000,
                    completed=self.BASE + 31_000))     # 100 tok / 1 s = 100.0
        # (20*200 + 100*100) / 300 = 46.7; flat mean would say 60.0
        row = total_runs(dest)[0]
        self.assertAlmostEqual(row["tok_s"], 46.7, places=2)
        self.assertEqual(row["rate_weight"], 200 + 100)
        self.assertAlmostEqual(total_runs(dest, by_model=True)[0]["tok_s"],
                               46.7, places=2)
        self.assertEqual(total_runs(dest, by_model=True)[0]["rate_weight"],
                         200 + 100)

    def test_daily_runs_rate_is_token_weighted_mean(self):
        day = ms(2026, 11, 1)
        dest = self.runs_dest(
            run_row("r1", progress=200, started=day, completed=day + 10_000),
            run_row("r2", progress=100, started=day + 30_000,
                    completed=day + 31_000))
        row = daily_runs(dest)[0]
        self.assertAlmostEqual(row["tok_s"], 46.7, places=2)
        self.assertEqual(row["rate_weight"], 200 + 100)

    def test_runs_without_a_rate_are_excluded_from_the_mean(self):
        dest = self.runs_dest(
            run_row("r1", progress=200, started=self.BASE,
                    completed=self.BASE + 10_000),     # 20.0
            run_row("r2", progress=100, started=self.BASE + 30_000,
                    completed=self.BASE + 31_000),     # 100.0
            run_row("r3", progress=9999, started=self.BASE + 60_000,
                    completed=None))                   # unfinished: no rate
        row = total_runs(dest)[0]
        self.assertAlmostEqual(row["tok_s"], 46.7, places=2)
        # r3 is unfinished (no rate): its 9,999 progress tokens must not add
        # to the weight either.
        self.assertEqual(row["rate_weight"], 200 + 100)


class AggregateTurnWeightTests(unittest.TestCase):
    """Daily/total/hero TOK/S is the token-weighted mean of the session rates
    it folds, weighted by GENERATED tokens (the numerator being averaged), not
    by in+out."""

    BASE = ms(2026, 11, 16)

    def dest_with(self, sessions, steps):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(sessions, steps)
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    def turn(self, mid, sid, generated, span_ms):
        """One assistant step: `generated` tokens streamed over `span_ms`,
        i.e. a single turn at `generated * 1000 / span_ms` tok/s."""
        return step_msg(mid, sid=sid, first=self.BASE,
                        streamed=self.BASE + span_ms, out=generated,
                        reasoning=0)

    def two_rate_dest(self):
        # A: 100 generated tokens in 1.0 s = 100.0 tok/s (in+out = 100,100).
        # B: 1000 generated tokens in 20 s = 50.0 tok/s (in+out = 2,000).
        # Old in+out weight lets A dominate (99.0); generated weight gives A
        # only 100 of the 1,100 weight -> (100*100 + 50*1000)/1100 = 54.5.
        return self.dest_with(
            [src_row(sid="a", tin=100_000, tout=100, created=self.BASE,
                     updated=self.BASE + 5_000),
             src_row(sid="b", tin=1_000, tout=1_000, created=self.BASE,
                     updated=self.BASE + 5_000)],
            [self.turn("a1", "a", 100, 1000),
             self.turn("b1", "b", 1000, 20000)])

    def test_total_uses_generated_tokens_as_weight(self):
        self.assertEqual(total_totals(self.two_rate_dest())[0]["tok_s"], 54.5)

    def test_rateless_session_drops_out_of_both_sides(self):
        # A third session with no steps has no rate; its huge in+out must not
        # drag the mean down to it, nor dilute the two sessions that have one.
        dest = self.dest_with(
            [src_row(sid="a", tin=100_000, tout=100, created=self.BASE,
                     updated=self.BASE + 5_000),
             src_row(sid="b", tin=1_000, tout=1_000, created=self.BASE,
                     updated=self.BASE + 5_000),
             src_row(sid="c", tin=999_999, tout=999_999, created=self.BASE,
                     updated=self.BASE + 5_000)],
            [self.turn("a1", "a", 100, 1000),
             self.turn("b1", "b", 1000, 20000)])
        self.assertEqual(total_totals(dest)[0]["tok_s"], 54.5)

    def test_daily_matches_total_when_sessions_share_a_day(self):
        dest = self.two_rate_dest()
        daily = daily_totals(dest)
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["tok_s"], total_totals(dest)[0]["tok_s"])

    def test_hero_matches_the_table(self):
        rows = total_totals(self.two_rate_dest())
        table = _ANSI_RE.sub("", render_table(rows))
        hero = _ANSI_RE.sub("", "\n".join(report_header("", rows)))
        # The value the table actually renders for the TOK/S cell must be the
        # same number the hero quotes (not a re-derivation of the mean here).
        shown = f"{rows[0]['tok_s']:.1f}"
        self.assertIn(shown, table)
        self.assertIn(shown + " tok/s", hero)

    def test_hero_over_per_model_rows_reproduces_grand_total(self):
        # Two models, two sessions each, with GENUINELY DIFFERENT turn rates
        # AND each session's turn rate differing from its wall-clock value
        # (in+out tokens / (updated-created)). One step per session, so the
        # turn rate is generated*1000/span_ms. All arithmetic by hand:
        #   unsloth s1: 200/1.0s = 200.0  (wall 1200/4.0s = 300.0), gen 200
        #   unsloth s2: 400/4.0s = 100.0  (wall  550/5.0s = 110.0), gen 400
        #   lemonade s3: 100/1.0s = 100.0 (wall  100/2.0s =  50.0), gen 100
        #   lemonade s4: 300/2.0s = 150.0 (wall  200/4.0s =  50.0), gen 300
        #   unsloth:  (200*200 + 100*400)/600  = 80000/600  = 133.3
        #   lemonade: (100*100 + 150*300)/400  = 55000/400  = 137.5
        #   grand:    (200*200 + 100*400 + 100*100 + 150*300)/1000
        #                                        = 135000/1000 = 135.0
        # A wall-clock basis would give 212.4 instead — so a rate-basis
        # regression fails this pin. Weighting the per-model rows by in+out
        # instead of generated tokens also fails the rate_weight assertions.
        dest = self.dest_with(
            [src_row(sid="s1", tin=1000, tout=200, created=self.BASE,
                     updated=self.BASE + 4_000),
             src_row(sid="s2", tin=500, tout=50, created=self.BASE + 60_000,
                     updated=self.BASE + 65_000),
             src_row(sid="s3", model=LEMONADE_JSON, tin=90, tout=10,
                     created=self.BASE, updated=self.BASE + 2_000),
             src_row(sid="s4", model=LEMONADE_JSON, tin=190, tout=10,
                     created=self.BASE + 30_000, updated=self.BASE + 34_000)],
            [self.turn("a1", "s1", 200, 1000),
             self.turn("a2", "s2", 400, 4000),
             self.turn("a3", "s3", 100, 1000),
             self.turn("a4", "s4", 300, 2000)])
        grand = total_totals(dest)[0]["tok_s"]
        self.assertEqual(grand, 135.0)
        rows = {r["model"]: r for r in total_totals(dest, by_model=True)}
        unsloth = rows["unsloth-studio/" + UNSLOTH_MODEL_ID]
        lemonade = rows["lemonade/gpt-oss-120b"]
        self.assertEqual(unsloth["tok_s"], 133.3)
        self.assertEqual(lemonade["tok_s"], 137.5)
        # The weight each aggregate row carries is the Σ generated tokens of
        # its rate-bearing sessions — NOT its in+out total.
        self.assertEqual(unsloth["rate_weight"], 600)
        self.assertEqual(lemonade["rate_weight"], 400)
        self.assertNotEqual(unsloth["rate_weight"], unsloth["total"])
        self.assertNotEqual(lemonade["rate_weight"], lemonade["total"])
        hero = _ANSI_RE.sub("", "\n".join(
            report_header("", list(rows.values()))))
        shown = re.search(r"(-?\d+\.\d+) tok/s", hero)
        self.assertIsNotNone(shown)
        self.assertLessEqual(abs(float(shown.group(1)) - grand), 0.1)

    def runs_dest(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_runs_src([
            run_row("r1", progress=243, started=self.BASE,
                    completed=self.BASE + 16_082),
            run_row("r2", progress=51, started=self.BASE + 30_000,
                    completed=self.BASE + 30_000 + 4_911),
        ])
        self.addCleanup(src.close)
        import_unsloth_runs(src, dest)
        return dest

    def test_run_rows_carry_progress_as_rate_weight(self):
        rows = query_runs(self.runs_dest())
        self.assertEqual([r["rate_weight"] for r in rows], [243, 51])
        # The key restates the weight; it must not move any displayed number.
        self.assertEqual([r["rate_weight"] for r in rows],
                         [r["total"] for r in rows])

    def test_runs_hero_unchanged(self):
        rows = query_runs(self.runs_dest())
        hero = _ANSI_RE.sub("", "\n".join(report_header("", rows)))
        self.assertIn("14.3 tok/s", hero)


class HeroTokSTests(unittest.TestCase):
    """The hero carries the same token-weighted TOK/S as the table under it,
    and says nothing when none of its rows has a rate."""

    # rate_weight differs from total on purpose: the hero must use the rows'
    # own generated-token weight, not their in+out total.
    ROWS = [{"total": 1200, "tokens_input": 1000, "tokens_output": 200,
             "rate_weight": 240, "tok_s": 240.0, "date": "2026-11-16"},
            {"total": 550, "tokens_input": 500, "tokens_output": 50,
             "rate_weight": 1100, "tok_s": 110.0, "date": "2026-11-17"}]

    def hero(self, **kw):
        return _ANSI_RE.sub("", "\n".join(report_header("x", self.ROWS, **kw)))

    def test_hero_shows_token_weighted_tok_s(self):
        # (240*240 + 110*1100) / 1340 = 133.3; in+out weight would say 199.1.
        self.assertIn("133.3 tok/s", self.hero())

    def test_hero_falls_back_to_total_without_rate_weight(self):
        # Event/reconcile rows carry no rate_weight: their hero must still use
        # in+out total exactly as before.
        rows = [{k: v for k, v in r.items() if k != "rate_weight"}
                for r in self.ROWS]
        text = _ANSI_RE.sub("", "\n".join(report_header("x", rows)))
        self.assertIn("199.1 tok/s", text)

    def test_hero_omits_tok_s_when_no_row_has_a_rate(self):
        rows = [{"total": 300, "tokens_input": 200, "tokens_output": 100,
                 "tok_s": None, "date": "2026-11-16"}]
        text = _ANSI_RE.sub("", "\n".join(report_header("x", rows)))
        self.assertNotIn("tok/s", text)
        self.assertIn("300 tokens", text)

    def test_hero_omits_tok_s_for_rows_without_the_key(self):
        # reconcile-shaped rows and the older hero fixtures carry no tok_s.
        rows = [{"total": 300, "tokens_input": 200, "tokens_output": 100,
                 "date": "2026-11-16"}]
        self.assertNotIn("tok/s",
                         _ANSI_RE.sub("", "\n".join(report_header("x", rows))))

    def test_hero_tok_s_sits_between_out_and_cost(self):
        text = self.hero(cost_est=123.456)
        self.assertIn("tok/s", text)
        self.assertLess(text.index("out"), text.index("tok/s"))
        self.assertLess(text.index("tok/s"), text.index("~$"))

    def test_hero_real_cost_tail_sits_after_estimate(self):
        text = self.hero(cost_est=1.0, real_cost=5.0)
        self.assertIn("$5.00", text)
        self.assertLess(text.index("~$1.00"), text.index("$5.00"))

    def test_hero_tok_s_painted_accent(self):
        hero = report_header("x", self.ROWS)[1]
        r, g, b = NEON["accent"]
        self.assertIn(f"\x1b[38;2;{r};{g};{b}m133.3\x1b[0m", hero)

    def test_hero_unit_words_are_not_painted(self):
        # "tokens"/"in"/"out" render in the terminal's own color; the unit
        # words of the newer stats must join them, not wear their number's hue.
        hero = report_header("x", self.ROWS)[1]
        self.assertIn("\x1b[0m tok/s", hero)
        self.assertNotIn("tok/s\x1b[0m", hero)
        self.assertIn("\x1b[0m rows", hero)

    def test_hero_tok_s_stays_plain_when_color_is_off(self):
        lines = report_header("x", self.ROWS, color=False)
        self.assertNotIn("\x1b", "\n".join(lines))
        self.assertIn("133.3 tok/s", lines[1])


class ParseDateTests(unittest.TestCase):
    def test_parse_date_accepts_good_value(self):
        self.assertEqual(parse_date("2026-11-01"), ms(2026, 11, 1))

    def test_parse_date_rejects_bad_value(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_date("13-2026")


class RenderTableTests(unittest.TestCase):
    ROW = {"date": "2026-11-01", "model": "unsloth-studio/unsloth/Qwen3.8",
           "tokens_input": 12345, "tokens_output": 678, "total": 13023,
           "cost": 0.0123, "title": "a session title"}

    def test_render_table_smoke(self):
        out = render_table([self.ROW])
        lines = out.splitlines()
        self.assertIn("DATE", lines[0])
        self.assertTrue(any("unsloth-studio/unsloth/Qwen3.8" in ln
                            for ln in lines[1:]))
        self.assertTrue(any("12,345" in ln for ln in lines[1:]))

    def test_render_table_empty(self):
        self.assertIn("no sessions match", render_table([]))


def make_file_db(path, rows=()):
    """A real on-disk fake source DB (for CLI-level tests)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE session_v2 (
               id TEXT PRIMARY KEY, model TEXT,
               tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
               tokens_cache_read INTEGER, tokens_cache_write INTEGER, cost REAL,
               title TEXT, directory TEXT, agent TEXT,
               time_created INTEGER, time_updated INTEGER)"""
    )
    for row in rows:
        conn.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    return conn


class DailyTotalsTests(unittest.TestCase):
    def test_daily_totals_groups_by_date(self):
        src = make_src([
            src_row(sid="a1", tin=1000, tout=100, created=ms(2026, 11, 1)),
            src_row(sid="a2", tin=2000, tout=200, created=ms(2026, 11, 1)),
            src_row(sid="b1", tin=500, tout=50, created=ms(2026, 11, 5)),
        ])
        self.addCleanup(src.close)
        dest = make_dest()
        self.addCleanup(dest.close)
        record(src, dest, now_ms=1)
        days = daily_totals(dest)
        self.assertEqual([d["date"] for d in days], ["2026-11-01", "2026-11-05"])
        first = days[0]
        self.assertEqual(first["sessions"], 2)
        self.assertEqual(first["tokens_input"], 3000)
        self.assertEqual(first["tokens_output"], 300)
        self.assertEqual(first["total"], 3300)


# Shared CLI harness: every CLI test class runs main() with both streams
# captured, and the colored ones fake a tty through the same gate. Kept in one
# place so a change to how a run is captured is one edit, not twelve.
class CliHarness(unittest.TestCase):
    """Base for CLI tests: run_main captures stdout/stderr; colored_main also
    forces the color gate on (the in-process stand-in for a terminal)."""

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def colored_main(self, argv):
        with unittest.mock.patch.object(tokenstat, "_color_enabled",
                                        return_value=True):
            return self.run_main(argv)


def rgbs(bar):
    """The 24-bit foreground triples painted into a bar, left to right."""
    return [tuple(int(g) for g in m) for m in
            re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", bar)]


class CliTests(CliHarness):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_implicit_record_fallback_missing_src(self):
        code, out, err = self.run_main(
            ["--db", self.db, "--model", "x", "--src",
             os.path.join(self.tmp.name, "nope.db")])
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)
        self.assertIn("no sessions match", out)

    def test_record_subcommand_prints_counts(self):
        self.addCleanup(make_file_db(self.src, [src_row()]).close)
        code, out, _ = self.run_main(
            ["record", "--db", self.db, "--src", self.src])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(),
                         r"inserted=\d+ updated=\d+ unchanged=\d+")

    def test_record_then_query_implicit_records(self):
        self.addCleanup(make_file_db(self.src, [src_row()]).close)
        code, out, err = self.run_main(
            ["--db", self.db, "--src", self.src])
        self.assertEqual(code, 0)
        self.assertNotIn("warning:", err)
        self.assertIn("unsloth-studio", out)

    def test_json_output_parses(self):
        self.addCleanup(make_file_db(
            self.src, [src_row(sid="a1", created=ms(2026, 11, 1)),
                       src_row(sid="a2", created=ms(2026, 11, 1))]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--json", "--daily"])
        self.assertEqual(code, 0)
        days = json.loads(out)
        self.assertEqual(len(days), 1)
        self.assertEqual(sorted(days[0].keys()),
                         sorted(["date", "sessions", "tokens_input",
                                 "tokens_output", "total", "cost", "tok_s",
                                 "rate_weight", "tokens_cache_read",
                                 "tokens_cache_write"]))


class ReviewFixTests(unittest.TestCase):
    """Regression pins for findings from the whole-branch review."""

    def setUp(self):
        src = make_src([
            src_row(sid="big", tin=5000, tout=900, created=ms(2026, 11, 1)),
            src_row(sid="tiny", tin=100, tout=5, created=ms(2026, 11, 5)),
        ])
        self.src = src
        self.addCleanup(src.close)
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        record(src, self.dest, now_ms=1)

    def test_daily_totals_applies_token_thresholds(self):
        days = daily_totals(self.dest, min_out=100)
        self.assertEqual([d["date"] for d in days], ["2026-11-01"])
        days = daily_totals(self.dest, min_in=1000)
        self.assertEqual([d["date"] for d in days], ["2026-11-01"])

    def test_record_keeps_labels_when_source_nulls_them(self):
        self.src.execute("UPDATE session_v2 SET model = NULL, title = NULL"
                         " WHERE id = 'big'")
        record(self.src, self.dest, now_ms=2)
        row = self.dest.execute(
            "SELECT provider, model_id, title FROM usage"
            " WHERE session_id = 'big'").fetchone()
        self.assertEqual(row[0], "unsloth-studio")
        self.assertEqual(row[1], "unsloth/Qwen3.8-Flash-Next-GGUF")
        self.assertEqual(row[2], "a session")

    def test_until_includes_whole_local_day_on_dst_fall_back(self):
        # 2026-11-01 is a 25-hour local day here (fall back); a session at
        # 23:30 local must be inside --until 2026-11-01.
        late = int(datetime.datetime(2026, 11, 1, 23, 30).timestamp() * 1000)
        self.src.execute("INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         src_row(sid="late", tin=77, created=late))
        record(self.src, self.dest, now_ms=2)
        rows = query_sessions(self.dest, since_ms=ms(2026, 11, 1),
                              until_ms=ms(2026, 11, 1))
        self.assertIn(77, [r["tokens_input"] for r in rows])

    def test_report_totals_stable_across_repeated_records(self):
        before = sum(r["total"] for r in query_sessions(self.dest))
        record(self.src, self.dest, now_ms=2)
        record(self.src, self.dest, now_ms=3)
        after = sum(r["total"] for r in query_sessions(self.dest))
        self.assertEqual(before, after)


class StoreErrorTests(unittest.TestCase):
    def test_unopenable_store_warns_and_exits_nonzero_no_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--db", "/nonexistent-dir-xyz/usage.db"])
        self.assertNotEqual(code, 0)
        self.assertIn("warning:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


def make_unsloth_src(rows=()):
    """Fake Unsloth Studio DB with the api_usage_events columns we read."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE api_usage_events (
               id TEXT PRIMARY KEY, subject TEXT, endpoint TEXT, model TEXT,
               status TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
               total_tokens INTEGER, created_at INTEGER)"""
    )
    for row in rows:
        conn.execute("INSERT INTO api_usage_events VALUES (?,?,?,?,?,?,?,?,?)", row)
    return conn


def unsloth_event(eid="e1", model=UNSLOTH_MODEL_ID, prompt=1000, completion=50,
                  created=1_764_000_000_000):
    return (eid, "unsloth", "/v1/chat/completions", model, "completed",
            prompt, completion, prompt + completion, created)


def make_file_unsloth_db(path, rows=()):
    """On-disk fake Unsloth Studio DB (for CLI-level tests)."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE api_usage_events (
               id TEXT PRIMARY KEY, subject TEXT, endpoint TEXT, model TEXT,
               status TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
               total_tokens INTEGER, created_at INTEGER)"""
    )
    for row in rows:
        conn.execute("INSERT INTO api_usage_events VALUES (?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    return conn


def make_runs_src(rows=()):
    """Fake Studio DB with the chat_generation_runs columns we read."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE chat_generation_runs (
           id TEXT PRIMARY KEY, request_json TEXT, progress_tokens INTEGER,
           started_at INTEGER, completed_at INTEGER, status TEXT)""")
    for r in rows:
        conn.execute("INSERT INTO chat_generation_runs VALUES (?,?,?,?,?,?)", r)
    return conn


def run_row(rid="r1", model="unsloth/Ornith-1.5-122B-GGUF", progress=100,
            started=1_764_000_000_000, completed=1_764_000_010_000,
            status="completed"):
    return (rid, json.dumps({"model": model}), progress, started, completed,
            status)


class ImportRunsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)

    def stored(self, rid="r1"):
        return self.dest.execute(
            "SELECT model, progress_tokens FROM unsloth_runs WHERE run_id = ?",
            (rid,)).fetchone()

    def test_ensure_schema_has_unsloth_runs(self):
        cols = [c[1] for c in self.dest.execute("PRAGMA table_info(unsloth_runs)")]
        self.assertEqual(cols, ["run_id", "model", "progress_tokens",
                                "started_at", "completed_at", "status"])

    def test_import_runs_inserts_then_is_idempotent(self):
        src = make_runs_src([run_row("r1"), run_row("r2", progress=20)])
        self.addCleanup(src.close)
        first = import_unsloth_runs(src, self.dest)
        self.assertEqual(first, {"inserted": 2, "updated": 0, "unchanged": 0})
        second = import_unsloth_runs(src, self.dest)
        self.assertEqual(second, {"inserted": 0, "updated": 0, "unchanged": 2})

    def test_import_runs_extracts_model_from_request_json(self):
        src = make_runs_src([run_row("r1")])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)
        self.assertEqual(self.stored()[0], "unsloth/Ornith-1.5-122B-GGUF")

    def test_import_runs_never_shrinks_on_regression(self):
        src = make_runs_src([run_row("r1", progress=100)])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)
        src.execute("UPDATE chat_generation_runs SET progress_tokens = 50"
                    " WHERE id = 'r1'")
        import_unsloth_runs(src, self.dest)
        self.assertEqual(self.stored()[1], 100)

    def test_import_runs_missing_table_is_noop(self):
        conn = sqlite3.connect(":memory:")  # no chat_generation_runs table
        self.addCleanup(conn.close)
        self.assertEqual(import_unsloth_runs(conn, self.dest),
                         {"inserted": 0, "updated": 0, "unchanged": 0})

    def test_import_runs_non_dict_request_json_keeps_row_blank_model(self):
        # Valid JSON that is NOT an object ('null', '[1,2]') is an EXTRACTION
        # failure, not a row error (mirrors parse_model): model becomes "" and
        # the row still inserts — its progress/start are valid data.
        src = make_runs_src([
            ("r_null", 'null', 42, 1_764_000_000_000, 1_764_000_010_000,
             "completed"),
            ("r_list", '[1,2]', 7, 1_764_000_000_000, 1_764_000_010_000,
             "completed"),
        ])
        self.addCleanup(src.close)
        counts = import_unsloth_runs(src, self.dest)
        self.assertEqual(counts["inserted"], 2)
        self.assertEqual(self.stored("r_null"), ("", 42))
        self.assertEqual(self.stored("r_list"), ("", 7))


class RunsQueryTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_runs_src([
            run_row("r1", model="unsloth/Qwen3.8-Flash-Next-GGUF", progress=243,
                    started=ms(2026, 11, 1),
                    completed=ms(2026, 11, 1) + 16082),
            run_row("r2", model="unsloth/Ornith-1.5-122B-GGUF", progress=51,
                    started=ms(2026, 11, 5), completed=ms(2026, 11, 5) + 4911),
        ])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)

    def test_query_runs_reports_progress_and_rate(self):
        rows = query_runs(self.dest)
        self.assertEqual([r["progress_tokens"] for r in rows], [243, 51])
        # tok_s is rounded to one decimal (per the run-rate contract), so
        # compare against the rounded rate, not the raw quotient.
        self.assertAlmostEqual(rows[0]["tok_s"], round(243 / 16.082, 1))

    def test_query_runs_blank_rate_when_no_duration(self):
        src = make_runs_src([run_row("r3", model="rate-blank-probe",
                                     started=1_764_000_000_000,
                                     completed=None)])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)
        rows = query_runs(self.dest, model="rate-blank-probe")
        self.assertIsNone(rows[0]["tok_s"])

    def test_query_runs_blank_rate_when_status_not_completed(self):
        # A failed run that recorded completed_at must NOT render a rate:
        # TOK/S is blank when the run hasn't completed (spec), so the gate
        # is completed_at set AND status == "completed".
        src = make_runs_src([run_row("r4", model="failed-probe",
                                     progress=500,
                                     started=1_764_000_000_000,
                                     completed=1_764_000_010_000,
                                     status="failed")])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)
        rows = query_runs(self.dest, model="failed-probe")
        self.assertIsNone(rows[0]["tok_s"])

    def test_daily_runs_groups_by_started_day(self):
        days = daily_runs(self.dest)
        self.assertEqual([d["date"] for d in days], ["2026-11-01", "2026-11-05"])

    def test_total_runs_per_model(self):
        rows = total_runs(self.dest, by_model=True)
        self.assertEqual({r["model"] for r in rows},
                         {"unsloth/Ornith-1.5-122B-GGUF",
                          "unsloth/Qwen3.8-Flash-Next-GGUF"})


class ImportUnslothTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)

    def stored_total(self):
        row = self.dest.execute(
            "SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens)"
            " FROM unsloth_events").fetchone()
        return row if row[0] else (0, 0, 0)

    def test_import_inserts_then_is_idempotent(self):
        src = make_unsloth_src([unsloth_event("e1"), unsloth_event("e2", prompt=20)])
        self.addCleanup(src.close)
        first = import_unsloth(src, self.dest)
        self.assertEqual(first, {"inserted": 2, "updated": 0, "unchanged": 0})
        second = import_unsloth(src, self.dest)
        self.assertEqual(second, {"inserted": 0, "updated": 0, "unchanged": 2})
        self.assertEqual(self.stored_total(), (2, 1020, 100))

    def test_import_never_shrinks_on_source_regression(self):
        src = make_unsloth_src([unsloth_event("e1", prompt=100)])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        src.execute("UPDATE api_usage_events SET prompt_tokens = 50 WHERE id='e1'")
        import_unsloth(src, self.dest)
        stored = self.dest.execute(
            "SELECT prompt_tokens FROM unsloth_events"
            " WHERE event_id='e1'").fetchone()
        self.assertEqual(stored[0], 100)

    def test_query_events_filters(self):
        src = make_unsloth_src([
            unsloth_event("e1", created=ms(2026, 11, 1)),
            unsloth_event("e2", model="unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
                          prompt=50, completion=7, created=ms(2026, 11, 5)),
        ])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        rows = query_events(self.dest, model="qwen3-coder")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens_output"], 7)  # completion maps to OUT
        both = query_events(self.dest, since_ms=ms(2026, 11, 1),
                            until_ms=ms(2026, 11, 5))
        self.assertEqual(len(both), 2)
        only_big = query_events(self.dest, min_in=500)
        self.assertEqual([r["date"] for r in only_big], ["2026-11-01"])

    def test_daily_events_groups_by_request_date(self):
        src = make_unsloth_src([
            unsloth_event("e1", prompt=100, completion=1, created=ms(2026, 11, 1)),
            unsloth_event("e2", prompt=200, completion=2, created=ms(2026, 11, 1)),
            unsloth_event("e3", prompt=400, completion=4, created=ms(2026, 11, 5)),
        ])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        days = daily_events(self.dest)
        self.assertEqual([d["date"] for d in days], ["2026-11-01", "2026-11-05"])
        self.assertEqual(days[0]["sessions"], 2)  # request count under same key
        self.assertEqual(days[0]["tokens_input"], 300)
        self.assertEqual(days[0]["total"], 303)

    def test_reconcile_computes_gap(self):
        osrc = make_src([src_row(sid="s1", tin=1000, tcr=5000, tcw=0, tout=200)])
        self.addCleanup(osrc.close)
        record(osrc, self.dest, now_ms=1)
        usrc = make_unsloth_src([
            unsloth_event("e1", prompt=9000, completion=300),
            unsloth_event("e2", prompt=1000, completion=100),
        ])
        self.addCleanup(usrc.close)
        import_unsloth(usrc, self.dest)
        rows = reconcile(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], UNSLOTH_MODEL_ID)
        self.assertEqual(r["attributed_in"], 6000)   # in + cache_read
        self.assertEqual(r["reported_prompt"], 10000)
        self.assertEqual(r["gap_prompt"], 4000)      # lost to pruning
        self.assertEqual(r["attributed_out"], 250)  # out + reasoning (200+50)
        self.assertEqual(r["reported_completion"], 400)
        self.assertEqual(r["gap_completion"], 150)  # 400 - (200 out + 50 reasoning)


def make_file_runs_db(path, events=(), runs=()):
    """On-disk fake Studio DB holding both tables we ingest."""
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE api_usage_events (
           id TEXT PRIMARY KEY, subject TEXT, endpoint TEXT, model TEXT,
           status TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
           total_tokens INTEGER, created_at INTEGER)""")
    conn.execute("""CREATE TABLE chat_generation_runs (
           id TEXT PRIMARY KEY, request_json TEXT, progress_tokens INTEGER,
           started_at INTEGER, completed_at INTEGER, status TEXT)""")
    for r in events:
        conn.execute("INSERT INTO api_usage_events VALUES (?,?,?,?,?,?,?,?,?)", r)
    for r in runs:
        conn.execute("INSERT INTO chat_generation_runs VALUES (?,?,?,?,?,?)", r)
    conn.commit()
    return conn


class CliUnslothTests(CliHarness):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.udb = os.path.join(self.tmp.name, "studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_unsloth_subcommand_prints_counts(self):
        self.addCleanup(make_file_unsloth_db(
            self.udb, [unsloth_event("e1")]).close)
        code, out, _ = self.run_main(
            ["import-unsloth", "--db", self.db, "--unsloth-db", self.udb])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(),
                         r"inserted=\d+ updated=\d+ unchanged=\d+")

    def test_import_unsloth_subcommand_also_imports_runs(self):
        self.addCleanup(make_file_runs_db(
            self.udb, events=[unsloth_event("e1")],
            runs=[run_row("r1")]).close)
        code, out, _ = self.run_main(
            ["import-unsloth", "--db", self.db, "--unsloth-db", self.udb])
        self.assertEqual(code, 0)
        probe = sqlite3.connect(self.db)
        self.addCleanup(probe.close)
        stored = probe.execute(
            "SELECT COUNT(*) FROM unsloth_runs").fetchone()[0]
        self.assertEqual(stored, 1)

    def test_from_unsloth_query_renders_events(self):
        self.addCleanup(make_file_unsloth_db(self.udb, [unsloth_event("e1")]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-unsloth"])
        self.assertEqual(code, 0)
        self.assertIn(UNSLOTH_MODEL_ID, out)

    def test_from_runs_renders_runs(self):
        self.addCleanup(make_file_runs_db(self.udb, runs=[run_row("r1")]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-runs"])
        self.assertEqual(code, 0)
        self.assertIn("TOK/S", out.splitlines()[0])

    def test_from_runs_with_cost_renders_dollar_column(self):
        # composition pin: --from-runs x --cost (run rows price output-only)
        self.addCleanup(make_file_runs_db(self.udb, runs=[run_row("r1")]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-runs", "--cost"])
        self.assertEqual(code, 0)
        self.assertIn("COST", out)
        self.assertIn("$", out)

    def test_from_runs_with_daily_renders_table(self):
        # composition pin: --from-runs x --daily (closes a coverage gap)
        self.addCleanup(make_file_runs_db(self.udb, runs=[run_row("r1")]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-runs", "--daily"])
        self.assertEqual(code, 0)
        self.assertIn("DATE", out.splitlines()[0])

    def test_from_runs_rejects_min_flags(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
                      "--unsloth-db", self.udb, "--from-runs", "--min-in", "10"])
        self.assertEqual(cm.exception.code, 2)

    def test_reconcile_subcommand(self):
        self.addCleanup(make_file_unsloth_db(self.udb, [unsloth_event("e1")]).close)
        code, out, _ = self.run_main(
            ["reconcile", "--db", self.db, "--src",
             os.path.join(self.tmp.name, "none.db"), "--unsloth-db", self.udb])
        self.assertEqual(code, 0)
        self.assertIn(UNSLOTH_MODEL_ID, out)

    def test_implicit_unsloth_import_falls_back_when_missing(self):
        code, out, err = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", os.path.join(self.tmp.name, "no-studio.db")])
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)


class ReviewFixTests2(unittest.TestCase):
    """Regression pins for whole-branch review of increment 2."""

    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)

    def test_reconcile_one_sided_models_both_directions(self):
        # usage-only model: negative gap (OpenCode counted what Studio's log lacks)
        osrc = make_src([src_row(sid="u1", model=LEMONADE_JSON, tin=1500, tout=30)])
        self.addCleanup(osrc.close)
        record(osrc, self.dest, now_ms=1)  # lemonade: not in unsloth side at all
        # unsloth-only model
        usrc = make_unsloth_src([unsloth_event("e1", prompt=900, completion=9)])
        self.addCleanup(usrc.close)
        import_unsloth(usrc, self.dest)
        rows = {r["model"]: r for r in reconcile(self.dest)}
        u = rows[UNSLOTH_MODEL_ID]
        self.assertEqual((u["sessions"], u["attributed_in"]), (0, 0))
        self.assertEqual(u["reported_prompt"], 900)
        self.assertEqual(u["gap_prompt"], 900)

    def test_import_skips_bad_row_keeps_good_rows(self):
        src = make_unsloth_src([unsloth_event("good", prompt=100)])
        self.addCleanup(src.close)
        src.execute("INSERT INTO api_usage_events VALUES ('bad','unsloth','/x',"
                    "'m','completed',5,5,10,NULL)")
        counts = import_unsloth(src, self.dest)
        self.assertEqual(counts.get("skipped"), 1)
        self.assertEqual(counts["inserted"], 1)
        stored = self.dest.execute(
            "SELECT COUNT(*), SUM(prompt_tokens) FROM unsloth_events").fetchone()
        self.assertEqual(stored, (1, 100))  # good row persisted despite bad one

    def test_event_filter_accepts_provider_prefixed_string(self):
        src = make_unsloth_src([unsloth_event("e1")])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        # the string shown in session reports must also work in event mode
        self.assertEqual(len(query_events(self.dest, model="unsloth-studio")), 1)
        self.assertEqual(len(query_events(self.dest, model="qwen3.8")), 1)

    def test_event_until_dst_boundary(self):
        late = int(datetime.datetime(2026, 11, 1, 23, 30).timestamp() * 1000)
        src = make_unsloth_src([unsloth_event("e1", created=late)])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        rows = query_events(self.dest, since_ms=ms(2026, 11, 1),
                            until_ms=ms(2026, 11, 1))
        self.assertEqual(len(rows), 1)

    def test_report_stable_across_repeated_imports(self):
        src = make_unsloth_src([unsloth_event("e1", prompt=500),
                                unsloth_event("e2", prompt=7)])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        before = sum(r["total"] for r in query_events(self.dest))
        import_unsloth(src, self.dest)
        after = sum(r["total"] for r in query_events(self.dest))
        self.assertEqual(before, after)

    def test_reconcile_rejects_unsupported_flags(self):
        # An explicit --db keeps this hermetic: the default store now lives in
        # ~/.tokenstat, which only exists once the migration has run, and an
        # unopenable store exits 1 at open time — before this guard's exit 2.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                main(["--db", os.path.join(tmp, "usage.db"),
                      "reconcile", "--since", "2026-11-01"])
        self.assertEqual(cm.exception.code, 2)


class CliUnslothFailTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_import_unsloth_fails_nonzero_when_missing(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["import-unsloth", "--db",
                         os.path.join(self.tmp.name, "usage.db"),
                         "--unsloth-db", os.path.join(self.tmp.name, "none.db")])
        self.assertEqual(code, 1)
        self.assertIn("warning:", err.getvalue())


class TotalTotalsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_src([
            src_row(sid="s1", tin=1000, tout=200, cost=0.01),          # unsloth-studio
            src_row(sid="s2", model=LEMONADE_JSON, tin=500, tout=50,
                    cost=0.02),                                        # lemonade
        ])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)

    def test_total_totals_single_grand_row(self):
        rows = total_totals(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1500, 250, 1750))
        self.assertAlmostEqual(r["cost"], 0.03)

    def test_total_totals_per_model_when_filtered(self):
        src = make_src([src_row(sid="s3", model=LEMONADE_JSON, tin=10, tout=1,
                                cost=0.0)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=2)
        rows = total_totals(self.dest, model="lemonade")
        self.assertEqual([r["model"] for r in rows], ["lemonade/gpt-oss-120b"])
        self.assertEqual((rows[0]["sessions"], rows[0]["tokens_input"]), (2, 510))

    def test_total_totals_keeps_same_model_id_separate_per_provider(self):
        a = '{"id":"m1","providerID":"prov-a"}'
        b = '{"id":"m1","providerID":"prov-b"}'
        src = make_src([src_row(sid="p1", model=a), src_row(sid="p2", model=b)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=2)
        rows = total_totals(self.dest, model="m1")
        self.assertEqual(sorted(r["model"] for r in rows), ["prov-a/m1", "prov-b/m1"])

    def test_total_totals_filters_apply_before_summing(self):
        src = make_src([src_row(sid="d1", created=ms(2026, 11, 1), tin=9000),
                        src_row(sid="d2", created=ms(2026, 11, 5), tin=7)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=2)
        r = total_totals(self.dest, since_ms=ms(2026, 11, 1),
                         until_ms=ms(2026, 11, 1))[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (1, 9000))
        # threshold pre-filters individual sessions, not the aggregate:
        # s1 has tin>=1000; s2/d2 excluded by both date and threshold
        r = total_totals(self.dest, min_in=1000)[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (2, 1000 + 9000))

    def test_total_totals_empty_result(self):
        self.assertEqual(total_totals(self.dest, model="nope"), [])
        empty = make_dest()
        self.addCleanup(empty.close)
        self.assertEqual(total_totals(empty), [])


    def test_total_totals_by_model_without_filter(self):
        rows = total_totals(self.dest, by_model=True)
        self.assertEqual([r["model"] for r in rows],
                         ["lemonade/gpt-oss-120b",
                          "unsloth-studio/unsloth/Qwen3.8-Flash-Next-GGUF"])
        self.assertEqual({r["sessions"] for r in rows}, {1})


class SessionLimitTests(unittest.TestCase):
    """`limit` caps the set to the N most recent sessions (by time_created,
    ties broken by session_id), applied AFTER the other filters."""

    def store(self, rows):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_src(rows)
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    @staticmethod
    def five():
        """Five sessions with distinct time_created values (oldest first)."""
        return [
            src_row(sid="c1", title="oldest", created=ms(2026, 11, 1)),
            src_row(sid="c2", title="second", created=ms(2026, 11, 2)),
            src_row(sid="c3", title="third", created=ms(2026, 11, 3)),
            src_row(sid="c4", title="fourth", created=ms(2026, 11, 4)),
            src_row(sid="c5", title="newest", created=ms(2026, 11, 5)),
        ]

    @staticmethod
    def titles(rows):
        return [r["title"] for r in rows]

    @staticmethod
    def mixed_models():
        """Five sessions: three provA, two provB; the overall two most recent
        are a-newest (provA) and b-newest (provB)."""
        prov_a = '{"id":"modA","providerID":"provA"}'
        prov_b = '{"id":"modB","providerID":"provB"}'
        return [
            src_row(sid="a1", model=prov_a, title="a-old",
                    created=ms(2026, 11, 1)),
            src_row(sid="a2", model=prov_a, title="a-mid",
                    created=ms(2026, 11, 2)),
            src_row(sid="b1", model=prov_b, title="b-newer",
                    created=ms(2026, 11, 3)),
            src_row(sid="b2", model=prov_b, title="b-newest",
                    created=ms(2026, 11, 4)),
            src_row(sid="a3", model=prov_a, title="a-newest",
                    created=ms(2026, 11, 5)),
        ]

    def test_no_limit_returns_every_session_oldest_first(self):
        dest = self.store(self.five())
        expected = ["oldest", "second", "third", "fourth", "newest"]
        self.assertEqual(self.titles(query_sessions(dest, limit=None)),
                         expected)
        # limit=0 means "no cap" too — identical result set and order
        self.assertEqual(self.titles(query_sessions(dest, limit=0)),
                         self.titles(query_sessions(dest)))

    def test_limit_two_returns_two_newest_oldest_first(self):
        dest = self.store(self.five())
        # oldest-first order is preserved so the caller can reverse for display
        self.assertEqual(self.titles(query_sessions(dest, limit=2)),
                         ["fourth", "newest"])

    def test_limit_larger_than_population_returns_everything(self):
        dest = self.store(self.five())
        self.assertEqual(len(query_sessions(dest, limit=100)), 5)

    def test_limit_applies_after_other_filters(self):
        dest = self.store(self.mixed_models())
        # the two most recent sessions OVERALL are a-newest and b-newest; the
        # cap must count survivors of the model filter, so b-newest is skipped
        self.assertEqual(
            self.titles(query_sessions(dest, model="provA", limit=2)),
            ["a-mid", "a-newest"])

    def test_filter_and_cap_bind_in_order(self):
        # the same call as above: every returned row must actually be provA —
        # a mis-ordered positional bind raises, or returns the wrong set
        dest = self.store(self.mixed_models())
        rows_out = query_sessions(dest, model="provA", limit=2)
        self.assertEqual(len(rows_out), 2)
        for row in rows_out:
            self.assertIn("provA", row["model"])

    def test_ties_break_on_session_id(self):
        # two sessions share time_created; the cap boundary lands on the tie,
        # so the smaller session_id wins — deterministically across calls
        tie = ms(2026, 11, 3)
        dest = self.store([
            src_row(sid="tie_b", title="later-tie", created=tie),
            src_row(sid="tie_a", title="earlier-tie", created=tie),
        ])
        first = self.titles(query_sessions(dest, limit=1))
        self.assertEqual(first, ["earlier-tie"])
        self.assertEqual(self.titles(query_sessions(dest, limit=1)), first)


class AggregateLimitTests(unittest.TestCase):
    """The cap reaches the aggregate views: daily_totals and total_totals see
    only the capped session set, and the rate side and sum side agree."""

    def store(self, rows, messages):
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_step_src(rows, messages)
        self.addCleanup(src.close)
        record(src, dest, now_ms=1)
        return dest

    @staticmethod
    def fixture():
        """Four sessions across two days and two models, each with one valid
        step row (so every session has a rate and a rate_weight of 100). The
        two most recent sessions are both the unsloth model on 2026-11-05."""
        lem, uns = LEMONADE_JSON, UNSLOTH_JSON
        d1, d3 = ms(2026, 11, 1), ms(2026, 11, 5)
        rows = [
            src_row(sid="d1", model=lem, created=d1),
            src_row(sid="d2", model=uns, created=d1 + 1000),
            src_row(sid="d3", model=uns, created=d3),
            src_row(sid="d4", model=uns, created=d3 + 10_000),
        ]
        messages = [
            step_msg("m-" + sid, sid=sid, created=created,
                     first=created + 100, streamed=created + 2100,
                     out=100, reasoning=0)
            for sid, created in (("d1", d1), ("d2", d1 + 1000),
                                 ("d3", d3), ("d4", d3 + 10_000))
        ]
        return rows, messages

    def test_daily_totals_limit_returns_only_the_capped_days(self):
        dest = self.store(*self.fixture())
        days = daily_totals(dest, limit=2)
        self.assertEqual([d["date"] for d in days], ["2026-11-05"])
        self.assertEqual(days[0]["sessions"], 2)
        # no cap still aggregates everything
        uncapped = daily_totals(dest)
        self.assertEqual([d["date"] for d in uncapped],
                         ["2026-11-01", "2026-11-05"])
        self.assertEqual(sum(d["sessions"] for d in uncapped), 4)

    def test_total_totals_by_model_limit_returns_only_those_models(self):
        dest = self.store(*self.fixture())
        capped = total_totals(dest, by_model=True, limit=2)
        # the two most recent sessions are both the unsloth model — the
        # lemonade day-1 row must drop out entirely
        self.assertEqual([r["model"] for r in capped],
                         ["unsloth-studio/" + UNSLOTH_MODEL_ID])
        self.assertEqual(capped[0]["sessions"], 2)
        self.assertEqual(len(total_totals(dest, by_model=True)), 2)

    def test_total_totals_rate_weight_matches_capped_sessions(self):
        # Review Focus 1 pin: the rate side and the sum side saw the same set
        dest = self.store(*self.fixture())
        capped = query_sessions(dest, limit=2)
        self.assertEqual(len(capped), 2)
        total = total_totals(dest, limit=2)[0]
        self.assertEqual(total["sessions"], 2)
        self.assertEqual(total["rate_weight"],
                         sum(r["rate_weight"] for r in capped))


class TotalEventsTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_unsloth_src([
            unsloth_event("e1", prompt=900, completion=30),
            unsloth_event("e2", model="other-model", prompt=100, completion=5),
        ])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)

    def test_total_events_single_grand_row(self):
        rows = total_events(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1000, 35, 1035))
        self.assertEqual(r["cost"], 0.0)

    def test_total_events_per_model_when_filtered(self):
        src = make_unsloth_src([unsloth_event("e3", prompt=50)])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)  # same model as e1
        rows = total_events(self.dest, model="qwen")
        self.assertEqual([r["model"] for r in rows], [UNSLOTH_MODEL_ID])
        self.assertEqual((rows[0]["sessions"], rows[0]["tokens_input"]), (2, 950))

    def test_total_events_provider_prefixed_filter(self):
        # the provider-prefixed string shown in session reports selects just
        # that model's events (every event belongs to the unsloth provider)
        rows = total_events(self.dest, model="unsloth-studio/" + UNSLOTH_MODEL_ID)
        self.assertEqual([r["model"] for r in rows], [UNSLOTH_MODEL_ID])

    def test_total_events_thresholds_and_empty(self):
        r = total_events(self.dest, min_in=500)[0]
        self.assertEqual((r["sessions"], r["tokens_input"]), (1, 900))
        self.assertEqual(total_events(self.dest, model="nope"), [])

    def test_total_events_by_model_without_filter(self):
        rows = total_events(self.dest, by_model=True)
        self.assertEqual([r["model"] for r in rows],
                         ["other-model", UNSLOTH_MODEL_ID])
        self.assertEqual(sum(r["sessions"] for r in rows), 2)


class AggregateRowShapeTests(unittest.TestCase):
    """--json serialises rows in dict insertion order, so the key order of
    every aggregate row is pinned output, not an implementation detail."""

    KEYS = {
        "daily_events": ["date", "sessions", "tokens_input", "tokens_output",
                         "total", "cost", "tok_s"],
        "daily_runs": ["date", "sessions", "tokens_input", "progress_tokens",
                       "tokens_output", "total", "tok_s", "rate_weight"],
        "daily_totals": ["date", "sessions", "tokens_input", "tokens_output",
                         "total", "cost", "tokens_cache_read",
                         "tokens_cache_write", "tok_s", "rate_weight"],
        "total_events": ["model", "sessions", "tokens_input", "tokens_output",
                         "total", "cost", "tok_s"],
        "total_runs": ["model", "sessions", "tokens_input", "progress_tokens",
                       "tokens_output", "total", "tok_s", "rate_weight"],
        "total_totals": ["model", "sessions", "tokens_input", "tokens_output",
                         "total", "cost", "tokens_cache_read",
                         "tokens_cache_write", "tok_s", "rate_weight"],
    }

    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_src([
            src_row(sid="s1", created=ms(2026, 11, 1)),
            src_row(sid="s2", model=LEMONADE_JSON, created=ms(2026, 11, 2)),
        ])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        usrc = make_unsloth_src([
            unsloth_event("e1", created=ms(2026, 11, 1)),
            unsloth_event("e2", model="other-model", created=ms(2026, 11, 2)),
        ])
        self.addCleanup(usrc.close)
        import_unsloth(usrc, self.dest)
        rsrc = make_runs_src([
            run_row("r1", started=ms(2026, 11, 1),
                    completed=ms(2026, 11, 1) + 10_000),
            run_row("r2", model="other/run", started=ms(2026, 11, 2),
                    completed=ms(2026, 11, 2) + 10_000),
        ])
        self.addCleanup(rsrc.close)
        import_unsloth_runs(rsrc, self.dest)

    def test_every_aggregate_view_keeps_its_key_order(self):
        calls = {
            "daily_events": lambda: daily_events(self.dest),
            "daily_runs": lambda: daily_runs(self.dest),
            "daily_totals": lambda: daily_totals(self.dest),
            "total_events": lambda: total_events(self.dest),
            "total_runs": lambda: total_runs(self.dest),
            "total_totals": lambda: total_totals(self.dest),
        }
        for name, call in calls.items():
            with self.subTest(view=name):
                rows = call()
                self.assertTrue(rows, "%s returned no rows to pin" % name)
                self.assertEqual(list(rows[0]), self.KEYS[name])

    def test_per_model_rows_keep_the_same_key_order(self):
        # the per-model branch of the total_* family is a separate SQL path
        # from the grand-total row; --json would expose a reordered dict
        # there just the same, so pin it too
        calls = {
            "total_events": lambda: total_events(self.dest, by_model=True),
            "total_runs": lambda: total_runs(self.dest, by_model=True),
            "total_totals": lambda: total_totals(self.dest, by_model=True),
        }
        for name, call in calls.items():
            with self.subTest(view=name):
                rows = call()
                self.assertTrue(rows, "%s returned no rows to pin" % name)
                self.assertEqual(list(rows[0]), self.KEYS[name])


class CliTotalTests(CliHarness):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1"),                        # unsloth-studio model
            src_row(sid="s2", model=qwen_lemonade),   # lemonade qwen-mini
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_total_flag_renders_grand_row(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total"])
        self.assertEqual(code, 0)
        self.assertIn("(all)", out)
        self.assertIn("TOTAL", out)

    def test_total_flag_per_model_rows(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model", "qwen"])
        self.assertEqual(code, 0)
        self.assertIn("lemonade/qwen-mini", out)
        self.assertIn(f"unsloth-studio/{UNSLOTH_MODEL_ID}", out)
        self.assertNotIn("(all)", out)

    def test_total_json_shape(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]), {"model", "sessions", "tokens_input",
                           "tokens_output", "total", "cost", "share",
                           "tok_s", "rate_weight", "tokens_cache_read",
                           "tokens_cache_write"})

    def test_total_rejects_daily(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--total", "--daily"])
        self.assertEqual(cm.exception.code, 2)

    def test_reconcile_rejects_total(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["reconcile", "--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--total"])
        self.assertEqual(cm.exception.code, 2)

    def test_total_bare_model_lists_all_models(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("lemonade/qwen-mini", out)
        self.assertIn(f"unsloth-studio/{UNSLOTH_MODEL_ID}", out)
        self.assertNotIn("(all)", out)

    def test_bare_model_with_daily_renders_daily_report(self):
        # superseded 2026-11-14: bare --model no longer errors; with --daily
        # it means "all models" and renders the ordinary daily table
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--daily", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("DATE", out.splitlines()[0])
        self.assertNotIn("SHARE", out)

    def test_from_unsloth_total_bare_model(self):
        udb = os.path.join(self.tmp.name, "studio.db")
        self.addCleanup(make_file_unsloth_db(
            udb, [unsloth_event("e1"),
                  unsloth_event("e2", model="other-model")]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", udb, "--from-unsloth", "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn(UNSLOTH_MODEL_ID, out)
        self.assertIn("other-model", out)
        self.assertNotIn("(all)", out)

    def test_cost_flag_adds_cost_column_and_json_field(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost"])
        self.assertEqual(code, 0)
        self.assertIn("COST", out)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost", "--json"])
        self.assertIn("cost_est", json.loads(out)[0])

    def test_cost_default_off_still_has_no_cost_column(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total"])
        self.assertEqual(code, 0)
        self.assertNotIn("COST", out)

    def test_cost_on_any_model_and_piped_plain(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost"])
        self.assertEqual(code, 0)
        self.assertIn("$", out)          # estimate prints for non-Claude traffic
        self.assertNotIn("\x1b", out)    # piped output stays plain

    def test_cost_keeps_share_bar_padded(self):
        # regression pin: appending COST after BAR must not disable paint_bars
        # (the print path's identity check on SHARE_COLUMNS would silently
        # turn bars off for --cost --total --model)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model", "--cost"])
        self.assertEqual(code, 0)
        unsloth_line = next(ln for ln in out.splitlines() if "unsloth" in ln)
        # SHARE percent value, then a gap (the "SHARE %" header widened the
        # percent column), then block glyphs — bars still painted
        self.assertRegex(unsloth_line, r"\d+\.\d%\s+[█░▒▓]")

    def test_reconcile_rejects_cost(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["reconcile", "--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--cost"])
        self.assertEqual(cm.exception.code, 2)

    def test_cost_bare_header_names_pinned_model(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost"])
        self.assertEqual(code, 0)
        # single-line header (human decision): "COST · <model> Prices" sits
        # on the one header line, directly above the rule.
        lines = out.splitlines()
        self.assertIn("COST \u00b7 Claude Sonnet 5 Prices", lines[0])
        self.assertTrue(lines[1].startswith("\u2500"))

    def test_cost_slug_changes_header(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--cost=deepseek-v3.2"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("COST \u00b7 DeepSeek V3.2 Prices", lines[0])

    def test_cost_family_alias_equals_model_slug(self):
        base = ["--db", self.db, "--src", self.src, "--unsloth-db",
                self.no_udb, "--total"]
        _, alias, _ = self.run_main(base + ["--cost=qwen"])
        _, model, _ = self.run_main(base + ["--cost=qwen-plus"])
        self.assertEqual(alias, model)
        lines = alias.splitlines()
        self.assertIn("COST \u00b7 Qwen-Plus Prices", lines[0])

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
        # Exit 2 is not enough: the old space form (--cost-tier opus, the
        # documented form for a year) must say the flag was removed, not
        # complain about the word argparse swallowed as the command.
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(err):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb, "--total",
                  "--cost-tier", "opus"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--cost-tier was removed", err.getvalue())
        self.assertIn("--cost=", err.getvalue())

    def test_cost_bare_equals_explicit_default_slug(self):
        # spec "left to the builder" pin: bare --cost IS --cost=sonnet-5.
        base = ["--db", self.db, "--src", self.src, "--unsloth-db",
                self.no_udb, "--total"]
        _, bare, _ = self.run_main(base + ["--cost"])
        _, pinned, _ = self.run_main(base + ["--cost=sonnet-5"])
        self.assertEqual(bare, pinned)


class RealCostTests(CliHarness):
    """--real-cost: the stored real USD as a display column."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1"),                        # unsloth-studio model
            src_row(sid="s2", model=qwen_lemonade),   # lemonade qwen-mini
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

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
        lines = out.splitlines()
        # estimate column still left of the real one: both names now share
        # the single header line; compare their start positions.
        self.assertLess(lines[0].index("COST"),
                        lines[0].index("REAL COST"))

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

    def test_real_cost_on_daily_and_sessions_views(self):
        # review fix: the column belongs to every session-family view, not
        # just --total.
        base = ["--db", self.db, "--src", self.src, "--unsloth-db",
                self.no_udb]
        code, out, _ = self.run_main(base + ["--daily", "--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("REAL COST", out)
        self.assertIn("$0.02", out)          # both sessions share one day
        code, out, _ = self.run_main(base + ["--sessions", "--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("REAL COST", out)
        self.assertIn("$0.01", out)          # per-session stored cost

    def test_real_cost_zero_renders_zero(self):
        # README's "free-tier traffic honestly renders $0.00" pin.
        free = os.path.join(self.tmp.name, "free.db")
        self.addCleanup(make_file_db(free, [src_row(sid="s9", cost=0.0)]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", free, "--unsloth-db", self.no_udb,
             "--total", "--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("$0.00", out)


class CostTests(unittest.TestCase):
    """annotate_cost: reference model-tier price overlay on row copies."""

    def test_annotate_cost_session_uses_all_token_classes_sonnet(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000,
                 "tokens_cache_read": 1_000_000, "tokens_cache_write": 1_000_000}]
        (r,) = annotate_cost(rows, "sonnet-5")
        # 2 + 10 + 0.20 + 2.50 = 14.70 per Mtok of each class
        self.assertAlmostEqual(r["cost_est"], 14.70)

    def test_annotate_cost_tier_changes_number(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 0,
                 "tokens_cache_read": 0, "tokens_cache_write": 0}]
        self.assertAlmostEqual(annotate_cost(rows, "haiku-4.5")[0]["cost_est"], 1.0)
        self.assertAlmostEqual(annotate_cost(rows, "opus-5")[0]["cost_est"], 5.0)

    def test_annotate_cost_event_uses_prompt_completion(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet-5")[0]["cost_est"], 12.0)

    def test_annotate_cost_run_uses_progress_only(self):
        from tokenstat import annotate_cost
        rows = [{"progress_tokens": 1_000_000, "tokens_input": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet-5")[0]["cost_est"], 10.0)

    def test_run_aggregate_rows_carry_progress_tokens(self):
        # regression pin (task-4 review): --cost prices by key presence, so
        # run AGGREGATE rows must carry progress_tokens — without it they
        # silently price via the event branch at the input rate.
        from tokenstat import annotate_cost
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_runs_src([run_row("r1", progress=100),
                             run_row("r2", progress=400)])
        self.addCleanup(src.close)
        import_unsloth_runs(src, dest)
        self.assertIn("progress_tokens", daily_runs(dest)[0])
        (row,) = total_runs(dest, by_model=True)  # both runs share one model
        self.assertIn("progress_tokens", row)
        # output-only pricing: (100 + 400) * 10 / 1e6
        (priced,) = annotate_cost(total_runs(dest, by_model=True), "sonnet-5")
        self.assertAlmostEqual(priced["cost_est"], 0.005)

    def test_annotate_cost_does_not_mutate_input_rows(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 10, "tokens_output": 10}]
        out = annotate_cost(rows, "sonnet-5")
        self.assertNotIn("cost_est", rows[0])  # inputs untouched...
        self.assertIn("cost_est", out[0])      # ...copies carry the estimate

    def test_resolve_cost_tier_alias_maps_to_mid_tier(self):
        from tokenstat import resolve_cost_tier, FAMILY_ALIASES, PRICES
        for alias, target in FAMILY_ALIASES.items():
            self.assertEqual(resolve_cost_tier(alias), target)
            self.assertIn(target, PRICES)
            self.assertTrue(PRICES[target]["name"])

    def test_resolve_cost_tier_passes_model_slugs_through(self):
        from tokenstat import resolve_cost_tier
        self.assertEqual(resolve_cost_tier("sonnet-5"), "sonnet-5")

    def test_prices_and_family_aliases_do_not_collide(self):
        # A collision would make resolve_cost_tier silently shadow a real
        # model slug with the alias's target.
        from tokenstat import PRICES, FAMILY_ALIASES
        self.assertFalse(set(PRICES) & set(FAMILY_ALIASES))


class CostFormatTests(unittest.TestCase):
    """cost-kind cells render $X.XX (2 decimals, human decision 2026-11-17)."""

    def test_render_table_cost_two_decimals(self):
        from tokenstat import render_table
        table = render_table([{"cost_est": 14.7}],
                             [("COST", "cost_est", "cost")])
        # "$14.70" alone would also match the old "$14.7000" — pin the cell.
        self.assertIn("$14.70\n", table)
        self.assertNotIn("$14.7000", table)

    def test_render_table_sub_cent_cost_renders_zero(self):  # review-focus pin
        from tokenstat import render_table
        table = render_table([{"cost_est": 0.004}],
                             [("COST", "cost_est", "cost")])
        self.assertIn("$0.00\n", table)
        self.assertNotIn("$0.0040", table)

    def test_paint_cells_cost_two_decimals(self):
        from tokenstat import paint_cells
        (row,) = paint_cells([{"cost_est": 14.7}])
        self.assertIn("$14.70\x1b", row["cost_est"])  # cell ends at the reset

    def test_paint_cells_paints_stored_cost_key(self):  # review fix: REAL COST
        # The --real-cost column's key is "cost", not "cost_est" — it must be
        # themed like every other numeric cell, not emitted bare.
        from tokenstat import paint_cells
        (row,) = paint_cells([{"cost": 0.02}])
        self.assertEqual(row["cost"], "\x1b[38;2;244;240;255m$0.02\x1b[0m")

    def test_render_table_two_line_header(self):
        # "\n" in a header wraps the cell (mechanism kept; the shipped COST
        # header is single-line); header text BOTTOM-aligns, so each
        # column's last line sits on the rule directly above the data.
        from tokenstat import render_table
        table = render_table([{"a": 1, "cost_est": 14.7}],
                             [("A", "a", "int"),
                              ("PRICE\nClaude Sonnet 5 Rates",
                               "cost_est", "cost")])
        self.assertEqual(table.splitlines(),
                         ["   PRICE",
                          "A  Claude Sonnet 5 Rates",
                          "\u2500" * 24,
                          "1  $14.70"])

    def test_render_table_two_line_header_pads_later_columns(self):
        # A column AFTER the wrapped one: its name rides the bottom header
        # line and its data aligns past the wrapped column's widest line.
        from tokenstat import render_table
        table = render_table([{"cost_est": 14.7, "z": 5}],
                             [("PRICE\nClaude Sonnet 5 Rates",
                               "cost_est", "cost"),
                              ("Z", "z", "int")])
        lines = table.splitlines()
        self.assertEqual(lines[0], "PRICE")
        self.assertEqual(lines[1], "Claude Sonnet 5 Rates  Z")
        self.assertEqual(lines[2], "\u2500" * 24)
        self.assertEqual(lines[3], "$14.70" + " " * 17 + "5")

    def test_render_table_rule_separates_header(self):
        # every table gets a rule between head and body (human decision:
        # the wrapped header floated ambiguously between the two).
        from tokenstat import render_table
        table = render_table([{"a": 1, "b": 2}],
                             [("A", "a", "int"), ("B", "b", "int")])
        self.assertEqual(table.splitlines(),
                         ["A  B", "\u2500" * 4, "1  2"])


class CacheOverlayTests(unittest.TestCase):
    """annotate_cache/with_cache: display-only CACHE overlay primitives."""

    def test_annotate_cache_combines_read_and_write(self):
        from tokenstat import annotate_cache
        (r,) = annotate_cache([{"tokens_input": 1, "tokens_cache_read": 7000,
                                "tokens_cache_write": 100}])
        self.assertEqual(r["cache"], 7100)

    def test_annotate_cache_missing_keys_default_to_zero(self):
        from tokenstat import annotate_cache
        (r,) = annotate_cache([{}])
        self.assertEqual(r["cache"], 0)

    def test_annotate_cache_does_not_mutate_input_rows(self):
        from tokenstat import annotate_cache
        rows = [{"tokens_cache_read": 5, "tokens_cache_write": 6}]
        out = annotate_cache(rows)
        self.assertNotIn("cache", rows[0])  # inputs untouched...
        self.assertIn("cache", out[0])      # ...copies carry the combined sum

    def test_with_cache_inserts_consecutively_before_total(self):
        from tokenstat import DAILY_COLUMNS, SHARE_COLUMNS, SESSION_COLUMNS, \
            with_cache
        for source in (SESSION_COLUMNS, DAILY_COLUMNS, SHARE_COLUMNS):
            headers = [h for h, _, _ in with_cache(source)]
            i = headers.index("CACHE")
            self.assertEqual((headers[i - 1], headers[i + 1]),
                             ("OUT", "TOTAL"))

    def test_with_cache_does_not_mutate_argument(self):
        from tokenstat import SESSION_COLUMNS, with_cache
        original = list(SESSION_COLUMNS)
        with_cache(SESSION_COLUMNS)
        self.assertEqual(SESSION_COLUMNS, original)


class CliCacheTests(CliHarness):
    """--cache overlay column end-to-end through main()."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1"),                        # unsloth-studio model
            src_row(sid="s2", model=qwen_lemonade),   # lemonade qwen-mini
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def base(self, *flags):
        return self.run_main(["--db", self.db, "--src", self.src,
                              "--unsloth-db", self.no_udb, *flags])

    def test_cache_flag_adds_column_to_session_view(self):
        code, out, _ = self.base("--cache")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"OUT\s+CACHE\s+TOTAL")
        self.assertIn("7,100", out)   # per seeded session: 7000 read + 100 write

    def test_cache_daily_view_shows_combined(self):
        code, out, _ = self.base("--daily", "--cache")
        self.assertEqual(code, 0)
        self.assertRegex(out.splitlines()[0], r"OUT\s+CACHE\s+TOTAL")
        self.assertIn("14,200", out)  # two seeded sessions

    def test_cache_total_view_shows_combined(self):
        code, out, _ = self.base("--total", "--cache")
        self.assertEqual(code, 0)
        all_row = next(ln for ln in out.splitlines() if "(all)" in ln)
        self.assertIn("14,200", all_row)

    def test_default_off_has_no_cache_column(self):
        code, out, _ = self.base("--total")
        self.assertEqual(code, 0)
        self.assertNotIn("CACHE", out)

    def test_cache_leaves_in_out_total_unchanged(self):
        code, out, _ = self.base("--total", "--cache")
        self.assertEqual(code, 0)
        all_row = next(ln for ln in out.splitlines() if "(all)" in ln)
        cells = all_row.split()
        # MODEL SESSIONS IN OUT CACHE TOTAL — IN, OUT, TOTAL keep their
        # no-cache values, with the new CACHE cell between OUT and TOTAL
        self.assertEqual(cells[2:6], ["2,000", "400", "14,200", "2,400"])

    def test_cache_keeps_share_bar_padded(self):
        # regression pin: inserting CACHE must not disable paint_bars
        code, out, _ = self.base("--total", "--model", "--cache")
        self.assertEqual(code, 0)
        unsloth_line = next(ln for ln in out.splitlines() if "unsloth" in ln)
        self.assertRegex(unsloth_line, r"\d+\.\d%\s+[█░▒▓]")

    def test_cost_and_cache_together_show_both_columns(self):
        code, out, _ = self.base("--total", "--cost", "--cache")
        self.assertEqual(code, 0)
        head = "\n".join(out.splitlines()[:2])  # --cost wraps to two lines
        self.assertIn("CACHE", head)
        self.assertIn("COST", head)

    def test_json_shape_unchanged_by_cache(self):
        _, plain, _ = self.base("--total", "--json")
        _, cached, _ = self.base("--total", "--cache", "--json")
        rows_plain, rows_cached = json.loads(plain), json.loads(cached)
        self.assertEqual(set(rows_plain[0]), set(rows_cached[0]))
        self.assertNotIn("cache", rows_cached[0])

    def test_reconcile_rejects_cache(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["reconcile", "--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--cache"])
        self.assertEqual(cm.exception.code, 2)

    def test_from_unsloth_rejects_cache(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--from-unsloth",
                      "--cache"])
        self.assertEqual(cm.exception.code, 2)

    def test_from_runs_rejects_cache(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--from-runs",
                      "--cache"])
        self.assertEqual(cm.exception.code, 2)


class DisplayAlignmentTests(CliHarness):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [src_row(sid="s1", cost=0.05)]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_table_left_aligns_numbers(self):
        columns = [("N", "n", "int"), ("T", "t", "text")]
        rows = [{"n": 5, "t": "x"}, {"n": 1000, "t": "y"}]
        self.assertEqual(render_table(rows, columns),
                         "N      T\n" + "\u2500" * 8
                         + "\n5      x\n1,000  y\n")

    def test_render_table_pads_ansi_cells_by_visible_width(self):
        # regression pin: --cost appends COST after painted BAR cells; width
        # math must ignore SGR escape bytes so every row's next column lands
        # at the same VISIBLE position (rows also differ by having no escapes
        # at all — zero-total rows render a plain empty bar)
        columns = [("BAR", "bar", "text"), ("COST", "c", "cost")]
        rows = [{"bar": "\x1b[38;2;80;168;255m█\x1b[0m", "c": 0.5},
                {"bar": "", "c": 1.25}]
        strip = lambda ln: re.sub(r"\x1b\[[0-9;]*m", "", ln)
        lines = [strip(ln) for ln in render_table(rows, columns).splitlines()]
        head, data = lines[0], lines[2:]     # lines[1] is the header rule
        self.assertEqual(head.index("COST"), data[0].index("$"))
        self.assertEqual(head.index("COST"), data[1].index("$"))

    def test_report_tables_have_no_cost_column(self):
        # every displayed report (sessions, daily, total) drops COST...
        for argv_extra in ([], ["--daily"], ["--total"]):
            with self.subTest(argv_extra=argv_extra):
                code, out, _ = self.run_main(
                    ["--db", self.db, "--src", self.src,
                     "--unsloth-db", self.no_udb] + argv_extra)
                self.assertEqual(code, 0)
                self.assertNotIn("COST", out)
        # ...while --json still carries the data
        code, out, _ = self.run_main(["--db", self.db, "--src", self.src,
                                      "--unsloth-db", self.no_udb, "--total",
                                      "--json"])
        self.assertEqual(code, 0)
        self.assertIn("cost", json.loads(out)[0])


class AddSharesTests(unittest.TestCase):
    """add_shares: share-of-overall-total + relative bar for aggregate rows."""

    def test_share_is_percent_of_row_total_over_sum(self):
        rows = [{"model": "a", "total": 750}, {"model": "b", "total": 250}]
        out = add_shares(rows)
        self.assertEqual([r["share"] for r in out], [75.0, 25.0])

    def test_original_rows_not_mutated(self):
        rows = [{"model": "a", "total": 1}]
        add_shares(rows)
        self.assertNotIn("share", rows[0])

    def test_bar_scaled_to_largest_row_with_smooth_blocks(self):
        rows = [{"model": "a", "total": 100}, {"model": "b", "total": 50},
                {"model": "c", "total": 1}]
        out = add_shares(rows, bar_width=20)
        self.assertEqual(out[0]["bar"], "█" * 20)  # biggest row: full blocks
        self.assertEqual(out[1]["bar"], "█" * 10)  # exact half: no fraction
        self.assertEqual(out[2]["bar"], "░")       # 0.2 cells -> light shade

    def test_bar_shows_fraction_when_not_exact(self):
        rows = [{"model": "a", "total": 3}, {"model": "b", "total": 1}]
        out = add_shares(rows, bar_width=30)
        self.assertEqual(out[0]["bar"], "█" * 30)
        # 1/3 of max over width 30 = exactly 10 full blocks
        self.assertEqual(out[1]["bar"], "█" * 10)
        rows2 = [{"model": "a", "total": 7}, {"model": "b", "total": 1}]
        out2 = add_shares(rows2, bar_width=30)
        # 30/7 = 4.285... -> 4 full blocks + 2/8 fraction (light shade)
        self.assertEqual(out2[1]["bar"], "████░")

    def test_fraction_shades_are_all_full_cell_height(self):
        # the old eighth glyphs (U+2581..2587) sit lower than U+2588 in many
        # fonts; only full-cell glyphs may appear so bars never rag at bottom
        allowed = set("█░▒▓")
        rows = [{"model": "a", "total": t} for t in (1000, 97, 43, 9, 1)]
        for r in add_shares(rows, bar_width=20):
            self.assertTrue(set(r["bar"]) <= allowed, r["bar"])

    def test_tiny_nonzero_row_never_renders_empty(self):
        rows = [{"model": "a", "total": 10_000}, {"model": "b", "total": 1}]
        out = add_shares(rows, bar_width=30)
        self.assertEqual(out[1]["bar"], "░")  # rounds to nothing -> lightest

    def test_zero_total_rows_get_no_bar_and_zero_share(self):
        rows = [{"model": "a", "total": 0}, {"model": "b", "total": 0}]
        out = add_shares(rows)
        self.assertEqual([r["share"] for r in out], [0.0, 0.0])
        self.assertEqual([r["bar"] for r in out], ["", ""])


class CliShareTests(CliHarness):
    """--total per-model views (both sources) show SHARE + BAR; bare --model
    is now a valid shortcut for --total --model."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1000),   # unsloth-studio: 10k
            src_row(sid="s2", model=qwen_lemonade, tin=4500, tout=500),  # 5k
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_total_per_model_table_shares_and_bars(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        # share pair (human decision): the percent column reads "SHARE %",
        # the bar column "SHARE" — the pair is legible at a glance.
        self.assertIn("SHARE %  SHARE", lines[0])
        self.assertNotIn("BAR", lines[0])
        unsloth_line = next(ln for ln in lines if "unsloth-studio" in ln)
        lemonade_line = next(ln for ln in lines if "lemonade" in ln)
        self.assertIn("66.7%", unsloth_line)
        self.assertIn("33.3%", lemonade_line)
        self.assertTrue(unsloth_line.count("█") > lemonade_line.count("█"))

    def test_bare_model_is_now_valid_per_model_totals(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--model"])
        self.assertEqual(code, 0)
        self.assertIn("lemonade/qwen-mini", out)
        self.assertIn(f"unsloth-studio/{UNSLOTH_MODEL_ID}", out)
        self.assertNotIn("(all)", out)
        self.assertIn("SHARE", out.splitlines()[0])

    def test_total_grand_row_shares_full(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total"])
        self.assertEqual(code, 0)
        self.assertIn("100.0%", out)

    def test_total_json_carries_share_not_bar(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertIn("share", r)
            self.assertNotIn("bar", r)
        self.assertAlmostEqual(
            sum(r["share"] for r in rows), 100.0, places=6)

    def test_from_unsloth_total_shares(self):
        udb = os.path.join(self.tmp.name, "studio.db")
        self.addCleanup(make_file_unsloth_db(
            udb, [unsloth_event("e1", prompt=900, completion=100),
                  unsloth_event("e2", model="other-model",
                                prompt=500)]).close)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", udb, "--from-unsloth", "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("SHARE", out.splitlines()[0])
        self.assertIn("1,000", out)  # unsloth total renders
        self.assertIn("35.5%", out)  # other-model = 550/1,550

    # Superseded for bare runs on 2026-11-15 by the compact default dashboard
    # (bare now shows the share panel; daily remains SHARE/BAR-free).
    def test_session_and_daily_tables_unchanged(self):
        for extra in (["--daily"],):
            with self.subTest(extra=extra):
                code, out, _ = self.run_main(
                    ["--db", self.db, "--src", self.src,
                     "--unsloth-db", self.no_udb] + extra)
                self.assertEqual(code, 0)
                self.assertNotIn("SHARE", out)
                self.assertNotIn("BAR", out)

    def test_per_model_rows_sort_by_share_desc(self):
        # fixture models: unsloth-studio total 10k (66.7%) vs lemonade 5k
        # (33.3%); alphabetical order would put lemonade first, so assert
        # the unsloth line precedes the lemonade line
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        u = next(i for i, ln in enumerate(lines) if "unsloth-studio" in ln)
        l = next(i for i, ln in enumerate(lines) if "lemonade" in ln)
        self.assertLess(u, l)

    def test_share_json_sorted_desc_ties_alphabetical(self):
        # own fixture: three models, one big (66.7%) and a tie at 16.7% each;
        # expected order: zzz/zzz, then the tied pair alphabetically
        src = os.path.join(self.tmp.name, "src3.db")
        aaa_one = '{"id":"one","providerID":"aaa"}'
        aaa_two = '{"id":"two","providerID":"aaa"}'
        zzz = '{"id":"zzz","providerID":"zzz"}'
        self.addCleanup(make_file_db(src, [
            src_row(sid="t1", model=zzz, tin=9000, tout=1000),
            src_row(sid="t2", model=aaa_one, tin=2250, tout=250),
            src_row(sid="t3", model=aaa_two, tin=2250, tout=250),
        ]).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "usage3.db"),
             "--src", src, "--unsloth-db", self.no_udb,
             "--total", "--model", "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual([r["model"] for r in rows],
                         ["zzz/zzz", "aaa/one", "aaa/two"])


class PaintBarsTests(unittest.TestCase):
    """paint_bars: display-only padding + heat-gradient color for BAR cells."""

    def test_padding_both_sides_without_color(self):
        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 5},
                {"model": "b", "total": 0, "share": 0.0, "bar": ""}]
        out = paint_bars(rows, color=False)
        self.assertEqual(out[0]["bar"], " ████ " .replace("████", "█" * 5))
        self.assertEqual(out[1]["bar"], "  ")

    def test_original_rows_not_mutated(self):
        rows = [{"model": "a", "total": 1, "share": 100.0, "bar": "█",
                 "tokens_input": 1, "tokens_output": 0}]
        paint_bars(rows, color=True)
        self.assertEqual(rows[0]["bar"], "█")

    def test_color_truecolor_wraps_padded_bar(self):
        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "██",
                 "tokens_input": 5, "tokens_output": 5}]
        out = paint_bars(rows, color=True)
        r = out[0]
        self.assertTrue(r["bar"].startswith(" \x1b[38;2;"))  # padding stays plain
        self.assertTrue(r["bar"].endswith("\x1b[0m "))
        # stripping the SGR sequences leaves the padded plain bar
        import re
        stripped = [re.sub(r"\x1b\[[0-9;]*m", "", r["bar"]) for r in out]
        self.assertEqual(stripped, [" ██ "])

    def test_gradient_endpoints_are_in_and_out_colors(self):
        from tokenstat import TOKEN_IN_RGB, TOKEN_OUT_RGB
        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 10,
                 "tokens_input": 5, "tokens_output": 5}]
        bar = paint_bars(rows, color=True)[0]["bar"]

        colors = rgbs(bar)
        self.assertEqual(len(colors), 10)         # every glyph its own color
        self.assertEqual(colors[0], TOKEN_IN_RGB)   # left end: pure input hue
        self.assertEqual(colors[-1], TOKEN_OUT_RGB)  # right end: pure output hue

    def test_gradient_endpoints_follow_an_arbitrary_theme(self):
        # the gradient must read its endpoints from the theme it is handed,
        # not from the import-time TOKEN_IN_RGB/TOKEN_OUT_RGB globals
        theme = dict(NEON, tin=(10, 200, 190), tout=(250, 30, 90))
        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 8,
                 "tokens_input": 5, "tokens_output": 5}]
        colors = rgbs(paint_bars(rows, color=True, theme=theme)[0]["bar"])
        self.assertEqual(colors[0], (10, 200, 190))   # left end = theme tin
        self.assertEqual(colors[-1], (250, 30, 90))   # right end = theme tout

    def test_gradient_endpoints_round_trip_for_every_theme(self):
        from tokenstat import _token_rgb, gradient_stops
        for slug, (_, theme) in THEMES.items():
            stops = gradient_stops(theme)
            self.assertEqual(len(stops), 7, slug)
            self.assertEqual(_token_rgb(0.0, stops), theme["tin"], slug)
            self.assertEqual(_token_rgb(1.0, stops), theme["tout"], slug)

    def test_default_paint_bars_unchanged_without_theme_arg(self):
        # the default path (no theme=) still paints the NEON-derived pins
        from tokenstat import TOKEN_IN_RGB, TOKEN_OUT_RGB

        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 8,
                 "tokens_input": 5, "tokens_output": 5}]
        colors = rgbs(paint_bars(rows, color=True)[0]["bar"])
        self.assertEqual(colors[0], TOKEN_IN_RGB)
        self.assertEqual(colors[-1], TOKEN_OUT_RGB)

    def test_gradient_transition_sits_at_in_out_ratio(self):
        def blue_count(tin, tout):
            rows = [{"model": "a", "total": tin + tout, "share": 100.0,
                     "bar": "█" * 20, "tokens_input": tin,
                     "tokens_output": tout}]
            bar = paint_bars(rows, color=True)[0]["bar"]
            import re
            rgbs = [tuple(int(g) for g in m) for m in
                    re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", bar)]
            return sum(1 for (r_, g_, b_) in rgbs if b_ > r_)  # bluish glyphs

        self.assertGreater(blue_count(180, 20), blue_count(20, 180))
        self.assertEqual(blue_count(200, 0), 20)   # all input: all blue
        self.assertEqual(blue_count(0, 200), 0)    # all output: no blue

    def test_gradient_stays_vivid_no_gray_midpoints(self):
        # direct RGB lerp blue->orange passes through gray (~delta 21); the
        # gradient must keep every glyph highly chromatic (via HSL hue sweep)
        rows = [{"model": "a", "total": 10, "share": 100.0, "bar": "█" * 20,
                 "tokens_input": 5, "tokens_output": 5}]
        bar = paint_bars(rows, color=True)[0]["bar"]
        import re
        rgbs = [tuple(int(g) for g in m) for m in
                re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", bar)]
        for rgb in rgbs:
            self.assertGreater(max(rgb) - min(rgb), 100,
                               f"washed-out midpoint {rgb}")

    def test_zero_total_row_not_colored(self):
        rows = [{"model": "z", "total": 0, "share": 0.0, "bar": "",
                 "tokens_input": 0, "tokens_output": 0}]
        out = paint_bars(rows, color=True)
        self.assertNotIn("\x1b", out[0]["bar"])


class PaintCellsTests(unittest.TestCase):
    """paint_cells: per-class truecolor on the rendered token/cost cells."""

    def test_class_colors_applied(self):
        rows = paint_cells([{"tokens_input": "10", "tokens_output": "5",
                             "total": "15"}])
        self.assertIn("38;2;{};{};{}m".format(*NEON["tin"]),
                      rows[0]["tokens_input"])
        self.assertIn("38;2;{};{};{}m".format(*NEON["tout"]),
                      rows[0]["tokens_output"])
        self.assertIn("1;38;2;{};{};{}m".format(*NEON["ttotal"]),
                      rows[0]["total"])

    def test_total_column_painted_ttotal(self):
        rows = paint_cells([{"total": 7300000, "tokens_input": None}])
        self.assertIn("1;38;2;{};{};{}m".format(*NEON["ttotal"]), rows[0]["total"])

    def test_share_column_painted_tshare(self):
        rows = paint_cells([{"total": 7300000, "share_str": "58.7%"}])
        self.assertIn("38;2;{};{};{}m".format(*NEON["tshare"]), rows[0]["share_str"])

    def test_tok_s_column_painted_accent(self):
        # Same magenta the hero's TOK/S stat wears.
        rows = paint_cells([{"total": 100, "tok_s": 240.0}])
        self.assertIn("38;2;{};{};{}m240.0".format(*NEON["accent"]),
                      str(rows[0]["tok_s"]))

    def test_sessions_column_painted_tshare(self):
        # Same teal the hero's row-count stat wears; the key carries runs in
        # --from-runs and requests in --from-unsloth, and follows everywhere.
        rows = paint_cells([{"sessions": 54, "total": 100}])
        self.assertIn("38;2;{};{};{}m54".format(*NEON["tshare"]),
                      str(rows[0]["sessions"]))

    def test_blank_tok_s_stays_blank(self):
        rows = paint_cells([{"total": 100, "tok_s": None}])
        self.assertIsNone(rows[0]["tok_s"])

    def test_untouched_keys_and_missing_keys(self):
        rows = paint_cells([{"model": "m", "date": "d"}])
        self.assertEqual(rows[0], {"model": "m", "date": "d"})

    def test_original_rows_not_mutated(self):
        src = [{"total": "15"}]
        paint_cells(src)
        self.assertEqual(src[0]["total"], "15")

    def test_table_alignment_survives_coloring(self):
        rows = [{"date": "2026-11-15", "model": "m", "tokens_input": 100,
                 "tokens_output": 50, "total": 150, "tok_s": None,
                 "title": ""}]
        table = render_table(paint_cells(rows), SESSION_COLUMNS)
        plain = render_table(rows, SESSION_COLUMNS)
        self.assertEqual([_visible(l) for l in table.splitlines()],
                         [_visible(l) for l in plain.splitlines()])


class PanelTests(unittest.TestCase):
    """panel: rounded frame with a title strip, all lines equal visible width."""

    def test_all_lines_share_visible_width(self):
        lines = panel("sessions", ["plain", "\x1b[38;2;96;165;250mcolored\x1b[0m"])
        widths = {_visible(ln) for ln in lines}
        self.assertEqual(len(widths), 1)

    def test_border_glyphs_present(self):
        lines = panel("t", ["x"])
        self.assertTrue(lines[0].lstrip().startswith("╭"))
        self.assertTrue(lines[-1].lstrip().startswith("╰"))
        self.assertIn("tokenstat · t", _ANSI_RE.sub("", lines[0]))

    def test_empty_lines_list_still_frames(self):
        lines = panel("t", [])
        self.assertEqual(len(lines), 2)   # top + bottom only

    def test_title_carries_label_role_after_accent_prefix(self):
        # the strip is two runs: accent-bold " tokenstat · ", then the title
        # itself in the label role (roles are the spec of the redesign)
        accent = "\x1b[1;38;2;{};{};{}m".format(*NEON["accent"])
        label = "\x1b[38;2;{};{};{}m".format(*NEON["label"])
        top = panel("sessions", ["x"])[0]
        self.assertIn(accent + " tokenstat · " + "\x1b[0m" + label + "sessions ",
                      top)
        self.assertNotIn(label, panel("sessions", ["x"])[1])   # body: no title


class ReportHeaderTests(unittest.TestCase):
    """report_header: hero panel with grand totals and the date span."""

    def test_hero_line_totals_and_row_count(self):
        rows = [{"total": 5_000_000, "tokens_input": 4_000_000,
                 "tokens_output": 1_000_000, "date": "2026-11-15"},
                {"total": 3_000_000, "tokens_input": 2_000_000,
                 "tokens_output": 1_000_000, "date": "2026-11-06"}]
        text = _ANSI_RE.sub("", "\n".join(report_header("sessions", rows)))
        self.assertIn("8.00M tokens", text)
        self.assertIn("2 rows", text)
        self.assertIn("2026-11-06", text)
        self.assertIn("2026-11-15", text)

    def test_runs_rows_without_input_keys_are_safe(self):
        rows = [{"total": 10, "date": None}]
        text = _ANSI_RE.sub("", "\n".join(report_header("runs", rows)))
        self.assertIn("10 tokens", text)      # compact() falls through to str

    def test_empty_rows_still_frames(self):
        lines = report_header("x", [])
        self.assertTrue(lines[0].lstrip().startswith("╭"))


class ColorEnabledTests(unittest.TestCase):
    def test_color_requires_tty_and_no_no_color(self):
        from tokenstat import _color_enabled

        class Tty:
            def isatty(self): return True

        class Pipe:
            def isatty(self): return False

        self.assertTrue(_color_enabled(Tty(), {}))
        self.assertFalse(_color_enabled(Tty(), {"NO_COLOR": "1"}))
        self.assertFalse(_color_enabled(Pipe(), {}))


class NeonThemeTests(unittest.TestCase):
    def test_neon_dict_shape(self):
        self.assertEqual(
            set(NEON), {"border", "accent", "label", "num", "tin", "tout",
                        "tcach", "track", "ttotal", "tshare"})
        for key, rgb in NEON.items():
            self.assertEqual(len(rgb), 3, key)
            self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in rgb), key)

    def test_gradient_endpoints_follow_theme(self):
        from tokenstat import TOKEN_IN_RGB, TOKEN_OUT_RGB
        self.assertEqual(TOKEN_IN_RGB, NEON["tin"])
        self.assertEqual(TOKEN_OUT_RGB, NEON["tout"])

    def test_compact_formats_millions_thousands_plain(self):
        self.assertEqual(compact(7_770_000), "7.77M")
        self.assertEqual(compact(12_340), "12.3k")
        self.assertEqual(compact(999), "999")
        self.assertEqual(compact(0), "0")


class ThemeRegistryTests(unittest.TestCase):
    BUILTIN_SLUGS = {"neon-midnight", "tokyo-night", "dracula", "nord",
                     "gruvbox-dark", "solarized-dark", "monokai", "one-dark",
                     "solarized-light", "paper"}

    def test_builtin_slugs_and_display_names(self):
        self.assertEqual(set(THEMES), self.BUILTIN_SLUGS)
        for slug, (name, colors) in THEMES.items():
            self.assertTrue(name and name[0].isupper(), slug)

    def test_every_theme_has_exactly_the_nine_roles(self):
        for slug, (_, colors) in THEMES.items():
            self.assertEqual(set(colors), set(THEME_ROLES), slug)
            for role, rgb in colors.items():
                self.assertEqual(len(rgb), 3, f"{slug}/{role}")
                self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255
                                    for v in rgb), f"{slug}/{role}")

    def test_neon_midnight_is_the_existing_palette_unchanged(self):
        name, colors = THEMES["neon-midnight"]
        self.assertEqual(name, "Neon Midnight")
        self.assertEqual(colors, {role: NEON[role] for role in THEME_ROLES})

    def test_data_colors_are_pairwise_distinct(self):
        # IN / OUT / CACHE / TOTAL / SHARE / accent must be tellable apart: no two
        # equal, and no two within a 60-unit Manhattan distance on the same line.
        for slug, (_, c) in THEMES.items():
            picks = [c[r] for r in ("tin", "tout", "tcach", "ttotal", "tshare", "accent")]
            for i, a in enumerate(picks):
                for b in picks[i + 1:]:
                    self.assertGreater(sum(abs(x - y) for x, y in zip(a, b)), 60,
                                       f"{slug}: {picks[i]} vs {b}")


class ThemeFileTests(unittest.TestCase):
    """Custom theme files: parse, strict-validate, scan the themes folder."""

    # class attribute on ThemeRegistryTests, not a module global — reference it
    BUILTIN_SLUGS = ThemeRegistryTests.BUILTIN_SLUGS

    GOOD = ('{"name": "My Theme", "colors": {"border": "#101010", "accent": "#202020",'
            ' "label": "#303030", "num": "#F0F0F0", "tin": "#0000FF", "tout": "#FF0000",'
            ' "tcach": "#00FF00", "ttotal": "#FF00FF", "tshare": "#00FFFF"}}')

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.themes = os.path.join(self.tmp.name, "themes")
        os.mkdir(self.themes)

    def write_theme(self, slug, text):
        path = os.path.join(self.themes, slug + ".json")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_parses_hex_case_insensitively(self):
        name, colors = load_theme_file(self.write_theme("mine", self.GOOD))
        self.assertEqual((name, colors["border"], colors["tin"]),
                         ("My Theme", (16, 16, 16), (0, 0, 255)))

    def test_extra_keys_are_ignored(self):
        obj = json.loads(self.GOOD)
        obj["author"] = "someone"
        obj["colors"]["track"] = "#404040"  # legacy role name, not one of the nine
        name, colors = load_theme_file(self.write_theme("extra", json.dumps(obj)))
        self.assertEqual(name, "My Theme")
        self.assertEqual(set(colors), set(THEME_ROLES))
        self.assertNotIn("track", colors)

    def test_missing_role_raises(self):
        obj = json.loads(self.GOOD)
        del obj["colors"]["tin"]
        with self.assertRaises(ThemeError) as ctx:
            load_theme_file(self.write_theme("hole", json.dumps(obj)))
        self.assertIn("tin", str(ctx.exception))
        self.assertIn("hole.json", str(ctx.exception))

    def test_bad_hex_raises(self):
        for bad in ("blue", "#12345G"):
            obj = json.loads(self.GOOD)
            obj["colors"]["tin"] = bad
            with self.assertRaises(ThemeError) as ctx:
                load_theme_file(self.write_theme("badhex", json.dumps(obj)))
            self.assertIn("tin", str(ctx.exception), bad)
            self.assertIn("badhex.json", str(ctx.exception), bad)

    def test_rgb_out_of_range_raises(self):
        for bad in ("#100", [0, 0, 255, 255]):
            obj = json.loads(self.GOOD)
            obj["colors"]["tin"] = bad
            with self.assertRaises(ThemeError) as ctx:
                load_theme_file(self.write_theme("range", json.dumps(obj)))
            self.assertIn("tin", str(ctx.exception), bad)
            self.assertIn("range.json", str(ctx.exception), bad)

    def test_non_dict_shapes_raise(self):
        with self.assertRaises(ThemeError):        # top level a list
            load_theme_file(self.write_theme("list-top", "[1, 2]"))
        with self.assertRaises(ThemeError):        # "colors" a list
            load_theme_file(self.write_theme("list-colors",
                                             '{"name": "T", "colors": [1]}'))
        with self.assertRaises(ThemeError):        # "name" a number
            load_theme_file(self.write_theme("num-name",
                                             '{"name": 7, "colors": {}}'))

    def test_bad_json_raises(self):
        with self.assertRaises(ThemeError) as ctx:
            load_theme_file(self.write_theme("broken", "{oops"))
        self.assertIn("broken.json", str(ctx.exception))

    def test_blank_name_raises(self):
        # "" and whitespace-only are both "no name": a theme that would render
        # an empty title in --theme's list is rejected, not accepted.
        for blank in ("", "   ", "\t\n"):
            obj = json.loads(self.GOOD)
            obj["name"] = blank
            with self.assertRaises(ThemeError) as ctx:
                load_theme_file(self.write_theme("blank-name", json.dumps(obj)))
            self.assertIn("name", str(ctx.exception), repr(blank))
            self.assertIn("blank-name.json", str(ctx.exception), repr(blank))

    def test_lowercase_hex_literals_parse(self):
        # the case-insensitive pin above only ever feeds uppercase letters, and
        # a hand-written theme file is usually lowercase
        obj = json.loads(self.GOOD)
        obj["colors"] = {role: "#a1b2c3" for role in THEME_ROLES}
        name, colors = load_theme_file(self.write_theme("lower", json.dumps(obj)))
        self.assertEqual(name, "My Theme")
        self.assertEqual(set(colors), set(THEME_ROLES))
        self.assertEqual(colors["tin"], (0xA1, 0xB2, 0xC3))
        self.assertEqual(colors["tshare"], (0xA1, 0xB2, 0xC3))

    def test_non_utf8_bytes_raise_and_scan_skips(self):
        # e.g. UTF-16-authored or a binary file misnamed .json: UnicodeDecodeError
        # must surface as ThemeError naming the file, and a scan must skip it
        # with a warning instead of aborting every run.
        path = os.path.join(self.themes, "mojibake.json")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe{")
        with self.assertRaises(ThemeError) as ctx:
            load_theme_file(path)
        self.assertIn("mojibake.json", str(ctx.exception))
        warnings = []
        found = scan_themes(dir=self.themes, warn=warnings.append)
        self.assertNotIn("mojibake", found)
        self.assertEqual(len(warnings), 1)
        self.assertIn("mojibake.json", warnings[0])
        self.assertEqual(set(self.BUILTIN_SLUGS), set(found))

    def test_scan_merges_and_shadows(self):
        self.write_theme("nord", self.GOOD)
        found = scan_themes(dir=self.themes)
        self.assertEqual(found["nord"], ("My Theme", dict(
            load_theme_file(self.write_theme("nord", self.GOOD))[1])))
        self.assertEqual(set(self.BUILTIN_SLUGS)
                         | {"nord"}, set(found))
        self.assertEqual(len(found), 10)  # 10 built-in slugs, nord shadowed in place

    def test_scan_without_folder_is_builtin_only(self):
        missing = os.path.join(self.tmp.name, "does-not-exist")
        self.assertEqual(scan_themes(dir=missing), dict(THEMES))

    def test_scan_skips_broken_file_with_warning(self):
        self.write_theme("mine", self.GOOD)
        self.write_theme("broken", "{oops")
        warnings = []
        found = scan_themes(dir=self.themes, warn=warnings.append)
        self.assertIn("mine", found)
        self.assertNotIn("broken", found)
        self.assertEqual(len(warnings), 1)
        self.assertIn("broken.json", warnings[0])
        self.assertIn("warning:", warnings[0])
        self.assertEqual(set(self.BUILTIN_SLUGS), set(found) - {"mine"})

    def test_scan_skips_config_json(self):
        # themes/config.json is the saved-default file, not a theme: a scan must
        # ignore it (no spurious warning) and never list "config" as a slug.
        self.write_theme("mine", self.GOOD)
        write_default_theme(os.path.join(self.themes, "config.json"), "nord")
        warnings = []
        found = scan_themes(dir=self.themes, warn=warnings.append)
        self.assertIn("mine", found)
        self.assertNotIn("config", found)
        self.assertEqual(warnings, [])
        self.assertEqual(set(self.BUILTIN_SLUGS) | {"mine"}, set(found))


class ThemeExampleTests(unittest.TestCase):
    """themes/examples/ ships reference themes in git: they must stay loadable,
    or the docs teach a format that no longer parses."""

    EXAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "themes", "examples")

    def test_every_example_theme_validates(self):
        files = sorted(f for f in os.listdir(self.EXAMPLES)
                       if f.endswith(".json"))
        self.assertTrue(files, "themes/examples/ should ship an example")
        for fname in files:
            with self.subTest(theme=fname):
                name, colors = tokenstat.load_theme_file(
                    os.path.join(self.EXAMPLES, fname))
                self.assertTrue(name.strip(), fname)
                self.assertEqual(set(colors), set(THEME_ROLES), fname)

    def test_examples_are_not_scanned_as_themes(self):
        # A subfolder is a library, not an active theme: scan_themes lists one
        # folder and does not walk, so an example only becomes a slug when the
        # file itself is copied into themes/.
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "examples")
            os.makedirs(nested)
            src = os.path.join(self.EXAMPLES, "gta-vi.json")
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
            with open(os.path.join(nested, "gta-vi.json"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
            self.assertNotIn("gta-vi", scan_themes(dir=tmp))


class ThemeConfigTests(unittest.TestCase):
    """themes/config.json: read/write the saved default, and resolve a slug."""

    GOOD = ThemeFileTests.GOOD  # a valid one-theme .json body

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # parent folder deliberately left absent: read/write tests create it
        self.themes = os.path.join(self.tmp.name, "themes")
        self.config = os.path.join(self.themes, "config.json")

    def write_config(self, text):
        os.makedirs(self.themes, exist_ok=True)
        with open(self.config, "w") as fh:
            fh.write(text)
        return self.config

    # -- read_default_theme --

    def test_read_missing_config_is_none_and_silent(self):
        warnings = []
        self.assertIsNone(read_default_theme(self.config, warn=warnings.append))
        self.assertEqual(warnings, [])

    def test_read_returns_the_slug(self):            # {"default": "nord"} -> "nord"
        self.write_config('{"default": "nord"}')
        self.assertEqual(read_default_theme(self.config), "nord")

    def test_read_broken_config_warns_and_is_none(self):
        self.write_config("{oops")
        warnings = []
        self.assertIsNone(read_default_theme(self.config, warn=warnings.append))
        self.assertEqual(len(warnings), 1)
        self.assertIn("config.json", warnings[0])

    def test_read_config_without_default_key_is_none(self):
        # a typo'd/lost "default" key, or a non-string/empty value, must warn
        # like any unusable config — not silently drop the saved theme
        for body in ('{"other": "x"}', '{"default": ""}', '{"default": 5}'):
            self.write_config(body)
            warnings = []
            self.assertIsNone(read_default_theme(self.config, warn=warnings.append))
            self.assertEqual(len(warnings), 1, body)
            self.assertIn("config.json", warnings[0])

    def test_read_config_that_is_not_an_object_warns_and_is_none(self):
        # valid JSON that is not an object (a hand-edited list, an empty file
        # that parsed to null) has nowhere to keep a slug: warn, no default.
        for body in ("[1, 2]", "null"):
            self.write_config(body)
            warnings = []
            self.assertIsNone(read_default_theme(self.config, warn=warnings.append))
            self.assertEqual(len(warnings), 1, body)
            self.assertIn("config.json", warnings[0])
            self.assertIn("JSON object", warnings[0], body)

    # -- write_default_theme --

    def test_write_creates_folder_and_file(self):    # parent dir absent beforehand
        self.assertFalse(os.path.isdir(self.themes))
        write_default_theme(self.config, "nord")
        self.assertEqual(read_default_theme(self.config), "nord")
        with open(self.config) as fh:
            self.assertEqual(fh.read(), '{"default": "nord"}\n')

    # -- resolve_theme --

    def test_resolve_none_is_neon(self):
        self.assertEqual(resolve_theme(None), NEON)

    def test_resolve_slug(self):                     # "gruvbox-dark" -> GRUVBOX_DARK
        self.assertEqual(resolve_theme("gruvbox-dark"), GRUVBOX_DARK)

    def test_resolve_unknown_lists_slugs(self):      # ThemeError names "nope" + "nord"
        with self.assertRaises(ThemeError) as ctx:
            resolve_theme("nope")
        msg = str(ctx.exception)
        self.assertIn("nope", msg)
        self.assertIn("nord", msg)

    def test_resolve_sees_a_folder_theme(self):      # tmp dir with one .json
        os.makedirs(self.themes, exist_ok=True)
        with open(os.path.join(self.themes, "mine.json"), "w") as fh:
            fh.write(self.GOOD)
        colors = resolve_theme("mine", dir=self.themes)
        self.assertEqual(colors["border"], (16, 16, 16))
        self.assertEqual(colors["tin"], (0, 0, 255))


class CliColorTests(CliHarness):
    """Piped (non-tty) CLI output stays plain text but keeps bar padding."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1000),
            src_row(sid="s2", model=qwen_lemonade, tin=4500, tout=500),
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_piped_output_plain_but_padded(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        unsloth_line = next(ln for ln in out.splitlines() if "unsloth" in ln)
        # SHARE value then a space-gap before the bar starts
        self.assertRegex(unsloth_line, r"\d+\.\d%   [█░▒▓ ]+$")


class CliNeonTests(CliHarness):
    """tty-colored CLI output: neon header/panels, per-glyph gradient glyph
    bars, painted cells. Color is forced by patching the _color_enabled gate."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1000),
            src_row(sid="s2", model=qwen_lemonade, tin=4500, tout=500),
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")
        self.udb = os.path.join(self.tmp.name, "studio.db")
        # This repo's own themes/ folder stays invisible to these tests: they
        # pin the Neon Midnight default, and a default someone saved with
        # --theme-default must not repaint them. An absent folder plus an absent
        # config is exactly the "no theme chosen" state.
        no_themes = os.path.join(self.tmp.name, "no-themes")
        for name, value in (("THEMES_DIR", no_themes),
                            ("THEME_CONFIG",
                             os.path.join(no_themes, "config.json"))):
            patcher = unittest.mock.patch.object(tokenstat, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_share_rows_render_gradient_glyph_bars(self):
        # runs have no in/out split (all-input rows), but on a terminal their
        # share bars are the SAME per-glyph gradient glyph bars as any other
        # share row — an all-input row renders the pure input-blue endpoint
        self.addCleanup(make_file_runs_db(self.udb,
                                          runs=[run_row("r1")]).close)
        _, out, _ = self.colored_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-runs", "--total", "--model"])
        self.assertIn("█", out)                       # glyph bars, not blocks
        self.assertIn("38;2;{};{};{}m".format(*NEON["tin"]), out)  # input-blue glyphs (fg)
        self.assertNotIn("48;2;", out)                # no background blocks

    def test_reconcile_colored_skips_hero_header(self):
        # compare view pin: no hero "tokens ·" line — reconcile rows carry
        # no summed token classes, and a zero hero would imply values the
        # view never computed
        _, out, _ = self.colored_main(
            ["reconcile", "--db", self.db, "--src", self.src,
             "--unsloth-db", self.no_udb])
        plain = _ANSI_RE.sub("", out)
        self.assertIn("tokenstat · reconcile", plain)
        self.assertNotIn("tokens ·", plain)

    def test_share_view_colored_uses_gradient_glyph_bars(self):
        code, out, _ = self.colored_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("█", out)                           # glyph bars remain
        self.assertIn("38;2;", out)                       # gradient foreground
        self.assertNotIn("48;2;", out)                    # no background blocks
        self.assertIn("38;2;{};{};{}m".format(*NEON["tin"]), out)   # input-blue endpoint glyphs
        self.assertIn("38;2;{};{};{}m".format(*NEON["tout"]), out)  # output-pink endpoint glyphs
        # panel() paints the title strip in two roles, so the title is
        # asserted on ANSI-stripped output (paint splits the substring)
        self.assertIn("tokenstat · usage by model", _ANSI_RE.sub("", out))
        self.assertIn("sorted by share", _ANSI_RE.sub("", out))

    def test_piped_share_view_output_has_no_escapes(self):
        # regression pin: piped runs stay byte-plain even with the violet
        # TOTAL / teal SHARE painting (painting is gated by _color_enabled)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)

    def test_piped_bare_dashboard_has_no_escapes(self):
        # the dashboard path renders hero + panels unconditionally, so the
        # panel/header painters must honor the color gate too: piped stays
        # byte-plain (spec non-goal: piped/NO_COLOR output is plain text)
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)

    def test_cache_overlay_column_is_lime_under_color(self):
        _, out, _ = self.colored_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model", "--cache"])
        self.assertIn("38;2;{};{};{}m".format(*NEON["tcach"]), out)  # CACHE column

    def test_header_panel_prints(self):
        _, out, _ = self.colored_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertIn("tokens", out.splitlines()[0] + out.splitlines()[1])
        self.assertIn("╭", out)

    def test_empty_result_plain_even_when_color_enabled(self):
        _, out, _ = self.colored_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--model", "zzz-nothing"])
        self.assertEqual(out, "no sessions match\n")      # Review Focus pin

    def test_piped_cli_still_byte_identical(self):
        _, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])                       # no patch: piped
        self.assertNotIn("\x1b", out)
        self.assertIn("█", out)                           # glyphs remain plain-path


def share_rows():
    """One complete share-view row (every SHARE_COLUMNS key with a value)."""
    return [{"model": "a", "sessions": 2, "tokens_input": 5,
             "tokens_output": 5, "total": 10, "tok_s": 12.5,
             "share_str": "100.0%", "bar": "█" * 4}]


class ThemePaintTests(unittest.TestCase):
    """Every paint function takes its theme explicitly: no paint path may
    read the NEON global, so a handed-in theme paints every hue on screen."""

    def test_panel_uses_the_given_theme(self):
        theme = dict(NEON, border=(1, 2, 3), label=(4, 5, 6))
        out = "\n".join(panel("t", ["x"], theme=theme))
        self.assertIn("38;2;1;2;3m", out)      # border hue
        self.assertIn("38;2;4;5;6m", out)      # title label hue
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["border"]), out)

    def test_header_block_uses_the_given_theme(self):
        theme = dict(NEON, label=(7, 7, 7), border=(8, 8, 8))
        lines = render_table([{"model": "m", "total": 1}],
                             SESSION_COLUMNS).splitlines()
        paint_header_block(lines, SESSION_COLUMNS, theme=theme)
        self.assertIn("38;2;7;7;7m", lines[0])   # header text = label role
        self.assertIn("38;2;8;8;8m", lines[1])   # rule = border role

    def test_report_header_uses_the_given_theme(self):
        theme = dict(NEON, ttotal=(9, 9, 9), tshare=(11, 11, 11))
        rows = [{"total": 7_300_000, "tokens_input": 1, "tokens_output": 1}]
        hero = "\n".join(report_header("t", rows, theme=theme, unit="models"))
        self.assertIn("38;2;9;9;9m", hero)       # grand total = ttotal
        self.assertIn("38;2;11;11;11m", hero)    # row count = tshare

    def test_table_panel_threads_the_theme_through_every_painter(self):
        # A registry dict — the exact shape Task 6 resolves and hands down.
        theme = THEMES["dracula"][1]
        out = "\n".join(_table_panel("t", share_rows(), SHARE_COLUMNS,
                                     True, True, theme=theme))
        self.assertIn("38;2;{};{};{}m".format(*theme["tin"]), out)     # bar + IN
        self.assertIn("38;2;{};{};{}m".format(*theme["tout"]), out)    # bar + OUT
        self.assertIn("38;2;{};{};{}m".format(*theme["ttotal"]), out)  # TOTAL
        self.assertIn("38;2;{};{};{}m".format(*theme["label"]), out)   # header text
        self.assertIn("38;2;{};{};{}m".format(*theme["border"]), out)  # rule + frame
        # Dracula differs from Neon Midnight on every role — asserted first so a
        # future palette edit fails with a clear message, and one leftover NEON
        # read anywhere in the paint layer shows up in the loop below.
        for role in THEME_ROLES:
            self.assertNotEqual(theme[role], NEON[role], role)
            self.assertNotIn("38;2;{};{};{}m".format(*NEON[role]), out)

    def test_table_panel_without_a_theme_is_the_neon_default(self):
        # the default keyword keeps today's output byte-identical
        args = ("t", share_rows(), SHARE_COLUMNS, True, True)
        self.assertEqual(_table_panel(*args), _table_panel(*args, theme=NEON))

    def test_table_panel_plain_path_stays_plain_with_a_theme(self):
        # color=False must not smuggle escape bytes in through the theme
        out = "\n".join(_table_panel("t", share_rows(), SHARE_COLUMNS,
                                     True, False, theme=THEMES["nord"][1]))
        self.assertNotIn("\x1b", out)


class CliThemeTests(CliHarness):
    """--theme / --theme-default: the two actions, precedence, painted bytes.
    Modeled on CliNeonTests: same fixture, same color-forcing helper."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        qwen_lemonade = '{"id":"qwen-mini","providerID":"lemonade"}'
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1000),
            src_row(sid="s2", model=qwen_lemonade, tin=4500, tout=500),
        ]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")
        self.base = ["--db", self.db, "--src", self.src,
                     "--unsloth-db", self.no_udb]
        # Every test gets an isolated themes folder. main() must read the
        # THEMES_DIR / THEME_CONFIG module globals at call time (the default
        # arguments of resolve_theme/read_default_theme are bound at def time),
        # so patching them here is what steers the CLI. The folder stays absent
        # until a test creates it.
        self.themes = os.path.join(self.tmp.name, "themes")
        self.config = os.path.join(self.themes, "config.json")
        for name, value in (("THEMES_DIR", self.themes),
                            ("THEME_CONFIG", self.config)):
            patcher = unittest.mock.patch.object(tokenstat, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def exit_two(self, argv):
        """Run main() expecting argparse's exit 2; returns the stderr text."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(err):
            main(argv)
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def write_theme_file(self, slug, text):
        os.makedirs(self.themes, exist_ok=True)
        path = os.path.join(self.themes, slug + ".json")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    # -- the list action --

    def test_bare_theme_lists_themes_and_marks_default(self):
        code, out, _ = self.run_main(["--theme"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), len(THEMES))     # one line per theme
        self.assertIn("nord \u2014 Nord", out)
        self.assertIn("gruvbox-dark \u2014 Gruvbox Dark", out)
        self.assertEqual(sorted(ln.split(" \u2014 ")[0] for ln in lines),
                         sorted(THEMES))              # sorted, slug first
        # nothing saved: Neon Midnight is the default in force
        neon = next(ln for ln in lines if ln.startswith("neon-midnight"))
        self.assertTrue(neon.endswith("(default)"), neon)
        # with a saved default the marker moves to that line
        write_default_theme(self.config, "nord")
        code, out, _ = self.run_main(["--theme"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        nord = next(ln for ln in lines if ln.startswith("nord "))
        self.assertTrue(nord.endswith("(default)"), nord)
        neon = next(ln for ln in lines if ln.startswith("neon-midnight"))
        self.assertNotIn("(default)", neon)

    def test_help_advertises_the_list_action(self):
        # Listing is only useful if --help says it exists: the bare flag leads
        # the --theme help line so it is not a hidden feature.
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stdout(out):
            main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        # argparse wraps help at the terminal width, so compare reflowed text
        help_text = " ".join(out.getvalue().split())
        self.assertIn("bare --theme lists all themes and exits", help_text)

    def test_bare_theme_warns_about_a_broken_theme_file(self):
        # The list is where a user looks for the theme they just wrote, so a
        # file that failed validation must say why it is missing.
        self.write_theme_file("broken", "{oops")
        self.write_theme_file("mine", ThemeFileTests.GOOD)
        code, out, err = self.run_main(["--theme"])
        self.assertEqual(code, 0)
        self.assertIn("mine \u2014 My Theme", out)
        self.assertNotIn("broken", out)
        self.assertIn("warning:", err)
        self.assertIn("broken.json", err)

    def test_a_stale_default_marks_no_line(self):
        # a saved slug whose file is gone marks nothing in the list: the marker
        # only ever lands on a listed theme. The run itself warns and falls back
        # (pinned by test_a_stale_default_warns_and_still_renders).
        write_default_theme(self.config, "gone")
        _, out, _ = self.run_main(["--theme"])
        self.assertEqual(len([ln for ln in out.splitlines()
                              if ln.endswith("(default)")]), 0)

    def test_a_stale_default_is_explained_on_stderr(self):
        # zero markers with no explanation is a silent bug in the one place the
        # user came to diagnose a missing theme: name the config and the slug.
        write_default_theme(self.config, "gone")
        code, out, err = self.run_main(["--theme"])
        self.assertEqual(code, 0)
        self.assertIn("config.json", err)
        self.assertIn("gone", err)
        self.assertIn("warning:", err)
        # ...and stdout's list is byte-stable: one line per theme, nothing added
        self.assertEqual(len(out.splitlines()), len(THEMES))
        # a default that does resolves needs no explanation
        write_default_theme(self.config, "nord")
        code, out, err = self.run_main(["--theme"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(out.splitlines()), len(THEMES))
        self.assertEqual(len([ln for ln in out.splitlines()
                              if ln.endswith("(default)")]), 1)

    # -- the save action --

    def test_theme_default_saves_and_later_runs_use_it(self):
        code, out, _ = self.run_main(["--theme-default=nord"])
        self.assertEqual(code, 0)
        self.assertIn("default theme: nord (Nord)", out)
        with open(self.config) as fh:
            self.assertEqual(json.load(fh), {"default": "nord"})
        # a later run with no --theme wears the saved theme
        code, out, _ = self.colored_main(self.base + ["--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*NORD["tin"]), out)
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["tin"]), out)

    def test_bare_theme_default_saves_this_runs_theme(self):
        code, out, _ = self.run_main(["--theme", "nord", "--theme-default"])
        self.assertEqual(code, 0)
        self.assertIn("default theme: nord (Nord)", out)
        self.assertEqual(read_default_theme(self.config), "nord")

    def test_theme_default_wins_over_the_list_action(self):
        # Both flags at once: `--theme=SLUG --theme-default` is the documented
        # "save this run's theme" form, so the save short-circuits before the
        # list is ever considered — the list must not be printed instead.
        for argv in (["--theme=nord", "--theme-default"],
                     ["--theme-default=nord", "--theme"]):
            code, out, _ = self.run_main(argv)
            self.assertEqual(code, 0, argv)
            self.assertEqual(out, "default theme: nord (Nord)\n", argv)
            self.assertEqual(read_default_theme(self.config), "nord")

    def test_theme_default_without_a_theme_errors(self):
        # and the message names the flag and the fix, not just the refusal
        err = self.exit_two(["--theme-default"])
        self.assertIn("--theme=SLUG", err)
        self.assertIn("same invocation", err)
        self.assertIn("--theme-default=SLUG", err)
        self.assertFalse(os.path.exists(self.config))

    def test_theme_default_rejects_an_unknown_slug(self):
        # write_default_theme does not validate: a typo must never land in
        # themes/config.json.
        err = self.exit_two(["--theme-default=nope"])
        self.assertIn("nope", err)
        self.assertIn("nord", err)                    # the slugs are listed
        self.assertFalse(os.path.exists(self.config))

    def test_an_unwritable_config_target_is_a_clean_error(self):
        # A read-only themes/ (here: the parent folder is a plain file, which
        # needs no chmod and so cannot leak permissions into the cleanup) is a
        # CLI failure like every other one: tokenstat: error: ..., exit 2, and
        # never a Python traceback.
        blocker = os.path.join(self.tmp.name, "not-a-folder")
        with open(blocker, "w") as fh:
            fh.write("a file where a folder should be")
        stuck = os.path.join(blocker, "config.json")
        with unittest.mock.patch.object(tokenstat, "THEME_CONFIG", stuck):
            err = self.exit_two(["--theme-default=nord"])
        self.assertIn("could not write", err)
        self.assertIn("config.json", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(stuck))

    # -- the empty-value forms --

    def test_empty_theme_value_is_no_theme_given(self):
        # `--theme=` is not the empty slug: it behaves as if the flag were
        # absent, so the saved default still applies.
        write_default_theme(self.config, "nord")
        code, out, _ = self.colored_main(self.base + ["--theme="])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*NORD["tin"]), out)
        # the same invocation piped: a report still renders, and nothing
        # complains about an empty slug (nord is still the saved default)
        code, out, err = self.run_main(self.base + ["--theme="])
        self.assertEqual(code, 0)
        self.assertIn("tokens \u00b7", _ANSI_RE.sub("", out))
        self.assertNotIn("unknown theme", err)

    def test_empty_theme_default_value_takes_the_bare_flag_path(self):
        # `--theme-default=` is the bare flag, not a request to save '': it
        # errors the same way, and with a --theme=SLUG it saves like the bare form.
        err = self.exit_two(["--theme-default="])
        self.assertIn("--theme=SLUG", err)
        self.assertNotIn("unknown theme", err)
        self.assertFalse(os.path.exists(self.config))
        code, out, _ = self.run_main(["--theme=nord", "--theme-default="])
        self.assertEqual(code, 0)
        self.assertEqual(out, "default theme: nord (Nord)\n")
        self.assertEqual(read_default_theme(self.config), "nord")

    # -- errors --

    def test_unknown_theme_exits_2_and_lists(self):
        err = self.exit_two(self.base + ["--theme", "nope"])
        self.assertIn("nope", err)
        self.assertIn("nord", err)                    # available slugs listed

    def test_theme_swallowing_a_command_is_a_theme_error(self):
        # Review Focus: `--theme reconcile` is the known --cost quirk — the
        # error is about the bad slug, not argparse blaming the command word.
        err = self.exit_two(self.base + ["--theme", "reconcile"])
        self.assertIn("reconcile", err)
        self.assertIn("available", err)
        self.assertNotIn("invalid choice", err)

    def test_a_stale_default_warns_and_still_renders(self):
        # A saved default naming a theme file that no longer exists is a config
        # that cannot be used: warn naming the config, and render with Neon
        # Midnight. The data is fine — a cosmetic default must not cost the user
        # their report until they hand-edit a file.
        write_default_theme(self.config, "gone")
        code, out, err = self.run_main(self.base)
        self.assertEqual(code, 0)
        self.assertIn("tokens \u00b7", _ANSI_RE.sub("", out))   # the report rendered
        self.assertIn("config.json", err)
        self.assertIn("gone", err)                             # and why
        self.assertNotIn("\x1b", out)                          # piped stays plain
        # the fallback is the built-in default theme, painted on a terminal
        code, out, _ = self.colored_main(self.base + ["--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*NEON["tin"]), out)

    def test_a_stale_default_naming_a_broken_file_gives_the_real_reason(self):
        # A default that points at a theme file the user CAN see, but which
        # failed validation, must not read as a plain "unknown theme": the scan
        # gets resolve_theme's warn hook here, so its own reason prints first.
        self.write_theme_file("x", '{"name": "X"}')     # valid JSON, no colors
        write_default_theme(self.config, "x")
        code, out, err = self.run_main(self.base)
        self.assertEqual(code, 0)
        self.assertIn("x.json", err)                    # names the file
        self.assertIn("colors", err)                    # and the real reason
        self.assertIn("config.json", err)               # and who points at it
        self.assertIn("tokens \u00b7", _ANSI_RE.sub("", out))   # report rendered
        # and it is Neon Midnight that rendered, not a half-loaded theme
        code, out, _ = self.colored_main(self.base + ["--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*NEON["tin"]), out)

    def test_an_explicit_missing_theme_still_fails_loudly(self):
        # The fallback is only for the saved default: a slug the user typed this
        # run is their own request, so an unknown one stays a hard exit 2.
        write_default_theme(self.config, "gone")
        err = self.exit_two(self.base + ["--theme=gone"])
        self.assertIn("gone", err)
        # an explicit, valid theme wins over the broken config and says nothing
        # about it (the fixture's absent studio.db warns by design — the config
        # must not add to that noise)
        code, out, err = self.colored_main(self.base + ["--theme=nord"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*NORD["tin"]), out)
        self.assertNotIn("config.json", err)

    # -- painting --

    def test_theme_flag_paints_that_palette(self):
        code, out, _ = self.colored_main(self.base + ["--total", "--model",
                                                      "--theme", "gruvbox-dark"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;{};{};{}m".format(*GRUVBOX_DARK["tin"]), out)
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["tin"]), out)
        self.assertIn("38;2;{};{};{}m".format(*GRUVBOX_DARK["ttotal"]), out)

    def test_theme_flag_moves_the_bar_gradient(self):
        # the share-bar endpoints come from the resolved theme, not the
        # import-time TOKEN_IN_RGB/TOKEN_OUT_RGB globals
        _, out, _ = self.colored_main(self.base + ["--total", "--model",
                                                   "--theme=gruvbox-dark"])
        line = next(ln for ln in out.splitlines() if "\u2588" in ln)
        # the bar is the line's last column: start at the SGR code in front of
        # its first glyph, stop at the reset that closes the gradient run
        start = line.rindex("\x1b[", 0, line.index("\u2588"))
        bar = line[start:].split("\x1b[0m")[0]
        rgbs = [tuple(int(g) for g in m) for m in
                re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", bar)]
        self.assertGreater(len(rgbs), 1)
        self.assertEqual(rgbs[0], GRUVBOX_DARK["tin"])    # left end: theme tin
        self.assertEqual(rgbs[-1], GRUVBOX_DARK["tout"])  # right end: theme tout

    def test_hero_frame_wears_the_theme(self):
        # report_header hands its theme to panel(); pin the frame (and the
        # title strip / header text label role) wearing the theme's hues.
        _, out, _ = self.colored_main(self.base + ["--theme=nord"])
        self.assertIn("38;2;{};{};{}m".format(*NORD["border"]), out)
        self.assertIn("38;2;{};{};{}m".format(*NORD["label"]), out)
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["border"]), out)
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["label"]), out)

    def test_folder_theme_resolves_through_the_cli(self):
        # main passes THEMES_DIR (read at call time) into the scan, so a file
        # dropped in themes/ is selectable by slug with no code change.
        self.write_theme_file("mine", ThemeFileTests.GOOD)
        code, out, _ = self.colored_main(self.base + ["--total", "--model",
                                                      "--theme=mine"])
        self.assertEqual(code, 0)
        self.assertIn("38;2;0;0;255m", out)          # GOOD's tin
        self.assertNotIn("38;2;{};{};{}m".format(*NEON["tin"]), out)

    def test_broken_theme_config_warns_on_a_normal_run(self):
        # a config that exists but cannot be used must not silently drop the
        # saved theme: main passes warn= through to read_default_theme.
        os.makedirs(self.themes, exist_ok=True)
        with open(self.config, "w") as fh:
            fh.write("{oops")
        code, out, err = self.run_main(self.base)
        self.assertEqual(code, 0)
        self.assertIn("warning:", err)
        self.assertIn("config.json", err)

    # -- overlay, gating, plumbing --

    def test_theme_alone_still_renders_the_dashboard(self):
        # --theme is a display overlay like --cost/--cache: the bare-dashboard
        # trigger stays a truly bare invocation.
        code, out, _ = self.run_main(self.base + ["--theme=nord"])
        self.assertEqual(code, 0)
        plain = _ANSI_RE.sub("", out)
        self.assertIn("usage by model \u00b7 top 5 by share", plain)
        self.assertIn("sessions \u00b7 5 most recent", plain)
        self.assertIn("tokens \u00b7", plain)          # the hero is there too

    def test_theme_actions_work_without_a_store(self):
        # Review Focus: both actions short-circuit before implicit ingestion,
        # so they work even when --db cannot be opened at all.
        dead = os.path.join(self.tmp.name, "no-such-dir-xyz", "usage.db")
        code, out, _ = self.run_main(["--db", dead, "--theme-default=nord"])
        self.assertEqual(code, 0)
        self.assertIn("default theme: nord", out)
        code, out, _ = self.run_main(["--db", dead, "--theme"])
        self.assertEqual(code, 0)
        self.assertIn("nord \u2014 Nord", out)

    def test_theme_never_colors_piped_or_json_output(self):
        # Review Focus: color stays gated by _color_enabled under every theme.
        for extra in (["--theme=nord"],
                      ["--theme=gruvbox-dark", "--total", "--model"],
                      ["--theme=nord", "--daily"]):
            code, out, _ = self.run_main(self.base + extra)
            self.assertEqual(code, 0, extra)
            self.assertNotIn("\x1b", out, extra)
        _, plain_json, _ = self.run_main(
            self.base + ["--total", "--model", "--json"])
        _, themed_json, _ = self.run_main(
            self.base + ["--total", "--model", "--json", "--theme=nord"])
        self.assertEqual(plain_json, themed_json)

    def test_theme_default_is_gitignored(self):
        # themes/config.json is personal state: this task owns the rule.
        # --no-index matters: without it, check-ignore exits 1 for a TRACKED
        # path, so committing the file would turn this into a confusing
        # "not ignored" failure instead of the intended tripwire.
        repo = os.path.dirname(os.path.abspath(tokenstat.__file__))
        proc = subprocess.run(["git", "check-ignore", "--no-index", "-q",
                               "themes/config.json"],
                              cwd=repo, capture_output=True)
        self.assertEqual(proc.returncode, 0,
                         "themes/*.json must be gitignored")


class DefaultPathsTests(unittest.TestCase):
    def test_state_lives_in_one_home_directory(self):
        h = "/tmp/fake-home"
        self.assertEqual(tokenstat.state_dir(h), "/tmp/fake-home/.tokenstat")
        self.assertEqual(tokenstat.default_db(h), "/tmp/fake-home/.tokenstat/usage.db")
        self.assertEqual(tokenstat.themes_dir(h), "/tmp/fake-home/.tokenstat/themes")
        self.assertEqual(tokenstat.theme_config(h),
                         "/tmp/fake-home/.tokenstat/themes/config.json")

    def test_xdg_data_home_moves_the_opencode_default(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"XDG_DATA_HOME": "/xdg/data"}):
            self.assertEqual(tokenstat.default_src("/tmp/fake-home"),
                             "/xdg/data/opencode/opencode.db")

    def test_empty_or_relative_xdg_data_home_is_ignored(self):
        for value in ("", "relative/path"):
            with unittest.mock.patch.dict(os.environ, {"XDG_DATA_HOME": value}):
                self.assertEqual(tokenstat.default_src("/tmp/fake-home"),
                                 "/tmp/fake-home/.local/share/opencode/opencode.db")

    def test_unsloth_default_is_the_studio_location(self):
        self.assertEqual(tokenstat.unsloth_db("/tmp/fake-home"),
                         "/tmp/fake-home/.unsloth/studio/studio.db")

    def test_module_defaults_point_at_the_state_dir(self):
        # Pin the import-time bindings themselves: exec a second, isolated
        # copy of tokenstat.py under a fake HOME and check each global
        # against a literal. Comparing a live global with the function it is
        # assigned from holds by construction and cannot fail. The live
        # module is never reloaded, so the class identities (ThemeError et
        # al.) the rest of the suite imported at module level stay intact.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "tokenstat_fresh", tokenstat.__file__)
        fresh = importlib.util.module_from_spec(spec)
        with unittest.mock.patch.dict(os.environ, {"HOME": "/tmp/fake-home",
                                                   "XDG_DATA_HOME": ""}):
            spec.loader.exec_module(fresh)
        self.assertEqual(fresh.STATE_DIR, "/tmp/fake-home/.tokenstat")
        self.assertEqual(fresh.DEFAULT_DB, "/tmp/fake-home/.tokenstat/usage.db")
        self.assertEqual(fresh.THEMES_DIR, "/tmp/fake-home/.tokenstat/themes")
        self.assertEqual(fresh.THEME_CONFIG,
                         "/tmp/fake-home/.tokenstat/themes/config.json")
        self.assertEqual(fresh.DEFAULT_SRC,
                         "/tmp/fake-home/.local/share/opencode/opencode.db")
        self.assertEqual(fresh.DEFAULT_UNSLOTH_DB,
                         "/tmp/fake-home/.unsloth/studio/studio.db")


def read_text(path):
    """Small file's contents, closed on the way out: the suite must stay clean
    under -W error::ResourceWarning, so no bare open(...).read() here."""
    with open(path) as fh:
        return fh.read()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.legacy = os.path.join(self.tmp.name, "legacy")
        os.makedirs(self.legacy)

    def test_creates_the_state_dir_and_uses_it_when_empty(self):
        self.assertEqual(tokenstat.default_store(self.state, self.legacy),
                         os.path.join(self.state, "usage.db"))
        self.assertTrue(os.path.isdir(self.state))

    def test_a_legacy_store_moves_into_the_state_dir(self):
        old = os.path.join(self.legacy, "usage.db")
        with open(old, "w") as fh:
            fh.write("history")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, os.path.join(self.state, "usage.db"))
        self.assertEqual(read_text(used), "history")
        self.assertFalse(os.path.exists(old))
        self.assertIn("migrated", err.getvalue())

    def test_wal_and_shm_siblings_move_too(self):
        for suffix in ("", "-wal", "-shm"):
            with open(os.path.join(self.legacy, "usage.db" + suffix), "w") as fh:
                fh.write(suffix)
        tokenstat.default_store(self.state, self.legacy)
        for suffix in ("", "-wal", "-shm"):
            self.assertFalse(os.path.exists(
                os.path.join(self.legacy, "usage.db" + suffix)))

    def test_migration_is_a_no_op_once_the_store_exists(self):
        os.makedirs(self.state)
        keep = os.path.join(self.state, "usage.db")
        with open(keep, "w") as fh:
            fh.write("new")
        with open(os.path.join(self.legacy, "usage.db"), "w") as fh:
            fh.write("old")
        tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(read_text(keep), "new")

    def test_an_unmovable_legacy_store_is_used_in_place(self):
        old = os.path.join(self.legacy, "usage.db")
        with open(old, "w") as fh:
            fh.write("history")
        with unittest.mock.patch.object(tokenstat.os, "replace",
                                        side_effect=OSError("cross-device")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, old)                      # never an empty store
        self.assertIn("could not migrate", err.getvalue())

    def test_a_sibling_that_will_not_move_leaves_the_legacy_store_whole(self):
        # A sibling that refuses to move (immutable file, another filesystem, a
        # live connection on the -wal) must not cost the history: if the main
        # file has already crossed over, returning the legacy path hands main()
        # a path with nothing at it, so sqlite3.connect creates a fresh EMPTY
        # store there and the real history sits unseen at the new path forever
        # (the next run sees both files and skips the migration block).
        old = os.path.join(self.legacy, "usage.db")
        for suffix in ("", "-wal"):
            with open(old + suffix, "w") as fh:
                fh.write("history" + suffix)
        real_replace = os.replace

        def replace(src, dst):
            if src.endswith("-wal"):
                raise OSError("locked by another connection")
            return real_replace(src, dst)

        with unittest.mock.patch.object(tokenstat.os, "replace",
                                        side_effect=replace):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                used = tokenstat.default_store(self.state, self.legacy)
        # Whatever path comes back must be one that still holds the history.
        self.assertTrue(os.path.exists(used), used)
        self.assertEqual(used, old)
        self.assertEqual(read_text(used), "history")
        # A -wal must never be stranded on the legacy side: uncheckpointed
        # commits live in the WAL, so it belongs with its main file or not at
        # all — the two are either both here or both gone.
        self.assertEqual(os.path.exists(old), os.path.exists(old + "-wal"),
                         "-wal stranded away from its main db")
        self.assertIn("could not migrate", err.getvalue())

    def test_a_main_file_that_will_not_move_pulls_the_siblings_back(self):
        # The rollback half: the siblings are already across when the store
        # itself fails, so they must come back with it. A half-migrated store
        # (main here, WAL there) is worse than either whole store.
        old = os.path.join(self.legacy, "usage.db")
        for suffix in ("", "-wal", "-shm"):
            with open(old + suffix, "w") as fh:
                fh.write("history" + suffix)
        real_replace = os.replace

        def replace(src, dst):
            if src.endswith("usage.db"):
                raise OSError("cross-device link")
            return real_replace(src, dst)

        with unittest.mock.patch.object(tokenstat.os, "replace",
                                        side_effect=replace):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, old)
        self.assertEqual(read_text(old), "history")
        for suffix in ("-wal", "-shm"):
            self.assertEqual(read_text(old + suffix), "history" + suffix)
            self.assertFalse(os.path.exists(
                os.path.join(self.state, "usage.db" + suffix)))
        self.assertIn("could not migrate", err.getvalue())

    def test_a_lost_migration_race_lands_on_the_winners_store(self):
        # Two bare runs (the hourly cron and an interactive one) can both pass
        # the "legacy exists and state does not" check. The winner moves the
        # store; the loser's os.replace then raises FileNotFoundError, and the
        # loser must end up on the winner's POPULATED store — returning the
        # legacy path here would seed an empty store beside the script that
        # cron then fills with fresh rows.
        old = os.path.join(self.legacy, "usage.db")
        new = os.path.join(self.state, "usage.db")
        with open(old, "w") as fh:
            fh.write("history")
        real_replace = os.replace

        def replace(src, dst):
            real_replace(src, dst)     # the winner's move lands first
            raise FileNotFoundError(2, "No such file or directory")

        with unittest.mock.patch.object(tokenstat.os, "replace",
                                        side_effect=replace):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, new)
        self.assertEqual(read_text(new), "history")
        self.assertFalse(os.path.exists(old))
        # Nothing went wrong here, so nothing to warn about: the store is not
        # empty and it is not the legacy one.
        self.assertNotIn("could not migrate", err.getvalue())

    def test_an_unwritable_state_dir_falls_back_to_the_legacy_store(self):
        old = os.path.join(self.legacy, "usage.db")
        with open(old, "w") as fh:
            fh.write("history")
        with unittest.mock.patch.object(tokenstat.os, "makedirs",
                                        side_effect=OSError("read-only")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                used = tokenstat.default_store(self.state, self.legacy)
        self.assertEqual(used, old)
        self.assertIn("could not create", err.getvalue())

    def test_the_module_globals_steer_the_defaults_at_call_time(self):
        # state/legacy are read from the globals when the function runs, not
        # frozen into def-time defaults: that is what lets a caller repoint
        # STATE_DIR and LEGACY_DIR (and what keeps main()'s run out of $HOME
        # the moment the globals move).
        with unittest.mock.patch.object(tokenstat, "STATE_DIR", self.state), \
                unittest.mock.patch.object(tokenstat, "LEGACY_DIR",
                                           self.legacy):
            self.assertEqual(tokenstat.default_store(),
                             os.path.join(self.state, "usage.db"))
        self.assertTrue(os.path.isdir(self.state))

    def test_legacy_theme_files_move_with_the_store(self):
        old_themes = os.path.join(self.legacy, "themes")
        os.makedirs(os.path.join(old_themes, "examples"))
        with open(os.path.join(old_themes, "mine.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(old_themes, "README.md"), "w") as fh:
            fh.write("docs")
        with open(os.path.join(old_themes, "examples", "gta.json"), "w") as fh:
            fh.write("{}")
        with contextlib.redirect_stderr(io.StringIO()):
            tokenstat.default_store(self.state, self.legacy)
        self.assertTrue(os.path.exists(
            os.path.join(self.state, "themes", "mine.json")))
        self.assertTrue(os.path.exists(os.path.join(old_themes, "README.md")))
        self.assertTrue(os.path.exists(
            os.path.join(old_themes, "examples", "gta.json")))


class FreshMachineTests(unittest.TestCase):
    """The end-user promise, end to end: a machine that has never run tokenstat
    gets a report, and tokenstat's own state appears under $HOME — never
    beside the script, and never depending on the working directory."""

    def test_runs_from_anywhere_against_an_empty_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            home = os.path.join(tmp, "home")
            elsewhere = os.path.join(tmp, "elsewhere")
            for path in (bin_dir, home, elsewhere):
                os.makedirs(path)
            # A copy of the script with no legacy state beside it: this is a
            # machine that has never seen tokenstat, not this working tree.
            script = os.path.join(bin_dir, "tokenstat.py")
            shutil.copyfile(tokenstat.__file__, script)
            # OpenCode history but no tokenstat state: the dashboard needs a
            # session to render, and the absent studio.db is what leaves the
            # two "could not read" warnings on stderr (report mode snapshots
            # events and runs, so it warns once per step).
            src_dir = os.path.join(home, ".local", "share", "opencode")
            os.makedirs(src_dir)
            make_file_db(os.path.join(src_dir, "opencode.db"),
                         [src_row(sid="s1", title="alpha")]).close()
            proc = subprocess.run(
                [sys.executable, script], cwd=elsewhere,
                env={"HOME": home, "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.isfile(
                os.path.join(home, ".tokenstat", "usage.db")))
            self.assertIn("tokenstat", proc.stdout)
            self.assertEqual(proc.stderr.count("could not read"), 2)
            self.assertIn(tokenstat.unsloth_db(home), proc.stderr)
            # Nothing was written next to the script it ran from.
            self.assertFalse(os.path.exists(os.path.join(bin_dir, "usage.db")))


class SchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_filter_indexes(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        created = dest.execute(
            "PRAGMA index_info(idx_unsloth_events_created)").fetchall()
        tcreated = dest.execute(
            "PRAGMA index_info(idx_usage_time_created)").fetchall()
        self.assertEqual(len(created), 1)
        self.assertEqual(len(tcreated), 1)


class StepsSchemaTests(unittest.TestCase):
    def test_ensure_schema_creates_opencode_steps(self):
        dest = make_dest()
        self.addCleanup(dest.close)
        # PRAGMA table_info -> (cid, name, type, notnull, dflt_value, pk).
        # Pin (name, type, notnull, pk) so a wrongly-typed, wrongly-nullable
        # or non-PRIMARY-KEY column fails — names alone cannot catch that.
        shape = [(r[1], r[2], r[3], r[5])
                 for r in dest.execute("PRAGMA table_info(opencode_steps)")]
        self.assertEqual(shape, [
            ("message_id", "TEXT", 0, 1),           # PRIMARY KEY (SQLite leaves notnull=0)
            ("session_id", "TEXT", 1, 0),           # NOT NULL
            ("turn", "INTEGER", 1, 0),              # NOT NULL DEFAULT 0
            ("step_ms", "INTEGER", 0, 0),           # nullable
            ("tokens_generated", "INTEGER", 1, 0),  # NOT NULL DEFAULT 0
            ("time_created", "INTEGER", 1, 0),      # NOT NULL
        ])
        idx = [r[1] for r in dest.execute(
            "PRAGMA index_list(opencode_steps)")]
        self.assertIn("idx_opencode_steps_session_turn", idx)


class TurnIndexTests(unittest.TestCase):
    def test_walks_back_past_synthetic_to_the_opening_user(self):
        # Reference session: a mid-turn synthetic must NOT open the turn.
        t = ["user", "assistant", "synthetic", "assistant", "assistant", "idle"]
        self.assertEqual(_turn_index(t, 5, legacy=False), 0)

    def test_stops_at_the_preceding_idle(self):
        t = ["user", "assistant", "idle", "user", "assistant", "assistant"]
        self.assertEqual(_turn_index(t, 5, legacy=False), 3)

    def test_synthetic_opens_a_turn_when_no_user_precedes(self):
        t = ["user", "assistant", "idle", "synthetic", "assistant"]
        self.assertEqual(_turn_index(t, 4, legacy=False), 3)

    def test_legacy_returns_nearest_input_and_does_not_walk_past_idle(self):
        t = ["user", "assistant", "idle", "user", "assistant"]
        self.assertEqual(_turn_index(t, 4, legacy=True), 3)

    def test_legacy_nearest_input_differs_from_full_walk(self):
        # A mid-turn synthetic is where the two rules genuinely disagree.
        # legacy exercises the "nearest preceding input" short-circuit: it
        # stops at the synthetic (index 1) and never reaches the user. The
        # non-legacy full walk remembers the last input while walking back,
        # so the user at index 0 overwrites it — the synthetic does not open
        # the turn. Deleting the `if legacy: return i` short-circuit makes the
        # legacy call fall through to the full walk and return 0, failing here.
        t = ["user", "synthetic", "assistant"]
        self.assertEqual(_turn_index(t, 3, legacy=True), 1)    # nearest input
        self.assertEqual(_turn_index(t, 3, legacy=False), 0)   # turn-opening user

    def test_no_input_message_returns_minus_one(self):
        self.assertEqual(_turn_index(["assistant", "assistant"], 2, False), -1)


class FilterHelperTests(unittest.TestCase):
    """Shared filter primitives: one LIKE pattern builder and one date window,
    used by all three _*_filters builders."""

    def test_like_pattern_is_lowered_and_escaped(self):
        self.assertEqual(tokenstat._like_pattern("A%B_C"), "%a\\%b\\_c%")

    def test_date_clauses_are_inclusive_and_until_day_end(self):
        since, until = ms(2026, 11, 1), ms(2026, 11, 3)
        where, params = tokenstat._date_clauses("time_created", since, until)
        self.assertEqual(where, ["time_created >= ?", "time_created < ?"])
        self.assertEqual(params, [since, tokenstat._day_end_ms(until)])

    def test_date_clauses_are_empty_when_unbounded(self):
        self.assertEqual(tokenstat._date_clauses("created_at", None, None),
                         ([], []))


class LikeEscapingTests(unittest.TestCase):
    """LIKE metacharacters in --model patterns are escaped before wrapping,
    so they match literally and can never act as wildcards."""

    def setUp(self):
        self.dest = make_dest()
        self.addCleanup(self.dest.close)
        src = make_src([src_row(sid="a", model='{"id":"50%off","providerID":"x"}'),
                        src_row(sid="b", model=LEMONADE_JSON)])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)

    def test_model_percent_is_literal(self):
        # "gpt-50-mini" contains the digit pair "50": an unescaped `%`
        # (pattern %50%%) would match it too. Only a literal "50%" may match.
        src = make_src([src_row(sid="p2",
                                model='{"id":"gpt-50-mini","providerID":"z"}')])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=1)
        rows = query_sessions(self.dest, model="50%")
        self.assertEqual([r["model"] for r in rows],
                         ["x/50%off"])   # only the literal-percent model

    def test_model_underscore_is_literal(self):
        src = make_src([src_row(sid="c", model='{"id":"a_b","providerID":"y"}')])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=2)
        # "axb" matches when `_` is a wildcard (pattern %a_b%) but does not
        # contain the literal "a_b".
        src = make_src([src_row(sid="u2", model='{"id":"axb","providerID":"w"}')])
        self.addCleanup(src.close)
        record(src, self.dest, now_ms=2)
        rows = query_sessions(self.dest, model="a_b")
        self.assertEqual([r["model"] for r in rows], ["y/a_b"])

    def test_event_percent_is_literal(self):
        # The event filter has two LIKE clauses sharing one pattern param; if
        # either lacked ESCAPE '\', the OR would let "gpt-50-mini" leak in.
        src = make_unsloth_src([unsloth_event("e1", model="x/50%off"),
                                unsloth_event("e2", model="gpt-50-mini")])
        self.addCleanup(src.close)
        import_unsloth(src, self.dest)
        rows = query_events(self.dest, model="50%")
        self.assertEqual([r["model"] for r in rows], ["x/50%off"])

    def test_run_percent_is_literal(self):
        # Run-path pin: _run_filters was already escaping when this test was
        # added — regression guard, not a RED demonstration. "gpt-50-mini"
        # gives it teeth: without ESCAPE the pattern %50%% matches it too.
        src = make_runs_src([run_row("rp", model="x/50%off"),
                             run_row("rd"),
                             run_row("rg", model="gpt-50-mini")])
        self.addCleanup(src.close)
        import_unsloth_runs(src, self.dest)
        rows = query_runs(self.dest, model="50%")
        self.assertEqual([r["model"] for r in rows], ["x/50%off"])


class NoResourceWarningTests(unittest.TestCase):
    """Regression pin: main() must close every sqlite connection it opens
    (our store plus each read-only source). An unclosed one surfaces on
    stderr as a 'ResourceWarning: unclosed database' finalizer message."""

    def test_cli_run_emits_no_resource_warnings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.db")
            self.addCleanup(make_file_db(src, [src_row()]).close)
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "tokenstat.py")
            proc = subprocess.run(
                [sys.executable, "-W", "error::ResourceWarning", script,
                 "--db", os.path.join(tmp, "usage.db"), "--src", src,
                 "--unsloth-db", os.path.join(tmp, "no-studio.db")],
                capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ResourceWarning", proc.stderr)


class PanelEmptyTitleTests(unittest.TestCase):
    def test_empty_title_drops_the_separator(self):
        first = _ANSI_RE.sub("", panel("", ["content here"])[0])
        self.assertTrue(first.startswith("\u256d tokenstat "))
        self.assertNotIn("\u00b7", first)

    def test_empty_title_keeps_uniform_width(self):
        lines = panel("", ["a", "longer content line"])
        self.assertEqual(len({_visible(ln) for ln in lines}), 1)

    def test_report_header_forwards_empty_title(self):
        rows = [{"total": 100, "tokens_input": 60, "tokens_output": 40,
                 "date": "2026-11-15"}]
        lines = report_header("", rows)
        self.assertNotIn("\u00b7", _ANSI_RE.sub("", lines[0]))
        self.assertIn("100 tokens", _ANSI_RE.sub("", lines[1]))

    def test_hero_tints_in_and_out_like_the_tables(self):
        rows = [{"total": 300, "tokens_input": 200, "tokens_output": 100,
                 "date": "2026-11-15"}]
        hero = report_header("x", rows)[1]
        r, g, b = NEON["tin"]
        self.assertIn(f"\x1b[38;2;{r};{g};{b}m200\x1b[0m in", hero)
        r, g, b = NEON["tout"]
        self.assertIn(f"\x1b[38;2;{r};{g};{b}m100\x1b[0m out", hero)

    def test_hero_grand_total_painted_ttotal(self):
        rows = [{"total": 7300000, "tokens_input": 5900000,
                 "tokens_output": 1400000}]
        hero = "\n".join(report_header("all models", rows))
        self.assertIn("1;38;2;{};{};{}m7.30M".format(*NEON["ttotal"]), hero)

    def test_hero_row_count_painted_tshare(self):
        rows = [{"total": 1, "tokens_input": 1, "tokens_output": 0}]
        hero = "\n".join(report_header("all models", rows, unit="models"))
        self.assertIn("38;2;{};{};{}m1\x1b[0m model".format(*NEON["tshare"]),
                      hero)

    def test_hero_row_count_unit_word_is_not_painted(self):
        # the count wears the SHARE teal; the word joins the other unit words
        # in the terminal's own color
        rows = [{"total": 1, "tokens_input": 1, "tokens_output": 0}]
        hero = "\n".join(report_header("all models", rows, unit="models"))
        self.assertNotIn("model\x1b[0m", hero)

    def test_hero_unit_label_is_configurable(self):
        rows = [{"total": 300, "tokens_input": 200, "tokens_output": 100,
                 "date": "2026-11-15"},
                {"total": 100, "tokens_input": 40, "tokens_output": 60,
                 "date": "2026-11-16"}]
        self.assertIn("2 rows", _ANSI_RE.sub("", report_header("x", rows)[1]))
        self.assertIn("2 sessions", _ANSI_RE.sub(
            "", report_header("x", rows, unit="sessions")[1]))

    def test_hero_unit_singularizes_for_one_row(self):
        rows = [{"total": 100, "tokens_input": 60, "tokens_output": 40,
                 "date": "2026-11-15"}]
        self.assertIn("1 session", _ANSI_RE.sub(
            "", report_header("x", rows, unit="sessions")[1]))

    def test_hero_appends_cost_only_when_given(self):
        rows = [{"total": 300, "tokens_input": 200, "tokens_output": 100,
                 "date": "2026-11-15"}]
        plain = _ANSI_RE.sub("", report_header("x", rows)[1])
        self.assertNotIn("~$", plain)
        r, g, b = NEON["num"]
        hero = report_header("x", rows, cost_est=123.456)[1]
        self.assertIn(f"\x1b[38;2;{r};{g};{b}m~$123.46\x1b[0m", hero)


class DefaultDashboardTests(CliHarness):
    """Bare tokenstat run: hero + top-5 models + 5 most recent sessions."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")
        other = '{"id":"m3","providerID":"prov3"}'
        # 7 sessions, 3 models, increasing time_created (s1 oldest).
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", title="t-s1", created=ms(2026, 11, 1)),
            src_row(sid="s2", title="t-s2", model=LEMONADE_JSON,
                    created=ms(2026, 11, 2)),
            src_row(sid="s3", title="t-s3", created=ms(2026, 11, 3)),
            src_row(sid="s4", title="t-s4", model=other, tin=5_000_000,
                    tout=900_000, tcr=0, created=ms(2026, 11, 4)),
            src_row(sid="s5", title="t-s5", created=ms(2026, 11, 5)),
            src_row(sid="s6", title="t-s6", model=LEMONADE_JSON,
                    created=ms(2026, 11, 6)),
            src_row(sid="s7", title="t-s7", created=ms(2026, 11, 7)),
        ]).close)

    def bare(self, extra=()):
        return self.run_main(["--db", self.db, "--src", self.src,
                              "--unsloth-db", self.no_udb] + list(extra))

    def test_bare_shows_both_panels_model_first(self):
        code, out, _ = self.bare()
        self.assertEqual(code, 0)
        self.assertIn("usage by model \u00b7 top 5 by share", out)
        self.assertIn("sessions \u00b7 5 most recent", out)
        # "sessions ·" alone now also occurs in the hero ("N sessions · ..."),
        # so pin the model panel against the sessions PANEL TITLE specifically.
        self.assertLess(out.index("usage by model"),
                        out.index("sessions \u00b7 5 most recent"))

    def test_bare_hero_has_no_section_name(self):
        code, out, _ = self.bare()
        first = _ANSI_RE.sub("", out.splitlines()[0])
        self.assertTrue(first.startswith("\u256d tokenstat "))
        self.assertNotIn("\u00b7", first)

    def test_dashboard_share_pair_is_labeled(self):
        _, plain, _ = self.bare()
        plain = _ANSI_RE.sub("", plain)
        # both share columns carry headers now (human decision): the pair
        # reads "SHARE %  SHARE"; no "BAR" label anywhere.
        self.assertIn("SHARE %  SHARE", plain)
        self.assertNotIn("BAR", plain)

    def test_bare_hero_counts_sessions_not_rows(self):
        _, plain, _ = self.bare()
        self.assertIn("7 sessions", _ANSI_RE.sub("", plain))

    def test_sessions_capped_at_five_newest_first(self):
        code, out, _ = self.bare()
        for gone in ("t-s1", "t-s2"):
            self.assertNotIn(gone, out)
        for kept in ("t-s3", "t-s4", "t-s5", "t-s6", "t-s7"):
            self.assertIn(kept, out)
        self.assertLess(out.index("t-s7"), out.index("t-s3"))

    def test_model_panel_top_five_by_share(self):
        code, out, _ = self.bare()
        self.assertIn("SHARE", out)
        # 3 models only exist here: all show, unsloth-studio dominates.
        self.assertIn(f"unsloth-studio/{UNSLOTH_MODEL_ID}", out)
        self.assertIn("prov3/m3", out)
        self.assertIn("lemonade/gpt-oss-120b", out)

    def test_model_panel_truncates_to_five(self):
        six = os.path.join(self.tmp.name, "six.db")
        rows = [src_row(sid=f"m{i}", title=f"t-m{i}",
                        model='{"id":"mod%d","providerID":"p"}' % i,
                        tin=1000 * (i + 2), tcr=0,
                        created=ms(2026, 11, i + 1)) for i in range(6)]
        self.addCleanup(make_file_db(six, rows).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u4.db"),
             "--src", six, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        # mod0 has the smallest total (2000+200=2200 vs larger tin): dropped.
        self.assertNotIn("p/mod0", out)
        for i in range(1, 6):
            self.assertIn(f"p/mod{i}", out)
        # shares are over ALL six models (grand total 28,200): mod5 renders
        # 7200/28200 = 25.5%. Computing shares AFTER truncating to five would
        # render 27.7% — this pins the spec's share-over-all decision.
        self.assertIn("25.5%", out)

    def test_bare_json_unchanged_all_sessions(self):
        code, out, _ = self.bare(["--json"])
        rows = json.loads(out)
        self.assertEqual(len(rows), 7)
        self.assertNotIn("SHARE", out)

    def test_flagged_views_keep_full_tables(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src,
             "--unsloth-db", self.no_udb, "--since", "2026-11-01"])
        self.assertEqual(code, 0)
        self.assertNotIn("5 most recent", out)
        self.assertIn("t-s1", out)  # full (unfiltered-by-cap) table

    def test_cost_and_cache_overlays_on_both_panels(self):
        code, out, _ = self.bare(["--cost", "--cache"])
        self.assertEqual(code, 0)
        plain = _ANSI_RE.sub("", out)
        self.assertGreaterEqual(plain.count("CACHE"), 2)
        self.assertGreaterEqual(plain.count("COST"), 2)
        self.assertRegex(plain, r"OUT\s+CACHE\s+TOTAL")
        self.assertEqual(sum(1 for ln in plain.splitlines()
                             if ln.rstrip(" │").endswith("Prices")), 2)

    def test_hero_shows_total_estimate_only_with_cost(self):
        # 6 default src_rows est (tin*2+tout*10+tcr*.2+tcw*2.5)/1e6 = .00565
        # each, plus s4 (5M in, 900k out) = 19.00025 -> grand total ~$19.03 —
        # computed over ALL sessions, not the truncated model table.
        code, out, _ = self.bare(["--cost"])
        self.assertEqual(code, 0)
        self.assertIn("~$19.03", _ANSI_RE.sub("", out))
        _, plain_out, _ = self.bare()
        self.assertNotIn("~$", plain_out)

    def test_bare_cost_header_names_pinned_model(self):
        code, out, _ = self.bare(["--cost"])
        self.assertEqual(code, 0)
        plain = _ANSI_RE.sub("", out)
        self.assertEqual(plain.count("Claude Sonnet 5 Prices"), 2)

    def test_dashboard_hero_real_total_over_all_sessions(self):
        # review-focus pin: 7 sessions x stored $0.01 = $0.07, not the
        # truncated top-5 panel's sum.
        code, out, _ = self.bare(["--real-cost"])
        self.assertEqual(code, 0)
        self.assertIn("$0.07", _ANSI_RE.sub("", out))
        self.assertNotIn("~$", out)          # no estimate tail without --cost

    def test_fewer_than_five_shows_all(self):
        small = os.path.join(self.tmp.name, "small.db")
        self.addCleanup(make_file_db(small, [
            src_row(sid="only", title="t-only")]).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u2.db"),
             "--src", small, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        self.assertIn("t-only", out)

    def test_empty_store_plain_no_match(self):
        empty = os.path.join(self.tmp.name, "empty-src.db")
        self.addCleanup(make_file_db(empty, []).close)
        code, out, _ = self.run_main(
            ["--db", os.path.join(self.tmp.name, "u3.db"),
             "--src", empty, "--unsloth-db", self.no_udb])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "no sessions match")


class HeroUnitTests(unittest.TestCase):
    """Flagged-view heroes name their rows: days/sessions/models/
    requests/runs (heroes only print on a tty, so _color_enabled is faked)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", created=ms(2026, 11, 1)),
            src_row(sid="s2", model=LEMONADE_JSON, created=ms(2026, 11, 2)),
        ]).close)
        self.udb = os.path.join(self.tmp.name, "studio.db")
        runs = [(f"r{i}", '{"model":"m"}', 500, ms(2026, 11, 1) + i * 1000,
                 ms(2026, 11, 1) + i * 1000 + 500, "completed")
                for i in (0, 1)]
        self.addCleanup(make_file_runs_db(
            self.udb, [unsloth_event("e1"), unsloth_event("e2"),
                       unsloth_event("e3")], runs).close)

    def run_hero(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("tokenstat._color_enabled",
                                 lambda *a: True), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["--db", self.db, "--src", self.src,
                         "--unsloth-db", self.udb] + list(flags))
        self.assertEqual(code, 0)
        return _ANSI_RE.sub("", out.getvalue())

    def test_flagged_session_view_counts_sessions(self):
        self.assertIn("2 sessions", self.run_hero("--since", "2026-11-01"))

    def test_daily_counts_days(self):
        self.assertIn("2 days", self.run_hero("--daily"))

    def test_grand_total_drops_its_echo_hero(self):
        # The lone "(all)" row already IS the grand total: hero would be a
        # pure echo, so --total renders table-only even on a tty.
        plain = self.run_hero("--total")
        self.assertIn("(all)", plain)
        self.assertNotIn("tokens ·", plain)

    def test_single_match_per_model_view_drops_hero(self):
        # Same redundancy when the per-model filter matches exactly one row.
        plain = self.run_hero("--total", "--model", "lemonade")
        self.assertIn("lemonade/gpt-oss-120b", plain)
        self.assertNotIn("tokens ·", plain)

    def test_per_model_totals_count_models(self):
        self.assertIn("2 models", self.run_hero("--total", "--model"))

    def test_unsloth_events_count_requests(self):
        self.assertIn("3 requests", self.run_hero("--from-unsloth"))

    def test_runs_count_runs(self):
        self.assertIn("2 runs", self.run_hero("--from-runs"))


class CliSessionsTests(CliHarness):
    """--sessions[=N] scopes session views to the N most recent sessions."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        # 26 sessions of distinct time_created, s00 oldest: --sessions
        # (default 20) must drop s00..s05 and keep s06..s25.
        base = ms(2026, 11, 1)
        self.addCleanup(make_file_db(self.src, [
            src_row(sid=f"s{i:02d}", title=f"t-{i:02d}",
                    created=base + i * 3_600_000)   # hourly, all distinct
            for i in range(26)]).close)

    def sess(self, *extra):
        return self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb]
            + list(extra))

    def sess_colored(self, *extra):
        return self.colored_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb]
            + list(extra))

    def plain(self, out):
        return _ANSI_RE.sub("", out)

    def assert_exit_2(self, *extra):
        """parser.error paths: exit 2, stderr names --sessions, no table.

        Calls main directly (as CliTotalTests' exit-2 pins do): run_main
        wraps its own stderr, which a raised SystemExit never reaches."""
        err, out = io.StringIO(), io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(out):
            main(["--db", self.db, "--src", self.src,
                  "--unsloth-db", self.no_udb] + list(extra))
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--sessions", err.getvalue())
        return err.getvalue(), out.getvalue()

    # -- direct unit pins for the two new helpers --

    def test_most_recent_reverses_and_truncates(self):
        from tokenstat import most_recent
        rows = [{"n": 1}, {"n": 2}, {"n": 3}]
        self.assertEqual(most_recent(rows, 2), [{"n": 3}, {"n": 2}])
        self.assertEqual(most_recent(rows, 0), rows[::-1])   # 0 = all
        self.assertEqual(most_recent(rows, -1), rows[::-1])  # negative = all
        self.assertEqual(most_recent(rows, 9), rows[::-1])
        self.assertEqual(rows, [{"n": 1}, {"n": 2}, {"n": 3}])  # no mutation

    def test_sessions_limit_resolver(self):
        from tokenstat import SESSIONS_BARE, _sessions_limit
        self.assertIsNone(_sessions_limit(None, False, False))
        self.assertEqual(_sessions_limit(SESSIONS_BARE, False, False), 20)
        self.assertEqual(_sessions_limit(SESSIONS_BARE, True, False), 0)
        self.assertEqual(_sessions_limit(SESSIONS_BARE, False, True), 0)
        self.assertEqual(_sessions_limit(5, False, False), 5)
        self.assertEqual(_sessions_limit(0, False, False), 0)
        self.assertEqual(_sessions_limit(-1, False, False), -1)  # CLI rejects

    # -- the list view --

    def test_bare_flag_lists_20_newest_first(self):
        code, out, _ = self.sess_colored("--sessions")
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 20 most recent", text)
        for gone in (f"t-{i:02d}" for i in range(6)):
            self.assertNotIn(gone, text)
        for kept in ("t-06", "t-12", "t-25"):
            self.assertIn(kept, text)
        self.assertLess(text.index("t-25"), text.index("t-06"))

    def test_five_form_lists_five(self):
        code, out, _ = self.sess_colored("--sessions=5")
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 5 most recent", text)
        for kept in ("t-21", "t-22", "t-23", "t-24", "t-25"):
            self.assertIn(kept, text)
        for gone in ("t-20", "t-00"):
            self.assertNotIn(gone, text)
        self.assertLess(text.index("t-25"), text.index("t-21"))

    def test_capped_list_view_takes_the_cost_overlay(self):
        # Overlay smoke: --cost must not change the view -- the capped list
        # still renders, with the COST column appended.
        code, out, _ = self.sess_colored("--sessions=3", "--cost")
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 3 most recent", text)
        self.assertIn("COST", text)
        for kept in ("t-23", "t-24", "t-25"):
            self.assertIn(kept, text)
        self.assertNotIn("t-22", text)   # exactly 3 rows

    def test_space_form_behaves_identically(self):
        _, eq_out, _ = self.sess("--sessions=5")
        _, sp_out, _ = self.sess("--sessions", "5")
        self.assertEqual(eq_out, sp_out)

    def test_zero_lists_all(self):
        code, out, _ = self.sess_colored("--sessions=0")
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 all", text)
        for i in range(26):
            self.assertIn(f"t-{i:02d}", text)
        self.assertLess(text.index("t-25"), text.index("t-00"))

    def test_sessions_does_not_trigger_dashboard(self):
        code, out, _ = self.sess("--sessions")
        self.assertEqual(code, 0)
        self.assertNotIn("usage by model \u00b7 top 5 by share", out)

    def test_negative_exits_2_without_a_table(self):
        err, out = self.assert_exit_2("--sessions=-1")
        self.assertEqual(out.strip(), "")  # no table printed

    def test_non_integer_exits_2(self):
        self.assert_exit_2("--sessions=abc")

    def test_unsupported_with_from_unsloth(self):
        err, _ = self.assert_exit_2("--from-unsloth", "--sessions")
        self.assertIn("--from-unsloth", err)

    def test_unsupported_with_from_runs(self):
        err, _ = self.assert_exit_2("--from-runs", "--sessions")
        self.assertIn("--from-runs", err)

    def test_unsupported_with_reconcile(self):
        err, _ = self.assert_exit_2("reconcile", "--sessions")
        self.assertIn("reconcile", err)

    # -- aggregates under the cap --

    def test_total_three_totals_only_those_three(self):
        _, out, _ = self.sess("--sessions=3", "--json")
        listed = json.loads(out)
        self.assertEqual(len(listed), 3)
        _, out, _ = self.sess("--sessions=3", "--total", "--json")
        (grand,) = json.loads(out)
        self.assertEqual(grand["total"],
                         sum(r["total"] for r in listed))

    def test_sessions_total_equals_plain_total(self):
        _, bare, _ = self.sess("--total")
        _, capped, _ = self.sess("--sessions", "--total")
        self.assertEqual(bare, capped)

    def test_sessions_bare_model_equals_plain_model(self):
        # Bare --model is the --total shortcut, so the resolver sees an
        # effective want_total: a valueless --sessions means ALL sessions
        # there too, and the share view is byte-identical to plain --model.
        _, plain_out, _ = self.sess("--model")
        _, capped, _ = self.sess("--sessions", "--model")
        self.assertEqual(plain_out, capped)

    def test_sessions_daily_equals_plain_daily(self):
        _, bare, _ = self.sess("--daily")
        _, capped, _ = self.sess("--sessions", "--daily")
        self.assertEqual(bare, capped)

    def test_daily_three_spans_only_those_sessions_days(self):
        _, out, _ = self.sess("--sessions=3", "--json")
        spans = sorted({r["date"] for r in json.loads(out)})
        _, out, _ = self.sess("--sessions=3", "--daily", "--json")
        days = json.loads(out)
        self.assertEqual(sorted(d["date"] for d in days), spans)

    # -- aggregate titles --

    def title_of(self, out):
        """The visible panel title, as rendered in the strip (colour
        stripped): the top border reads `╭ tokenstat · {title} ───╮`."""
        text = self.plain(out)
        m = re.search(r"tokenstat \u00b7 (.*?) \u2500+\u256e", text)
        self.assertIsNotNone(m, f"no title strip in:\n{text}")
        return m.group(1)

    def test_daily_three_titles_last_3_sessions(self):
        _, out, _ = self.sess_colored("--sessions=3", "--daily")
        self.assertEqual(self.title_of(out), "daily \u00b7 last 3 sessions")

    def test_total_three_titles_last_3_sessions(self):
        _, out, _ = self.sess_colored("--sessions=3", "--total")
        self.assertEqual(self.title_of(out), "totals \u00b7 last 3 sessions")

    def test_share_view_title_carries_both_parts(self):
        _, out, _ = self.sess_colored("--sessions=3", "--total", "--model")
        title = self.title_of(out)
        self.assertIn("usage by model", title)
        self.assertIn("last 3 sessions", title)

    def test_uncapped_titles_stay_exact(self):
        # No cap in force: every view keeps the title it rendered before the
        # flag existed, byte for byte.
        for argv, expected in ((["--daily"], "daily"),
                               (["--total"], "totals"),
                               (["--model"], "usage by model \u00b7 sorted by share"),
                               (["--model", "unsloth"], "sessions")):
            with self.subTest(argv=argv):
                _, out, _ = self.sess_colored(*argv)
                self.assertEqual(self.title_of(out), expected)

    def test_zero_cap_titles_match_uncapped_output(self):
        # 0 means "all": the suffix must not appear, so the whole output is
        # byte-identical to the plain view.
        for argv in (["--total"], ["--daily"], ["--model"]):
            with self.subTest(argv=argv):
                _, bare, _ = self.sess_colored(*argv)
                _, capped, _ = self.sess_colored("--sessions=0", *argv)
                self.assertEqual(bare, capped)

    # -- model filter and JSON --

    def test_model_pattern_lists_four_most_recent_matching(self):
        lemon = os.path.join(self.tmp.name, "lemon.db")
        base = ms(2026, 11, 1)
        self.addCleanup(make_file_db(lemon, [
            src_row(sid=f"L{i}", model=LEMONADE_JSON, title=f"t-L{i}",
                    created=base + i * 3_600_000)
            for i in range(6)]
            + [src_row(sid="u1", title="t-u1", created=base + 7 * 3_600_000)]
        ).close)
        db = os.path.join(self.tmp.name, "usage2.db")
        code, out, _ = self.colored_main(
            ["--db", db, "--src", lemon, "--unsloth-db", self.no_udb,
             "--sessions=4", "--model", "lemonade"])
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 4 most recent", text)
        for kept in ("t-L2", "t-L3", "t-L4", "t-L5"):
            self.assertIn(kept, text)
        for gone in ("t-L0", "t-L1", "t-u1"):
            self.assertNotIn(gone, text)

    def test_json_emits_20_newest_first(self):
        code, out, _ = self.sess("--sessions", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 20)
        self.assertEqual([r["title"] for r in rows],
                         [f"t-{i:02d}" for i in range(25, 5, -1)])

    # -- dashboard regression --

    def test_bare_dashboard_still_five_most_recent(self):
        code, out, _ = self.sess_colored()
        self.assertEqual(code, 0)
        text = self.plain(out)
        self.assertIn("sessions \u00b7 5 most recent", text)
        for kept in ("t-21", "t-22", "t-23", "t-24", "t-25"):
            self.assertIn(kept, text)
        for gone in ("t-20", "t-00"):
            self.assertNotIn(gone, text)


GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


class ByteIdentityGoldenTests(CliHarness):
    """The committed byte-identity harness: what the CLI prints, pinned to the byte.

    The theme increment promised that a theme only moves a byte when you ask
    for one: flag-off output is exactly what it was before themes existed, and
    piped / `--json` output stays plain under every palette. That was proven
    once against the pre-theme tree by a throwaway harness; this is the version
    that stays, so the next refactor cannot move a byte quietly.

    Each case runs twice — piped (color gate closed) and on a fake terminal
    (gate forced open) — and both streams are compared against a file in
    `golden/`. When a change is *meant* to move bytes, read the diff, then
    regenerate deliberately:

        TOKENSTAT_UPDATE_GOLDEN=1 python3 -m unittest test_tokenstat

    The bytes are clock-independent (every fixture row carries an explicit
    timestamp) and TZ is pinned to UTC, because date columns render in local
    time. The themes folder is redirected to an empty temp dir: a saved default
    on this machine must not repaint a golden.
    """

    # 18 invocations x {piped, tty} = the 36 comparisons the theme review made.
    CASES = (
        ("bare", ()),
        ("total", ("--total",)),
        ("total-model", ("--total", "--model")),
        ("daily", ("--daily",)),
        ("cache", ("--cache",)),
        ("cost", ("--cost",)),
        ("real-cost", ("--real-cost",)),
        ("sessions3", ("--sessions=3",)),
        ("json", ("--json",)),
        ("json-total-model", ("--json", "--total", "--model")),
        ("total-model-cache-cost", ("--total", "--model", "--cache", "--cost")),
        ("daily-cost", ("--daily", "--cost")),
        ("model-filter", ("--total", "--model", "gpt-oss")),
        ("since", ("--since", "2026-11-05")),
        ("min-in", ("--total", "--model", "--min-in", "2000")),
        ("unsloth-total", ("--from-unsloth", "--total")),
        ("unsloth-json", ("--from-unsloth", "--json")),
        ("runs-total-model", ("--from-runs", "--total", "--model")),
    )
    # The two views the overlay tests sweep every palette through: the dashboard
    # and the share table, i.e. panels and a painted table.
    OVERLAY_CASES = (("bare", ()), ("total-model", ("--total", "--model")))

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        # Date columns come from datetime.fromtimestamp(), i.e. local time, so
        # a golden captured in one zone has to be read in the same one.
        cls._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    @classmethod
    def tearDownClass(cls):
        if cls._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._tz
        time.tzset()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1200, tcr=7000, tcw=100,
                    title="alpha", created=ms(2026, 11, 4)),
            src_row(sid="s2", model=LEMONADE_JSON, tin=4500, tout=500,
                    tcr=0, tcw=0, cost=0.02, title="beta",
                    created=ms(2026, 11, 5)),
            src_row(sid="s3", model=LEMONADE_JSON, tin=1500, tout=250,
                    cost=0.03, title="gamma", created=ms(2026, 11, 6)),
            src_row(sid="s4", tin=250, tout=60, title="delta",
                    created=ms(2026, 11, 7)),
        ]).close)
        self.udb = os.path.join(self.tmp.name, "studio.db")
        self.addCleanup(make_file_runs_db(
            self.udb,
            events=[unsloth_event("e1", prompt=3000, completion=400),
                    unsloth_event("e2", prompt=800, completion=90,
                                  created=ms(2026, 11, 6))],
            runs=[run_row("r1"), run_row("r2", progress=40)]).close)
        self.base = ["--db", self.db, "--src", self.src,
                     "--unsloth-db", self.udb]
        self.themes = os.path.join(self.tmp.name, "themes")
        self.config = os.path.join(self.themes, "config.json")
        for name, value in (("THEMES_DIR", self.themes),
                            ("THEME_CONFIG", self.config)):
            patcher = unittest.mock.patch.object(tokenstat, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_case(self, extra, tty):
        """One invocation, piped or on a fake terminal; stdout must be the only
        thing it writes."""
        code, out, err = (self.colored_main if tty else self.run_main)(
            self.base + list(extra))
        self.assertEqual(code, 0, extra)
        self.assertEqual(err, "", extra)
        return out

    def golden_path(self, name, tty):
        return os.path.join(GOLDEN_DIR,
                            "%s.%s.txt" % (name, "tty" if tty else "piped"))

    def assert_golden(self, name, tty, text):
        path = self.golden_path(name, tty)
        if os.environ.get("TOKENSTAT_UPDATE_GOLDEN") == "1":
            os.makedirs(GOLDEN_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            return
        self.assertTrue(
            os.path.exists(path),
            "no golden for %r yet: TOKENSTAT_UPDATE_GOLDEN=1 "
            "python3 -m unittest test_tokenstat" % name)
        with open(path, encoding="utf-8", newline="") as fh:
            self.assertEqual(
                fh.read(), text,
                "bytes moved in %s mode for case %r" % (
                    "tty" if tty else "piped", name))

    def test_flag_off_bytes_match_the_goldens(self):
        for name, extra in self.CASES:
            for tty in (False, True):
                with self.subTest(case=name, tty=tty):
                    self.assert_golden(name, tty, self.run_case(extra, tty))

    def test_golden_folder_holds_exactly_the_matrix(self):
        # A renamed or dropped case must not leave a golden nobody reads.
        want = {os.path.basename(self.golden_path(n, t))
                for n, _ in self.CASES for t in (False, True)}
        have = {f for f in os.listdir(GOLDEN_DIR) if f.endswith(".txt")}
        self.assertEqual(have, want)

    def test_piped_bytes_carry_no_escapes(self):
        for name, extra in self.CASES:
            with self.subTest(case=name):
                self.assertNotIn("\x1b", self.run_case(extra, tty=False))

    def test_tty_bytes_paint_except_for_json(self):
        # Harness guard, not a product claim: if the color gate ever broke, the
        # tty goldens would go plain and still match their (plain) files.
        for name, extra in self.CASES:
            with self.subTest(case=name):
                painted = "38;2;" in self.run_case(extra, tty=True)
                self.assertEqual(painted, "--json" not in extra, name)

    def test_default_tty_bytes_are_neon_midnight_bytes(self):
        """NEON (frozen) and the neon-midnight theme are one palette, so naming
        it must not move a byte — and drift between the two shows up here."""
        for name, extra in self.CASES:
            with self.subTest(case=name):
                self.assertEqual(
                    self.run_case(extra, tty=True),
                    self.run_case(list(extra) + ["--theme=neon-midnight"],
                                  tty=True), name)

    def test_no_theme_moves_a_piped_byte(self):
        for name, extra in self.OVERLAY_CASES:
            plain = self.run_case(extra, tty=False)
            for slug in sorted(THEMES):
                with self.subTest(case=name, theme=slug):
                    self.assertEqual(
                        self.run_case(list(extra) + ["--theme=" + slug],
                                      tty=False), plain, slug)

    def test_every_theme_does_move_a_tty_byte(self):
        # The mirror of the test above: without it, "no theme moves a piped
        # byte" would also pass if --theme were silently ignored.
        for name, extra in self.OVERLAY_CASES:
            plain = self.run_case(extra, tty=True)
            for slug in sorted(THEMES):
                with self.subTest(case=name, theme=slug):
                    painted = self.run_case(list(extra) + ["--theme=" + slug],
                                            tty=True)
                    if slug == "neon-midnight":
                        self.assertEqual(painted, plain, slug)
                    else:
                        self.assertNotEqual(painted, plain, slug)

    def test_a_saved_default_repaints_but_never_repipes(self):
        for name, extra in self.OVERLAY_CASES:
            plain, painted = (self.run_case(extra, tty=False),
                              self.run_case(extra, tty=True))
            write_default_theme(self.config, "dracula")
            try:
                with self.subTest(case=name):
                    self.assertEqual(self.run_case(extra, tty=False), plain,
                                     name)
                    self.assertNotEqual(self.run_case(extra, tty=True),
                                        painted, name)
            finally:
                os.remove(self.config)  # the next case starts flag-off again


class HiddenOptionsTests(CliHarness):
    """`-h` shows what a person needs and nothing else: the internal plumbing
    options stay fully usable but unadvertised."""

    HIDDEN = ("--min-in", "--min-out", "--db", "--src", "--unsloth-db",
              "--from-unsloth")
    VISIBLE = ("--model", "--since", "--until", "--daily", "--total",
               "--sessions", "--json", "--from-runs", "--cost", "--real-cost",
               "--cache", "--theme", "--theme-default")

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.udb = os.path.join(self.tmp.name, "studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def help_text(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            main(["--help"])
        return out.getvalue()

    def test_internal_options_are_not_advertised(self):
        text = self.help_text()
        for flag in self.HIDDEN:
            self.assertNotIn(flag, text)

    def test_public_options_are_still_advertised(self):
        # A raw substring match passes for the wrong reason: "--model" is
        # matched inside --total's help, "--cost" inside --json's, "--theme"
        # inside --theme-default's. Parse the options section instead:
        # argparse indents a declared option with exactly two spaces and
        # wraps its help at column 24, so a two-space line starting with a
        # flag is a line argparse declares — prose cannot fake one.
        options = self.help_text().split("options:\n", 1)[1]
        declared = set(re.findall(r"(?m)^  (-{1,2}[A-Za-z][\w-]*)", options))
        for flag in self.VISIBLE:
            self.assertIn(flag, declared, flag)
        self.assertEqual(declared, set(self.VISIBLE) | {"-h"})

    def test_no_advertised_help_text_names_a_hidden_option(self):
        # a flag -h does not document must not appear inside another option's
        # help, or the help points at something it never lists
        text = self.help_text()
        self.assertNotIn("--from-unsloth", text.split("options:")[1])

    def test_hidden_options_still_parse_and_run(self):
        # hidden from -h, not removed: each still parses and exits 0
        self.addCleanup(make_file_db(self.src, [src_row()]).close)
        self.addCleanup(make_file_unsloth_db(
            self.udb, [unsloth_event("e1")]).close)
        base = ["--db", self.db, "--src", self.src, "--unsloth-db", self.udb]
        for extra in (["--min-in", "5"], ["--from-unsloth"],
                      ["--min-out", "5", "--daily"]):
            with self.subTest(extra=extra):
                code, _, _ = self.run_main(base + extra)
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
