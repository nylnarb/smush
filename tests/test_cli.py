"""Tests for ltc.cli argument handling and compress/decompress round-trip.

Run: python -m unittest discover -s tests    (from the repo root)
No third-party test dependency - stdlib unittest only.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from ltc import cli


class CliRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def test_compress_then_decompress_json_round_trips(self):
        original = json.dumps([
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ])
        in_path = self._path("in.json")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(original)

        compressed_path = self._path("compressed.txt")
        meta_path = self._path("meta.json")

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["compress", in_path, "--meta-out", meta_path])
        self.assertEqual(rc, 0)
        with open(compressed_path, "w", encoding="utf-8") as f:
            f.write(out.getvalue())

        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["compressor"], "json")

        out2 = io.StringIO()
        with redirect_stdout(out2):
            rc2 = cli.main(["decompress", compressed_path, "--meta", meta_path])
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(out2.getvalue()), json.loads(original))

    def test_stats_flag_prints_to_stderr_and_stdout_stays_clean(self):
        in_path = self._path("in.json")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]))

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["compress", in_path, "--stats",
                            "--meta-out", self._path("meta.json")])
        self.assertEqual(rc, 0)
        self.assertIn("reduction=", err.getvalue())
        # stdout must be exactly the compressed payload, nothing else.
        self.assertNotIn("reduction=", out.getvalue())

    def test_default_meta_path_derives_from_input_filename(self):
        in_path = self._path("payload.json")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(json.dumps([{"a": 1}, {"a": 2}]))

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["compress", in_path])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(in_path + ".ltc.meta.json"))

    def test_explicit_type_overrides_auto_detection(self):
        in_path = self._path("plain.txt")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write("just some plain text, not json/log/listing")

        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["compress", in_path, "--type", "json",
                            "--meta-out", self._path("meta.json")])
        # Not valid JSON -> json_compressor should still handle gracefully
        # (fails open to identity per its own contract) rather than crashing the CLI.
        self.assertEqual(rc, 0)

    def test_invalid_type_choice_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["compress", "somefile", "--type", "not-a-real-type"])

    def test_missing_subcommand_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main([])


PYTEST_RUN = """============================= test session starts ==============================
collected 3 items

tests/test_a.py ..F                                                      [100%]

=================================== FAILURES ===================================
_________________________________ test_broken __________________________________

    def test_broken():
>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:9: AssertionError
=========================== short test summary info ============================
FAILED tests/test_a.py::test_broken - assert 1 == 2
========================= 1 failed, 2 passed in 0.04s ==========================
"""


class LossyFooterTests(unittest.TestCase):
    """The footer is the only safety mechanism for lossy auto-compression."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def _compress(self, content: str, *extra) -> str:
        in_path = self._path("in.txt")
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(content)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["compress", in_path,
                           "--meta-out", self._path("meta.json"), *extra])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_lossy_result_gets_a_footer_naming_what_was_dropped(self):
        out = self._compress(PYTEST_RUN, "--footer-when-lossy")
        footer = out.strip().splitlines()[-1]
        self.assertTrue(footer.startswith("[ltc pytest -"), footer)
        self.assertIn("dropped", footer)
        self.assertTrue(footer.endswith("]"), footer)

    def test_footer_never_swallows_the_failure_it_summarizes(self):
        # A footer that replaced content instead of annotating it would be
        # far worse than no footer at all.
        out = self._compress(PYTEST_RUN, "--footer-when-lossy")
        self.assertIn("test_broken", out)

    def test_reversible_result_gets_no_footer(self):
        out = self._compress(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]),
                             "--footer-when-lossy")
        self.assertNotIn("[ltc ", out)

    def test_footer_is_opt_in(self):
        self.assertNotIn("[ltc ", self._compress(PYTEST_RUN))

    def test_footer_starts_on_its_own_line(self):
        # Without the newline guard the footer would be glued to the last
        # line of real output, corrupting it.
        out = self._compress(PYTEST_RUN.rstrip("\n"), "--footer-when-lossy")
        lines = out.splitlines()
        self.assertTrue(lines[-1].startswith("[ltc "))
        self.assertEqual(1, sum(1 for ln in lines if ln.startswith("[ltc ")))

    def test_footer_helper_is_pure_and_returns_none_when_reversible(self):
        from ltc import compress_payload
        reversible = compress_payload('[{"a": 1}, {"a": 2}]')
        self.assertIsNone(cli.lossy_footer(reversible))
        lossy = compress_payload(PYTEST_RUN)
        self.assertIsNotNone(cli.lossy_footer(lossy))


if __name__ == "__main__":
    unittest.main()
