"""
Report the issues Codacy would flag as new on this branch and fix the ones Ruff can.

Codacy fails a pull request on any issue that its merge-base (git) does not have. This
runs in the dev image the checks behind nearly all of Codacy's findings on CUBE: Bandit
with pyproject.toml as its configuration, pyflakes and dodgy (Codacy's Prospector), Pylint
with Codacy's plugin and rules and ``uv audit`` for the lockfiles (Codacy's Trivy). It
checks the files changed since the merge-base, uncommitted and untracked ones included,
in the working tree and in the merge-base's tree and reports what only the working tree
has, keyed as Codacy keys an issue: file, rule, source line text and message.

Advisories are the exception: both trees are audited with today's advisories while
Codacy compares with its stored analysis of master, which can predate some of them. So
the advisories of a changed lockfile that the merge-base has too are listed as well,
marked ``?``: Codacy may still call them new.

    just lint [--all] [--base REF]
    just lint-fix [--unsafe] [--base REF]

``fix`` applies Ruff's fixes for the new pyflakes issues (safe ones only unless
``--unsafe``) but none that would touch an older issue's line, then checks again. Exit
status: 0 clean, 1 new issues, 2 failure. A git worktree cannot be checked this way: its
``.git`` file names a path the container does not see.
"""

import argparse
import json
import re
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from repo import Branch, ToolError, Tree, go_to_top, list_issues, run


# Lockfiles and the uv project each belongs to.
LOCKFILES = {"uv.lock": ".", "benchmarks/uv.lock": "benchmarks"}

# The Pylint rules Codacy's coding standard for CUBE enables (as its API listed them).
PYLINT_RULES = """
    E0100 E0101 E0102 E0103 E0104 E0105 E0106 E0107 E0108 E0110 E0112 E0113 E0114 E0115
    E0117 E0202 E0203 E0211 E0236 E0238 E0239 E0240 E0241 E0301 E0302 E0601 E0603 E0604
    E0701 E0702 E0704 E0710 E0711 E0712 E1003 E1102 E1111 E1120 E1121 E1123 E1124 E1125
    E1126 E1127 E1132 E1200 E1201 E1205 E1206 E1300 E1301 E1302 E1303 E1304 E1305 E1306
    W0101 W0102 W0106 W0109 W0122 W0123 W0150 W0199 W0221 W0222 W0233 W0410 W0601 W0622
    W0702 W0705 W0711 W1300 W1301 W1302 W1303 W1305 W1306 W1307
""".split()

# A fix can leave another for the next pass, e.g. two names of one import statement.
FIX_PASSES = 10

CODACY_GATE = "Codacy fails a pull request on any new issue"


def bandit(tree, files):
    # as Codacy does: the tree's pyproject.toml is Bandit's configuration when it has one
    config = ["-c", "pyproject.toml"] if (tree.root / "pyproject.toml").is_file() else []
    out = run(["bandit", "-q", *config, "-f", "json", *files], tree.root)

    return [
        tree.issue(
            r["filename"].removeprefix("./"), r["line_number"], f"Bandit {r['test_id']}",
            r["issue_text"],
        )
        for r in json.loads(out)["results"]
    ]


def pyflakes(tree, files):
    fmt = "%(path)s\t%(row)d\t%(code)s\t%(text)s"
    out = run(["flake8", "--isolated", "--select=F", f"--format={fmt}", *files],
              tree.root)
    issues = []

    for row in filter(None, out.splitlines()):
        path, number, code, message = row.split("\t", 3)
        issues.append(tree.issue(path, int(number), f"pyflakes {code}", message))
    return issues


def dodgy(tree, files):
    try:
        from dodgy.checks import check_file
    except ImportError:
        raise ToolError("dodgy is not installed: the dev image predates it, `just build` "
                        "makes a new one") from None
    return [
        tree.issue(path, number, f"dodgy {key}", message)
        for path in files
        for number, key, message in check_file(str(tree.root / path))
    ]


def pylint(tree, files):
    cmd = [
        "pylint", "--persistent=n", "--score=n", "--jobs=0", "--output-format=json2",
        "--load-plugins=pylint_django",
        "--disable=django-installed-checker,django-model-checker",
        "--disable=all", f"--enable={','.join(PYLINT_RULES)}", *files,
    ]
    out = run(cmd, tree.root, ok=range(32))  # 32 is a usage error, the rest flag findings

    return [
        tree.issue(m["path"], m["line"], f"Pylint {m['messageId']}", m["message"])
        for m in json.loads(out)["messages"]
    ]


def advisories(tree, lockfiles):
    issues = []

    for lockfile in lockfiles:
        project = LOCKFILES[lockfile]
        cmd = [
            "uv", "audit", "--frozen", "--no-dev", "--preview-features",
            "audit-command,json-output", "--output-format", "json", "--project", project,
        ]

        # a lockfile not audited is no lockfile without advisories
        try:
            vulns = json.loads(run(cmd, tree.root))["vulnerabilities"]
        except (ToolError, ValueError, KeyError) as e:
            raise ToolError(
                f"{lockfile} could not be audited in {tree.root}: {e}") from None

        upgrade = (
            "just lock-upgrade {}" if project == "."
            else f"just uv lock --upgrade-package {{}} --directory {project}"
        )
        seen = set()

        for vuln in vulns:
            name, version = vuln["dependency"]["name"], vuln["dependency"]["version"]
            ids = [vuln["id"], *vuln.get("aliases", [])]
            advisory = min((i for i in ids if i.startswith("GHSA-")), default=vuln["id"])

            if (name, advisory) in seen:  # the same advisory from another database
                continue

            seen.add((name, advisory))
            fixed = ", ".join(vuln.get("fix_versions") or ["no release yet"])
            summary = vuln.get("summary", "")
            message = f"{name} {version}: {advisory} {summary} (fixed in {fixed})"
            number = locked_line(tree, lockfile, name)
            issues.append(tree.issue(lockfile, number, "uv audit", message,
                                     upgrade.format(name)))
    return issues


def locked_line(tree, lockfile, name):
    """
    The line Codacy's Trivy reports a package's advisories on: the first one naming it,
    often another package's dependency on it rather than its own entry.
    """
    for number, line in enumerate((tree.root / lockfile).read_text().split("\n"), 1):
        if f'name = "{name}"' in line:
            return number
    return 0


def analyse(tree, files):
    python = [f for f in files if f.endswith(".py")]
    issues = []

    if python:
        for check in (bandit, pyflakes, dodgy, pylint):
            issues += tree.results(check, python)

    lockfiles = [f for f in files if f in LOCKFILES]

    if lockfiles:
        issues += tree.results(advisories, lockfiles)
    return issues


def compare(branch):
    """
    Return the working tree's issues in the changed files, and those the base lacks.
    """
    with ThreadPoolExecutor(2) as pool:
        found = pool.submit(analyse, Tree("."), branch.files)
        old = pool.submit(analyse, branch.base_tree, branch.in_base(branch.files))
        head, base = found.result(), set(old.result())
    return head, [i for i in head if i not in base]


def report(branch, issues, new, show_all):
    """
    List the new issues (with ``show_all`` every issue, the new ones marked ``*``) and
    the advisories of changed lockfiles that the base has too, marked ``?``.
    """
    new_set = set(new)
    unsure = [i for i in issues if i.rule == "uv audit" and i not in new_set]
    list_issues(branch, issues, new, show_all, command="lint", gate=CODACY_GATE,
                unsure=unsure)

    if unsure:
        print(f"{len(unsure)} advisories (? above) predate the branch here, but Codacy "
              "may call them new: its analysis of master can predate them. Upgrading "
              "clears them")


def ruff_fixes(files, unsafe):
    """
    Ruff's fixes for pyflakes rules as (row, edits) pairs, by file, rule and the name or
    line they concern.
    """
    cmd = ["ruff", "check", "--isolated", "--no-cache", "--select=F",
           "--output-format=json"]
    fixes: dict[tuple, list] = {}

    for d in json.loads(run([*cmd, *files])):
        fix = d.get("fix")

        if fix and fix["edits"] and (unsafe or fix["applicability"] == "safe"):
            path = str(Path(d["filename"]).relative_to(Path.cwd()))
            row = d["location"]["row"]
            key = (path, d["code"], subject(d["message"], row))
            fixes.setdefault(key, []).append((row, fix["edits"]))
    return fixes


def subject(message, line):
    """
    What a pyflakes message is about: the name it quotes, else its line.
    """
    match = re.search(r"['`]([^'`]+)['`]", message)
    return match.group(1).split(" as ")[0] if match else line


def span(edit):
    return (edit["location"]["row"], edit["location"]["column"]), (
        edit["end_location"]["row"], edit["end_location"]["column"])


def rows(edit):
    """
    The rows an edit changes: an edit that ends at the start of a row leaves that row.
    """
    (row, _), (end_row, end_col) = span(edit)
    return range(row, end_row if end_col == 1 and end_row > row else end_row + 1)


def overlaps(edits, others):
    return any(s < e2 and s2 < e
               for s, e in map(span, edits)
               for s2, e2 in map(span, others))


def apply_edits(path, edits):
    """
    Apply Ruff's edits (1-based rows and columns of code points, end exclusive) to a
    file. Rows end at \\n, \\r\\n or \\r as Ruff's do, the line ends stay as they are and
    a byte order mark, which Ruff's columns leave out, stays in front.
    """
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()

    bom = "﻿" if text.startswith("﻿") else ""
    text = text[len(bom):]
    starts = [0, *(m.end() for m in re.finditer(r"\r\n|\r|\n", text))]

    for edit in sorted(edits, key=span, reverse=True):
        (row, col), (end_row, end_col) = span(edit)
        start, end = starts[row - 1] + col - 1, starts[end_row - 1] + end_col - 1
        text = text[:start] + (edit.get("content") or "") + text[end:]

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(bom + text)


def fix(branch, unsafe):
    """
    Apply Ruff's fixes to the new pyflakes issues until it has none left to apply. Each
    issue takes the fix Ruff gives nearest its line for its name, and no fix that would
    change the line of an issue the base has too.
    """
    python = [f for f in branch.files if f.endswith(".py")]

    if not python:  # flake8 given no files would check the whole tree
        print("No Python file changed: nothing for Ruff to fix")
        return

    in_base = branch.in_base(python)
    old = set(branch.base_tree.results(pyflakes, in_base)) if in_base else set()
    applied = 0

    for _ in range(FIX_PASSES):
        found = pyflakes(Tree("."), python)
        new = [i for i in found if i not in old]
        kept = {(i.path, i.line) for i in found if i in old}
        fixes = ruff_fixes(sorted({i.path for i in new}), unsafe) if new else {}
        chosen: dict[str, list] = {}

        for issue in new:
            candidates = fixes.get((issue.path, issue.rule.split()[1],
                                    subject(issue.message, issue.line)), [])

            if not candidates:
                continue

            nearest = min(candidates, key=lambda c: abs(c[0] - issue.line))
            candidates.remove(nearest)
            edits = nearest[1]
            touched = {(issue.path, r) for edit in edits for r in rows(edit)}

            if touched & kept or overlaps(chosen.get(issue.path, []), edits):
                continue

            chosen.setdefault(issue.path, []).extend(edits)
            applied += 1

        if not chosen:
            break

        for path, edits in chosen.items():
            apply_edits(path, edits)
    print(f"Applied {applied} fixes" if applied else "No new issue Ruff can fix")


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0].strip())
    parser.add_argument("command", choices=("check", "fix"))
    parser.add_argument("--base",
                        help="branch to compare with (origin/master, else master)")
    parser.add_argument("--all", action="store_true",
                        help="list pre-existing issues too")
    parser.add_argument("--unsafe", action="store_true",
                        help="also apply Ruff's unsafe fixes")
    args = parser.parse_args(argv)

    try:
        go_to_top()

        with tempfile.TemporaryDirectory(prefix="lint-base-") as workdir:
            branch = Branch.open(args.base, workdir)

            if not branch.files:
                print(f"No files changed since {branch.base[:10]} ({branch.ref})")
                return 0

            if args.command == "fix":
                fix(branch, args.unsafe)

            issues, new = compare(branch)
            report(branch, issues, new, args.all)
            return 1 if new else 0
    except (ToolError, OSError, ValueError, KeyError, tarfile.TarError) as e:
        # a tool that crashed or printed something else than its JSON included
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
