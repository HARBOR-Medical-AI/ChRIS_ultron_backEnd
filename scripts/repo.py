"""
What the development scripts share (lint.py, reformat.py): running a tool, git in the
mounted checkout, and the files a branch changed since its merge-base.

A script of scripts/ imports this module by name: Python puts the directory of the script
it runs, and ``unittest discover -s scripts`` the directory it searches, on ``sys.path``.
"""

import os
import subprocess  # nosec B404 - runs git and the tools with fixed argument lists
import time
from pathlib import Path


# Right after a commit, Docker Desktop's file sharing can show the container the new ref a
# moment before the commit object and then git exits 128: try again after these delays.
GIT_RETRY_DELAYS = (0.5, 1.0)


class ToolError(Exception):
    """
    A tool that is missing or failed; ``status`` is its exit status when it ran.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def run(cmd, cwd=".", ok=(0, 1), text=True, label=None, stdin=None):
    """
    Run a command and return its stdout, giving it ``stdin`` when set; fail on an exit
    status not in ``ok``.
    """
    try:
        proc = subprocess.run(  # nosec B603 nosemgrep - fixed argument lists, no shell
            cmd, cwd=cwd, capture_output=True, text=text, check=False, input=stdin
        )
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]} is not installed: the dev image predates it, "
                        f"`just build` makes a new one") from None

    if proc.returncode not in ok:
        stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
        name = label or " ".join(cmd[:3])
        raise ToolError(f"{name} exited {proc.returncode}: {stderr.strip()[-2000:]}",
                        proc.returncode)
    return proc.stdout


def git(*args, ok=(0,), text=True):
    # The checkout is mounted into the container, maybe owned by another uid.
    cmd = ["git", "-c", "safe.directory=*", *args]

    for delay in (*GIT_RETRY_DELAYS, None):
        try:
            return run(cmd, ok=ok, text=text, label=f"git {args[0]}")
        except ToolError as e:
            if delay is None or e.status != 128:
                raise
            time.sleep(delay)


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
