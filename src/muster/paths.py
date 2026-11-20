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
from dataclasses import dataclass
from pathlib import Path

from muster.config import Config, ConfigError
from muster.globs import check_globs, matches_any


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


def walk(root: Path, key: str, folders: list[str]) -> list[str]:
    """The files under the folders (repository-relative), sorted and without duplicates. A folder
    that does not exist is a ConfigError naming the key. Symbolic links to folders are not followed."""
    def fail(exc: OSError) -> None:
        raise ConfigError(f"{key}: cannot read {exc.filename}: {exc.strerror or exc}")

    found: set[str] = set()
    for i, folder in enumerate(folders):
        rel = normalize(folder)
        if not exists_exact(root, rel, folder=True):
            raise ConfigError(f"{key}[{i}]: folder {folder} does not exist")
        top = root / rel
        for dirpath, _, filenames in os.walk(top, onerror=fail, followlinks=False):
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


def check_source_globs(cfg: Config) -> None:
    for key in ("include", "exclude", "generated"):
        check_globs(f"source.{key}", cfg.get(f"source.{key}", []))


def source_sets(cfg: Config) -> SourceSets:
    """The classification set and the test set of the profile's [source] table."""
    check_source_globs(cfg)
    include = cfg.get("source.include", [])
    exclude = cfg.get("source.exclude", [])
    generated = cfg.get("source.generated", [])
    sources = [
        path
        for path in walk(cfg.root, "source.roots", cfg.get("source.roots", []))
        if matches_any(include, path) and not matches_any(exclude, path) and not matches_any(generated, path)
    ]
    tests = frozenset(
        path
        for path in walk(cfg.root, "source.tests", cfg.get("source.tests", []))
        if matches_any(include, path) and not matches_any(generated, path)
    )
    return SourceSets(sources, tests)


def root_of(path: str, roots: list[str]) -> str | None:
    """The folder of `roots` that holds `path`; the deepest one when roots nest."""
    holding = [root for root in map(normalize, roots) if not root or path.startswith(root + "/")]
    return max(holding, key=len, default=None)
