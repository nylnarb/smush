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


class LossyContractTests(unittest.TestCase):
    """A lossy result must always name what it dropped.

    This is the invariant the whole lossy design rests on (see README, "Key
    idea"): reversibility is traded away *in exchange for* an explicit list.
    A codec that returns reversible=False with an empty `dropped` has taken
    the trade without paying for it, and the CLI footer degrades to the
    useless "dropped unspecified". Asserted across every registered codec so a
    codec added later cannot quietly reintroduce the hole.
    """

    def _samples(self):
        d = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")
        for name in sorted(os.listdir(d)):
            if not name.endswith(".cmd"):
                yield os.path.join(d, name)

    def test_every_lossy_sample_names_what_it_dropped(self):
        from ltc import compress_payload
        seen_lossy = 0
        for path in self._samples():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            result = compress_payload(content)
            if result.lossy:
                seen_lossy += 1
                self.assertTrue(result.dropped,
                                f"{os.path.basename(path)} is lossy "
                                f"({result.compressor}) but dropped is empty")
        self.assertGreater(seen_lossy, 0, "no lossy samples — test proves nothing")

    def test_blank_line_only_collapse_still_names_its_loss(self):
        # The case that caught this: `unittest -v` output where the pytest
        # codec finds no progress lines or passing names to drop, and only
        # blank-line padding collapses.
        from ltc.compressors import pytest_compressor
        text = "Ran 12 tests in 0.4s\n\n\n\nOK\n"
        result = pytest_compressor.compress(text)
        if result.lossy:
            self.assertTrue(result.dropped)
            self.assertTrue(any("blank" in d for d in result.dropped),
                            result.dropped)


if __name__ == "__main__":
    unittest.main()
