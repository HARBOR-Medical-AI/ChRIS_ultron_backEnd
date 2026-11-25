"""
List the mypy errors that this branch adds.

mypy checks the whole project twice, in a copy of the working tree's files as git sees
them (tracked and untracked, not ignored) and in the tree of its merge-base with
origin/master, both times with the working tree's settings (``[tool.mypy]`` and the Django
plugin's ``[tool.django-stubs]`` in pyproject.toml), and the errors only the working tree
has are listed. They are keyed as ``just lint`` keys an issue (file, error code, source
line text and message, without the line numbers a message names) and counted: a new copy
of a line that has an older error counts as new. Checking the whole project rather than
the changed files finds what a change breaks in a file it did not touch, such as a call
to a function whose signature changed, a query on a model field it renamed, or an import
of a module it deleted. A renamed file's errors are all new, as ``just lint``'s issues
are.

A syntax error stops mypy at once, so a tree that has one gets no other error listed:
the working tree's syntax errors are then the only errors listed, and a merge-base that
has one cannot be compared with. Both trees are checked, and their settings and models
imported, with the packages of the dev image: an error that only a package upgrade brings
shows in both trees and is not listed, and a merge-base that imports a package the image
lacks cannot be checked.

    just typecheck [--strict] [--tests] [--all] [--base REF]

``--strict`` adds mypy's strict checks (``mypy --strict``), which report every function
without annotations, and more. Its new errors are advisory: the run exits 1 on them too,
but ``just typecheck`` without it is the check. ``--tests`` checks the test modules too.
``--all`` also lists the older errors of the changed files, the new ones marked ``*``.
Exit status: 0 none new, 1 new errors, 2 failure. A git worktree cannot be checked this
way: its ``.git`` file names a path the container does not see.
"""

import argparse
import contextlib
import dataclasses
import io
import json
import math
import os
import re
import sys
import tarfile
import tempfile
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from repo import Branch, ToolError, execute, go_to_top, list_issues, working_tree_copy


#: the tables of pyproject.toml that mypy and its Django plugin read
TOOL_TABLES = ("mypy", "django-stubs")

#: the exclusions of the settings that leave the test modules out (``--tests`` drops them)
TEST_EXCLUDES = ("/tests/", "^scripts/test_[^/]*\\.py$")

#: a key TOML takes without quotes
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

#: a line number that an error's message names ("already defined on line 922"): it moves
#: with the code, so the comparison leaves it out
LINE_NUMBER = re.compile(r"\bline \d+\b")

GATE = "Fix them, or silence a false one with `# type: ignore[<code>]` on its line"
STRICT_GATE = ("Strict mode is advisory: it exits 1 on new errors too, but"
               " `just typecheck` without --strict is the check")


def settings(pyproject, strict=False, tests=False):
    """
    Return the tables of ``pyproject`` that mypy and its Django plugin read, by name.
    mypy's gets ``strict = true`` with ``strict``, and loses the test exclusions with
    ``tests``. Its ``exclude`` is always a list: a string becomes a list of one.
    """
    with open(pyproject, "rb") as f:
        tool = tomllib.load(f).get("tool", {})

    tables = {name: dict(tool[name]) for name in TOOL_TABLES if name in tool}

    if "mypy" not in tables:
        raise ToolError(f"{pyproject} has no [tool.mypy] table")

    mypy = tables["mypy"]
    exclude = mypy.get("exclude", [])
    mypy["exclude"] = [exclude] if isinstance(exclude, str) else list(exclude)

    if strict:
        mypy["strict"] = True

    if tests:
        mypy["exclude"] = [p for p in mypy["exclude"] if p not in TEST_EXCLUDES]
    return tables


def toml_key(name):
    """
    ``name`` as a TOML key: bare when TOML takes it so, quoted otherwise.
    """
    return name if BARE_KEY.fullmatch(name) else toml_string(name)


def toml_string(value):
    """
    A string, boolean, number or list of them as TOML writes it. JSON writes them alike,
    but for the DEL character, which TOML wants escaped.
    """
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_value(value):
    """
    ``value`` as TOML: a string, a boolean, an integer, a finite float, or a list of them;
    any other value is a ToolError.
    """
    def writable(item):
        return (isinstance(item, (str, bool, int))
                or isinstance(item, float) and math.isfinite(item))

    if not all(writable(item) for item in (value if isinstance(value, list)
                                           else [value])):
        raise ToolError(f"cannot write {value!r} to mypy's settings file")

    return toml_string(value)


def toml(tables):
    """
    ``tables`` as the ``[tool.<name>]`` tables of a TOML file. A table's values are those
    toml_value writes, or lists of tables of such values (mypy's ``overrides``), written
    as arrays of tables.
    """
    lines = []

    for name, table in tables.items():
        lines.append(f"[tool.{toml_key(name)}]")
        arrays = {k: v for k, v in table.items()
                  if isinstance(v, list) and v and all(isinstance(i, dict) for i in v)}

        lines += [f"{toml_key(k)} = {toml_value(v)}" for k, v in table.items()
                  if k not in arrays]

        for k, items in arrays.items():
            for item in items:
                lines.append(f"\n[[tool.{toml_key(name)}.{toml_key(k)}]]")
                lines += [f"{toml_key(ik)} = {toml_value(iv)}" for ik, iv in item.items()]

        lines.append("")
    return "\n".join(lines)


def checked_paths(root, config_file):
    """
    Return the paths of the ``files`` setting of ``config_file`` that the tree at ``root``
    has and that hold a file for mypy to check, found as mypy itself reads the setting
    (commas, globs) and searches a folder. mypy stops on a path that is missing or holds
    nothing to check, which can happen in either tree. The search changes this process's
    working directory, so make one call at a time.
    """
    try:
        from mypy.config_parser import parse_config_file
        from mypy.find_sources import InvalidSourceList, create_source_list
        from mypy.options import Options
    except ImportError:
        raise ToolError("mypy is not installed: the dev image predates it, `just build` "
                        "makes a new one") from None

    with contextlib.chdir(root):
        options, errors = Options(), io.StringIO()
        parse_config_file(options, lambda: None, str(config_file), io.StringIO(), errors)

        if not options.files:
            raise ToolError(f"mypy's settings name no `files` to check "
                            f"{errors.getvalue().strip()}".strip())
        try:
            return [path for path in options.files
                    if os.path.exists(path)
                    and create_source_list([path], options, allow_empty_dir=True)]
        except InvalidSourceList as e:
            raise ToolError(f"mypy cannot check {root}: {e}") from None


def mypy(tree, paths, config_file, cache_dir, label):
    """
    Run mypy on ``paths`` (relative to the top of ``tree``) with ``config_file`` and its
    cache in ``cache_dir``, and return its errors. A tree with no path to check has none.
    ``label`` names the tree in a failure.
    """
    if not paths:
        return []

    root = tree.root.resolve()
    # the Django plugin imports the settings and models from mypy_path and the working
    # directory, which it appends to sys.path: a PYTHONPATH entry would come first and
    # import the working tree's instead
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = execute(["mypy", "--config-file", str(config_file), "--output", "json",
                    "--no-error-summary", "--show-traceback", "--cache-dir",
                    str(cache_dir), *paths], root, env=env)
    issues, other = [], []

    for row in filter(None, proc.stdout.splitlines()):
        try:
            d = json.loads(row)
        except ValueError:
            other.append(row)
            continue

        name = os.path.relpath(root / d["file"], root)

        # a note explains the error before it, or is reveal_type's
        if d["severity"] != "error" or name.startswith(".."):
            continue

        message = " ".join(part.strip() for part in d["message"].splitlines())

        issues.append(
            tree.issue(name, max(d.get("line") or 0, 0), d.get("code") or "error",
                       message, d.get("hint") or ""))

    # mypy exits 0 without errors, 1 with them, and 2 on a syntax error, which it lists;
    # any other end is a failure, a run cut short too: it would list only some errors
    finished = proc.returncode in (0, 1) or proc.returncode == 2 and any(
        i.rule == "syntax" for i in issues)

    if other or not finished:
        detail = "\n".join([*other, proc.stderr.strip()]).strip()[-2000:]
        raise ToolError(f"mypy on {label} exited {proc.returncode} after {len(issues)} "
                        f"errors: {detail}", proc.returncode)
    return issues


def comparable(issue):
    """
    ``issue`` without the line numbers its message names, which move with the code.
    """
    return dataclasses.replace(issue, message=LINE_NUMBER.sub("line N", issue.message))


def new_issues(found, base):
    """
    The issues of ``found`` that ``base`` lacks, compared without the line numbers their
    messages name, and counted: where ``found`` has n issues with one key and ``base`` m,
    the last n - m of them are new.
    """
    remaining = Counter(map(comparable, base))
    new = []

    for issue in found:
        key = comparable(issue)

        if remaining[key]:
            remaining[key] -= 1
        else:
            new.append(issue)
    return new


def compare(branch, head, config_file, workdir):
    """
    The errors of ``head`` (the working tree's copy) in the changed files or new
    anywhere, and the new ones. A syntax error stops mypy, so the working tree's syntax
    errors are all its new errors, and a merge-base that has one is a ToolError.
    """
    head_paths = checked_paths(head.root, config_file)
    base_paths = checked_paths(branch.base_tree.root, config_file)
    base_label = f"the merge-base {branch.base[:10]}"

    if not head_paths:
        raise ToolError("the working tree has no file for mypy to check: see `files` in "
                        "[tool.mypy]")

    with ThreadPoolExecutor(2) as pool:
        found = pool.submit(mypy, head, head_paths, config_file, workdir / "cache-head",
                            "the working tree")
        base = pool.submit(mypy, branch.base_tree, base_paths, config_file,
                           workdir / "cache-base", base_label)
        found, base = found.result(), base.result()

    blocking = [i for i in found if i.rule == "syntax"]

    if blocking:
        return blocking, blocking

    stopped = next((i for i in base if i.rule == "syntax"), None)

    if stopped:
        raise ToolError(f"mypy stopped at a syntax error of {base_label} "
                        f"({stopped.path}:{stopped.line}), so the working tree cannot be "
                        f"compared with it: name another branch with --base")

    new = new_issues(found, base)
    new_ids = {id(i) for i in new}
    changed = set(branch.files)
    return [i for i in found if i.path in changed or id(i) in new_ids], new


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0].strip())
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--base",
                        help="branch to compare with (origin/master, else master)")
    parser.add_argument("--all", action="store_true",
                        help="list the older errors of the changed files too")
    parser.add_argument("--strict", action="store_true",
                        help="add mypy's strict checks (advisory; exits 1 on new errors)")
    parser.add_argument("--tests", action="store_true",
                        help="check the test modules too")
    args = parser.parse_args(argv)

    # the recipe that lists them all, with the options of this one
    command = " ".join(["typecheck", *(["--strict"] if args.strict else []),
                        *(["--tests"] if args.tests else []),
                        *(["--base", args.base] if args.base else [])])

    try:
        go_to_top()
        tables = settings("pyproject.toml", args.strict, args.tests)

        with tempfile.TemporaryDirectory(prefix="typecheck-") as directory:
            workdir = Path(directory)
            (workdir / "base").mkdir()  # a base without files extracts nothing
            branch = Branch.open(args.base, workdir / "base")

            if not branch.files and not branch.deleted:
                print(f"No files changed since {branch.base[:10]} ({branch.ref})")
                return 0

            # both trees with the working tree's settings, from a file of their own: the
            # merge-base's are older, and the options change them
            config_file = workdir / "mypy.toml"
            config_file.write_text(toml(tables))
            head = working_tree_copy(workdir / "head")
            issues, new = compare(branch, head, config_file, workdir)
            list_issues(branch, issues, new, args.all, command=command,
                        gate=STRICT_GATE if args.strict else GATE, scope="the project")
            return 1 if new else 0
    except (ToolError, OSError, ValueError, KeyError, tarfile.TarError) as e:
        # mypy that failed or printed something other than its JSON included; a settings
        # file that is not TOML is a ValueError
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
