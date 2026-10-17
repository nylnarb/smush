"""Tests for the pytest / unittest compressor (lossy).

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.

The contract under test is narrower than the reversible codecs': nothing here
round-trips. What must hold instead is that (a) everything a reader would act
on survives, (b) everything discarded is named in `dropped`, and (c) the codec
never claims to be reversible.
"""

import unittest

from smush import compress_payload, decompress


PYTEST_RUN = """\
============================= test session starts =============================
platform win32 -- Python 3.11.4, pytest-7.4.0, pluggy-1.2.0
rootdir: C:\\Users\\alice\\repos\\widget
plugins: cov-4.1.0
collected 42 items

tests/test_parser.py ..........                                          [ 23%]
tests/test_render.py .......F..                                          [ 47%]

================================== FAILURES ===================================
_____________________________ test_render_escapes _____________________________

    def test_render_escapes():
>       assert out == "&lt;b&gt;"
E       AssertionError: assert '<b>' == '&lt;b&gt;'

tests/test_render.py:41: AssertionError
=============================== warnings summary ==============================
tests/test_store.py:7
  DeprecationWarning: datetime.utcnow() is deprecated
=========================== short test summary info ===========================
FAILED tests/test_render.py::test_render_escapes - AssertionError
================== 1 failed, 41 passed, 2 warnings in 0.42s ===================
"""

ALL_PASSING = """\
============================= test session starts =============================
platform linux -- Python 3.11.4, pytest-7.4.0, pluggy-1.2.0
rootdir: /home/alice/widget
collected 42 items

tests/test_parser.py ..........                                          [ 23%]
tests/test_store.py ................................                     [100%]

============================== 42 passed in 0.31s =============================
"""

UNITTEST_RUN = """\
..F...
======================================================================
FAIL: test_escapes (tests.test_render.RenderTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/home/alice/widget/tests/test_render.py", line 41, in test_escapes
    self.assertEqual(out, "&lt;b&gt;")
AssertionError: '<b>' != '&lt;b&gt;'

----------------------------------------------------------------------
Ran 62 tests in 0.137s

FAILED (failures=1)
"""

LIBRARY_TRACEBACK = """\
================================== FAILURES ===================================
________________________________ test_request _________________________________
Traceback (most recent call last):
  File "tests/test_client.py", line 12, in test_request
    client.get("/x")
  File "/usr/lib/python3.11/site-packages/requests/api.py", line 73, in get
    return request("get", url, **kwargs)
  File "/usr/lib/python3.11/site-packages/requests/sessions.py", line 589, in request
    resp = self.send(prep, **send_kwargs)
ConnectionError: refused
============================ 1 failed in 0.10s ================================
"""


def run(text: str):
    return compress_payload(text, "pytest")


class TestKeepsWhatMatters(unittest.TestCase):
    def test_failure_detail_survives_verbatim(self):
        out = run(PYTEST_RUN).compressed
        self.assertIn("test_render_escapes", out)
        self.assertIn("E       AssertionError: assert '<b>' == '&lt;b&gt;'", out)
        self.assertIn("tests/test_render.py:41: AssertionError", out)

    def test_summary_and_tally_survive(self):
        out = run(PYTEST_RUN).compressed
        self.assertIn("FAILED tests/test_render.py::test_render_escapes", out)
        self.assertIn("1 failed, 41 passed", out)

    def test_all_passing_run_keeps_its_tally(self):
        res = run(ALL_PASSING)
        self.assertIn("42 passed in 0.31s", res.compressed)
        self.assertLess(res.compressed_tokens, res.original_tokens)

    def test_unittest_output_keeps_failure_and_trailer(self):
        out = run(UNITTEST_RUN).compressed
        self.assertIn("FAIL: test_escapes", out)
        self.assertIn("AssertionError: '<b>' != '&lt;b&gt;'", out)
        self.assertIn("Ran 62 tests in 0.137s", out)
        self.assertIn("FAILED (failures=1)", out)

    def test_application_frame_kept_library_frames_folded(self):
        res = run(LIBRARY_TRACEBACK)
        self.assertIn('File "tests/test_client.py", line 12', res.compressed)
        self.assertNotIn("site-packages", res.compressed)
        self.assertIn("library frame(s) hidden", res.compressed)
        self.assertIn("ConnectionError: refused", res.compressed)


class TestDropsCeremony(unittest.TestCase):
    def test_session_header_and_warnings_are_gone(self):
        out = run(PYTEST_RUN).compressed
        self.assertNotIn("platform win32", out)
        self.assertNotIn("plugins:", out)
        self.assertNotIn("rootdir:", out)
        self.assertNotIn("DeprecationWarning", out)

    def test_progress_lines_are_gone(self):
        out = run(PYTEST_RUN).compressed
        self.assertNotIn("[ 23%]", out)
        self.assertNotIn("tests/test_parser.py ..", out)

    def test_bars_lose_their_padding_but_keep_their_titles(self):
        out = run(PYTEST_RUN).compressed
        self.assertIn("== FAILURES ==", out)
        self.assertNotIn("=" * 20, out)


VERBOSE_RUN = """\
test_a (tests.test_parser.ParserTests.test_a) ... ok
test_b (tests.test_parser.ParserTests.test_b) ... ok
test_c (tests.test_render.RenderTests.test_c) ... FAIL
test_d (tests.test_render.RenderTests.test_d) ... skipped 'needs network'
tests/test_store.py::test_write PASSED                                   [ 90%]
tests/test_store.py::test_read FAILED                                    [100%]

Ran 6 tests in 0.05s

FAILED (failures=2, skipped=1)
"""


class TestVerboseOutput(unittest.TestCase):
    """`-v` runs are one line per test — the single largest source of waste."""

    def test_passing_test_names_are_dropped(self):
        res = run(VERBOSE_RUN)
        self.assertNotIn("test_a ", res.compressed)
        self.assertNotIn("test_write PASSED", res.compressed)
        self.assertIn("3 passing test name(s)", res.dropped)

    def test_failures_and_skips_are_kept(self):
        out = run(VERBOSE_RUN).compressed
        self.assertIn("test_c (tests.test_render.RenderTests.test_c) ... FAIL", out)
        self.assertIn("test_read FAILED", out)
        self.assertIn("skipped 'needs network'", out)
        self.assertIn("FAILED (failures=2, skipped=1)", out)

    def test_large_reduction(self):
        # Half of this fixture is failures, which are all kept; a real run
        # (107 tests, 2 with interleaved stderr) measures 86.5%.
        self.assertGreater(run(VERBOSE_RUN).reduction, 0.3)


class TestLossyContract(unittest.TestCase):
    def test_result_declares_itself_lossy(self):
        res = run(PYTEST_RUN)
        self.assertFalse(res.reversible)
        self.assertTrue(res.lossy)
        self.assertTrue(res.metadata["lossy"])

    def test_dropped_names_each_category(self):
        res = run(PYTEST_RUN)
        joined = " ".join(res.dropped)
        self.assertIn("progress line", joined)
        self.assertIn("header line", joined)
        self.assertEqual(res.dropped, res.metadata["dropped"])

    def test_decompress_returns_input_unchanged(self):
        res = run(PYTEST_RUN)
        self.assertEqual(decompress(res.compressed, res.metadata), res.compressed)

    def test_empty_input_is_identity_and_reversible(self):
        res = run("")
        self.assertEqual(res.compressor, "identity")
        self.assertTrue(res.reversible)
        self.assertEqual(res.dropped, [])

    def test_nothing_to_drop_stays_reversible(self):
        res = run("just a line of text\n")
        self.assertEqual(res.compressor, "identity")
        self.assertTrue(res.reversible)


class TestRouting(unittest.TestCase):
    def test_command_hint_wins(self):
        res = compress_payload(ALL_PASSING, "auto", command="python -m pytest -q")
        self.assertEqual(res.compressor, "pytest")

    def test_sniffed_without_a_hint(self):
        self.assertEqual(compress_payload(PYTEST_RUN, "auto").compressor, "pytest")
        self.assertEqual(compress_payload(UNITTEST_RUN, "auto").compressor, "pytest")

    def test_plain_traceback_still_goes_to_the_log_codec(self):
        # A bare traceback is not a test run: the lossless codec must keep it.
        text = ("Traceback (most recent call last):\n"
                '  File "app.py", line 3, in <module>\n'
                "    main()\n"
                "ValueError: boom\n")
        self.assertEqual(compress_payload(text, "auto").compressor, "log")


class TestDeterminism(unittest.TestCase):
    def test_repeatable(self):
        a, b = run(PYTEST_RUN), run(PYTEST_RUN)
        self.assertEqual(a.compressed, b.compressed)
        self.assertEqual(a.metadata, b.metadata)


if __name__ == "__main__":
    unittest.main()
