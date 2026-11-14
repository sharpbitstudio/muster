#!/usr/bin/env python3
"""Hygiene check for a public repository.

Without arguments it scans the working tree of the repository that contains
the current directory: every tracked file plus every untracked file that is
not ignored, so new files are covered before they are staged. With
``--range BASE..HEAD`` it also checks each commit of that range: the message
and every file the commit adds or changes.

Checks:

- absolute home paths (Windows user profiles, Linux and macOS home folders,
  WSL drive mounts);
- email addresses other than GitHub noreply addresses and ``example.*``
  domains;
- non-ASCII characters outside ``NON_ASCII_ALLOW``;
- binary files;
- a ``.gitleaksignore`` file anywhere;
- the inline gitleaks suppression marker;
- ``.gitleaks.toml`` and ``.gitattributes`` byte-identical to their canonical
  content (compared by SHA-256);
- in commit messages of the range: any co-author trailer and any
  "generated with" line.

Each finding is one line, ``path:line: [category] detail``. Line 0 means the
whole file. Matched text is never printed, so a public CI log does not repeat
what it reports.

Exit codes: 0 clean, 1 findings, 2 usage or git error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys

MIN_PYTHON = (3, 11)

# SHA-256 of the canonical bytes of the pinned files:
#   .gitattributes = b"* text=auto eol=lf\n"
#   .gitleaks.toml = b"[extend]\nuseDefault = true\n"
PINNED = {
    ".gitattributes": "d60f352d0db1404c70afb4bb8b2ca3fd1c610572aa40720e8a0b7baa7885418c",
    ".gitleaks.toml": "27630a96d6c55755cc37620f3933d5cab94b1eb78a726a32e11212972525d76e",
}

# Non-ASCII characters allowed in file content, paths and commit messages.
# Empty on purpose: the repository is plain ASCII. Add a character only as an
# escape sequence, never as the literal character, with the reason in the
# commit message.
NON_ASCII_ALLOW: frozenset[str] = frozenset()

# The patterns are phrased so that this file does not match them itself.
HOME_PATH_RES = (
    re.compile(r"\b[A-Za-z]:[\\/]+Users\b", re.IGNORECASE),
    re.compile(r"/(?:home|Users)/[^\s/'\"<>]+"),
    re.compile(r"/mnt/[A-Za-z]/"),
)
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}(?![\w-])"
)
MARKER_RE = re.compile(re.escape("gitleaks" + ":allow"), re.IGNORECASE)
TRAILER_RE = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE)
GENERATED_RE = re.compile(r"generated\s+with", re.IGNORECASE)

NOREPLY_SUFFIX = "@users.noreply.github.com"
GITHUB_NOREPLY = "noreply" + "@github.com"

MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"


class GitError(Exception):
    pass


def git(top: str | None, *args: str, input: bytes | None = None) -> bytes:
    try:
        r = subprocess.run(["git", *args], cwd=top, input=input, capture_output=True)
    except OSError as e:
        raise GitError(f"cannot run git: {e}") from None
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}"
        raise GitError(f"git {args[0]}: {msg}")
    return r.stdout


def safe(text: str) -> str:
    """ASCII-only rendering of a path or name for the report."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def email_allowed(addr: str) -> bool:
    a = addr.lower()
    domain = a.rpartition("@")[2]
    return a.endswith(NOREPLY_SUFFIX) or a == GITHUB_NOREPLY or domain.startswith("example.")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, where: str, line: int, category: str, detail: str) -> None:
        self.lines.append(f"{safe(where)}:{line}: [{category}] {detail}")

    def text(self, text: str, where: str, *, message: bool = False) -> None:
        """Line checks on decoded text: file content or a commit message."""
        for n, line in enumerate(text.split("\n"), 1):
            for rx in HOME_PATH_RES:
                for m in rx.finditer(line):
                    self.add(where, n, "abs-path", f"absolute home path at column {m.start() + 1}")
            for m in EMAIL_RE.finditer(line):
                if not email_allowed(m.group(0)):
                    self.add(where, n, "email", f"email address not allowed at column {m.start() + 1}")
            bad = [(col, ch) for col, ch in enumerate(line, 1) if ord(ch) > 127 and ch not in NON_ASCII_ALLOW]
            if bad:
                col, ch = bad[0]
                more = f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""
                self.add(where, n, "non-ascii", f"U+{ord(ch):04X} at column {col}{more}")
            for m in MARKER_RE.finditer(line):
                self.add(where, n, "suppression-marker", f"inline gitleaks suppression marker at column {m.start() + 1}")
            if message:
                if TRAILER_RE.search(line):
                    self.add(where, n, "ai-attribution", "co-author trailer")
                if GENERATED_RE.search(line):
                    self.add(where, n, "ai-attribution", "'generated with' line")

    def path(self, path: str, where: str) -> None:
        """Checks on the path name itself."""
        base = path.rsplit("/", 1)[-1]
        if base.lower() == ".gitleaksignore":
            self.add(where, 0, "gitleaksignore", ".gitleaksignore file is not allowed")
        if any(ord(ch) > 127 and ch not in NON_ASCII_ALLOW for ch in path):
            self.add(where, 0, "non-ascii", "non-ASCII character in the path")

    def blob(self, path: str, data: bytes, where: str) -> None:
        """Checks on the bytes of one file."""
        base = path.rsplit("/", 1)[-1].lower()
        if base in PINNED:
            digest = hashlib.sha256(data).hexdigest()
            if digest != PINNED[base]:
                self.add(where, 0, "pin", f"{base} differs from the canonical content (sha256 {digest})")
        if b"\0" in data:
            self.add(where, 0, "binary", "binary file")
            return
        self.text(data.decode("utf-8", "replace"), where)


def split_z(raw: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def scan_tree(top: str, rep: Report) -> None:
    raw = git(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = sorted(set(split_z(raw)))
    for name in PINNED:
        if name not in paths:
            rep.add(name, 0, "pin", f"{name} is missing")
    for path in paths:
        rep.path(path, path)
        full = os.path.join(top, *path.split("/"))
        if os.path.islink(full):
            data = os.fsencode(os.readlink(full))
        elif os.path.isfile(full):
            with open(full, "rb") as f:
                data = f.read()
        else:
            continue  # deleted from the working tree, or a submodule folder
        rep.blob(path, data, path)


def resolve(top: str, rev: str) -> str:
    try:
        return git(top, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").decode().strip()
    except GitError:
        raise GitError(f"not a commit: {rev}") from None


def scan_range(top: str, spec: str, rep: Report) -> None:
    base_rev, sep, head_rev = spec.partition("..")
    if not sep or not base_rev or not head_rev or head_rev.startswith("."):
        raise ValueError(f"--range must be BASE..HEAD, got {spec!r}")
    base = resolve(top, base_rev)
    head = resolve(top, head_rev)
    # Parents before children, so a file is reported at the commit that added it.
    rows = git(top, "rev-list", "--topo-order", "--reverse", "--parents", f"{base}..{head}").decode().split("\n")
    seen: set[tuple[str, str]] = set()
    for row in filter(None, rows):
        sha, *parents = row.split()
        short = sha[:12]
        raw = git(top, "cat-file", "commit", sha)
        message = raw.partition(b"\n\n")[2]
        rep.text(message.decode("utf-8", "replace"), f"commit/{short}", message=True)
        if parents:
            diff = git(top, "diff-tree", "-r", "-z", "--no-renames", "--diff-filter=d", parents[0], sha)
        else:
            diff = git(top, "diff-tree", "-r", "-z", "--no-renames", "--diff-filter=d", "--root", sha)
        fields = split_z(diff)
        for meta, path in zip(fields[0::2], fields[1::2]):
            _, new_mode, _, blob, _ = meta.lstrip(":").split(" ")
            if (path, blob) in seen:
                continue
            seen.add((path, blob))
            where = f"{path}@{short}"
            rep.path(path, where)
            if new_mode == MODE_GITLINK:
                continue
            rep.blob(path, git(top, "cat-file", "blob", blob), where)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print("hygiene.py needs Python 3.11 or newer", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="hygiene.py", description="Public repository hygiene check.")
    ap.add_argument("--range", metavar="BASE..HEAD", help="also check the commits of this range")
    args = ap.parse_args(argv)
    rep = Report()
    try:
        top = git(None, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
        scan_tree(top, rep)
        if args.range is not None:
            scan_range(top, args.range, rep)
    except ValueError as e:
        print(f"hygiene.py: {e}", file=sys.stderr)
        return 2
    except GitError as e:
        print(f"hygiene.py: {safe(str(e))}", file=sys.stderr)
        return 2
    for line in rep.lines:
        print(line)
    if rep.lines:
        print(f"hygiene: {len(rep.lines)} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
