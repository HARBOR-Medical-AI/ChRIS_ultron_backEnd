"""
Format with Ruff the lines of Python this branch changed and the statements they are in.

Ruff's whole-file format would rewrite most of CUBE's Python files. Therefore this formats
only what the branch touches: the lines changed since the merge-base with origin/master
(uncommitted ones included) and the whole of a file git does not track or knows only
under a new name (renames are not followed). Ruff runs with its default style and no
configuration (``--isolated``), widening a range to the whole statements it touches (and
emptying a blank line next to one); the passes repeat until they change nothing more, so
that ``check`` finds nothing after ``format``, and a file they do not settle in
MAX_PASSES is a failure.

    just format [--base REF]
    just format-check [--base REF]

``format`` rewrites the files; ``check`` prints what ``format`` would change as a diff and
changes nothing. Exit status: 0 nothing (left) to format, 1 ``check`` found something, 2
failure, such as a file Ruff cannot parse.

Codacy keys an issue by the text of its line. Therefore reformatting a line that carries
an issue the merge-base has too makes it new to Codacy: run ``just lint`` afterwards.
"""

import argparse
import difflib
import re
import sys
import tempfile
from pathlib import Path

from repo import ToolError, changed_files, git, go_to_top, merge_base, run


# a hunk header of `git diff -U0`: the new side starts at a line and spans a count of
# lines, 1 when the count is left out
HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# the most passes over a file: a pass can widen into a line the one before left (a blank
# line above a statement it rewrote), and the next finds nothing more
MAX_PASSES = 4


def changed_lines(diff):
    """
    The ranges of lines, 1-based and inclusive, that a ``git diff -U0`` adds or changes
    in the new file. A hunk that only deletes changes no line of it.
    """
    ranges = []

    for line in diff.split(b"\n"):
        match = HUNK.match(line)

        if match:
            start, count = int(match[1]), int(match[2] or 1)

            if count:
                ranges.append((start, start + count - 1))
    return ranges


def merged(ranges):
    """
    The ranges in order, those that overlap or touch made one.
    """
    out = []

    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def first_difference(old, new):
    """
    The first line, 1-based, at which two texts differ.
    """
    old_lines, new_lines = old.split(b"\n"), new.split(b"\n")

    for number, (a, b) in enumerate(zip(old_lines, new_lines), 1):
        if a != b:
            return number
    return min(len(old_lines), len(new_lines)) + 1


def format_ranges(text, ranges, format_range):
    """
    Apply ``format_range(text, start, end)`` to each range of lines, the last first, so
    that a range's numbers stay valid while the ones below it change their line counts.
    Ruff can widen a range upwards to the start of a statement: a range above that
    reaches into the lines a call rewrote then ends where they begin, and one that lies
    within them is done.
    """
    pending = merged(ranges)

    while pending:
        start, end = pending.pop()
        new = format_range(text, start, end)

        if new != text:
            first = first_difference(text, new)
            pending = [(s, min(e, first - 1)) for s, e in pending if s < first]
            text = new
    return text


def ruff(path, text, start=None, end=None):
    """
    ``text``, the content of ``path``, formatted by Ruff: the lines ``start`` to ``end``
    (and the whole statements they touch) or else all of it.
    """
    cmd = ["ruff", "format", "--isolated", "--no-cache", "--stdin-filename", path]

    if start is not None:
        # the end of a range is a position Ruff leaves out: the start of the next line
        cmd.append(f"--range={start}-{end + 1}")

    try:
        return run([*cmd, "-"], ok=(0,), text=False, label="ruff format", stdin=text)
    except ToolError as e:
        raise ToolError(f"{path}: {e}") from None


def base_content(base, path):
    """
    The content of ``path`` at the commit ``base``, empty when it has no such file.
    """
    if not git("ls-tree", "--name-only", base, "--", path).strip():
        return b""
    return git("show", f"{base}:{path}", text=False)


def changed_ranges(old, new):
    """
    The ranges of lines of ``new`` that ``git diff -U0`` shows changed from ``old``.
    """
    with tempfile.TemporaryDirectory() as directory:
        a, b = Path(directory, "a"), Path(directory, "b")
        a.write_bytes(old)
        b.write_bytes(new)
        diff = git("diff", "--no-index", "-U0", "--no-color", "--no-ext-diff", str(a),
                   str(b), ok=(0, 1), text=False)
    return changed_lines(diff)


def formatted(base, path, untracked):
    """
    The content of ``path`` and what it is with the lines the branch changed formatted,
    pass after pass until one changes nothing; raises when MAX_PASSES all changed it.
    """
    text = Path(path).read_bytes()

    if path in untracked:
        return text, ruff(path, text)

    # a file new since the merge-base is one range of all its lines
    old, new = base_content(base, path), text

    for _ in range(MAX_PASSES):
        ranges = changed_ranges(old, new)
        result = format_ranges(new, ranges, lambda t, s, e: ruff(path, t, s, e))

        if result == new:
            return text, new
        new = result
    raise ToolError(f"{path}: Ruff's format of the changed lines did not settle in "
                    f"{MAX_PASSES} passes; format those lines by hand")


def unified(path, old, new):
    return "".join(difflib.unified_diff(
        old.decode(errors="replace").splitlines(keepends=True),
        new.decode(errors="replace").splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("command", choices=("format", "check"))
    parser.add_argument("--base",
                        help="branch to compare with (origin/master, else master)")
    args = parser.parse_args(argv)

    try:
        go_to_top()
        ref, base = merge_base(args.base)
        files, untracked = changed_files(base)
        python = [f for f in files if f.endswith(".py")]

        if not python:
            print(f"No Python file changed since {base[:10]} ({ref})")
            return 0

        changes = []

        for path in python:
            old, new = formatted(base, path, untracked)

            if new != old:
                changes.append((path, old, new))

        if args.command == "check":
            for path, old, new in changes:
                print(unified(path, old, new), end="")

            print(f"{len(changes)} of {len(python)} changed Python files would be "
                  f"reformatted" if changes else "Nothing to format")
            return 1 if changes else 0

        for path, _, new in changes:
            Path(path).write_bytes(new)
            print(f"Reformatted {path}")

        print(f"Reformatted {len(changes)} of {len(python)} changed Python files"
              if changes else "Nothing to format")
        return 0
    except (ToolError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
