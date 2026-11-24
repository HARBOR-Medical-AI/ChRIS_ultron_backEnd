"""
Tests for scripts/typecheck.py, run in the dev image by ``just lint-test``. mypy runs for
real, on repositories of their own, except in the tests that feed the script mypy's output.
"""

import io
import json
import os
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import repo
import typecheck
from test_repo import RepositoryTestCase


#: the repository's own pyproject.toml
REPOSITORY_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


PYPROJECT = """\
[tool.mypy]
files = ["src", "scripts"]
exclude = ["/__pycache__/", "/tests/", "^scripts/test_[^/]*\\\\.py$"]
mypy_path = "src"
explicit_package_bases = true
check_untyped_defs = true

[[tool.mypy.overrides]]
module = ["untyped.*"]
ignore_missing_imports = true

[tool.django-stubs]
django_settings_module = "config.settings"

[tool.other]
x = 1
"""

OLD_ERROR = "def f() -> int:\n    return 'a'\n"
UNTYPED = "def f(a):\n    return a\n"


def write_files(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


class SettingsTests(unittest.TestCase):

    def settings(self, text, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "pyproject.toml"
        path.write_text(text)
        return typecheck.settings(path, **kwargs)

    def test_the_tables_mypy_reads_in_strict_mode_or_with_the_tests_on_request(self):
        tables = self.settings(PYPROJECT)
        self.assertEqual(list(tables), ["mypy", "django-stubs"])
        self.assertNotIn("strict", tables["mypy"])

        self.assertTrue(self.settings(PYPROJECT, strict=True)["mypy"]["strict"])
        self.assertEqual(self.settings(PYPROJECT, tests=True)["mypy"]["exclude"],
                         ["/__pycache__/"])

    def test_an_exclude_given_as_a_string_is_one_pattern(self):
        text = '[tool.mypy]\nexclude = "(/migrations/|/tests/)"\n'

        self.assertEqual(self.settings(text)["mypy"]["exclude"], ["(/migrations/|/tests/)"])
        self.assertEqual(self.settings(text, tests=True)["mypy"]["exclude"],
                         ["(/migrations/|/tests/)"])

    def test_a_pyproject_without_mypy_s_settings_is_a_failure(self):
        with self.assertRaisesRegex(repo.ToolError, "no \\[tool.mypy\\] table"):
            self.settings("[tool.other]\nx = 1\n")


class TomlTests(unittest.TestCase):

    def test_the_tables_written_read_back_the_same(self):
        tables = {"mypy": {"files": ["a", "b"], "strict": True, "n": 3, "ratio": 0.5,
                           "note": 'quotes " \\ é 🐍 a\ttab and a \x7f',
                           "a key": "quoted",
                           "empty": [], "overrides": [{"module": ["x.*"], "flag": False},
                                                      {"module": "y"}]},
                  "django-stubs": {"django_settings_module": "config.settings"}}

        self.assertEqual(tomllib.loads(typecheck.toml(tables))["tool"], tables)

    def test_a_value_toml_cannot_hold_this_way_is_a_failure(self):
        for value in ({"nested": 1}, float("inf"), [["nested"]], None):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(repo.ToolError, "cannot write"):
                typecheck.toml({"mypy": {"key": value}})


class RepositorySettingsTests(unittest.TestCase):
    """
    The repository's own pyproject.toml, which --tests and the written settings file
    depend on.
    """

    def test_tests_drops_exclusions_the_repository_s_settings_have(self):
        exclude = typecheck.settings(REPOSITORY_PYPROJECT)["mypy"]["exclude"]

        for pattern in typecheck.TEST_EXCLUDES:
            self.assertIn(pattern, exclude)

    def test_its_tables_written_read_back_the_same(self):
        for options in ({}, {"strict": True, "tests": True}):
            with self.subTest(options=options):
                tables = typecheck.settings(REPOSITORY_PYPROJECT, **options)
                self.assertEqual(tomllib.loads(typecheck.toml(tables))["tool"], tables)


class CheckedPathsTests(unittest.TestCase):

    def paths(self, files, settings):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        write_files(root, files)
        (root / "mypy.toml").write_text(f"[tool.mypy]\n{settings}")
        return typecheck.checked_paths(root, root / "mypy.toml")

    def test_the_paths_the_tree_has_with_a_file_to_check(self):
        files = {"src/a.py": "", "scripts/test_x.py": "", "empty/data.txt": "",
                 "one.py": ""}
        settings = ('files = ["src", "scripts", "empty", "one.py", "missing", "gone.py"]\n'
                    'exclude = ["^scripts/test_[^/]*\\\\.py$"]\n')

        self.assertEqual(self.paths(files, settings), ["src", "one.py"])

    def test_the_files_setting_is_read_as_mypy_reads_it(self):
        files = {"src/a.py": "", "src/b.py": "", "lib/c.py": ""}

        self.assertEqual(self.paths(files, 'files = "lib, missing"\n'), ["lib"])
        self.assertEqual(sorted(self.paths(files, 'files = ["src/*.py"]\n')),
                         ["src/a.py", "src/b.py"])

    def test_settings_that_name_no_files_are_a_failure(self):
        with self.assertRaisesRegex(repo.ToolError, "name no `files` to check"):
            self.paths({"src/a.py": ""}, "strict = true\n")


class MypyOutputTests(unittest.TestCase):
    """
    What is kept of mypy's JSON lines and exit status, given to the script instead of
    mypy's own.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "a.py").write_text("first\nsecond\n")

    def row(self, severity, message, code=None, line=1, hint=None, file="a.py"):
        return json.dumps({"file": file, "line": line, "column": 1, "message": message,
                           "hint": hint, "code": code, "severity": severity})

    def mypy(self, returncode, *rows, stderr="", paths=("a.py",)):
        proc = SimpleNamespace(returncode=returncode, stdout="\n".join(rows) + "\n",
                               stderr=stderr)

        with mock.patch.object(typecheck, "execute", return_value=proc) as execute:
            issues = typecheck.mypy(repo.Tree(self.root), list(paths),
                                    Path("/c/mypy.toml"), "/cache", "the tree")
        return issues, execute

    def test_errors_of_the_tree_are_kept_and_made_issues(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/live/tree"}):
            issues, execute = self.mypy(
                1,
                self.row("error", "Incompatible\n  types", "assignment", 2, "use int"),
                self.row("note", "Revealed type is int", "misc"),
                self.row("error", "A module-wide error", None, -1),
                self.row("error", "outside", "misc", file="../b.py"))

        self.assertEqual([(i.path, i.line, i.rule, i.text, i.message, i.hint)
                          for i in issues], [
            ("a.py", 2, "assignment", "second", "Incompatible types", "use int"),
            ("a.py", 0, "error", "", "A module-wide error", "")])
        self.assertEqual(execute.call_args.args, (
            ["mypy", "--config-file", "/c/mypy.toml", "--output", "json",
             "--no-error-summary", "--show-traceback", "--cache-dir", "/cache", "a.py"],
            self.root))
        self.assertNotIn("PYTHONPATH", execute.call_args.kwargs["env"])

    def test_a_tree_without_a_path_to_check_has_no_errors(self):
        issues, execute = self.mypy(1, self.row("error", "never read"), paths=())

        self.assertEqual(issues, [])
        execute.assert_not_called()

    def test_a_syntax_error_stops_mypy_with_status_2_and_is_listed(self):
        issues, _ = self.mypy(2, self.row("error", "Unexpected EOF", "syntax"))

        self.assertEqual([i.rule for i in issues], ["syntax"])

    def test_a_run_that_did_not_finish_is_a_failure_whatever_it_listed(self):
        for returncode, rows, stderr, message in (
                (2, (), "Traceback ...\nKeyError: 'x'",
                 "mypy on the tree exited 2 after 0 errors: Traceback"),
                (2, (self.row("error", "Duplicate module named 'a'"),), "",
                 "exited 2 after 1 errors"),
                (-9, (self.row("error", "x", "misc"),) * 3, "", "exited -9 after 3 errors"),
                (2, ("Error constructing plugin instance of NewSemanalDjangoPlugin",),
                 "INTERNAL ERROR", "Error constructing plugin instance.*\nINTERNAL ERROR"),
                (1, ("Found 1 error",), "", "exited 1 after 0 errors: Found 1 error")):
            with self.subTest(message=message), \
                    self.assertRaisesRegex(repo.ToolError, message):
                self.mypy(returncode, *rows, stderr=stderr)


class NewIssuesTests(unittest.TestCase):

    def test_issues_are_counted_by_key(self):
        def issue(line):
            return repo.Issue("a.py", "rule", "return 'a'", "message", line)

        found = [issue(2), issue(6), issue(9)]

        new = typecheck.new_issues(found, [issue(2)])

        self.assertEqual([i.line for i in new], [6, 9])
        self.assertEqual(typecheck.new_issues(found, found), [])

    def test_the_line_numbers_a_message_names_are_left_out(self):
        def issue(message):
            return repo.Issue("a.py", "no-redef", "def f():", message, 5)

        old = issue('Name "f" already defined on line 1')

        moved = issue('Name "f" already defined on line 2')
        other = issue('Name "g" already defined on line 2')

        self.assertEqual(typecheck.new_issues([moved], [old]), [])
        self.assertEqual(typecheck.new_issues([other], [old]), [other])


class TypecheckTestCase(RepositoryTestCase):
    """
    A repository of its own with mypy's settings, and typecheck.py's ``main`` run in it.
    """

    SETTINGS = PYPROJECT.split("[tool.django-stubs]")[0]  # no Django project here

    def branch(self, base):
        super().branch({"pyproject.toml": self.SETTINGS, **base})

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = typecheck.main(["check", *argv])
        return status, out.getvalue(), err.getvalue()


class CheckTests(TypecheckTestCase):

    def test_a_new_error_is_listed_and_an_older_one_that_moved_is_not(self):
        self.branch({"src/a.py": OLD_ERROR})
        self.write({"src/a.py": "x = 1\n\n\n" + OLD_ERROR})

        status, out, err = self.main()
        self.assertEqual(status, 0, out + err)
        self.assertIn("No new issues in the project, 1 files changed", out)
        self.assertIn("1 issues of the changed files predate the branch: "
                      "`just typecheck --all`", out)

        self.write({"src/a.py": "x: str = 1\n\n\n" + OLD_ERROR})
        status, out, err = self.main()
        self.assertEqual(status, 1, out + err)
        self.assertIn("assignment", out)
        self.assertNotIn("return-value", out)
        self.assertIn("`# type: ignore[<code>]`", out)

    def test_a_new_copy_of_a_line_with_an_older_error_is_new(self):
        self.branch({"src/a.py": OLD_ERROR})
        self.write({"src/a.py": OLD_ERROR + "\n\n" + OLD_ERROR.replace("f()", "g()")})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("     6  return-value", out)
        self.assertNotIn("     2  return-value", out)

    def test_a_signature_change_is_listed_where_an_unchanged_file_calls_it(self):
        self.branch({"src/lib.py": "def f(a: int) -> int:\n    return a\n",
                     "src/use.py": "from lib import f\n\nf(1)\n"})
        self.write({"src/lib.py": "def f(a: int, b: int) -> int:\n    return a + b\n"})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/use.py\n      3  call-arg", out)

    def test_a_renamed_model_field_is_listed_where_an_unchanged_query_names_it(self):
        settings = self.SETTINGS.replace(
            "check_untyped_defs = true",
            'check_untyped_defs = true\nplugins = ["mypy_django_plugin.main"]')
        settings += '[tool.django-stubs]\ndjango_settings_module = "config.settings"\n'
        model = ("from django.db import models\n\n\nclass Thing(models.Model):\n"
                 "    name = models.CharField(max_length=10)\n")
        self.branch({"pyproject.toml": settings, "src/config/__init__.py": "",
                     "src/config/settings.py": 'INSTALLED_APPS = ["things"]\n',
                     "src/things/__init__.py": "", "src/things/models.py": model,
                     "src/use.py": 'from things.models import Thing\n\n'
                                   'Thing.objects.filter(name="x")\n'})
        self.write({"src/things/models.py": model.replace("name =", "title =")})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/use.py\n      3  misc", out)
        self.assertIn("Cannot resolve keyword 'name' into field", out)

    def test_all_lists_the_older_ones_of_the_changed_files_and_marks_the_new(self):
        self.branch({"src/lib.py": "def f(a: int) -> int:\n    return a\n",
                     "src/use.py": "from lib import f\n\nf(1)\n", "src/a.py": OLD_ERROR})
        self.write({"src/lib.py": "def f(a: int, b: int) -> int:\n    return a + b\n",
                    "src/a.py": "x = 1\n" + OLD_ERROR, "src/b.py": OLD_ERROR})

        status, out, err = self.main("--all")

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/a.py\n      3  return-value", out)    # older, changed file
        self.assertIn("src/b.py\n *    2  return-value", out)    # new file
        self.assertIn("src/use.py\n *    3  call-arg", out)      # new, unchanged file

    def test_the_errors_of_a_renamed_file_are_all_new(self):
        self.branch({"src/a.py": OLD_ERROR})
        repo.git("mv", "src/a.py", "src/b.py")

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/b.py\n      2  return-value", out)

    def test_the_base_is_checked_with_the_working_tree_s_settings(self):
        older = self.SETTINGS.replace('"/tests/"', '"/tests/", "old"')
        self.branch({"src/old.py": OLD_ERROR, "pyproject.toml": older})
        self.write({"pyproject.toml": self.SETTINGS})

        status, out, err = self.main()

        self.assertEqual(status, 0, out + err)
        self.assertNotIn("src/old.py", out)

    def test_a_path_only_the_working_tree_has_is_checked_there(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"pyproject.toml": self.SETTINGS.replace('"scripts"]',
                                                            '"scripts", "lib"]'),
                    "lib/b.py": OLD_ERROR})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("lib/b.py", out)

    def test_a_base_without_files_has_no_errors(self):
        RepositoryTestCase.branch(self, {})
        self.write({"pyproject.toml": self.SETTINGS, "src/a.py": OLD_ERROR})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/a.py", out)

    def test_a_working_tree_without_a_file_to_check_is_a_failure(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"pyproject.toml": self.SETTINGS.replace('["src", "scripts"]',
                                                            '["srcs"]')})

        status, _, err = self.main()

        self.assertEqual(status, 2)
        self.assertIn("the working tree has no file for mypy to check", err)

    def test_a_file_git_ignores_is_not_checked(self):
        self.branch({".gitignore": "ignored.py\n", "src/a.py": "x = 1\n"})
        self.write({"src/a.py": "x = 2\n", "src/ignored.py": OLD_ERROR})

        status, out, err = self.main()

        self.assertEqual(status, 0, out + err)

    def test_the_whole_tree_can_be_checked(self):
        whole = self.SETTINGS.replace('files = ["src", "scripts"]', 'files = ["."]')
        self.branch({"pyproject.toml": whole, "top.py": "x = 1\n"})
        self.write({"top.py": "x: str = 1\n"})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("top.py", out)

    def test_the_test_modules_are_checked_only_when_asked(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"src/tests/test_a.py": "y: int = 'a'\n",
                    "scripts/test_b.py": "z: int = 'b'\n"})

        status, out, err = self.main()
        self.assertEqual(status, 0, out + err)

        status, out, err = self.main("--tests")
        self.assertEqual(status, 1, out + err)
        self.assertIn("src/tests/test_a.py", out)
        self.assertIn("scripts/test_b.py", out)

    def test_strict_mode_lists_only_what_it_adds_as_advice(self):
        self.branch({"src/old.py": UNTYPED})
        self.write({"src/old.py": UNTYPED + "\n", "src/new.py": UNTYPED})

        status, out, err = self.main()
        self.assertEqual(status, 0, out + err)

        status, out, err = self.main("--strict")
        self.assertEqual(status, 1, out + err)
        self.assertIn("src/new.py", out)
        self.assertIn("no-untyped-def", out)
        self.assertNotIn("src/old.py", out)
        self.assertIn("Strict mode: advice; `just typecheck` without --strict is the check",
                      out)

        self.write({"src/new.py": "x = 1\n"})
        status, out, err = self.main("--strict")
        self.assertEqual(status, 0, out + err)
        self.assertIn("`just typecheck --strict --all` lists them", out)

    def test_a_syntax_error_is_a_new_error(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"src/a.py": "def f(:\n"})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("syntax", out)

    def test_a_syntax_error_both_trees_have_is_listed_rather_than_hiding_the_rest(self):
        self.branch({"src/a.py": "x = 1\n", "src/z.py": "def f(:\n"})
        self.write({"src/a.py": "x: str = 1\n"})

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/z.py\n      1  syntax", out)

    def test_a_merge_base_with_a_syntax_error_cannot_be_compared_with(self):
        self.branch({"src/a.py": OLD_ERROR, "src/z.py": "def f(:\n"})
        self.write({"src/z.py": "x = 1\n"})

        status, _, err = self.main()

        self.assertEqual(status, 2)
        self.assertIn("mypy stopped at a syntax error of the merge-base", err)
        self.assertIn("(src/z.py:1)", err)

    def test_a_branch_that_only_deletes_files_is_checked(self):
        self.branch({"src/lib.py": "def f() -> int:\n    return 1\n",
                     "src/use.py": "from lib import f\n\nf()\n"})
        repo.git("rm", "-q", "src/lib.py")

        status, out, err = self.main()

        self.assertEqual(status, 1, out + err)
        self.assertIn("src/use.py\n      1  import-not-found", out)
        self.assertIn("in the project, 1 files changed", out)

    def test_an_older_error_that_names_a_line_that_moved_is_not_new(self):
        redefined = "def f() -> None:\n    pass\n\n\ndef f() -> None:\n    pass\n"
        self.branch({"src/a.py": redefined})
        self.write({"src/a.py": "x = 1\n" + redefined})

        status, out, err = self.main()

        self.assertEqual(status, 0, out + err)

    def test_another_branch_is_compared_with_on_request(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.commit({"src/a.py": OLD_ERROR}, "an error on feature")
        repo.git("branch", "other")
        self.commit({"src/b.py": "y = 1\n"}, "after other")

        status, out, err = self.main("--base", "other")

        self.assertEqual(status, 0, out + err)
        self.assertIn("1 files changed since", out)
        self.assertIn("(other)", out)

    def test_mypy_that_fails_is_a_failure(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"src/a.py": "x = 2\n"})
        crash = SimpleNamespace(returncode=2, stdout="",
                                stderr="Traceback ...\nKeyError: 'x'")

        with mock.patch.object(typecheck, "execute", return_value=crash):
            status, _, err = self.main()

        self.assertEqual(status, 2)
        self.assertIn("error: mypy on the working tree exited 2 after 0 errors: Traceback",
                      err)

    def test_settings_that_are_not_toml_are_a_failure(self):
        self.branch({"src/a.py": "x = 1\n"})
        self.write({"pyproject.toml": "[tool.mypy\n"})

        status, _, err = self.main()

        self.assertEqual(status, 2)
        self.assertRegex(err, r"error: .*\(at line 1, column \d+\)")

    def test_no_change_is_no_check(self):
        self.branch({"src/a.py": "x = 1\n"})

        with mock.patch.object(typecheck, "mypy") as mypy:
            status, out, _ = self.main()

        self.assertEqual(status, 0)
        self.assertIn("No files changed", out)
        mypy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
