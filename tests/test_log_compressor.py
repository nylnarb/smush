"""Tests for the log / command-output compressor.

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.
"""

import unittest

from ltc import compress_payload, decompress
from ltc.compressors import log_compressor as lc


def roundtrip(text: str):
    """Compress then decompress via the public entry points."""
    res = compress_payload(text, "log")
    back = decompress(res.compressed, res.metadata)
    return res, back


# --- realistic fixtures -----------------------------------------------------

# 50 identical retry lines buried between a start and an end line.
REPEATED = (
    "Starting worker pool\n"
    + "WARN  connection refused, retrying...\n" * 50
    + "Worker pool ready\n"
)

# Same warning but with a changing attempt counter — 'near-identical', should
# still collapse because _run_normalize masks the digits.
NEAR_IDENTICAL = (
    "boot\n"
    + "".join(f"WARN  retry attempt {i} of 30\n" for i in range(1, 31))
    + "done\n"
)

# A traceback threading app code -> library code -> app code.
TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "app/main.py", line 42, in run\n'
    "    handler()\n"
    '  File "/usr/lib/python3.11/site-packages/flask/app.py", line 100, in dispatch\n'
    "    return func()\n"
    '  File "/usr/lib/python3.11/site-packages/werkzeug/serving.py", line 55, in call\n'
    "    rv = self.wsgi_app()\n"
    '  File "app/views.py", line 7, in index\n'
    "    boom()\n"
    "ValueError: boom\n"
)

# Long output that trips the head/tail cap. Lines are genuinely distinct (not
# merely digit-varying), so run-collapse leaves them alone and the cap fires.
_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
LONG = "".join(
    f"processing {_WORDS[i % len(_WORDS)]} record {_WORDS[(i * 3) % len(_WORDS)]}\n"
    for i in range(400)
)


class TestRoundTrip(unittest.TestCase):
    def test_repeated_lines_roundtrip_exactly(self):
        res, back = roundtrip(REPEATED)
        self.assertEqual(back, REPEATED)  # byte-for-byte
        self.assertTrue(res.reversible)
        self.assertEqual(res.compressor, "log")

    def test_near_identical_lines_roundtrip_exactly(self):
        res, back = roundtrip(NEAR_IDENTICAL)
        self.assertEqual(back, NEAR_IDENTICAL)

    def test_traceback_roundtrip_exactly(self):
        res, back = roundtrip(TRACEBACK)
        self.assertEqual(back, TRACEBACK)

    def test_long_output_roundtrip_exactly(self):
        res, back = roundtrip(LONG)
        self.assertEqual(back, LONG)

    def test_no_trailing_newline_roundtrip(self):
        text = "same\nsame\nsame\nsame\ntail"  # no final newline
        res, back = roundtrip(text)
        self.assertEqual(back, text)

    def test_all_three_transforms_together(self):
        combined = TRACEBACK + REPEATED + LONG
        res, back = roundtrip(combined)
        self.assertEqual(back, combined)


class TestCompression(unittest.TestCase):
    def test_repeats_collapse_and_shrink(self):
        res = compress_payload(REPEATED, "log")
        self.assertLess(res.compressed_tokens, res.original_tokens)
        # The 50-line run appears once, with a repeat count.
        self.assertEqual(res.compressed.count("connection refused"), 1)
        self.assertIn("×50", res.compressed)

    def test_near_identical_collapse(self):
        res = compress_payload(NEAR_IDENTICAL, "log")
        # One representative retry line, count of 30.
        self.assertEqual(res.compressed.count("retry attempt"), 1)
        self.assertIn("×30", res.compressed)

    def test_library_frames_hidden_app_frames_kept(self):
        res = compress_payload(TRACEBACK, "log")
        out = res.compressed
        # Application frames survive verbatim.
        self.assertIn("app/main.py", out)
        self.assertIn("app/views.py", out)
        # Library frames are folded away.
        self.assertNotIn("site-packages/flask/app.py", out)
        self.assertNotIn("werkzeug/serving.py", out)
        self.assertIn("library frame(s) hidden", out)

    def test_middle_dropped_with_note(self):
        res = compress_payload(LONG, "log")
        self.assertIn("lines dropped", res.compressed)
        self.assertLess(res.compressed_tokens, res.original_tokens)
        # Head and tail lines are preserved in the visible output.
        self.assertIn(LONG.split("\n", 1)[0], res.compressed)
        self.assertIn(LONG.rstrip("\n").rsplit("\n", 1)[-1], res.compressed)


class TestHeuristics(unittest.TestCase):
    def test_is_library_frame(self):
        self.assertTrue(lc._is_library_frame("/usr/lib/python3.11/site-packages/x.py"))
        self.assertTrue(lc._is_library_frame("C:\\Py\\lib\\python3.11\\os.py"))
        self.assertFalse(lc._is_library_frame("app/main.py"))
        self.assertFalse(lc._is_library_frame("./src/handler.py"))

    def test_run_normalize_masks_digits(self):
        self.assertEqual(
            lc._run_normalize("retry 12 of 30"),
            lc._run_normalize("retry 7 of 30"),
        )
        self.assertNotEqual(
            lc._run_normalize("retry ok"),
            lc._run_normalize("retry no"),
        )


class TestDeterminism(unittest.TestCase):
    def test_repeatable(self):
        a = compress_payload(REPEATED, "log")
        b = compress_payload(REPEATED, "log")
        self.assertEqual(a.compressed, b.compressed)
        self.assertEqual(a.metadata, b.metadata)


class TestRobustness(unittest.TestCase):
    def test_empty_input(self):
        res, back = roundtrip("")
        self.assertEqual(back, "")

    def test_no_op_text_roundtrips(self):
        text = "a single unremarkable line\nand another\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)

    def test_sentinel_collision_avoided(self):
        # Input already contains the default "~~" sentinel; picker must adapt.
        text = "~~run0 ×3\n" + "dup\ndup\ndup\ndup\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)
        self.assertNotIn(res.metadata["sentinel"], text)

    def test_auto_sniff_detects_traceback(self):
        res = compress_payload(TRACEBACK, "auto")
        self.assertEqual(res.compressor, "log")

    def test_auto_sniff_detects_repeated_lines(self):
        res = compress_payload(REPEATED, "auto")
        self.assertEqual(res.compressor, "log")


if __name__ == "__main__":
    unittest.main()
