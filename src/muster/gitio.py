"""Git helpers: running git, finding the repository and the merge base, and reading git output."""

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
