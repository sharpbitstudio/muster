"""Report adapters: each turns one report file into checked data.

Every problem of the input (a file that cannot be read or parsed, a wrong shape, a missing
attribute, a value of the wrong type or out of range) is a ReportError whose message names the file
as the caller passed it and the problem. Only a problem of the profile is a ConfigError. An adapter
does not decide what a missing file means: the caller checks that the file exists first, and any
OSError while reading it is a ReportError.
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from pathlib import Path

from muster.paths import normalize, walk
from muster.report import ReportError


def label(path: Path) -> str:
    """The file as a message names it when the caller gives no name: the path, with forward slashes."""
    return path.as_posix()


def read_xml(path: Path, name: str) -> ET.Element:
    """The root element of an XML file; a file that cannot be read or is not well-formed XML is a
    ReportError."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"{name}: cannot read: {exc.strerror or exc}") from None
    try:
        return ET.fromstring(data)
    except (ET.ParseError, LookupError, RecursionError, ValueError) as exc:
        raise ReportError(f"{name}: not well-formed XML: {exc}") from None


class SourceResolver:
    """Finds the file of a report name, such as `org/example/app/Main.java`, under the source roots.

    The roots are walked once: every file under every root, in exact case and without the include
    globs. `classified` is the classification set (include minus exclude minus generated).
    """

    def __init__(self, root: Path, roots: list[str], classified: frozenset[str]) -> None:
        self.roots = list(dict.fromkeys(normalize(folder) for folder in roots))
        self.files = frozenset(walk(root, "source.roots", roots))
        self.classified = classified

    def resolve(self, name: str) -> list[str]:
        """The repository-relative paths `<root>/<name>` that exist, over all roots, sorted."""
        found = {posixpath.join(folder, name) if folder else name for folder in self.roots}
        return sorted(path for path in found if path in self.files)
