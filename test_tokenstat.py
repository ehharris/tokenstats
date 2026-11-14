"""Tests for tokenstat.py — run with: python3 -m unittest test_tokenstat -v"""
import argparse
import contextlib
import datetime
import io
import json
import os
import sqlite3
import unittest

from tokenstat import (daily_events, daily_totals, ensure_schema, import_unsloth,
                       main, parse_date, parse_model, query_events,
                       query_sessions, reconcile, record, render_table)

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

    def stored(self, sid="s1"):
        cur = self.dest.execute(
            "SELECT provider, model_id, tokens_input, tokens_output,"
            " tokens_reasoning, tokens_cache_read, tokens_cache_write, cost"
            " FROM usage WHERE session_id = ?", (sid,))
        return cur.fetchone()

    def test_record_inserts_new_session(self):
        src = make_src([src_row()])
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
        record(src, self.dest, now_ms=1)
        result = record(src, self.dest, now_ms=2)
        self.assertEqual(result, {"inserted": 0, "updated": 0, "unchanged": 1})
        self.assertEqual(self.stored()[2], 1000)

    def test_record_updates_grown_session(self):
        src = make_src([src_row(tin=1000, tout=200)])
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
        record(src, self.dest, now_ms=1)
        src.execute("UPDATE session_v2 SET tokens_input = 50 WHERE id = 's1'")
        record(src, self.dest, now_ms=2)
        self.assertEqual(self.stored()[2], 100)

    def test_record_null_and_malformed_model(self):
        src = make_src([src_row(sid="n1", model=None),
                        src_row(sid="n2", model="not json")])
        record(src, self.dest, now_ms=1)
        for sid in ("n1", "n2"):
            row = self.stored(sid)
            self.assertEqual((row[0], row[1]), ("", ""), f"session {sid}")


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
        self.dest = make_dest()
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
                    "total", "cost", "title"]))
        self.assertEqual(row["total"], row["tokens_input"] + row["tokens_output"])


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
        dest = make_dest()
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
        make_file_db(self.src, [src_row()])
        code, out, _ = self.run_main(
            ["record", "--db", self.db, "--src", self.src])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(),
                         r"inserted=\d+ updated=\d+ unchanged=\d+")

    def test_record_then_query_implicit_records(self):
        make_file_db(self.src, [src_row()])
        code, out, err = self.run_main(
            ["--db", self.db, "--src", self.src])
        self.assertEqual(code, 0)
        self.assertNotIn("warning:", err)
        self.assertIn("unsloth-studio", out)

    def test_json_output_parses(self):
        make_file_db(self.src, [src_row(sid="a1", created=ms(2026, 11, 1)),
                                src_row(sid="a2", created=ms(2026, 11, 1))])
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", self.src, "--json", "--daily"])
        self.assertEqual(code, 0)
        days = json.loads(out)
        self.assertEqual(len(days), 1)
        self.assertEqual(sorted(days[0].keys()),
                         sorted(["date", "sessions", "tokens_input",
                                 "tokens_output", "total", "cost"]))


class ReviewFixTests(unittest.TestCase):
    """Regression pins for findings from the whole-branch review."""

    def setUp(self):
        src = make_src([
            src_row(sid="big", tin=5000, tout=900, created=ms(2026, 11, 1)),
            src_row(sid="tiny", tin=100, tout=5, created=ms(2026, 11, 5)),
        ])
        self.src = src
        self.dest = make_dest()
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


class ImportUnslothTests(unittest.TestCase):
    def setUp(self):
        self.dest = make_dest()

    def stored_total(self):
        row = self.dest.execute(
            "SELECT COUNT(*), SUM(prompt_tokens), SUM(completion_tokens)"
            " FROM unsloth_events").fetchone()
        return row if row[0] else (0, 0, 0)

    def test_import_inserts_then_is_idempotent(self):
        src = make_unsloth_src([unsloth_event("e1"), unsloth_event("e2", prompt=20)])
        first = import_unsloth(src, self.dest)
        self.assertEqual(first, {"inserted": 2, "updated": 0, "unchanged": 0})
        second = import_unsloth(src, self.dest)
        self.assertEqual(second, {"inserted": 0, "updated": 0, "unchanged": 2})
        self.assertEqual(self.stored_total(), (2, 1020, 100))

    def test_import_never_shrinks_on_source_regression(self):
        src = make_unsloth_src([unsloth_event("e1", prompt=100)])
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
        import_unsloth(src, self.dest)
        days = daily_events(self.dest)
        self.assertEqual([d["date"] for d in days], ["2026-11-01", "2026-11-05"])
        self.assertEqual(days[0]["sessions"], 2)  # request count under same key
        self.assertEqual(days[0]["tokens_input"], 300)
        self.assertEqual(days[0]["total"], 303)

    def test_reconcile_computes_gap(self):
        osrc = make_src([src_row(sid="s1", tin=1000, tcr=5000, tcw=0, tout=200)])
        record(osrc, self.dest, now_ms=1)
        usrc = make_unsloth_src([
            unsloth_event("e1", prompt=9000, completion=300),
            unsloth_event("e2", prompt=1000, completion=100),
        ])
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
        conn = make_file_unsloth_db(self.udb, [unsloth_event("e1")])
        code, out, _ = self.run_main(
            ["import-unsloth", "--db", self.db, "--unsloth-db", self.udb])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(),
                         r"inserted=\d+ updated=\d+ unchanged=\d+")

    def test_from_unsloth_query_renders_events(self):
        make_file_unsloth_db(self.udb, [unsloth_event("e1")])
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", self.udb, "--from-unsloth"])
        self.assertEqual(code, 0)
        self.assertIn(UNSLOTH_MODEL_ID, out)

    def test_reconcile_subcommand(self):
        make_file_unsloth_db(self.udb, [unsloth_event("e1")])
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


if __name__ == "__main__":
    unittest.main()
