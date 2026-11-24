"""The path model: every file is known by its repository-relative POSIX path.

The classification set is every file under [source].roots that matches an include glob and neither
an exclude nor a generated glob; the test set is every file under [source].tests that matches an
include glob and no generated glob. The LOC set is every file under [source].roots and
[source].tests that matches an include glob and neither a generated nor a [loc].exclude glob;
[source].exclude does not apply to it. The sets are found by walking the folders, so a comparison is
case-sensitive on every system, and a configured file or folder exists only when every part of its
path has exactly that name on disk.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import stat
from dataclasses import dataclass, field
from pathlib import Path

from muster.config import Config, ConfigError
from muster.globs import alternatives, matches, matches_any


def normalize(path: str) -> str:
    """A repository-relative path without `.` segments or a trailing slash; "" is the root."""
    norm = posixpath.normpath(path) if path else "."
    return "" if norm == "." else norm


def file_argument(text: str) -> str:
    """The argparse type of a file or a folder named on the command line: an empty name, or one with
    a NUL, is a usage error."""
    if not text:
        raise argparse.ArgumentTypeError("the file name is empty")
    if "\0" in text:
        raise argparse.ArgumentTypeError("the file name has a NUL")
    return text


def _exact(root: Path, path: str, strict: bool = False) -> Path | None:
    """`root / path` when every part of `path` has exactly that name on disk, else None. A folder
    that cannot be read hides what is in it; with `strict` its OSError is raised instead (a part
    that is missing or is no folder still gives None)."""
    current = root
    for part in [part for part in normalize(path).split("/") if part]:
        try:
            names = os.listdir(current)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            if strict:
                raise
            return None
        if part not in names:
            return None
        current = current / part
    return current


def exists_exact(root: Path, path: str, *, folder: bool = False) -> bool:
    """Whether `path` (relative to `root`) is a file, or a folder, whose every part has exactly the
    configured name. Path.exists() alone would accept another case on Windows and macOS. It never
    raises: a folder that cannot be read hides what is in it."""
    current = _exact(root, path)
    if current is None:
        return False
    return os.path.isdir(current) if folder else os.path.isfile(current)  # False on any OSError


def present_exact(root: Path, path: str, *, strict: bool = False) -> bool:
    """Whether anything, a file, a folder or another kind of entry, is at `path` (relative to
    `root`) with exactly the configured name of every part. It never raises; with `strict`, a
    folder on the way that cannot be read raises its OSError rather than hide the entry."""
    return _exact(root, path, strict) is not None


def _is_junction(path: str) -> bool:
    """os.path.isjunction() of Python 3.12 for 3.11: only a mount point is a junction, not any other
    reparse point (a cloud-synced folder, say). Only Windows has reparse tags."""
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if mount_point is None:
        return False
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) == mount_point
    except OSError:
        return False


def is_link(path: str) -> bool:
    """Whether `path` is a symbolic link or a Windows directory junction."""
    return os.path.islink(path) or getattr(os.path, "isjunction", _is_junction)(path)


def walk(
    root: Path, key: str, folders: list[str], errors: list[ConfigError] | None = None, *, skip: tuple[str, ...] = ()
) -> list[str]:
    """The files under the folders (repository-relative), sorted and without duplicates. A folder
    that does not exist, or one that cannot be read, is a ConfigError naming the key; given a list
    `errors`, it goes there instead and the walk goes on. A configured folder that is a link is
    walked; a linked folder (a symbolic link or a junction) below it is not, nor is a folder below
    it whose name is in `skip`. A message names a folder repository-relative."""

    def fail(error: ConfigError) -> None:
        if errors is None:
            raise error
        errors.append(error)

    def unreadable(exc: OSError) -> None:
        try:
            folder = Path(exc.filename).relative_to(root).as_posix()
        except (TypeError, ValueError):  # no file name, or one outside the root
            folder = exc.filename
        fail(ConfigError(f"{key}: cannot read {folder}: {exc.strerror or exc}"))

    found: set[str] = set()
    for i, folder in enumerate(folders):
        rel = normalize(folder)
        if not exists_exact(root, rel, folder=True):
            fail(ConfigError(f"{key}[{i}]: folder {folder} does not exist"))
            continue
        top = root / rel
        for dirpath, dirnames, filenames in os.walk(top, onerror=unreadable, followlinks=False):
            dirnames[:] = [
                name for name in dirnames if name not in skip and not is_link(os.path.join(dirpath, name))
            ]
            here = Path(dirpath).relative_to(top).as_posix()
            prefix = rel if here == "." else posixpath.join(rel, here)
            for name in filenames:
                if os.path.isfile(os.path.join(dirpath, name)):
                    found.add(posixpath.join(prefix, name))
    return sorted(found)


def resolve_glob(root: Path, glob: str) -> list[str]:
    """The files that match a glob of the profile (repository-relative), sorted and without
    duplicates. Each alternative is walked from its longest literal folder prefix, the leading
    folders without a `*`; an alternative without any `*` names one entry, which is listed when
    anything is there with exactly that name, a folder too, so that the reader refuses one that is
    not a file. The prefix must exist with exactly the names on disk and may be a link; a linked
    folder below it is not followed, nor is a folder named .git at any depth below it (the
    repository's own data holds no report). Matching is case-sensitive. A prefix that does not
    exist gives no files, and a folder that cannot be read hides its files: it never raises for the
    tree. The glob is one the profile check accepted (well-formed, relative, inside the
    repository)."""
    found: set[str] = set()
    for alternative in alternatives(glob):
        segments = alternative.split("/")
        if "*" not in alternative:
            if present_exact(root, alternative):
                found.add(normalize(alternative))
            continue
        prefix: list[str] = []
        for segment in segments[:-1]:
            if "*" in segment:
                break
            prefix.append(segment)
        start = "/".join(prefix)
        if not exists_exact(root, start, folder=True):
            continue
        hidden: list[ConfigError] = []  # a folder that cannot be read is skipped
        walked = walk(root, "glob", [start], hidden, skip=(".git",))
        found.update(path for path in walked if matches(alternative, path))
    return sorted(found)


@dataclass(frozen=True)
class SourceSets:
    sources: list[str]  # the classification set, sorted
    tests: frozenset[str]  # the test set
    error: ConfigError | None = None  # the first folder that was missing or unreadable
    walked_sources: list[str] = field(default_factory=list)  # every file under [source].roots, sorted
    walked_tests: list[str] = field(default_factory=list)  # every file under [source].tests, sorted


def source_sets(cfg: Config, *, defer: bool = False) -> SourceSets:
    """The classification set and the test set of the profile's [source] table, and every file
    walked for them, before any glob. A missing or unreadable folder raises its ConfigError, or with
    `defer` is kept in `error` while the other folders are still walked."""
    errors: list[ConfigError] | None = [] if defer else None
    include = cfg.get("source.include", [])
    exclude = cfg.get("source.exclude", [])
    generated = cfg.get("source.generated", [])
    walked_sources = walk(cfg.root, "source.roots", cfg.get("source.roots", []), errors)
    walked_tests = walk(cfg.root, "source.tests", cfg.get("source.tests", []), errors)
    sources = [
        path
        for path in walked_sources
        if matches_any(include, path) and not matches_any(exclude, path) and not matches_any(generated, path)
    ]
    tests = frozenset(path for path in walked_tests if matches_any(include, path) and not matches_any(generated, path))
    return SourceSets(sources, tests, errors[0] if errors else None, walked_sources, walked_tests)


def root_of(path: str, roots: list[str]) -> str | None:
    """The folder of `roots` that holds `path`; the deepest one when roots nest."""
    holding = [root for root in map(normalize, roots) if not root or path.startswith(root + "/")]
    return max(holding, key=len, default=None)


@dataclass(frozen=True)
class LocSet:
    files: list[str]  # the LOC set, sorted
    walked: list[str]  # every file under [source].roots and [source].tests, sorted
    error: ConfigError | None = None  # the first folder that was missing or unreadable


def _counted(cfg: Config, path: str) -> bool:
    """Whether a file under [source].roots or [source].tests is in the LOC set by the globs."""
    return (
        matches_any(cfg.get("source.include", []), path)
        and not matches_any(cfg.get("source.generated", []), path)
        and not matches_any(cfg.loc_exclude, path)
    )


def loc_set(cfg: Config, *, defer: bool = False) -> LocSet:
    """The LOC set of the profile and every file walked for it, before any glob. A missing or
    unreadable folder raises its ConfigError, or with `defer` is kept in `error` while the other
    folders are still walked."""
    errors: list[ConfigError] | None = [] if defer else None
    walked_roots = walk(cfg.root, "source.roots", cfg.get("source.roots", []), errors)
    walked_tests = walk(cfg.root, "source.tests", cfg.get("source.tests", []), errors)
    walked = sorted({*walked_roots, *walked_tests})
    return LocSet([path for path in walked if _counted(cfg, path)], walked, errors[0] if errors else None)


def in_loc_set(cfg: Config, path: str) -> bool:
    """Whether the repository-relative `path` belongs to the LOC set by its name alone: under a
    folder of [source].roots or [source].tests, and kept by the globs. The file need not exist, so
    a file that a change deleted is judged as well."""
    folders = [*cfg.get("source.roots", []), *cfg.get("source.tests", [])]
    return root_of(path, folders) is not None and _counted(cfg, path)


def group_of(cfg: Config, path: str) -> str | None:
    """The group of the repository-relative `path` for `loc`, by the deepest folder of
    [source].roots and [source].tests that holds it: "main" under a folder of roots; under a folder
    of tests "test-support" when a [loc].test_support glob matches the path, else "test"; None
    outside every folder. A folder listed in both lists counts as one of roots, so test_support
    applies only below a folder that is no more than a tests folder."""
    root = root_of(path, cfg.get("source.roots", []))
    tests = root_of(path, cfg.get("source.tests", []))
    if tests is None or (root is not None and len(root) >= len(tests)):
        return None if root is None else "main"
    return "test-support" if matches_any(cfg.loc_test_support, path) else "test"
