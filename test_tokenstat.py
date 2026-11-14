"""Tests for tokenstat.py — run with: python3 -m unittest test_tokenstat -v"""
import argparse
import contextlib
import datetime
import io
import json
import os
import sqlite3
import unittest

from tokenstat import (add_shares, daily_events, daily_totals, ensure_schema,
                       import_unsloth, main, parse_date, parse_model,
                       query_events, query_sessions, reconcile, record,
                       render_table, total_events, total_totals)

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


class ReviewFixTests2(unittest.TestCase):
    """Regression pins for whole-branch review of increment 2."""

    def setUp(self):
        self.dest = make_dest()

    def test_reconcile_one_sided_models_both_directions(self):
        # usage-only model: negative gap (OpenCode counted what Studio's log lacks)
        osrc = make_src([src_row(sid="u1", model=LEMONADE_JSON, tin=1500, tout=30)])
        record(osrc, self.dest, now_ms=1)  # lemonade: not in unsloth side at all
        # unsloth-only model
        usrc = make_unsloth_src([unsloth_event("e1", prompt=900, completion=9)])
        import_unsloth(usrc, self.dest)
        rows = {r["model"]: r for r in reconcile(self.dest)}
        u = rows[UNSLOTH_MODEL_ID]
        self.assertEqual((u["sessions"], u["attributed_in"]), (0, 0))
        self.assertEqual(u["reported_prompt"], 900)
        self.assertEqual(u["gap_prompt"], 900)

    def test_import_skips_bad_row_keeps_good_rows(self):
        src = make_unsloth_src([unsloth_event("good", prompt=100)])
        src.execute("INSERT INTO api_usage_events VALUES ('bad','unsloth','/x',"
                    "'m','completed',5,5,10,NULL)")
        counts = import_unsloth(src, self.dest)
        self.assertEqual(counts.get("skipped"), 1)
        self.assertEqual(counts["inserted"], 1)
        stored = self.dest.execute(
            "SELECT COUNT(*), SUM(prompt_tokens) FROM unsloth_events").fetchone()
        self.assertEqual(stored, (1, 100))  # good row persisted despite bad one

    def test_event_filter_accepts_provider_prefixed_string(self):
        import_unsloth(make_unsloth_src([unsloth_event("e1")]), self.dest)
        # the string shown in session reports must also work in event mode
        self.assertEqual(len(query_events(self.dest, model="unsloth-studio")), 1)
        self.assertEqual(len(query_events(self.dest, model="qwen3.8")), 1)

    def test_event_until_dst_boundary(self):
        late = int(datetime.datetime(2026, 11, 1, 23, 30).timestamp() * 1000)
        import_unsloth(make_unsloth_src([unsloth_event("e1", created=late)]),
                       self.dest)
        rows = query_events(self.dest, since_ms=ms(2026, 11, 1),
                            until_ms=ms(2026, 11, 1))
        self.assertEqual(len(rows), 1)

    def test_report_stable_across_repeated_imports(self):
        src = make_unsloth_src([unsloth_event("e1", prompt=500),
                                unsloth_event("e2", prompt=7)])
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
        record(make_src([
            src_row(sid="s1", tin=1000, tout=200, cost=0.01),          # unsloth-studio
            src_row(sid="s2", model=LEMONADE_JSON, tin=500, tout=50,
                    cost=0.02),                                        # lemonade
        ]), self.dest, now_ms=1)

    def test_total_totals_single_grand_row(self):
        rows = total_totals(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1500, 250, 1750))
        self.assertAlmostEqual(r["cost"], 0.03)

    def test_total_totals_per_model_when_filtered(self):
        record(make_src([src_row(sid="s3", model=LEMONADE_JSON, tin=10, tout=1,
                                 cost=0.0)]), self.dest, now_ms=2)
        rows = total_totals(self.dest, model="lemonade")
        self.assertEqual([r["model"] for r in rows], ["lemonade/gpt-oss-120b"])
        self.assertEqual((rows[0]["sessions"], rows[0]["tokens_input"]), (2, 510))

    def test_total_totals_keeps_same_model_id_separate_per_provider(self):
        a = '{"id":"m1","providerID":"prov-a"}'
        b = '{"id":"m1","providerID":"prov-b"}'
        record(make_src([src_row(sid="p1", model=a), src_row(sid="p2", model=b)]),
               self.dest, now_ms=2)
        rows = total_totals(self.dest, model="m1")
        self.assertEqual(sorted(r["model"] for r in rows), ["prov-a/m1", "prov-b/m1"])

    def test_total_totals_filters_apply_before_summing(self):
        record(make_src([src_row(sid="d1", created=ms(2026, 11, 1), tin=9000),
                         src_row(sid="d2", created=ms(2026, 11, 5), tin=7)]),
               self.dest, now_ms=2)
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
        import_unsloth(make_unsloth_src([
            unsloth_event("e1", prompt=900, completion=30),
            unsloth_event("e2", model="other-model", prompt=100, completion=5),
        ]), self.dest)

    def test_total_events_single_grand_row(self):
        rows = total_events(self.dest)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "(all)")
        self.assertEqual((r["sessions"], r["tokens_input"],
                          r["tokens_output"], r["total"]), (2, 1000, 35, 1035))
        self.assertEqual(r["cost"], 0.0)

    def test_total_events_per_model_when_filtered(self):
        import_unsloth(make_unsloth_src([unsloth_event("e3", prompt=50)]),
                       self.dest)  # same model as e1
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
        make_file_db(self.src, [
            src_row(sid="s1"),                        # unsloth-studio model
            src_row(sid="s2", model=qwen_lemonade),   # lemonade qwen-mini
        ])
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
                           "tokens_output", "total", "cost", "share"})

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
        make_file_unsloth_db(udb, [unsloth_event("e1"),
                                   unsloth_event("e2", model="other-model")])
        code, out, _ = self.run_main(
            ["--db", self.db, "--src", os.path.join(self.tmp.name, "none.db"),
             "--unsloth-db", udb, "--from-unsloth", "--total", "--model"])
        self.assertEqual(code, 0)
        self.assertIn(UNSLOTH_MODEL_ID, out)
        self.assertIn("other-model", out)
        self.assertNotIn("(all)", out)


class DisplayAlignmentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "usage.db")
        self.src = os.path.join(self.tmp.name, "src.db")
        make_file_db(self.src, [src_row(sid="s1", cost=0.05)])
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
        self.assertEqual(out[2]["bar"], "▂")       # 0.2 cells -> rounded eighth

    def test_bar_shows_fraction_when_not_exact(self):
        rows = [{"model": "a", "total": 3}, {"model": "b", "total": 1}]
        out = add_shares(rows, bar_width=30)
        self.assertEqual(out[0]["bar"], "█" * 30)
        # 1/3 of max over width 30 = exactly 10 full blocks
        self.assertEqual(out[1]["bar"], "█" * 10)
        rows2 = [{"model": "a", "total": 7}, {"model": "b", "total": 1}]
        out2 = add_shares(rows2, bar_width=30)
        # 30/7 = 4.285... -> 4 full blocks + 2/8 fraction (▂)
        self.assertEqual(out2[1]["bar"], "████▂")

    def test_tiny_nonzero_row_never_renders_empty(self):
        rows = [{"model": "a", "total": 10_000}, {"model": "b", "total": 1}]
        out = add_shares(rows, bar_width=30)
        self.assertEqual(out[1]["bar"], "▁")  # rounds to nothing -> lightest block

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
        make_file_db(self.src, [
            src_row(sid="s1", tin=9000, tout=1000),   # unsloth-studio: 10k
            src_row(sid="s2", model=qwen_lemonade, tin=4500, tout=500),  # 5k
        ])
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
        make_file_unsloth_db(udb, [unsloth_event("e1", prompt=900, completion=100),
                                   unsloth_event("e2", model="other-model",
                                                 prompt=500)])
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


if __name__ == "__main__":
    unittest.main()
