"""Tests for the savings ledger: schema migration, recording, breakdown.

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.
"""

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout

from smush import cli, stats_store
from smush.result import CompressResult


def make_result(compressor="listing", original=100, compressed=40, reversible=True,
                dropped=None):
    return CompressResult(
        compressed="x" * compressed,
        metadata={"compressor": compressor, "version": 1},
        original_tokens=original,
        compressed_tokens=compressed,
        compressor=compressor,
        reversible=reversible,
        dropped=list(dropped or []),
    )


class LedgerSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "ledger.db")

    def _columns(self):
        conn = sqlite3.connect(self.db)
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(compressions)")}
        finally:
            conn.close()

    def test_record_creates_all_columns(self):
        stats_store.record(self.db, make_result(), session_id="s1", command="ls -la")
        self.assertLessEqual({"command", "lossy", "reversible", "session_id"},
                             self._columns())

    def test_migrates_a_pre_existing_ledger_without_the_new_columns(self):
        """Rows written by an older smush must survive, not be dropped or rewritten."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE compressions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, compressor TEXT NOT NULL, "
            "original_tokens INTEGER NOT NULL, compressed_tokens INTEGER NOT NULL, "
            "saved_tokens INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO compressions (timestamp, compressor, original_tokens, "
            "compressed_tokens, saved_tokens) VALUES ('2026-01-01', 'json', 50, 20, 30)"
        )
        conn.commit()
        conn.close()

        stats_store.record(self.db, make_result(), command="git status")

        data = stats_store.breakdown(self.db)
        self.assertEqual(data["calls"], 2)
        self.assertEqual(data["saved_tokens"], 30 + 60)
        # The legacy row has no command; it must be labelled, not lost.
        labels = {row["label"] for row in data["commands"]}
        self.assertIn("(unknown)", labels)
        self.assertIn("git status", labels)

    def test_record_never_raises_on_an_unusable_path(self):
        stats_store.record(os.path.join(self.tmpdir, "no", "such", "dir.db"),
                           make_result())

    def test_breakdown_returns_none_when_the_db_is_unreadable(self):
        self.assertIsNone(stats_store.breakdown(
            os.path.join(self.tmpdir, "no", "such", "dir.db")))


class CommandLabelTests(unittest.TestCase):
    def test_keeps_two_words_so_git_subcommands_stay_apart(self):
        self.assertEqual(stats_store.command_label("git status -sb ."), "git status")
        self.assertEqual(stats_store.command_label("git diff HEAD~3"), "git diff")

    def test_collapses_the_varying_tail(self):
        self.assertEqual(stats_store.command_label("ls -la /some/deep/path"),
                         stats_store.command_label("ls -la /another/path"))

    def test_missing_command_is_labelled_not_dropped(self):
        self.assertEqual(stats_store.command_label(None), "(unknown)")
        self.assertEqual(stats_store.command_label("   "), "(unknown)")


class BreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "ledger.db")
        # Two useful compressions, one lossy, and two that saved nothing.
        stats_store.record(self.db, make_result("listing", 100, 40),
                           session_id="s1", command="ls -la /tmp")
        stats_store.record(self.db, make_result("listing", 200, 100),
                           session_id="s1", command="ls -la /var")
        stats_store.record(self.db, make_result("pytest", 400, 100, reversible=False,
                                                dropped=["41 passing tests"]),
                           session_id="s1", command="pytest -q")
        stats_store.record(self.db, make_result("identity", 30, 30),
                           session_id="s1", command="echo hi")
        stats_store.record(self.db, make_result("identity", 20, 20),
                           session_id="s2", command="whoami")

    def test_totals(self):
        data = stats_store.breakdown(self.db)
        self.assertEqual(data["calls"], 5)
        self.assertEqual(data["original_tokens"], 750)
        self.assertEqual(data["saved_tokens"], 60 + 100 + 300)
        self.assertAlmostEqual(data["reduction"], 460 / 750, places=4)

    def test_identity_rate_is_the_health_metric(self):
        data = stats_store.breakdown(self.db)
        self.assertEqual(data["identity_calls"], 2)
        self.assertAlmostEqual(data["identity_rate"], 0.4, places=4)

    def test_lossy_calls_are_counted(self):
        self.assertEqual(stats_store.breakdown(self.db)["lossy_calls"], 1)

    def test_per_compressor_split_is_ordered_by_savings(self):
        rows = stats_store.breakdown(self.db)["compressors"]
        self.assertEqual([r["name"] for r in rows], ["pytest", "listing", "identity"])
        self.assertEqual(rows[1]["calls"], 2)
        self.assertEqual(rows[1]["saved_tokens"], 160)

    def test_commands_are_grouped_by_label(self):
        rows = stats_store.breakdown(self.db)["commands"]
        by_label = {r["label"]: r for r in rows}
        self.assertEqual(by_label["ls -la"]["calls"], 2)
        self.assertEqual(by_label["ls -la"]["saved_tokens"], 160)
        self.assertEqual(rows[0]["label"], "pytest -q")

    def test_top_commands_is_capped(self):
        rows = stats_store.breakdown(self.db, top_commands=2)["commands"]
        self.assertEqual(len(rows), 2)

    def test_session_scope_restricts_every_figure(self):
        data = stats_store.breakdown(self.db, session_id="s2")
        self.assertEqual(data["calls"], 1)
        self.assertEqual(data["saved_tokens"], 0)
        self.assertEqual(data["identity_rate"], 1.0)
        self.assertEqual([r["label"] for r in data["commands"]], ["whoami"])

    def test_empty_ledger_reports_zeroes_not_a_crash(self):
        empty = os.path.join(self.tmpdir, "empty.db")
        stats_store.record(empty, make_result())
        conn = sqlite3.connect(empty)
        conn.execute("DELETE FROM compressions")
        conn.commit()
        conn.close()
        data = stats_store.breakdown(empty)
        self.assertEqual(data["calls"], 0)
        self.assertEqual(data["identity_rate"], 0.0)
        self.assertEqual(data["reduction"], 0.0)


class GainCliTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "ledger.db")
        stats_store.record(self.db, make_result("listing", 100, 40),
                           session_id="s1", command="ls -la /tmp")
        stats_store.record(self.db, make_result("identity", 30, 30),
                           session_id="s1", command="echo hi")

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_plain_gain_prints_one_total(self):
        rc, text = self._run(["gain", "--db", self.db])
        self.assertEqual(rc, 0)
        self.assertIn("60 tokens saved (all-time)", text)

    def test_breakdown_reports_identity_rate_and_splits(self):
        rc, text = self._run(["gain", "--db", self.db, "--breakdown"])
        self.assertEqual(rc, 0)
        self.assertIn("identity rate: 50%", text)
        self.assertIn("listing", text)
        self.assertIn("ls -la", text)

    def test_breakdown_honours_session_scope(self):
        rc, text = self._run(["gain", "--db", self.db, "--breakdown",
                              "--session-id", "nobody"])
        self.assertEqual(rc, 0)
        self.assertIn("no compressions recorded", text)

    def test_breakdown_on_an_unreadable_db_fails_loudly_but_calmly(self):
        rc, text = self._run(["gain", "--breakdown", "--db",
                              os.path.join(self.tmpdir, "no", "such.db")])
        self.assertEqual(rc, 1)
        self.assertIn("no stats available", text)

    def test_compress_records_the_command_it_was_given(self):
        payload = os.path.join(self.tmpdir, "in.txt")
        with open(payload, "w", encoding="utf-8") as f:
            f.write("total 4\n-rw-r--r-- 1 dev 0 Jul 24 18:59 a.txt\n"
                    "-rw-r--r-- 1 dev 0 Jul 24 18:59 b.txt\n")
        db = os.path.join(self.tmpdir, "recorded.db")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["compress", payload, "--log-stats", db,
                           "--command", "ls -la /tmp",
                           "--meta-out", os.path.join(self.tmpdir, "m.json")])
        self.assertEqual(rc, 0)
        labels = [r["label"] for r in stats_store.breakdown(db)["commands"]]
        self.assertEqual(labels, ["ls -la"])


if __name__ == "__main__":
    unittest.main()
