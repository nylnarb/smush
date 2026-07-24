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


if __name__ == "__main__":
    unittest.main()
