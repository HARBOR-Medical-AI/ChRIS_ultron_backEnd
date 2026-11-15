"""
Tests for scripts/reformat.py, run in the dev image by ``just lint-test``.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import reformat
import repo
from test_repo import RepositoryTestCase


# two statements Ruff reformats, each on a line of its own
UGLY = "x = {  1:2 }\ny = {  3:4 }\n"


class ChangedLinesTests(unittest.TestCase):

    def test_hunks_give_the_lines_they_add_and_a_deletion_none(self):
        diff = (b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                b"@@ -1 +1 @@\n-a\n+b\n"
                b"@@ -3,0 +4,2 @@ def f():\n+c\n+d\n"
                b"@@ -7,2 +8,0 @@\n-e\n-f\n"
                b"@@ -9,1 +9,1 @@\n- @@ -1 +1 @@ in a line of the file\n+x\n")

        self.assertEqual(reformat.changed_lines(diff), [(1, 1), (4, 5), (9, 9)])

    def test_ranges_that_touch_are_made_one(self):
        self.assertEqual(reformat.merged([(5, 6), (1, 2), (3, 3), (9, 9)]),
                         [(1, 3), (5, 6), (9, 9)])


class FormatRangesTests(unittest.TestCase):

    def test_the_last_range_goes_first_and_one_above_stops_where_a_call_widened_it(self):
        text = b"".join(b"line %d\n" % n for n in range(1, 11))
        calls = []

        def format_range(current, start, end):
            calls.append((start, end))
            if (start, end) == (6, 6):
                # widened upwards to line 3, and lines 3 to 6 became one
                lines = current.split(b"\n")
                return b"\n".join(lines[:2] + [b"joined"] + lines[6:])
            return current

        out = reformat.format_ranges(text, [(2, 4), (6, 6)], format_range)

        self.assertEqual(calls, [(6, 6), (2, 2)])
        self.assertEqual(out.split(b"\n")[2], b"joined")

    def test_a_range_within_lines_a_call_rewrote_is_done(self):
        text = b"a\nb\nc\nd\n"
        calls = []

        def format_range(current, start, end):
            calls.append((start, end))
            return b"A\nB\nC\nd\n" if (start, end) == (3, 3) else current

        reformat.format_ranges(text, [(1, 1), (3, 3)], format_range)

        self.assertEqual(calls, [(3, 3)])


class ReformatTests(RepositoryTestCase):

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = reformat.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_only_the_changed_lines_are_formatted(self):
        self.branch({"a.py": UGLY})
        self.write({"a.py": "x = {  1:2 }\ny = {  3:5 }\n"})

        status, out, err = self.main("format")

        self.assertEqual((self.root / "a.py").read_text(), "x = {  1:2 }\ny = {3: 5}\n")
        self.assertIn("Reformatted 1 of 1", out)
        self.assertEqual(status, 0, out + err)

    def test_committed_changes_count_and_a_new_file_is_formatted_whole(self):
        self.branch({"a.py": UGLY})
        self.commit({"b.py": UGLY}, "new file")

        status, out, err = self.main("format")

        self.assertEqual((self.root / "b.py").read_text(), "x = {1: 2}\ny = {3: 4}\n")
        self.assertEqual((self.root / "a.py").read_text(), UGLY)
        self.assertEqual(status, 0, out + err)

    def test_an_untracked_file_is_formatted_whole(self):
        self.branch({})
        self.write({"pkg/c.py": UGLY})

        status, out, err = self.main("format")

        self.assertEqual((self.root / "pkg/c.py").read_text(), "x = {1: 2}\ny = {3: 4}\n")
        self.assertEqual(status, 0, out + err)

    def test_deleted_lines_alone_leave_the_file_as_it_is(self):
        self.branch({"a.py": UGLY + "z = 3\n"})
        self.write({"a.py": UGLY})

        status, out, err = self.main("check")

        self.assertIn("Nothing to format", out)
        self.assertEqual(status, 0, out + err)

    def test_check_prints_the_diff_and_changes_nothing(self):
        self.branch({"a.py": UGLY})
        changed = "x = {  1:2 }\ny = {  3:5 }\n"
        self.write({"a.py": changed})

        status, out, err = self.main("check")

        self.assertEqual((self.root / "a.py").read_text(), changed)
        self.assertIn("-y = {  3:5 }\n+y = {3: 5}\n", out)
        self.assertIn("1 of 1 changed Python files would be reformatted", out)
        self.assertEqual(status, 1, out + err)

    def test_a_file_ruff_cannot_parse_is_a_failure_that_names_it(self):
        self.branch({})
        self.write({"bad.py": "def f(:\n"})

        status, _, err = self.main("format")

        self.assertIn("bad.py", err)
        self.assertEqual(status, 2)

    def test_no_python_file_changed_says_so(self):
        self.branch({"a.py": UGLY})
        self.write({"notes.md": "# notes\n"})

        status, out, err = self.main("check")

        self.assertIn("No Python file changed", out)
        self.assertEqual(status, 0, out + err)

    def test_check_finds_nothing_after_format(self):
        # the first pass starts inside the call and widens up to it; the next starts at
        # the call's first line, where Ruff also empties the blank line above it
        base = "def f():\n    a = 1\n    \n    g(1,\n      2)\n"
        self.branch({"a.py": base})
        self.write({"a.py": base.replace("2)", "3)")})

        self.assertEqual(self.main("format")[0], 0)
        status, out, err = self.main("check")

        self.assertIn("Nothing to format", out)
        self.assertEqual(status, 0, out + err)

    def test_a_file_known_only_under_a_new_name_is_formatted_whole(self):
        self.branch({"old.py": UGLY})
        repo.git("mv", "old.py", "new.py")

        status, out, err = self.main("format")

        self.assertEqual((self.root / "new.py").read_text(), "x = {1: 2}\ny = {3: 4}\n")
        self.assertEqual(status, 0, out + err)

    def test_a_changed_last_line_without_a_newline_is_formatted_and_check_agrees(self):
        self.branch({"a.py": "x = 1\n"})
        self.write({"a.py": "x = 1\ny = {  3:4 }"})

        self.assertEqual(self.main("format")[0], 0)
        status, out, err = self.main("check")

        # Ruff's range format leaves the end of the file as it is
        self.assertEqual((self.root / "a.py").read_text(), "x = 1\ny = {3: 4}")
        self.assertIn("Nothing to format", out)
        self.assertEqual(status, 0, out + err)

    def test_a_format_that_does_not_settle_is_a_failure_that_changes_nothing(self):
        # the case of test_check_finds_nothing_after_format, which takes two passes
        base = "def f():\n    a = 1\n    \n    g(1,\n      2)\n"
        changed = base.replace("2)", "3)")
        self.branch({"a.py": base})
        self.write({"a.py": changed})

        with mock.patch.object(reformat, "MAX_PASSES", 1):
            status, out, err = self.main("format")

        self.assertIn("a.py: Ruff's format of the changed lines did not settle in 1 passes",
                      err)
        self.assertEqual((self.root / "a.py").read_text(), changed)
        self.assertEqual(status, 2, out + err)

    def test_the_files_of_a_checkout_nested_in_this_one_are_left_out(self):
        # a worktree as the container sees it: its .git names a path it does not have
        self.branch({"a.py": UGLY})
        self.write({"worktree/.git": "gitdir: /nowhere/.git/worktrees/agent\n",
                    "worktree/b.py": UGLY})

        status, out, err = self.main("check")

        self.assertIn("No Python file changed", out)
        self.assertEqual((self.root / "worktree/b.py").read_text(), UGLY)
        self.assertEqual(status, 0, out + err)

    def test_a_base_that_does_not_exist_is_a_failure(self):
        self.branch({"a.py": UGLY})

        status, _, err = self.main("check", "--base", "no-such-branch")

        self.assertIn("no-such-branch", err)
        self.assertEqual(status, 2)

    def test_the_merge_base_with_another_branch(self):
        self.branch({"a.py": UGLY})
        self.commit({"a.py": "x = {  1:2 }\ny = {  3:5 }\n"}, "on feature")
        repo.git("branch", "other")
        self.commit({"a.py": "x = {  1:3 }\ny = {  3:5 }\n"}, "after other")

        status, out, err = self.main("format", "--base", "other")

        self.assertEqual((self.root / "a.py").read_text(), "x = {1: 3}\ny = {  3:5 }\n")
        self.assertEqual(status, 0, out + err)


if __name__ == "__main__":
    unittest.main()
