"""
Tests for scripts/repo.py, run in the dev image by ``just lint-test``, and the repository
the tests of every script that reads a branch make their own.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import repo


class RepositoryTestCase(unittest.TestCase):
    """
    Tests in a repository of their own, in a temporary directory, with the working
    directory at its top: master with the base files committed and a feature branch
    checked out on top of it.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        self.root = Path(directory.name)
        os.chdir(self.root)
        repo.git("init", "-q", "-b", "master")

        for key, value in (("user.name", "lint test"), ("user.email", "lint@test"),
                           ("commit.gpgsign", "false")):
            repo.git("config", key, value)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self, files=None, message="base"):
        self.write(files or {})
        repo.git("add", "-A")
        repo.git("commit", "-q", "--allow-empty", "-m", message)

    def branch(self, base):
        """
        Commit ``base`` on master and check out a feature branch on top of it.
        """
        self.commit(base)
        repo.git("checkout", "-q", "-b", "feature")


class RunTests(unittest.TestCase):

    def test_a_missing_tool_is_a_failure_not_a_traceback(self):
        with self.assertRaisesRegex(repo.ToolError, "not installed"):
            repo.run(["cube-lint-no-such-tool"])

    def test_an_exit_status_not_allowed_is_a_failure_with_that_status(self):
        with self.assertRaises(repo.ToolError) as raised:
            repo.run(["git", "no-such-command"], ok=(0,))

        self.assertEqual(raised.exception.status, 1)
        self.assertIn("git no-such-command exited 1", str(raised.exception))

    def test_a_command_gets_its_stdin_and_gives_its_stdout_as_bytes_on_request(self):
        out = repo.run(["git", "hash-object", "--stdin"], text=False, stdin=b"x\n")

        self.assertEqual(out, b"587be6b4c3f93f93c489c0111bba5596147a26cb\n")


class GitTests(unittest.TestCase):

    def test_a_ref_the_file_sharing_shows_late_is_read_again(self):
        late = repo.ToolError("git rev-parse exited 128: bad object", 128)

        with mock.patch.object(repo, "run", side_effect=[late, "abc\n"]) as run, \
                mock.patch.object(repo.time, "sleep") as sleep:
            self.assertEqual(repo.git("rev-parse", "HEAD"), "abc\n")

        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(repo.GIT_RETRY_DELAYS[0])

    def test_another_failure_and_the_last_try_are_not_retried(self):
        for effects, calls in (([repo.ToolError("exited 1", 1)], 1),
                               ([repo.ToolError("exited 128", 128)] * 3, 3)):
            with self.subTest(calls=calls), \
                    mock.patch.object(repo, "run", side_effect=effects) as run, \
                    mock.patch.object(repo.time, "sleep"), \
                    self.assertRaises(repo.ToolError):
                repo.git("status")

            self.assertEqual(run.call_count, calls)


class BranchFilesTests(RepositoryTestCase):

    def test_the_merge_base_is_with_master_without_origin_and_another_branch_on_request(self):
        self.branch({"a.py": "x = 1\n"})
        master = repo.git("rev-parse", "master").strip()
        self.commit({"a.py": "x = 2\n"}, "on feature")
        repo.git("branch", "other")
        self.commit({"a.py": "x = 3\n"}, "after other")

        self.assertEqual(repo.merge_base(None), ("master", master))
        self.assertEqual(repo.merge_base("other"),
                         ("other", repo.git("rev-parse", "other").strip()))

    def test_the_changed_files_are_the_branch_s_and_the_working_tree_s(self):
        self.branch({"same.py": "x = 1\n", "edited.py": "x = 1\n", "gone.py": "x = 1\n",
                     "old.py": "x = 1\n"})
        self.commit({"committed.py": "x = 1\n"}, "on feature")
        self.write({"edited.py": "x = 2\n", "new.py": "x = 1\n",
                    "nested/.git": "gitdir: /nowhere\n", "nested/n.py": "x = 1\n"})
        repo.git("rm", "-q", "gone.py")
        repo.git("mv", "old.py", "renamed.py")
        _, base = repo.merge_base(None)

        files, untracked = repo.changed_files(base)

        self.assertEqual(files, ["committed.py", "edited.py", "new.py", "renamed.py"])
        self.assertEqual(untracked, {"new.py"})

    def test_go_to_top_moves_to_the_top_from_a_folder_below(self):
        self.branch({"pkg/a.py": "x = 1\n"})
        os.chdir(self.root / "pkg")

        repo.go_to_top()

        self.assertEqual(Path.cwd().resolve(), self.root.resolve())


class BranchTests(RepositoryTestCase):

    def open(self):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        return repo.Branch.open(None, workdir.name)

    def test_changed_and_untracked_files_are_found_whatever_their_names(self):
        self.branch({"plain.py": "x = 1\n"})
        self.commit({"café.py": "x = 2\n"}, "unicode")
        self.write({"tab\tname.py": "x = 3\n", "with space.py": "x = 4\n"})

        self.assertEqual(self.open().files, ["café.py", "tab\tname.py", "with space.py"])

    def test_deleted_files_drop_out_and_renamed_ones_are_new(self):
        self.branch({"gone.py": "x = 1\n", "old.py": "x = 2\n", "kept.py": "x = 3\n"})
        repo.git("rm", "-q", "gone.py")
        repo.git("mv", "old.py", "new.py")
        repo.git("rm", "-q", "--cached", "kept.py")  # untracked now, but still there
        branch = self.open()

        self.assertEqual(branch.files, ["kept.py", "new.py"])
        self.assertEqual(branch.deleted, ["gone.py", "old.py"])
        self.assertEqual(branch.in_base(branch.files), ["kept.py"])

    def test_a_branch_that_only_deletes_files_has_its_base_extracted(self):
        self.branch({"gone.py": "x = 1\n", "kept.py": "x = 2\n"})
        repo.git("rm", "-q", "gone.py")
        branch = self.open()

        self.assertEqual((branch.files, branch.deleted), ([], ["gone.py"]))
        self.assertTrue((branch.base_tree.root / "gone.py").is_file())

    def test_a_base_without_files_is_an_empty_tree(self):
        self.branch({})
        self.write({"a.py": "x = 1\n"})

        self.assertEqual(self.open().files, ["a.py"])

    def test_unrelated_histories_say_so(self):
        self.branch({"a.py": "x = 1\n"})
        repo.git("checkout", "-q", "--orphan", "other")
        self.commit({"b.py": "x = 2\n"}, "unrelated")

        with self.assertRaisesRegex(repo.ToolError, "share no commit"):
            self.open()


class WorkingTreeCopyTests(RepositoryTestCase):

    def copy(self):
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.dest = Path(workdir.name) / "copy"
        tree = repo.working_tree_copy(self.dest)
        self.assertEqual(tree.root, self.dest)
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*")
                      if p.is_file() or p.is_symlink())

    def test_the_copy_holds_what_git_sees_of_the_working_tree(self):
        self.branch({".gitignore": "ignored.py\n__pycache__/\n", "kept.py": "x = 1\n",
                     "gone.py": "x = 1\n", "pkg/edited.py": "x = 1\n"})
        os.remove("gone.py")
        self.write({"pkg/edited.py": "x = 2\n", "pkg/new.pyi": "y: int\n",
                    "pkg/data.txt": "data\n", "ignored.py": "z = 1\n",
                    "pkg/__pycache__/a.cpython-312.pyc": "",
                    "nested/.git": "gitdir: /nowhere\n", "nested/n.py": "w = 1\n"})
        os.symlink("kept.py", "link.py")

        self.assertEqual(self.copy(), [".gitignore", "kept.py", "link.py", "pkg/data.txt",
                                       "pkg/edited.py", "pkg/new.pyi"])
        self.assertEqual((self.dest / "pkg/edited.py").read_text(), "x = 2\n")

    def test_a_symbolic_link_is_copied_as_one(self):
        self.branch({"kept.py": "x = 1\n"})
        os.symlink("kept.py", "link.py")

        self.assertEqual(self.copy(), ["kept.py", "link.py"])
        self.assertEqual(os.readlink(self.dest / "link.py"), "kept.py")


class ListIssuesTests(unittest.TestCase):

    def issue(self, path, line, rule, message, hint=""):
        return repo.Issue(path, rule, f"text {line}", message, line, hint)

    def list(self, issues, new, show_all=False, **kwargs):
        branch = SimpleNamespace(files=["a.py", "b.py"], deleted=["c.py"],
                                 base="0123456789ab", ref="origin/master")
        options = {"command": "check", "gate": "the gate", **kwargs}
        out = io.StringIO()
        with redirect_stdout(out):
            repo.list_issues(branch, issues, new, show_all, **options)
        return out.getvalue()

    def test_a_hint_lines_up_with_its_message_whatever_the_rule_width(self):
        long = self.issue("a.py", 1, "dodgy ssh_rsa_public_key", "a key", "remove it")
        short = self.issue("b.py", 4, "rule", "an issue", "do this")
        lines = self.list([long, short], [long, short]).splitlines()

        for message, hint in (("a key", "remove it"), ("an issue", "do this")):
            row = next(i for i, line in enumerate(lines) if line.endswith(message))
            self.assertEqual(lines[row].index(message), lines[row + 1].index(hint))

    def test_every_line_of_a_hint_lines_up_with_the_message(self):
        hinted = self.issue("a.py", 1, "rule", "an issue",
                            "Possible variants:\n    def f()")
        lines = self.list([hinted], [hinted]).splitlines()

        row = next(i for i, line in enumerate(lines) if line.endswith("an issue"))
        column = lines[row].index("an issue")
        self.assertEqual(lines[row + 1], " " * column + "Possible variants:")
        self.assertEqual(lines[row + 2], " " * column + "    def f()")

    def test_of_two_issues_with_one_key_only_the_new_one_is_marked(self):
        old, new = (repo.Issue("a.py", "rule", "return 'a'", "same", n) for n in (2, 6))
        self.assertEqual(old, new)  # one key: file, rule, line text and message

        out = self.list([old, new], [new], show_all=True)

        self.assertIn("      2  rule", out)
        self.assertIn(" *    6  rule", out)
        self.assertIn("the gate (* above)", out)

    def test_the_older_ones_are_counted_with_the_command_that_lists_them(self):
        old = self.issue("a.py", 2, "rule", "old")
        unsure = self.issue("b.py", 3, "rule", "maybe new")
        out = self.list([old, unsure], [], unsure=[unsure], command="check --strict")

        self.assertNotIn("old", out)
        self.assertIn(" ?    3  rule", out)
        self.assertIn("No new issues in 3 files changed since 0123456789 (origin/master)",
                      out)
        self.assertIn("1 issues there predate the branch: `just check --strict --all`", out)

    def test_a_scope_names_where_the_new_ones_were_looked_for(self):
        new = self.issue("d.py", 1, "rule", "broken")
        out = self.list([new], [new], scope="the project")

        self.assertIn("1 new issues in the project, 3 files changed since 0123456789", out)
        self.assertIn("\nthe gate\n", out)

        old = self.issue("a.py", 2, "rule", "old")
        out = self.list([old], [], scope="the project")

        self.assertIn("No new issues in the project, 3 files changed", out)
        self.assertIn("1 issues of the changed files predate the branch", out)


if __name__ == "__main__":
    unittest.main()
