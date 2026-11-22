"""Git helpers: running git, finding the repository and the merge base, reading git output, and
the changes since a base: the added lines of a diff, the untracked files and the line count of a
file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from muster.report import ReportError


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ReportError(f"git {' '.join(args[:3])} failed: {err[-1] if err else proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def find_repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True)
    if proc.returncode == 0:
        return Path(proc.stdout.decode("utf-8", "replace").strip())
    return Path.cwd().resolve()


def merge_base(repo: Path, base: str) -> str | None:
    """The merge base of `base` and HEAD, or None when git finds none. A ref that git could read as
    an option never reaches git: it raises ValueError."""
    problem = ref_problem(base)
    if problem:
        raise ValueError(f"base ref {base!r} {problem}")
    proc = subprocess.run(["git", "merge-base", base, "HEAD"], cwd=repo, capture_output=True)
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else None


def unquote_git_path(path: str) -> str:
    """Undo git's C-style quoting (kept for `"`, `\\` and control characters under quotePath=false)."""
    if not (len(path) >= 2 and path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    simple = {"n": b"\n", "t": b"\t", "r": b"\r", "a": b"\a", "b": b"\b", "f": b"\f", "v": b"\v"}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            octal = body[i + 1 : i + 4]
            if re.fullmatch(r"[0-7]{3}", octal):
                out.append(int(octal, 8))
                i += 4
                continue
            out += simple.get(body[i + 1], body[i + 1].encode("utf-8"))
            i += 2
            continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def records(text: str) -> list[str]:
    """The lines of `text` as git counts them: each ends at `\\n` (one trailing `\\r` is dropped), a
    last line without `\\n` is one more, and an empty text has none. str.splitlines() would also split
    on `\\f`, `\\v`, `\\x1c`-`\\x1e`, `\\x85`, U+2028, U+2029 and a lone `\\r` inside a line, and text-mode
    file iteration on a lone `\\r`.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # the terminator of the last line, not an empty line
    return [line[:-1] if line.endswith("\r") else line for line in lines]


DIFF_ARGS = (
    "-c", "core.quotePath=false", "diff", "-U0", "--inter-hunk-context=0", "--no-renames", "--no-ext-diff",
    "--no-color", "--no-textconv", "--text", "--src-prefix=a/", "--dst-prefix=b/",
)  # fmt: skip


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text: str, prefix: str) -> dict[str, set[int]]:
    """{path relative to `prefix`: new-side numbers of ADDED lines} from a unified diff.

    Only `+` lines count: context lines inside a hunk (git merges nearby hunks under a user
    `diff.interHunkContext`, even with -U0) are unchanged. Inside a hunk the old/new line counts of
    its header decide where it ends, so an added `++ x` (patch line `+++ x`) or a removed `-- x`
    is a hunk line, not a file header; a `diff --git ` line still starts the next file. Records
    come from `records()`, never from str.splitlines().
    """
    changed: dict[str, set[int]] = {}
    current = None
    old_left = new_left = 0
    new_nr = 0
    for line in records(text):
        if line.startswith("diff --git "):
            old_left = new_left = 0
        if old_left > 0 or new_left > 0:
            tag = line[:1]
            if tag == "+":
                if current is not None:
                    changed.setdefault(current, set()).add(new_nr)
                new_nr += 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            elif tag == " " or line == "":
                new_nr += 1
                old_left -= 1
                new_left -= 1
            # "\ No newline at end of file" changes no count
            continue
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            target = unquote_git_path(line[4:].rstrip("\t").rstrip())
            target = target[2:] if target.startswith("b/") else None  # /dev/null: deleted file
            current = target[len(prefix) :] if target and target.startswith(prefix) else None
        elif line.startswith("@@"):
            match = HUNK_RE.match(line)
            if match:
                old_left = 1 if match.group(2) is None else int(match.group(2))
                new_nr = int(match.group(3))
                new_left = 1 if match.group(4) is None else int(match.group(4))
    return changed


def untracked_files(repo: Path, pathspec: str) -> list[str]:
    out = git(repo, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z", "--", pathspec)
    return sorted(p for p in out.split("\0") if p)


def count_file_lines(path: Path) -> int:
    with open(path, "rb") as fh:
        return len(records(fh.read().decode("utf-8", "replace")))


def ref_problem(ref: str) -> str | None:
    """What makes `ref` unfit to pass to git as a revision, or None: an empty ref, one that git
    would read as an option, or one with a NUL that no command line can carry."""
    if not ref:
        return "is empty"
    if ref.startswith("-"):
        return "starts with -"
    if "\0" in ref:
        return "has a NUL"
    return None


def diff_since(repo: Path, commit: str, folders: list[str]) -> str:
    """The diff from `commit` to the work tree under `folders`, run in `repo`. `repo` may be a
    subfolder of the work tree: `--relative` makes the paths start at `repo`, as the folders do.
    Every folder is a literal pathspec, so glob characters in its name match only themselves."""
    pathspecs = [":(literal)" + folder for folder in folders]
    try:
        return git(repo, *DIFF_ARGS, "--relative", commit, "--", *pathspecs)
    except OSError as exc:
        raise ReportError(f"git diff could not run: {exc.strerror or exc}") from exc


def untracked_under(repo: Path, folders: list[str]) -> list[str]:
    """The untracked files under `folders` that no ignore rule covers, relative to `repo`, sorted.
    Every folder is a literal pathspec."""
    found: set[str] = set()
    try:
        for folder in folders:
            found.update(untracked_files(repo, ":(literal)" + folder))
    except OSError as exc:
        raise ReportError(f"git ls-files could not run: {exc.strerror or exc}") from exc
    return sorted(found)
