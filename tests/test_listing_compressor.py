"""Tests for the file-listing / search-result compressor.

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.
"""

import unittest

from smush import compress_payload, decompress
from smush.compressors import listing_compressor as lst


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
drwxr-xr-x  9 alice staff   288 Jul 24 17:43 smush
-rw-r--r--  1 alice staff   100 Jul 19 13:31 settings.local.json
"""


class TestLongFormatElision(unittest.TestCase):
    """The lossy path: `ls -l` columns go, names and modes stay."""

    def test_names_and_modes_survive(self):
        out = compress_payload(LS_LONG, "listing").compressed
        for name in (".gitignore", "README.md", "smush", "settings.local.json"):
            self.assertIn(name, out)
        self.assertIn("drwxr-xr-x smush", out)

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


MULTI_DIR = """.gitignore
README.md
setup.py
smush/__init__.py
smush/cli.py
smush/core.py
smush/compressors/git_compressor.py
smush/compressors/json_compressor.py
smush/compressors/log_compressor.py
tests/test_cli.py
tests/test_core.py
tests/test_harness.py
"""


class PerDirectoryGroupingTests(unittest.TestCase):
    """Multi-directory listings used to factor nothing at all.

    `_factor_prefix` needs ONE prefix shared by every line, so anything
    spanning several directories had a common prefix of "" and compressed by
    0%. Real listings — `git ls-files`, `find`, `grep -r` — are nearly always
    multi-directory, so this was the common case failing, not an edge case.
    Measured on this repo's own `git ls-files`: 0% before, 22% after.
    """

    def test_multi_directory_listing_now_compresses(self):
        res, back = roundtrip(MULTI_DIR)
        self.assertEqual(back, MULTI_DIR)
        self.assertGreater(res.reduction, 0.10, res.compressed)

    def test_grouping_is_exactly_reversible(self):
        res, back = roundtrip(MULTI_DIR)
        self.assertEqual(back, MULTI_DIR)
        self.assertTrue(res.reversible)
        self.assertEqual(res.dropped, [])

    def test_original_line_order_is_preserved(self):
        # Only consecutive runs are grouped; reordering would compress a little
        # better and stop the result being a faithful listing.
        res, _ = roundtrip(MULTI_DIR)
        kept = [ln for ln in res.compressed.split("\n")
                if ln and not ln.startswith(res.metadata["sentinel"])]
        self.assertEqual(kept[0], ".gitignore")
        self.assertLess(kept.index("cli.py"), kept.index("test_cli.py"))

    def test_runs_below_the_minimum_are_left_alone(self):
        # Two lines in a directory cannot pay for their own header line.
        text = "aaaa/one.py\naaaa/two.py\nbbbb/three.py\nbbbb/four.py\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)
        self.assertNotIn(f'{res.metadata["sentinel"]}dir', res.compressed)

    def test_bare_filenames_without_directories_are_untouched(self):
        text = "README.md\nLICENSE\nsetup.py\nMakefile\n"
        res, back = roundtrip(text)
        self.assertEqual(back, text)

    def test_grep_style_lines_group_too(self):
        text = "".join(f"src/app/mod_{i}.py:{i}:    return None\n"
                       for i in range(6))
        res, back = roundtrip(text)
        self.assertEqual(back, text)
        self.assertGreater(res.reduction, 0.0)

    def test_grouping_survives_capping(self):
        # The cap marker is the last line and carries no path; grouping must
        # step around it rather than trying to prefix it.
        text = "".join(f"pkg/sub/file_{i}.py\n" for i in range(lst._MAX_RESULTS + 20))
        res, back = roundtrip(text)
        self.assertEqual(back, text)


# A `find`-style listing of a repo, the shape where most of the volume is noise.
FIND_WITH_NOISE = """\
./README.md
./smush/cli.py
./smush/core.py
./smush/__pycache__/cli.cpython-311.pyc
./smush/__pycache__/core.cpython-311.pyc
./smush/__pycache__/harness.cpython-311.pyc
./.git/objects/1a/2b3c4d
./.git/objects/5e/6f7a8b
./.git/refs/heads/main
./node_modules/left-pad/index.js
./tests/test_cli.py
"""

# `ls -R`: a `dir:` header, then bare names, then a blank line. The names carry
# no path of their own, which is what makes a whole-block decision necessary.
LS_RECURSIVE = """\
smush:
__init__.py
cli.py
__pycache__

smush/__pycache__:
__init__.cpython-311.pyc
cli.cpython-311.pyc
core.cpython-311.pyc

smush/compressors:
__init__.py
listing_compressor.py
"""


class NoiseFilterTests(unittest.TestCase):
    """The lossy filter: most of a recursive listing is not the answer."""

    def test_find_style_noise_is_dropped(self):
        res = compress_payload(FIND_WITH_NOISE, "listing", command="find .")
        out = res.compressed
        self.assertNotIn(".pyc", out)
        self.assertNotIn("6f7a8b", out)
        self.assertNotIn("left-pad", out)
        # ...and everything that was actually asked for survives.
        self.assertIn("README.md", out)
        self.assertIn("cli.py", out)
        self.assertIn("test_cli.py", out)

    def test_dropping_declares_itself_and_counts_what_went(self):
        res = compress_payload(FIND_WITH_NOISE, "listing", command="find .")
        self.assertFalse(res.reversible)
        self.assertTrue(res.metadata["lossy"])
        joined = " ".join(res.dropped)
        self.assertIn("3 entries under __pycache__/", joined)
        self.assertIn("3 entries under .git/", joined)
        self.assertIn("1 entry under node_modules/", joined)

    def test_the_output_itself_says_it_was_filtered(self):
        # The footer is a CLI flag and the sidecar is overwritten every call;
        # the payload has to be self-describing on its own.
        res = compress_payload(FIND_WITH_NOISE, "listing", command="find .")
        sentinel = res.metadata["sentinel"]
        self.assertIn(f"{sentinel}drop 3 under __pycache__/", res.compressed)

    def test_it_is_worth_doing(self):
        res = compress_payload(FIND_WITH_NOISE, "listing", command="find .")
        self.assertGreater(res.reduction, 0.35, res.compressed)

    def test_ls_recursive_drops_the_whole_block(self):
        res = compress_payload(LS_RECURSIVE, "listing", command="ls -R smush")
        out = res.compressed
        self.assertNotIn(".cpython-311.pyc", out)
        # The header goes with its entries; no orphans left behind.
        self.assertNotIn("smush/__pycache__:", out)
        self.assertIn("smush/compressors:", out)
        self.assertIn("listing_compressor.py", out)
        self.assertGreater(res.reduction, 0.30, out)

    def test_an_empty_noise_block_is_left_alone(self):
        # It would save one line and force the report to say "0 entries".
        text = "smush:\ncli.py\ncore.py\n\nsmush/__pycache__:\n"
        res = compress_payload(text, "listing", command="ls -R smush")
        self.assertTrue(res.reversible)
        self.assertEqual(decompress(res.compressed, res.metadata), text)

    def test_ls_recursive_scored_zero_before_this(self):
        # Bare names inside a block share no prefix and cap nothing, so every
        # reversible transform in this codec is a no-op on `ls -R`. Filtering
        # is the only thing that has ever moved this shape.
        res = compress_payload(LS_RECURSIVE, "listing")
        self.assertEqual(res.tokens_saved, 0)


class NoiseFilterGateTests(unittest.TestCase):
    """The three gates. Getting one of these wrong ships a wrong answer."""

    def test_no_command_means_no_dropping(self):
        res = compress_payload(FIND_WITH_NOISE, "listing")
        self.assertTrue(res.reversible)
        self.assertEqual(res.dropped, [])
        self.assertIn(".pyc", res.compressed)
        self.assertEqual(decompress(res.compressed, res.metadata), FIND_WITH_NOISE)

    def test_a_command_naming_the_directory_keeps_it(self):
        res = compress_payload(FIND_WITH_NOISE, "listing",
                               command="find . -path '*node_modules*'")
        self.assertIn("left-pad", res.compressed)
        # The directories the user did not ask about are still filtered.
        self.assertNotIn(".pyc", res.compressed)

    def test_listing_a_noise_directory_directly_is_not_filtered(self):
        text = ".git/config\n.git/HEAD\n.git/refs/heads/main\n.git/index\n"
        res = compress_payload(text, "listing", command="ls -R .git/")
        self.assertTrue(res.reversible)
        self.assertEqual(decompress(res.compressed, res.metadata), text)

    def test_git_ls_files_is_exempt_entirely(self):
        # Tracked files are a curated set: anything under a noise directory is
        # there because somebody committed it on purpose.
        text = "README.md\nnode_modules/vendored/pinned.js\nsmush/cli.py\n"
        res = compress_payload(text, "listing", command="git ls-files")
        self.assertTrue(res.reversible)
        self.assertIn("pinned.js", res.compressed)

    def test_a_file_merely_named_like_a_noise_dir_survives(self):
        # Only directory components are noise. `build` the file is content.
        text = "src/build\nsrc/dist\nsrc/main.py\nsrc/util.py\n"
        res = compress_payload(text, "listing", command="find src")
        self.assertIn("build", res.compressed)
        self.assertIn("dist", res.compressed)
        self.assertTrue(res.reversible)

    def test_clean_listings_stay_reversible_under_a_command(self):
        for text in (FIND_STYLE, GREP_STYLE, WINDOWS_STYLE, MULTI_DIR):
            res = compress_payload(text, "listing", command="find .")
            self.assertTrue(res.reversible, text[:40])
            self.assertEqual(res.dropped, [])
            self.assertEqual(decompress(res.compressed, res.metadata), text)


class NoiseFilterInteractionTests(unittest.TestCase):
    """The filter must not break the reversible transforms it runs before."""

    def test_prefix_factoring_still_happens_after_filtering(self):
        text = "".join(
            [f"/srv/app/src/mod_{i}.py\n" for i in range(6)]
            + [f"/srv/app/.git/objects/{i:02d}/abcdef\n" for i in range(6)]
        )
        res = compress_payload(text, "listing", command="find /srv/app")
        self.assertNotIn("objects", res.compressed)
        # One header, not one prefix per line: the marker lines are appended
        # last precisely so they cannot break this.
        self.assertEqual(res.compressed.count("/srv/app/"), 1)
        self.assertGreater(res.reduction, 0.5, res.compressed)

    def test_everything_except_the_dropped_lines_still_reverses(self):
        text = "".join(
            [f"/srv/app/src/mod_{i}.py\n" for i in range(6)]
            + [f"/srv/app/.git/objects/{i:02d}/abcdef\n" for i in range(6)]
        )
        res = compress_payload(text, "listing", command="find /srv/app")
        back = decompress(res.compressed, res.metadata)
        expected = "".join(f"/srv/app/src/mod_{i}.py\n" for i in range(6))
        self.assertEqual(back, expected)

    def test_filtering_composes_with_capping(self):
        text = "".join(
            [f"pkg/sub/file_{i}.py\n" for i in range(lst._MAX_RESULTS + 20)]
            + [f"pkg/__pycache__/file_{i}.pyc\n" for i in range(30)]
        )
        res = compress_payload(text, "listing", command="find pkg")
        self.assertNotIn(".pyc", res.compressed)
        self.assertIn("results omitted", res.compressed)

    def test_missing_trailing_newline_does_not_glue_on_a_marker(self):
        text = "a/.git/x\nb/keep.py\nb/other.py\nb/last.py"
        res = compress_payload(text, "listing", command="find .")
        self.assertNotIn("last.py~", res.compressed)
        self.assertIn("last.py\n", res.compressed)


class NoiseHelperTests(unittest.TestCase):
    def test_active_dirs_excludes_anything_the_command_names(self):
        self.assertEqual(lst._active_noise_dirs(None), frozenset())
        self.assertEqual(lst._active_noise_dirs("   "), frozenset())
        active = lst._active_noise_dirs("find .")
        self.assertIn(".git", active)
        self.assertNotIn(".git", lst._active_noise_dirs("ls .git"))
        self.assertNotIn("build", lst._active_noise_dirs("npm run build"))

    def test_noise_hit_respects_include_last(self):
        noise = frozenset({"build"})
        self.assertIsNone(lst._noise_hit("src/build", noise, include_last=False))
        self.assertEqual(lst._noise_hit("src/build", noise, include_last=True), "build")
        self.assertEqual(lst._noise_hit("src/build/o.js", noise, include_last=False),
                         "build")

    def test_split_components_handles_both_separators_and_dots(self):
        self.assertEqual(lst._split_components("./a/b.py"), ["a", "b.py"])
        self.assertEqual(lst._split_components("C:\\a\\b.py"), ["C:", "a", "b.py"])


if __name__ == "__main__":
    unittest.main()
