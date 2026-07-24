"""Calibration for the heuristic token estimator.

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.

`ltc.tokens.estimate_tokens` ships no tokenizer, so its constants are fitted
rather than derived. This file is what keeps that fit honest: each fixture
below carries reference counts measured once, offline, with two real BPE
tokenizers (GPT-2 and DeepSeek-LLM via `transformers`). Neither library is a
dependency here — only the resulting integers are, so the test stays
stdlib-only and offline like the rest of ltc.

**Why two tokenizers and not one.** They disagree with each other by up to 1.5x
on the same text (GPT-2 charges a token per leading space, DeepSeek merges
whitespace away entirely), and neither is the tokenizer Claude actually uses.
Fitting tightly to either one would be false precision. The reference is their
mean, and the assertion is a band wide enough to be meaningful: any estimator
inside it is "as close to real tokenizers as they are to each other".

What this test protects is therefore not exactness but **trustworthy ratios** —
if a future change to the estimator silently doubles the cost of, say, banner
bars or hex ids, one of these fixtures moves out of band and says so.

To re-measure the reference counts after deliberately changing a fixture:

    from transformers import AutoTokenizer
    g = AutoTokenizer.from_pretrained("gpt2")
    d = AutoTokenizer.from_pretrained("deepseek-ai/deepseek-llm-7b-base")
    len(g.encode(text)), len(d.encode(text))
"""

import unittest

from ltc.tokens import estimate_tokens

# --- fixtures ---------------------------------------------------------------
# One per content family ltc actually sees, chosen so that each stresses a
# different rule in the estimator: banner bars, column padding, hex ids,
# structural punctuation, indentation, ordinary English.

FIXTURES = {
    "prose": (
        "The quick brown fox jumps over the lazy dog. Compression only pays "
        "off when the output is repetitive, so ordinary English prose is the "
        "worst case and a useful lower bound on the estimator's accuracy.\n"
    ),
    "pytest_run": (
        "============================= test session starts =============================\n"
        "platform win32 -- Python 3.11.9, pytest-8.2.0, pluggy-1.5.0\n"
        "rootdir: C:\\Users\\dev\\repos\\widget\n"
        "collected 42 items\n"
        "\n"
        "tests/test_core.py ..........................                           [ 62%]\n"
        "tests/test_render.py .............F..                                   [100%]\n"
        "\n"
        "================================== FAILURES ===================================\n"
        "___________________________ test_render_empty_rows ____________________________\n"
        "\n"
        "    def test_render_empty_rows():\n"
        "        table = render(rows=[])\n"
        ">       assert table.height == 1\n"
        "E       AssertionError: assert 0 == 1\n"
        "\n"
        "tests/test_render.py:88: AssertionError\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_render.py::test_render_empty_rows - AssertionError\n"
        "======================== 1 failed, 41 passed in 2.14s =========================\n"
    ),
    "git_status": (
        "On branch feature/token-ledger\n"
        "Your branch is ahead of 'origin/main' by 3 commits.\n"
        "  (use \"git push\" to publish your local commits)\n"
        "\n"
        "Changes to be committed:\n"
        "  (use \"git restore --staged <file>...\" to unstage)\n"
        "\tmodified:   ltc/stats_store.py\n"
        "\tnew file:   ltc/compressors/git_compressor.py\n"
        "\n"
        "Changes not staged for commit:\n"
        "\tmodified:   README.md\n"
        "\tmodified:   ltc/cli.py\n"
        "\n"
        "Untracked files:\n"
        "\tsamples/git_status.txt\n"
    ),
    "listing": (
        "total 96\n"
        "drwxr-xr-x  1 dev 197609     0 Jul 24 18:59 .\n"
        "drwxr-xr-x  1 dev 197609     0 Jul 24 13:03 ..\n"
        "-rw-r--r--  1 dev 197609 21949 Jul 24 19:00 README.md\n"
        "-rw-r--r--  1 dev 197609 12288 Jul 24 18:33 ltc_stats.db\n"
        "-rw-r--r--  1 dev 197609  4078 Jul 24 16:05 NEXT_SESSION_PROMPTS.txt\n"
        "drwxr-xr-x  1 dev 197609     0 Jul 24 18:53 ltc\n"
        "drwxr-xr-x  1 dev 197609     0 Jul 24 18:56 tests\n"
    ),
    "traceback": (
        "Traceback (most recent call last):\n"
        "  File \"/usr/lib/python3.11/runpy.py\", line 198, in _run_module_as_main\n"
        "    return _run_code(code, main_globals, None,\n"
        "  File \"/srv/app/ltc/cli.py\", line 86, in _run_compress\n"
        "    result = compress_payload(content, args.type)\n"
        "  File \"/srv/app/ltc/core.py\", line 216, in compress_payload\n"
        "    raise ValueError(f\"unsupported content_type {content_type!r}\")\n"
        "ValueError: unsupported content_type 'yaml'\n"
    ),
    "json_payload": (
        "[\n"
        "  {\"id\": 1, \"name\": \"Ada Lovelace\", \"email\": \"ada@example.com\", \"active\": true},\n"
        "  {\"id\": 2, \"name\": \"Alan Turing\", \"email\": \"alan@example.com\", \"active\": true},\n"
        "  {\"id\": 3, \"name\": \"Grace Hopper\", \"email\": \"grace@example.com\", \"active\": false}\n"
        "]\n"
    ),
    "grep_hits": (
        "ltc/core.py:33:def _sniff(content: str) -> str:\n"
        "ltc/core.py:91:def compress_payload(content, content_type=\"auto\"):\n"
        "ltc/cli.py:86:    result = compress_payload(content, args.type)\n"
        "ltc/mcp_server.py:44:    return compress_payload(text, kind).as_dict()\n"
        "tests/test_cli.py:12:    out = compress_payload(\"{}\", \"json\")\n"
    ),
    "hashes": (
        "commit 4f9a2b7c1d3e8f0a9b6c5d4e3f2a1b0c9d8e7f6a\n"
        "Author: Someone <someone@example.com>\n"
        "Date:   Fri Jul 24 18:59:02 2026 -0500\n"
        "\n"
        "    stage B: command-aware lossy codecs\n"
    ),
}

# fixture -> (gpt2 token count, deepseek token count), measured offline.
REFERENCE_COUNTS = {
    "prose": (42, 46),
    "pytest_run": (294, 190),
    "git_status": (144, 114),
    "listing": (186, 196),
    "traceback": (161, 135),
    "json_payload": (97, 70),
    "grep_hits": (130, 113),
    "hashes": (85, 84),
}

# The stated band. Per fixture we allow ±25%; in aggregate, where the
# per-family errors cancel, we hold the estimator to ±10%. Both are chosen to
# sit just outside the *current* worst case (0.83x on `listing`, 1.19x on
# `pytest_run`, 1.03x overall) so that ordinary drift passes and a real
# regression does not.
PER_FIXTURE_BAND = (0.75, 1.25)
AGGREGATE_BAND = (0.90, 1.10)


def _reference(name: str) -> float:
    """Mean of the two measured tokenizers for one fixture."""
    gpt2, deepseek = REFERENCE_COUNTS[name]
    return (gpt2 + deepseek) / 2.0


class TokenCalibrationTest(unittest.TestCase):
    def test_every_fixture_is_inside_the_band(self):
        for name, text in FIXTURES.items():
            with self.subTest(fixture=name):
                ratio = estimate_tokens(text) / _reference(name)
                low, high = PER_FIXTURE_BAND
                self.assertGreaterEqual(
                    ratio, low,
                    f"{name}: estimate is {ratio:.2f}x the reference — "
                    f"undercounting past the stated band")
                self.assertLessEqual(
                    ratio, high,
                    f"{name}: estimate is {ratio:.2f}x the reference — "
                    f"overcounting past the stated band")

    def test_aggregate_ratio_is_close_to_one(self):
        """No systematic bias: the errors above must not all point one way."""
        estimated = sum(estimate_tokens(t) for t in FIXTURES.values())
        reference = sum(_reference(n) for n in FIXTURES)
        ratio = estimated / reference
        low, high = AGGREGATE_BAND
        self.assertTrue(
            low <= ratio <= high,
            f"aggregate estimate is {ratio:.3f}x the reference "
            f"({estimated} vs {reference:.0f}); the estimator is biased")

    def test_fixtures_and_references_stay_in_sync(self):
        """A fixture edited without re-measuring would silently invalidate the band."""
        self.assertEqual(set(FIXTURES), set(REFERENCE_COUNTS))


class TokenEstimatorPropertiesTest(unittest.TestCase):
    """Properties the *ratio* depends on, independent of absolute accuracy."""

    def test_empty_text_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_monotonic_in_content(self):
        """Appending text can never lower the estimate.

        Compression reporting divides one estimate by another, so a
        non-monotonic estimator could show a "saving" for output that grew.
        """
        base = FIXTURES["traceback"]
        self.assertGreater(estimate_tokens(base + FIXTURES["prose"]),
                           estimate_tokens(base))

    def test_deterministic(self):
        text = FIXTURES["pytest_run"]
        self.assertEqual(estimate_tokens(text), estimate_tokens(text))

    def test_repeated_punctuation_is_nearly_free(self):
        """pytest banner bars must not dominate the estimate."""
        self.assertLessEqual(estimate_tokens("=" * 80), 10)

    def test_hex_ids_are_charged_per_character(self):
        """A 40-char sha really is ~40 tokens in every vocabulary measured."""
        sha = "4f9a2b7c1d3e8f0a9b6c5d4e3f2a1b0c9d8e7f6a"
        self.assertGreaterEqual(estimate_tokens(sha), 30)

    def test_single_spaces_are_free_but_padding_is_not(self):
        self.assertEqual(estimate_tokens("a b"), estimate_tokens("a") + estimate_tokens("b"))
        self.assertGreater(estimate_tokens("a" + " " * 12 + "b"), estimate_tokens("a b"))


if __name__ == "__main__":
    unittest.main()
