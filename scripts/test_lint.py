"""
Tests for scripts/lint.py, run in the dev image by ``just lint-test``.

A test that needs a repository makes one of its own in a temporary directory: master with
the base files committed and a feature branch checked out on top of it.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import lint


def edit(row, col, end_row, end_col, content=""):
    return {"location": {"row": row, "column": col},
            "end_location": {"row": end_row, "column": end_col}, "content": content}


class ApplyEditsTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "module.py"

    def apply(self, text, edits):
        self.path.write_bytes(text.encode())
        lint.apply_edits(self.path, edits)
        return self.path.read_bytes().decode()

    def test_crlf_line_ends_stay(self):
        text = "import os\r\nimport sys\r\nprint(sys)\r\n"

        self.assertEqual(self.apply(text, [edit(1, 1, 2, 1)]),
                         "import sys\r\nprint(sys)\r\n")

    def test_a_form_feed_or_a_line_separator_ends_no_row(self):
        text = 's = "a b"\x0c\nimport os\nx = 1\n'

        self.assertEqual(self.apply(text, [edit(2, 1, 3, 1)]), 's = "a b"\x0c\nx = 1\n')

    def test_a_byte_order_mark_stays_in_front_and_takes_no_column(self):
        # Ruff's fix for an unused os: "sys, os" (columns 8 to 15) becomes "sys"
        text = "﻿import sys, os\nprint(sys)\n"

        self.assertEqual(self.apply(text, [edit(1, 8, 1, 15, "sys")]),
                         "﻿import sys\nprint(sys)\n")

    def test_columns_count_code_points(self):
        text = 'name = "café"; import os\nprint(name)\n'

        self.assertEqual(self.apply(text, [edit(1, 14, 1, 25)]),
                         'name = "café"\nprint(name)\n')

    def test_adjacent_edits_apply_from_the_end(self):
        text = "a = 1\nb = 2\nc = 3\n"
        edits = [edit(1, 1, 2, 1), edit(2, 1, 3, 1, "B = 2\n")]

        self.assertEqual(self.apply(text, edits), "B = 2\nc = 3\n")


class HelperTests(unittest.TestCase):

    def test_the_rows_an_edit_changes(self):
        self.assertEqual(lint.rows(edit(3, 1, 4, 1)), range(3, 4))
        self.assertEqual(lint.rows(edit(3, 5, 3, 9)), range(3, 4))
        self.assertEqual(lint.rows(edit(3, 5, 5, 2)), range(3, 6))

    def test_the_subject_of_flake8_and_ruff_messages(self):
        for message, line, expected in (
                ("'os' imported but unused", 3, "os"),
                ("`os` imported but unused", 3, "os"),
                ("'os.path as p' imported but unused", 3, "os.path"),
                ("local variable 'x' is assigned to but never used", 3, "x"),
                ("f-string is missing placeholders", 7, 7)):
            with self.subTest(message=message):
                self.assertEqual(lint.subject(message, line), expected)

    def test_a_missing_tool_is_a_failure_not_a_traceback(self):
        with self.assertRaisesRegex(lint.LintError, "not installed"):
            lint.run(["cube-lint-no-such-tool"])


class AdvisoriesTests(unittest.TestCase):

    LOCK = ('[[package]]\nname = "botocore"\ndependencies = [\n    { name = "urllib3" },\n'
            ']\n\n[[package]]\nname = "urllib3"\nversion = "2.7.0"\n')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        (Path(directory.name) / "uv.lock").write_text(self.LOCK)
        self.tree = lint.Tree(directory.name)

    def test_one_advisory_from_two_databases_is_one_issue_on_the_first_reference(self):
        vuln = {"dependency": {"name": "urllib3", "version": "2.7.0"},
                "summary": "chunked", "fix_versions": ["2.8.0"]}
        report = {"vulnerabilities": [
            {**vuln, "id": "PYSEC-1", "aliases": ["GHSA-aaaa", "CVE-1"]},
            {**vuln, "id": "GHSA-aaaa", "aliases": ["PYSEC-1"]}]}

        with mock.patch.object(lint, "run", return_value=json.dumps(report)):
            issues = lint.advisories(self.tree, ["uv.lock"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].line, 4)
        self.assertEqual(issues[0].text, '{ name = "urllib3" },')
        self.assertEqual(issues[0].message, "urllib3 2.7.0: GHSA-aaaa chunked (fixed in 2.8.0)")
        self.assertEqual(issues[0].hint, "just lock-upgrade urllib3")

    def test_an_audit_that_fails_or_prints_no_json_is_a_failure(self):
        for effect in (lint.LintError("uv audit exited 2: offline"), ["not json"]):
            with self.subTest(effect=effect), \
                    mock.patch.object(lint, "run", side_effect=effect), \
                    self.assertRaisesRegex(lint.LintError, "could not be audited"):
                lint.advisories(self.tree, ["uv.lock"])


class ReportTests(unittest.TestCase):

    def issue(self, path, line, rule, message, hint=""):
        return lint.Issue(path, rule, f"text {line}", message, line, hint)

    def report(self, issues, new, show_all=False):
        branch = SimpleNamespace(files=["a.py", "uv.lock"], base="0123456789ab",
                                 ref="origin/master")
        out = io.StringIO()
        with redirect_stdout(out):
            lint.report(branch, issues, new, show_all)
        return out.getvalue()

    def test_the_advisories_both_trees_have_are_listed_as_unsure(self):
        old = self.issue("uv.lock", 4, "uv audit", "urllib3 2.7.0: GHSA-aaaa",
                         "just lock-upgrade urllib3")
        older = self.issue("a.py", 2, "pyflakes F401", "'os' imported but unused")
        out = self.report([old, older], [])

        self.assertIn(" ?    4  uv audit", out)
        self.assertNotIn("'os' imported", out)
        self.assertIn("1 issues there predate the branch", out)
        self.assertIn("1 advisories (? above)", out)

    def test_a_hint_lines_up_with_its_message_whatever_the_rule_width(self):
        long = self.issue("a.py", 1, "dodgy ssh_rsa_public_key", "a key", "remove it")
        short = self.issue("uv.lock", 4, "uv audit", "an advisory", "just lock-upgrade x")
        lines = self.report([long, short], [long, short]).splitlines()

        for message, hint in (("a key", "remove it"), ("an advisory", "just lock-upgrade")):
            row = next(i for i, line in enumerate(lines) if line.endswith(message))
            self.assertEqual(lines[row].index(message), lines[row + 1].index(hint))


class RepositoryTestCase(unittest.TestCase):
    """
    Tests in a repository of their own, with the working directory at its top.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.root = Path(directory.name)
        os.chdir(self.root)
        lint.git("init", "-q", "-b", "master")

        for key, value in (("user.name", "lint test"), ("user.email", "lint@test"),
                           ("commit.gpgsign", "false")):
            lint.git("config", key, value)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self, files=None, message="base"):
        self.write(files or {})
        lint.git("add", "-A")
        lint.git("commit", "-q", "--allow-empty", "-m", message)

    def branch(self, base):
        """
        Commit ``base`` on master and check out a feature branch on top of it.
        """
        self.commit(base)
        lint.git("checkout", "-q", "-b", "feature")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = lint.main(list(argv))
        return status, out.getvalue(), err.getvalue()


class BranchTests(RepositoryTestCase):

    def open(self):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        return lint.Branch.open(None, workdir.name)

    def test_changed_and_untracked_files_are_found_whatever_their_names(self):
        self.branch({"plain.py": "x = 1\n"})
        self.commit({"café.py": "x = 2\n"}, "unicode")
        self.write({"tab\tname.py": "x = 3\n", "with space.py": "x = 4\n"})

        self.assertEqual(self.open().files, ["café.py", "tab\tname.py", "with space.py"])

    def test_deleted_files_drop_out_and_renamed_ones_are_new(self):
        self.branch({"gone.py": "x = 1\n", "old.py": "x = 2\n"})
        lint.git("rm", "-q", "gone.py")
        lint.git("mv", "old.py", "new.py")
        branch = self.open()

        self.assertEqual(branch.files, ["new.py"])
        self.assertEqual(branch.in_base(branch.files), [])

    def test_a_base_without_files_is_an_empty_tree(self):
        self.branch({})
        self.write({"a.py": "x = 1\n"})

        self.assertEqual(self.open().files, ["a.py"])

    def test_unrelated_histories_say_so(self):
        self.branch({"a.py": "x = 1\n"})
        lint.git("checkout", "-q", "--orphan", "other")
        self.commit({"b.py": "x = 2\n"}, "unrelated")

        with self.assertRaisesRegex(lint.LintError, "share no commit"):
            self.open()


class CheckTests(RepositoryTestCase):

    def test_a_moved_line_keeps_its_issue_old_and_a_new_one_is_reported(self):
        self.branch({"a.py": "import os\n\nx = 1\n"})
        self.write({"a.py": "x = 1\n\nimport os\n"})

        status, out, err = self.main("check")
        self.assertEqual(status, 0, out + err)
        self.assertIn("No new issues", out)

        self.write({"a.py": "x = 1\n\nimport os\nimport sys\n"})
        status, out, err = self.main("check")
        self.assertEqual(status, 1, out + err)
        self.assertIn("'sys' imported but unused", out)
        self.assertNotIn("'os' imported", out)

    def test_a_tool_that_prints_no_json_is_a_failure(self):
        self.branch({})
        self.write({"a.py": "x = 1\n"})

        with mock.patch.object(lint, "pylint", side_effect=ValueError("Expecting value")):
            status, _, err = self.main("check")

        self.assertEqual(status, 2)
        self.assertIn("error: Expecting value", err)

    def test_a_lockfile_that_cannot_be_audited_is_a_failure(self):
        self.branch({})
        self.write({"uv.lock": "not a lockfile\n"})  # and no pyproject.toml

        status, _, err = self.main("check")

        self.assertEqual(status, 2)
        self.assertIn("uv.lock could not be audited", err)


class FixTests(RepositoryTestCase):

    def test_the_new_issue_is_fixed_and_the_old_one_of_the_same_name_is_left(self):
        old = "def a():\n    import os  # old\n    return 1\n"
        self.branch({"a.py": old})
        self.write({"a.py": "def z():\n    import os\n    return 2\n\n\n" + old})

        status, out, err = self.main("fix")

        self.assertEqual((self.root / "a.py").read_text(),
                         "def z():\n    return 2\n\n\n" + old)
        self.assertIn("Applied 1 fixes", out)
        self.assertEqual(status, 0, out + err)

    def test_a_fix_that_would_touch_an_old_issue_is_left_undone(self):
        self.branch({"a.py": "from os import path, sep\nprint(path)\n"})  # sep: old
        changed = "from os import path, sep\nprint(1)\n"  # path: new
        self.write({"a.py": changed})

        status, out, err = self.main("fix")

        self.assertEqual((self.root / "a.py").read_text(), changed)
        self.assertIn("No new issue Ruff can fix", out)
        self.assertEqual(status, 1, out + err)

    def test_no_python_file_changed_says_so(self):
        self.branch({})
        self.write({"notes.md": "# notes\n"})

        status, out, err = self.main("fix")

        self.assertIn("No Python file changed", out)
        self.assertEqual(status, 0, out + err)


if __name__ == "__main__":
    unittest.main()
