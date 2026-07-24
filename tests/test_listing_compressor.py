"""Tests for the file-listing / search-result compressor.

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.
"""

import unittest

from ltc import compress_payload, decompress
from ltc.compressors import listing_compressor as lst


def roundtrip(text: str):
    """Compress then decompress via the public entry points."""
    res = compress_payload(text, "listing")
    back = decompress(res.compressed, res.metadata)
    return res, back


# --- realistic fixtures -----------------------------------------------------

# A plain `find`/`ls -R`-style listing sharing a long project prefix.
FIND_STYLE = (
    "\n".join(
        f"/home/alice/projects/widget/src/module_{i}/file_{i}.py"
        for i in range(10)
    )
    + "\n"
)

# A grep -rn style listing: path:line:text, still sharing a common prefix.
GREP_STYLE = (
    "\n".join(
        f"/home/alice/projects/widget/src/module_{i}/file_{i}.py:{i + 1}: def handler():"
        for i in range(10)
    )
    + "\n"
)

# Windows-style backslash paths.
WINDOWS_STYLE = "\n".join(
    f"C:\\Users\\alice\\repo\\src\\file_{i}.py" for i in range(5)
) + "\n"

# Mixed listing — not all lines are paths, so prefix factoring must no-op.
MIXED = "3 files found:\n/a/b/c.py\n/a/b/d.py\n"

# More results than the cap, all sharing a prefix.
_MANY_COUNT = 250
MANY_RESULTS = (
    "\n".join(f"/srv/data/logs/app/day{i:04d}.log" for i in range(_MANY_COUNT))
    + "\n"
)


class TestRoundTrip(unittest.TestCase):
    def test_find_style_roundtrips_exactly(self):
        res, back = roundtrip(FIND_STYLE)
        self.assertEqual(back, FIND_STYLE)
        self.assertTrue(res.reversible)
        self.assertEqual(res.compressor, "listing")

    def test_grep_style_roundtrips_exactly(self):
        res, back = roundtrip(GREP_STYLE)
        self.assertEqual(back, GREP_STYLE)

    def test_windows_style_roundtrips_exactly(self):
        res, back = roundtrip(WINDOWS_STYLE)
        self.assertEqual(back, WINDOWS_STYLE)

    def test_mixed_listing_roundtrips_exactly(self):
        res, back = roundtrip(MIXED)
        self.assertEqual(back, MIXED)

    def test_capped_results_roundtrip_exactly(self):
        res, back = roundtrip(MANY_RESULTS)
        self.assertEqual(back, MANY_RESULTS)

    def test_no_trailing_newline_roundtrip(self):
        text = "/a/b/one.py\n/a/b/two.py\n/a/b/three"
        res, back = roundtrip(text)
        self.assertEqual(back, text)

    def test_empty_input(self):
        res, back = roundtrip("")
        self.assertEqual(back, "")


class TestCompression(unittest.TestCase):
    def test_common_prefix_factored_once(self):
        res = compress_payload(FIND_STYLE, "listing")
        out = res.compressed
        self.assertLess(res.compressed_tokens, res.original_tokens)
        # The shared directory prefix appears once, in the header.
        self.assertEqual(out.count("/home/alice/projects/widget/src/"), 1)
        self.assertIn("module_0/file_0.py", out)

    def test_grep_style_keeps_line_and_text_suffix(self):
        res = compress_payload(GREP_STYLE, "listing")
        out = res.compressed
        self.assertIn(":1: def handler():", out)
        self.assertEqual(out.count("/home/alice/projects/widget/src/"), 1)

    def test_mixed_listing_is_not_factored(self):
        res = compress_payload(MIXED, "listing")
        # Not homogeneous (first line isn't a path) -> no header emitted.
        self.assertNotIn(f'{res.metadata["sentinel"]}base', res.compressed)

    def test_capped_results_note_and_shrink(self):
        res = compress_payload(MANY_RESULTS, "listing")
        self.assertIn("results omitted", res.compressed)
        self.assertLess(res.compressed_tokens, res.original_tokens)
        # First kept line survives; lines past the cap are gone from view.
        self.assertIn("0000.log", res.compressed)
        self.assertNotIn("0249.log", res.compressed)


# `ls -la` long format: the one shape this codec handles lossily.
LS_LONG = """\
total 96
drwxr-xr-x  8 alice staff   256 Jul 24 17:43 .
-rw-r--r--  1 alice staff    39 Jul 18 14:02 .gitignore
-rw-r--r--  1 alice staff 17512 Jul 24 17:40 README.md
drwxr-xr-x  9 alice staff   288 Jul 24 17:43 ltc
-rw-r--r--  1 alice staff   100 Jul 19 13:31 settings.local.json
"""


class TestLongFormatElision(unittest.TestCase):
    """The lossy path: `ls -l` columns go, names and modes stay."""

    def test_names_and_modes_survive(self):
        out = compress_payload(LS_LONG, "listing").compressed
        for name in (".gitignore", "README.md", "ltc", "settings.local.json"):
            self.assertIn(name, out)
        self.assertIn("drwxr-xr-x ltc", out)

    def test_size_owner_and_date_columns_are_gone(self):
        out = compress_payload(LS_LONG, "listing").compressed
        self.assertNotIn("alice", out)
        self.assertNotIn("17512", out)
        self.assertNotIn("Jul 24", out)
        self.assertNotIn("total 96", out)

    def test_declares_itself_lossy_and_says_what_went(self):
        res = compress_payload(LS_LONG, "listing")
        self.assertFalse(res.reversible)
        self.assertTrue(res.metadata["lossy"])
        self.assertIn("size/owner/date", " ".join(res.dropped))
        self.assertLess(res.compressed_tokens, res.original_tokens)

    def test_plain_listings_stay_reversible(self):
        for text in (FIND_STYLE, GREP_STYLE, WINDOWS_STYLE, MANY_RESULTS):
            res = compress_payload(text, "listing")
            self.assertTrue(res.reversible)
            self.assertEqual(res.dropped, [])
            self.assertFalse(res.metadata["lossy"])

    def test_long_format_is_sniffed_without_a_command_hint(self):
        # Long-format lines contain no path separator, so the path-majority
        # test can't see them; the codec's own detector has to.
        self.assertEqual(compress_payload(LS_LONG, "auto").compressor, "listing")

    def test_decompress_reverses_everything_except_the_columns(self):
        res = compress_payload(LS_LONG, "listing")
        back = decompress(res.compressed, res.metadata)
        self.assertEqual(back, res.compressed)  # nothing else transformed here


class TestHeuristics(unittest.TestCase):
    def test_looks_like_path(self):
        self.assertTrue(lst._looks_like_path("/a/b/c.py"))
        self.assertTrue(lst._looks_like_path("C:\\a\\b.py"))
        self.assertFalse(lst._looks_like_path("just text"))
        self.assertFalse(lst._looks_like_path(""))

    def test_line_path_handles_grep_and_plain(self):
        self.assertEqual(lst._line_path("/a/b/c.py\n"), "/a/b/c.py")
        self.assertEqual(lst._line_path("/a/b/c.py:10: x = 1\n"), "/a/b/c.py")
        self.assertIsNone(lst._line_path("no path here\n"))

    def test_common_prefix_trims_to_separator(self):
        self.assertEqual(
            lst._common_prefix(["/home/alice/x.py", "/home/alicia/y.py"]),
            "/home/",
        )
        self.assertEqual(
            lst._common_prefix(["/a/b/c.py", "/a/b/d.py"]),
            "/a/b/",
        )
        self.assertEqual(lst._common_prefix(["/a/x", "/b/y"]), "/")


class TestDeterminism(unittest.TestCase):
    def test_repeatable(self):
        a = compress_payload(FIND_STYLE, "listing")
        b = compress_payload(FIND_STYLE, "listing")
        self.assertEqual(a.compressed, b.compressed)
        self.assertEqual(a.metadata, b.metadata)


class TestRobustness(unittest.TestCase):
    def test_sentinel_collision_avoided(self):
        text = "~~base /a/b/\n/a/b/one.py\n/a/b/two.py\n/a/b/three.py\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)
        self.assertNotIn(res.metadata["sentinel"], text)

    def test_short_prefix_not_factored(self):
        # Shared prefix is just "/", below _MIN_PREFIX_LEN -> left alone.
        text = "/a.py\n/b.py\n/c.py\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)
        self.assertNotIn(f'{res.metadata["sentinel"]}base', res.compressed)

    def test_auto_sniff_detects_listing(self):
        res = compress_payload(FIND_STYLE, "auto")
        self.assertEqual(res.compressor, "listing")

    def test_auto_sniff_does_not_steal_log(self):
        log_text = "Starting\n" + "retrying...\n" * 5 + "Done\n"
        res = compress_payload(log_text, "auto")
        self.assertEqual(res.compressor, "log")


if __name__ == "__main__":
    unittest.main()
