"""Tests for tokenstat.py — run with: python3 -m unittest test_tokenstat -v"""
import argparse
import contextlib
import datetime
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import unittest

from tokenstat import (_ANSI_RE, _visible, SUNSET, add_shares, block_bar,
                       compact, daily_events, daily_runs, daily_totals,
                       ensure_schema, import_unsloth, import_unsloth_runs,
                       main, paint_bars, panel, parse_date, parse_model,
                       query_events, query_runs, query_sessions, reconcile,
                       record, render_table, total_events, total_runs,
                       total_totals)

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


def src_row(sid="s1", model=UNSLOTH_JSON, tin=1000, tout=200, treas=50,
            tcr=7000, tcw=100, cost=0.01, title="a session", directory="/tmp/d",
            agent="build", created=1_764_000_000_000, updated=1_764_000_005_000):
    return (sid, model, tin, tout, treas, tcr, tcw, cost, title, directory,
            agent, created, updated)


def make_dest():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


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
                    "total", "cost", "title", "tok_s", "tokens_cache_read",
                    "tokens_cache_write"]))
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

    def test_session_rate_is_tokens_over_wall_clock_span(self):
        row = query_sessions(self.dest)[0]
        # (1000+200) tokens over 5 s = 240.0
        self.assertAlmostEqual(row["tok_s"], 240.0, places=2)

    def test_session_zero_span_rate_is_none(self):
        # 0-second span (time_updated == time_created) must yield None, not a
        # ZeroDivision.
        dest = make_dest()
        self.addCleanup(dest.close)
        src = make_src([src_row(sid="z", tin=100, tout=10,
                                 created=1_764_000_000_000,
                                 updated=1_764_000_000_000)])
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


class CliTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

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
                                 "tokens_cache_read", "tokens_cache_write"]))


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


class CliUnslothTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.udb = os.path.join(self.tmp.name, "studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

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
        with self.assertRaises(SystemExit) as cm:
            main(["reconcile", "--since", "2026-11-01"])
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


class CliTotalTests(unittest.TestCase):
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

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

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
                           "tok_s", "tokens_cache_read",
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
        # SHARE percent value, then the space-padded bar (two column
        # separators + paint_bars' leading pad), then block glyphs
        self.assertRegex(unsloth_line, r"\d+\.\d%   [█░▒▓]")

    def test_reconcile_rejects_cost(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                main(["reconcile", "--db", self.db, "--src", self.src,
                      "--unsloth-db", self.no_udb, "--cost"])
        self.assertEqual(cm.exception.code, 2)


class CostTests(unittest.TestCase):
    """annotate_cost: reference Claude-tier price overlay on row copies."""

    def test_annotate_cost_session_uses_all_token_classes_sonnet(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000,
                 "tokens_cache_read": 1_000_000, "tokens_cache_write": 1_000_000}]
        (r,) = annotate_cost(rows, "sonnet")
        # 3 + 15 + 0.30 + 3.75 = 22.05 per Mtok of each class
        self.assertAlmostEqual(r["cost_est"], 22.05)

    def test_annotate_cost_tier_changes_number(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 0,
                 "tokens_cache_read": 0, "tokens_cache_write": 0}]
        self.assertAlmostEqual(annotate_cost(rows, "haiku")[0]["cost_est"], 1.0)
        self.assertAlmostEqual(annotate_cost(rows, "opus")[0]["cost_est"], 5.0)

    def test_annotate_cost_event_uses_prompt_completion(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 1_000_000, "tokens_output": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet")[0]["cost_est"], 18.0)

    def test_annotate_cost_run_uses_progress_only(self):
        from tokenstat import annotate_cost
        rows = [{"progress_tokens": 1_000_000, "tokens_input": 1_000_000}]
        self.assertAlmostEqual(annotate_cost(rows, "sonnet")[0]["cost_est"], 15.0)

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
        # output-only pricing: (100 + 400) * 15 / 1e6
        (priced,) = annotate_cost(total_runs(dest, by_model=True), "sonnet")
        self.assertAlmostEqual(priced["cost_est"], 0.0075)

    def test_annotate_cost_does_not_mutate_input_rows(self):
        from tokenstat import annotate_cost
        rows = [{"tokens_input": 10, "tokens_output": 10}]
        out = annotate_cost(rows, "sonnet")
        self.assertNotIn("cost_est", rows[0])  # inputs untouched...
        self.assertIn("cost_est", out[0])      # ...copies carry the estimate


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


class CliCacheTests(unittest.TestCase):
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

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def base(self, *flags):
        return self.run_main(["--db", self.db, "--src", self.src,
                              "--unsloth-db", self.no_udb, *flags])

    def test_cache_flag_adds_column_to_session_view(self):
        code, out, _ = self.base("--cache")
        self.assertEqual(code, 0)
        self.assertRegex(out.splitlines()[0], r"OUT\s+CACHE\s+TOTAL")
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
        self.assertRegex(unsloth_line, r"\d+\.\d%   [█░▒▓]")

    def test_cost_and_cache_together_show_both_columns(self):
        code, out, _ = self.base("--total", "--cost", "--cache")
        self.assertEqual(code, 0)
        header = out.splitlines()[0]
        self.assertIn("CACHE", header)
        self.assertIn("COST", header)

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


class DisplayAlignmentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        self.addCleanup(make_file_db(self.src, [src_row(sid="s1", cost=0.05)]).close)
        self.no_udb = os.path.join(self.tmp.name, "no-studio.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_render_table_left_aligns_numbers(self):
        columns = [("N", "n", "int"), ("T", "t", "text")]
        rows = [{"n": 5, "t": "x"}, {"n": 1000, "t": "y"}]
        self.assertEqual(render_table(rows, columns),
                         "N      T\n5      x\n1,000  y\n")

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
        self.assertEqual(lines[0].index("COST"), lines[1].index("$"))
        self.assertEqual(lines[0].index("COST"), lines[2].index("$"))

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


class CliShareTests(unittest.TestCase):
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

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_total_per_model_table_shares_and_bars(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertIn("SHARE", lines[0])
        self.assertIn("BAR", lines[0])
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

    def test_session_and_daily_tables_unchanged(self):
        for extra in ([], ["--daily"]):
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

        def rgbs(bar):
            import re
            return [tuple(int(g) for g in m) for m in
                    re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", bar)]

        colors = rgbs(bar)
        self.assertEqual(len(colors), 10)         # every glyph its own color
        self.assertEqual(colors[0], TOKEN_IN_RGB)   # left end: pure input hue
        self.assertEqual(colors[-1], TOKEN_OUT_RGB)  # right end: pure output hue

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


class BlockBarTests(unittest.TestCase):
    """block_bar: truecolor background-block bar, exactly `width` cells wide."""

    def test_full_width_row_splits_at_in_ratio(self):
        bar = block_bar(100, 60, 100, 20)              # 60% in
        self.assertEqual(_visible(bar), 20)            # exactly width cells
        bg = re.findall(r"\x1b\[48;2;\d+;\d+;\d+m", bar)
        tin = f"\x1b[48;2;{SUNSET['tin'][0]};{SUNSET['tin'][1]};{SUNSET['tin'][2]}m"
        runs = [c for c in bg if c == tin]
        self.assertEqual(len(runs), 12)                # round(20 * 0.6)

    def test_zero_total_is_all_track(self):
        bar = block_bar(0, 0, 100, 10)
        self.assertEqual(_visible(bar), 10)
        track = f"\x1b[48;2;{SUNSET['track'][0]};{SUNSET['track'][1]};{SUNSET['track'][2]}m"
        self.assertEqual(bar, (track + " ") * 10 + "\x1b[0m")

    def test_tiny_nonzero_row_gets_at_least_one_cell(self):
        bar = block_bar(1, 1, 1_000_000, 20)
        self.assertEqual(_visible(bar), 20)
        tin = f"\x1b[48;2;{SUNSET['tin'][0]};{SUNSET['tin'][1]};{SUNSET['tin'][2]}m"
        self.assertIn(tin + " ", bar)                    # never all-track

    def test_scale_zero_safety(self):
        self.assertEqual(_visible(block_bar(5, 3, 0, 8)), 8)


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


class SunsetThemeTests(unittest.TestCase):
    def test_sunset_palette_has_all_roles(self):
        for key in ("border", "accent", "label", "num",
                    "tin", "tout", "tcach", "track"):
            self.assertEqual(len(SUNSET[key]), 3)
            self.assertTrue(all(0 <= v <= 255 for v in SUNSET[key]))

    def test_compact_formats_millions_thousands_plain(self):
        self.assertEqual(compact(7_770_000), "7.77M")
        self.assertEqual(compact(12_340), "12.3k")
        self.assertEqual(compact(999), "999")
        self.assertEqual(compact(0), "0")


class CliColorTests(unittest.TestCase):
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

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_piped_output_plain_but_padded(self):
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--unsloth-db", self.no_udb,
             "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        unsloth_line = next(ln for ln in out.splitlines() if "unsloth" in ln)
        # SHARE value then a space-gap before the bar starts
        self.assertRegex(unsloth_line, r"\d+\.\d%   [█░▒▓ ]+$")


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


if __name__ == "__main__":
    unittest.main()
