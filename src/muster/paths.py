"""The path model: every file is known by its repository-relative POSIX path.

The classification set is every file under [source].roots that matches an include glob and neither
an exclude nor a generated glob; the test set is every file under [source].tests that matches an
include glob and no generated glob. Both are found by walking the folders, so a comparison is
case-sensitive on every system, and a configured file or folder exists only when every part of its
path has exactly that name on disk.
"""

from __future__ import annotations

import os
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path

from muster.config import Config, ConfigError
from muster.globs import matches_any


def normalize(path: str) -> str:
    """A repository-relative path without `.` segments or a trailing slash; "" is the root."""
    norm = posixpath.normpath(path) if path else "."
    return "" if norm == "." else norm


def exists_exact(root: Path, path: str, *, folder: bool = False) -> bool:
    """Whether `path` (relative to `root`) is a file, or a folder, whose every part has exactly the
    configured name. Path.exists() alone would accept another case on Windows and macOS."""
    current = root
    for part in [part for part in normalize(path).split("/") if part]:
        try:
            names = os.listdir(current)
        except OSError:
            return False
        if part not in names:
            return False
        current = current / part
    return current.is_dir() if folder else current.is_file()


def is_link(path: str) -> bool:
    """Whether `path` is a symbolic link or a Windows directory junction."""
    if os.path.islink(path):
        return True
    if hasattr(os.path, "isjunction"):  # Python 3.12 and newer
        return os.path.isjunction(path)
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)  # only Windows has them
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def walk(root: Path, key: str, folders: list[str], errors: list[ConfigError] | None = None) -> list[str]:
    """The files under the folders (repository-relative), sorted and without duplicates. A folder
    that does not exist, or one that cannot be read, is a ConfigError naming the key; given a list
    `errors`, it goes there instead and the walk goes on. A configured folder that is a link is
    walked; a linked folder (a symbolic link or a junction) below it is not."""

    def fail(error: ConfigError) -> None:
        if errors is None:
            raise error
        errors.append(error)

    def unreadable(exc: OSError) -> None:
        fail(ConfigError(f"{key}: cannot read {exc.filename}: {exc.strerror or exc}"))

    found: set[str] = set()
    for i, folder in enumerate(folders):
        rel = normalize(folder)
        if not exists_exact(root, rel, folder=True):
            fail(ConfigError(f"{key}[{i}]: folder {folder} does not exist"))
            continue
        top = root / rel
        for dirpath, dirnames, filenames in os.walk(top, onerror=unreadable, followlinks=False):
            dirnames[:] = [name for name in dirnames if not is_link(os.path.join(dirpath, name))]
            here = Path(dirpath).relative_to(top).as_posix()
            prefix = rel if here == "." else posixpath.join(rel, here)
            for name in filenames:
                if os.path.isfile(os.path.join(dirpath, name)):
                    found.add(posixpath.join(prefix, name))
    return sorted(found)


@dataclass(frozen=True)
class SourceSets:
    sources: list[str]  # the classification set, sorted
    tests: frozenset[str]  # the test set
    error: ConfigError | None = None  # the first folder that was missing or unreadable


def source_sets(cfg: Config, *, defer: bool = False) -> SourceSets:
    """The classification set and the test set of the profile's [source] table. A missing or
    unreadable folder raises its ConfigError, or with `defer` is kept in `error` while the other
    folders are still walked."""
    errors: list[ConfigError] | None = [] if defer else None
    include = cfg.get("source.include", [])
    exclude = cfg.get("source.exclude", [])
    generated = cfg.get("source.generated", [])
    sources = [
        path
        for path in walk(cfg.root, "source.roots", cfg.get("source.roots", []), errors)
        if matches_any(include, path) and not matches_any(exclude, path) and not matches_any(generated, path)
    ]
    tests = frozenset(
        path
        for path in walk(cfg.root, "source.tests", cfg.get("source.tests", []), errors)
        if matches_any(include, path) and not matches_any(generated, path)
    )
    return SourceSets(sources, tests, errors[0] if errors else None)


def root_of(path: str, roots: list[str]) -> str | None:
    """The folder of `roots` that holds `path`; the deepest one when roots nest."""
    holding = [root for root in map(normalize, roots) if not root or path.startswith(root + "/")]
    return max(holding, key=len, default=None)
