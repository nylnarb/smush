"""Tests for ltc.harness: sample runner and round-trip verification.

Run: python -m unittest discover -s tests    (from the repo root)
No third-party test dependency - stdlib unittest only.
"""

import json
import os
import tempfile
import unittest

from ltc import harness


class HarnessSampleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _write(self, name: str, content: str) -> str:
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_run_sample_reports_round_trip_ok_for_json(self):
        path = self._write("data.json", json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]))
        result = harness.run_sample(path)
        self.assertEqual(result.compressor, "json")
        self.assertTrue(result.round_trip_ok)
        self.assertIsNone(result.error)
        self.assertGreater(result.original_tokens, 0)

    def test_run_sample_handles_plain_text_as_identity(self):
        path = self._write("plain.txt", "hello there, nothing special here")
        result = harness.run_sample(path)
        self.assertEqual(result.compressor, "identity")
        self.assertTrue(result.round_trip_ok)

    def test_run_directory_covers_all_files_and_repo_samples_pass(self):
        samples_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")
        results = harness.run_directory(samples_dir)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertTrue(r.round_trip_ok, f"round-trip failed for {r.path}: {r.error}")

    def test_format_table_includes_header_and_summary(self):
        path = self._write("data.json", json.dumps([{"a": 1}, {"a": 2}]))
        results = [harness.run_sample(path)]
        table = harness.format_table(results)
        self.assertIn("file", table)
        self.assertIn("round_trip", table)
        self.assertIn("average reduction", table)

    def test_main_returns_nonzero_exit_for_missing_directory(self):
        rc = harness.main(["/no/such/directory/at/all"])
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
