"""
What the development scripts share (lint.py, reformat.py, typecheck.py): running a tool,
git in the mounted checkout, the files a branch changed since its merge-base, the trees a
check compares (the merge-base's and the working tree's), the issues found in them and
their report.

A script of scripts/ imports this module by name: Python puts the directory of the script
it runs on ``sys.path``. ``unittest discover -s scripts`` does the same for the directory
it searches.
"""

import io
import os
import shutil
import subprocess  # nosec B404 - runs git and the tools with fixed argument lists
import tarfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path


# Right after a commit, Docker Desktop's file sharing can show the container the new ref
# a moment before the commit object and then git exits 128: try again after these delays.
GIT_RETRY_DELAYS = (0.5, 1.0)

# The width of the rule column in the report.
RULE_WIDTH = 14


class ToolError(Exception):
    """
    A tool that is missing or failed; ``status`` is its exit status when it ran.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def execute(cmd, cwd=".", text=True, stdin=None, env=None):
    """
    Run a command, giving it ``stdin`` and the environment ``env`` when set, and return
    the finished process whatever its exit status.
    """
    try:
        return subprocess.run(  # nosec B603 nosemgrep - fixed argument lists, no shell
            cmd, cwd=cwd, capture_output=True, text=text, check=False, input=stdin,
            env=env
        )
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} is not installed: the dev image predates it, "
                        f"`just build` makes a new one") from None


def run(cmd, cwd=".", ok=(0, 1), text=True, label=None, stdin=None):
    """
    Run a command and return its stdout, giving it ``stdin`` when set; fail on an exit
    status not in ``ok``.
    """
    proc = execute(cmd, cwd, text, stdin)

    if proc.returncode not in ok:
        stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
        name = label or " ".join(cmd[:3])

        raise ToolError(f"{name} exited {proc.returncode}: {stderr.strip()[-2000:]}",
                        proc.returncode)
    return proc.stdout


def git(*args, ok=(0,), text=True):
    # The checkout is mounted into the container, maybe owned by another uid.
    cmd = ["git", "-c", "safe.directory=*", *args]
    label = f"git {args[0]}"

    for delay in GIT_RETRY_DELAYS:
        try:
            return run(cmd, ok=ok, text=text, label=label)
        except ToolError as e:
            if e.status != 128:
                raise
            time.sleep(delay)
    return run(cmd, ok=ok, text=text, label=label)  # the last try


def go_to_top():
    """
    Make the top of the repository the working directory: git and the tools take paths
    from there.
    """
    os.chdir(git("rev-parse", "--show-toplevel").strip())


def merge_base(ref):
    """
    The branch to compare with, ``ref`` or else origin/master or else master, and the
    merge-base of HEAD with it.
    """
    if ref is None:
        known = git("rev-parse", "-q", "--verify", "origin/master", ok=(0, 1))
        ref = "origin/master" if known else "master"

    try:
        return ref, git("merge-base", "HEAD", ref).strip()
    except ToolError as e:
        if e.status != 1:
            raise
        raise ToolError(f"HEAD and {ref} share no commit: unrelated histories, or a "
                        f"shallow clone (`git fetch --unshallow`)") from None


def in_nested_repository(path):
    """
    Return True if ``path`` lies inside a nested Git repository or worktree.

    For example, an agent's worktree under ``.claude/worktrees`` has a ``.git``
    file pointing to a host path invisible to the container, causing Git to
    treat all files inside it as untracked.
    """
    return any((parent / ".git").exists() for parent in Path(path).parents
               if parent != Path("."))


def changed_files(base):
    """
    The files of the working tree changed since ``base``, uncommitted and untracked ones
    included, and the set of those git does not track. A deleted file drops out, a
    renamed one is new, and a file of a checkout nested in this one is left out.
    """
    # NUL-separated: no quoting of names with unusual characters
    changed = git("diff", "-z", "--name-only", "--no-renames", "--diff-filter=d", base)

    untracked = {f for f in git("ls-files", "-z", "--others", "--exclude-standard")
                 .split("\0") if f}

    files = sorted({f for f in changed.split("\0") + list(untracked)
                    if f and Path(f).is_file() and not in_nested_repository(f)})
    return files, untracked & set(files)


def deleted_files(base):
    """
    The files of ``base`` that the working tree no longer has, uncommitted deletions
    included; a renamed file's old name is one.
    """
    deleted = git("diff", "-z", "--name-only", "--no-renames", "--diff-filter=D", base)
    return sorted(f for f in deleted.split("\0")
                  if f and not Path(f).exists() and not in_nested_repository(f))


@dataclass(frozen=True)
class Issue:
    """
    An issue a check found, keyed as Codacy keys one: file, rule, source line text and
    message, so that an issue whose line only moved stays the same issue.
    """

    path: str
    rule: str
    text: str
    message: str
    line: int = field(default=0, compare=False)
    hint: str = field(default="", compare=False)


class Tree:
    """
    A checkout of the repository: the working tree, a copy of it or an extracted commit.
    A ``frozen`` tree does not change, so each check's results on it are kept.
    """

    def __init__(self, root, frozen=False):
        self.root = Path(root)
        self.frozen = frozen
        self._lines = {}
        self._results = {}

    def line(self, path, number):
        if path not in self._lines:
            try:
                text = (self.root / path).read_text(errors="replace")
                self._lines[path] = text.split("\n")
            except OSError:
                self._lines[path] = []

        lines = self._lines[path]
        return lines[number - 1].strip() if 0 < number <= len(lines) else ""

    def issue(self, path, number, rule, message, hint=""):
        return Issue(path, rule, self.line(path, number), message, number, hint)

    def results(self, check, files):
        if not self.frozen:
            return check(self, files)

        key = (check.__name__, tuple(files))

        if key not in self._results:
            self._results[key] = check(self, files)
        return self._results[key]


def working_tree_copy(dest):
    """
    Copy into ``dest`` the files git lists of the working tree (tracked and untracked, not
    ignored; deleted ones and those of a checkout nested in this one left out), the same
    files ``git archive`` gives of a commit, and return the copy as a Tree.
    """
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    dest.mkdir(parents=True, exist_ok=True)

    for name in sorted({f for f in listed.split("\0") if f}):
        path = Path(name)

        if not (path.is_file() or path.is_symlink()) or in_nested_repository(name):
            continue

        (dest / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / path, follow_symlinks=False)
    return Tree(dest)


@dataclass
class Branch:
    """
    The files changed and deleted since the merge-base, and the merge-base's tree to
    compare with.
    """

    ref: str
    base: str
    files: list
    base_tree: Tree
    deleted: list = field(default_factory=list)

    @classmethod
    def open(cls, ref, workdir):
        ref, base = merge_base(ref)
        files, _ = changed_files(base)
        deleted = deleted_files(base)

        # the archive of a commit without files holds a pax header alone, which tarfile
        # refuses
        if (files or deleted) and git("ls-tree", "--name-only", base).strip():
            archive = git("archive", base, text=False)

            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(workdir, filter="data")
        return cls(ref, base, files, Tree(workdir, frozen=True), deleted)

    def in_base(self, files):
        return [f for f in files if (self.base_tree.root / f).is_file()]


def list_issues(branch, issues, new, show_all, *, command, gate, unsure=(), scope=None):
    """
    Print the new issues, or with ``show_all`` all of ``issues`` (the new ones marked
    ``*``), and the ``unsure`` ones (marked ``?``). Then print how many are new and
    ``gate``, or, when none is, how many older ones there are and the ``just <command>
    --all`` that lists them. ``new`` and ``unsure`` hold items of ``issues``, told apart
    by identity, since an old and a new issue can share a key. ``scope`` says where the new
    ones were looked for, when not only in the changed files.
    """
    new_ids = {id(i) for i in new}
    unsure_ids = {id(i) for i in unsure}
    shown = issues if show_all else [*new, *unsure]
    width = max([RULE_WIDTH, *(len(i.rule) for i in shown)])
    path = None

    for issue in sorted(shown, key=lambda i: (i.path, i.line, i.rule)):
        if issue.path != path:
            path = issue.path
            print(f"\n{path}")

        mark = ("?" if id(issue) in unsure_ids
                else "*" if show_all and id(issue) in new_ids else " ")

        print(f" {mark}{issue.line:>5}  {issue.rule:<{width}} {issue.message}")

        if issue.hint:
            print(textwrap.indent(issue.hint, " " * (width + 10)))

    changed = len(branch.files) + len(branch.deleted)
    where = f"{changed} files changed since {branch.base[:10]} ({branch.ref})"
    where = f"{scope}, {where}" if scope else where

    print(f"\n{len(new)} new issues in {where}" if new else f"\nNo new issues in {where}")

    if new:
        print(gate + (" (* above)" if show_all else ""))
    elif len(issues) > len(unsure) and not show_all:
        older = "of the changed files" if scope else "there"
        print(f"{len(issues) - len(unsure)} issues {older} predate the branch: "
              f"`just {command} --all` lists them")
