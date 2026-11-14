"""Tests for tokenstat.py — run with: python3 -m unittest test_tokenstat -v"""
import sqlite3
import unittest

from tokenstat import ensure_schema, parse_model, record

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


if __name__ == "__main__":
    unittest.main()
