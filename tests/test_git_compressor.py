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


MULTI_FILE_DIFF = """\
diff --git a/added.py b/added.py
new file mode 100644
index 0000000..aa11bb2
--- /dev/null
+++ b/added.py
@@ -0,0 +1,2 @@
+import os
+print(os)
diff --git a/gone.py b/gone.py
deleted file mode 100755
index cc33dd4..0000000
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-print("bye")
diff --git a/old/name.py b/new/name.py
similarity index 92%
rename from old/name.py
rename to new/name.py
index ee55ff6..1122334 100644
--- a/old/name.py
+++ b/new/name.py
@@ -1,1 +1,1 @@ class Thing:
-    a = 1
+    a = 2
diff --git a/runme.sh b/runme.sh
old mode 100644
new mode 100755
index 5566778..99aabbc
--- a/runme.sh
+++ b/runme.sh
@@ -1,1 +1,1 @@
-echo one
+echo two
"""


def run(text: str, command=None):
    return compress_payload(text, "git", command=command)


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
        # The four-line preamble folds to one, but the path and both shas —
        # everything a reader needs to identify the file and its blobs — stay.
        self.assertIn("diff app.py 3f2a1b9..9c4d2e7", out)
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
        # The nearest context line on each side is kept as an anchor ...
        self.assertIn("    five = 5", out)
        self.assertIn("    six = 6", out)
        # ... and everything further out is replaced by a counted marker. One
        # anchor is enough because the hunk header still carries the line
        # numbers; see _DIFF_CONTEXT for why 2 -> 1 is worth so much more than
        # git's 3 -> 2.
        self.assertNotIn("    four = 4", out)
        self.assertNotIn("    seven = 7", out)
        self.assertNotIn("    one = 1", out)
        self.assertNotIn("    nine = 9", out)
        self.assertIn("unchanged line(s)", out)

    def test_hunk_header_keeps_line_numbers_and_drops_the_scope_hint(self):
        out = run(DIFF).compressed
        self.assertIn("@@ -10,12 +10,12 @@", out)
        self.assertNotIn("def handler()", out)
        self.assertTrue(any("hunk header scope hint" in d
                            for d in run(DIFF).dropped))

    def test_hunk_header_without_a_scope_hint_is_untouched(self):
        text = ("diff --git a/a.py b/a.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-a\n"
                "+b\n")
        self.assertIn("@@ -1,1 +1,1 @@", run(text).compressed)


class TestFileHeaderFolding(unittest.TestCase):
    """The four-line preamble folds to one, losing only repetition.

    Every assertion here is about something that must SURVIVE. The preamble is
    the densest ceremony in a diff and also the only place the path and shas
    appear, so folding it is exactly where a careless codec loses substance.
    """

    def test_paths_and_shas_all_survive(self):
        out = run(MULTI_FILE_DIFF).compressed
        for token in ("added.py", "aa11bb2", "gone.py", "cc33dd4",
                      "old/name.py", "new/name.py", "ee55ff6", "1122334",
                      "runme.sh", "5566778", "99aabbc"):
            self.assertIn(token, out)

    def test_new_and_deleted_and_rename_and_mode_facts_survive(self):
        out = run(MULTI_FILE_DIFF).compressed
        self.assertIn("diff added.py (new) 0000000..aa11bb2", out)
        self.assertIn("(deleted mode 100755)", out)
        self.assertIn("old/name.py -> new/name.py", out)
        self.assertIn("(mode 100644->100755)", out)

    def test_repetition_is_what_goes(self):
        out = run(MULTI_FILE_DIFF).compressed
        self.assertNotIn("diff --git", out)
        self.assertNotIn("--- a/", out)
        self.assertNotIn("+++ b/", out)
        self.assertNotIn("/dev/null", out)
        self.assertNotIn("similarity index", out)
        # The unchanged 100644 on an ordinary edit is the one mode that goes.
        self.assertNotIn("index ee55ff6", out)

    def test_every_changed_line_survives_byte_for_byte(self):
        out = run(MULTI_FILE_DIFF).compressed
        for line in MULTI_FILE_DIFF.split("\n"):
            if line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
                self.assertIn(line, out)

    def test_folding_is_declared_as_dropped(self):
        res = run(MULTI_FILE_DIFF)
        self.assertTrue(res.lossy)
        self.assertTrue(any("preamble" in d for d in res.dropped), res.dropped)

    def test_unparsable_preamble_is_passed_through_verbatim(self):
        # A quoted path is git telling us the name needs unquoting rules this
        # codec does not implement. Bail rather than guess at a path.
        text = ('diff --git "a/we ird.py" "b/we ird.py"\n'
                "index 1234567..7654321 100644\n"
                "@@ -1,1 +1,1 @@\n"
                "-a\n"
                "+b\n")
        out = run(text).compressed
        self.assertIn('diff --git "a/we ird.py" "b/we ird.py"', out)

    def test_unrecognized_extended_header_ends_the_preamble_intact(self):
        text = ("diff --git a/a.bin b/a.bin\n"
                "index 1234567..7654321 100644\n"
                "GIT quantum header 42\n"
                "@@ -1,1 +1,1 @@\n"
                "-a\n"
                "+b\n")
        self.assertIn("GIT quantum header 42", run(text).compressed)

    def test_binary_marker_survives(self):
        text = ("diff --git a/logo.png b/logo.png\n"
                "index 1234567..7654321 100644\n"
                "Binary files a/logo.png and b/logo.png differ\n")
        out = run(text).compressed
        self.assertIn("logo.png", out)
        self.assertIn("binary", out)


class TestWideContextGate(unittest.TestCase):
    """`git diff -U10` asked for context by name; trimming it answers a
    different question than the one asked."""

    def test_explicit_wide_context_suppresses_the_trim(self):
        out = run(DIFF, command="git diff -U10").compressed
        self.assertIn("    one = 1", out)
        self.assertIn("    nine = 9", out)
        self.assertNotIn("unchanged line(s)", out)

    def test_long_form_and_function_context_also_suppress_it(self):
        for cmd in ("git diff --unified=10", "git diff --function-context",
                    "git diff -W", "git diff -U 8"):
            self.assertIn("    one = 1", run(DIFF, command=cmd).compressed, cmd)

    def test_the_rest_of_the_codec_still_runs_under_wide_context(self):
        # Standing down on context is not standing down on ceremony.
        out = run(DIFF, command="git diff -U10").compressed
        self.assertIn("diff app.py 3f2a1b9..9c4d2e7", out)
        self.assertNotIn("+++ b/app.py", out)

    def test_no_hint_and_ordinary_diff_commands_still_trim(self):
        for cmd in (None, "git diff", "git show HEAD", "git diff --stat -p"):
            self.assertIn("unchanged line(s)", run(DIFF, command=cmd).compressed,
                          repr(cmd))

    def test_the_hint_reaches_the_codec_through_auto_dispatch(self):
        # Guards the core wiring, not the codec: git must be in _COMMAND_AWARE
        # or the gate above is dead code in production.
        res = compress_payload(DIFF, "auto", command="git diff -U10")
        self.assertEqual(res.compressor, "git")
        self.assertIn("    one = 1", res.compressed)


class TestDiffEdgeCases(unittest.TestCase):
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
