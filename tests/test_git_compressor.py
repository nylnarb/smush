"""Tests for the git compressor (lossy).

Run: python -m unittest discover -s tests    (from the repo root)
stdlib unittest only — no third-party test dependency.

The load-bearing assertions are the negative ones: no path, sha, state or
+/- line may ever disappear. Everything the codec is allowed to remove is
scaffolding a reader reconstructs for free.
"""

import unittest

from ltc import compress_payload, decompress


STATUS = """\
On branch stage-b
Your branch is up to date with 'origin/stage-b'.

Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
        modified:   ltc/core.py
        new file:   ltc/compressors/git_compressor.py

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
        modified:   README.md
        modified:   ltc/harness.py
        deleted:    old.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
        samples/git_status.txt
        tests/test_git_compressor.py
"""

DIFF = """\
diff --git a/app.py b/app.py
index 3f2a1b9..9c4d2e7 100644
--- a/app.py
+++ b/app.py
@@ -10,12 +10,12 @@ def handler():
     one = 1
     two = 2
     three = 3
     four = 4
     five = 5
-    old_call()
+    new_call()
     six = 6
     seven = 7
     eight = 8
     nine = 9
"""

LOG = """\
commit 3f2a1b9c4d2e5f60718293a4b5c6d7e8f9012345
Author: Alice Smith <alice@example.com>
Date:   Thu Jul 24 17:43:01 2026 -0500

    Add the git compressor

    It groups status paths and caps diff context.

commit 9c4d2e5f60718293a4b5c6d7e8f90123453f2a1b
Author: Bob Jones <bob@example.com>
Date:   Wed Jul 23 09:11:44 2026 -0500

    Fix the sniffer
"""


def run(text: str):
    return compress_payload(text, "git")


class TestStatus(unittest.TestCase):
    def test_every_path_and_state_survives(self):
        out = run(STATUS).compressed
        for path in ("ltc/core.py", "ltc/compressors/git_compressor.py",
                     "README.md", "old.py", "samples/git_status.txt",
                     "tests/test_git_compressor.py"):
            self.assertIn(path, out)
        for state in ("modified", "new file", "deleted"):
            self.assertIn(state, out)

    def test_branch_line_survives(self):
        self.assertIn("On branch stage-b", run(STATUS).compressed)

    def test_hint_lines_are_gone(self):
        out = run(STATUS).compressed
        self.assertNotIn('(use "git add', out)
        self.assertNotIn('(use "git restore', out)

    def test_same_state_paths_share_one_line(self):
        out = run(STATUS).compressed
        self.assertIn("modified: README.md, ltc/harness.py", out)
        # Distinct states within one section must not be merged.
        self.assertIn("new file: ltc/compressors/git_compressor.py", out)
        self.assertIn("deleted: old.py", out)

    def test_untracked_paths_are_grouped(self):
        out = run(STATUS).compressed
        self.assertIn("samples/git_status.txt, tests/test_git_compressor.py", out)

    def test_shrinks(self):
        res = run(STATUS)
        self.assertLess(res.compressed_tokens, res.original_tokens)


class TestDiff(unittest.TestCase):
    def test_changes_and_file_header_survive(self):
        out = run(DIFF).compressed
        self.assertIn("diff --git a/app.py b/app.py", out)
        self.assertIn("@@ -10,12 +10,12 @@", out)
        self.assertIn("-    old_call()", out)
        self.assertIn("+    new_call()", out)

    def test_scaffolding_is_gone(self):
        out = run(DIFF).compressed
        self.assertNotIn("index 3f2a1b9", out)
        self.assertNotIn("--- a/app.py", out)
        self.assertNotIn("+++ b/app.py", out)

    def test_context_is_capped_to_the_lines_around_a_change(self):
        out = run(DIFF).compressed
        # Nearest two context lines on each side are kept ...
        self.assertIn("    four = 4", out)
        self.assertIn("    five = 5", out)
        self.assertIn("    six = 6", out)
        self.assertIn("    seven = 7", out)
        # ... and the far ones are replaced by a counted marker.
        self.assertNotIn("    one = 1", out)
        self.assertNotIn("    nine = 9", out)
        self.assertIn("unchanged line(s)", out)

    def test_removed_line_starting_with_dashes_is_not_mistaken_for_scaffolding(self):
        text = ("diff --git a/n.md b/n.md\n"
                "@@ -1,3 +1,3 @@\n"
                " title\n"
                "---- a section rule ----\n"
                "+++ added marks +++\n"
                " end\n")
        out = run(text).compressed
        self.assertIn("---- a section rule ----", out)
        self.assertIn("+++ added marks +++", out)


class TestLog(unittest.TestCase):
    def test_each_commit_becomes_one_line_with_sha_date_author_subject(self):
        out = run(LOG).compressed
        self.assertIn("3f2a1b9 2026-07-24 Alice Smith: Add the git compressor", out)
        self.assertIn("9c4d2e5 2026-07-23 Bob Jones: Fix the sniffer", out)

    def test_emails_are_dropped(self):
        self.assertNotIn("@example.com", run(LOG).compressed)

    def test_reports_the_dropped_body_line(self):
        res = run(LOG)
        self.assertIn("commit message body line(s)", " ".join(res.dropped))
        self.assertNotIn("It groups status paths", res.compressed)

    def test_oneline_log_is_left_alone(self):
        text = "3f2a1b9 Add the git compressor\n9c4d2e5 Fix the sniffer\n"
        res = run(text)
        self.assertEqual(res.compressor, "identity")
        self.assertEqual(res.compressed, text)


class TestLossyContract(unittest.TestCase):
    def test_declares_itself_lossy(self):
        for text in (STATUS, DIFF, LOG):
            res = run(text)
            self.assertFalse(res.reversible)
            self.assertTrue(res.metadata["lossy"])
            self.assertTrue(res.dropped)

    def test_decompress_returns_input_unchanged(self):
        res = run(STATUS)
        self.assertEqual(decompress(res.compressed, res.metadata), res.compressed)

    def test_unrecognized_git_output_stays_reversible(self):
        text = "Everything up-to-date\n"
        res = run(text)
        self.assertEqual(res.compressor, "identity")
        self.assertTrue(res.reversible)
        self.assertEqual(res.compressed, text)

    def test_empty_input(self):
        res = run("")
        self.assertEqual(res.compressor, "identity")
        self.assertEqual(res.compressed, "")


class TestRouting(unittest.TestCase):
    def test_command_hint_routes_status(self):
        # Status is deliberately not sniffable, so the hint is the only way in.
        self.assertEqual(compress_payload(STATUS, "auto").compressor, "generic")
        self.assertEqual(
            compress_payload(STATUS, "auto", command="git status").compressor, "git")

    def test_diff_and_log_are_sniffed_without_a_hint(self):
        self.assertEqual(compress_payload(DIFF, "auto").compressor, "git")
        self.assertEqual(compress_payload(LOG, "auto").compressor, "git")

    def test_uninteresting_git_subcommands_are_not_hinted(self):
        # `git push` output is short; routing it here would only risk damage.
        res = compress_payload("Everything up-to-date\n", "auto", command="git push")
        self.assertEqual(res.compressor, "identity")


class TestDeterminism(unittest.TestCase):
    def test_repeatable(self):
        a, b = run(DIFF), run(DIFF)
        self.assertEqual(a.compressed, b.compressed)
        self.assertEqual(a.metadata, b.metadata)


if __name__ == "__main__":
    unittest.main()
