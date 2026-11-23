"""
Tests for scripts/repo.py, run in the dev image by ``just lint-test``, and the repository
the tests of every script that reads a branch make their own.
"""

import os
import tempfile
import unittest
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
