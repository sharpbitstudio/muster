"""Report adapters: each turns one report file into checked data.

Every problem of the input (a file that cannot be read or parsed, a wrong shape, a missing
attribute, a value of the wrong type or out of range) is a ReportError whose message names the file
as the caller passed it and the problem. Only a problem of the profile is a ConfigError. An adapter
does not decide what a missing file means: the caller checks with exists() that the file is there
first, and any OSError while reading it is a ReportError.
"""

from __future__ import annotations

import json
import math
import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from muster.paths import exists_exact, normalize, present_exact, walk
from muster.report import ReportError

# The range of a Java long, which JaCoCo and PIT write: counts and line numbers from 0, session
# times in epoch ms.
LONG_MIN, LONG_MAX = -(2**63), 2**63 - 1
_LONG_DIGITS = len(str(LONG_MAX))
_UNSIGNED_RE = re.compile(r"[0-9]+")
_SIGNED_RE = re.compile(r"-?[0-9]+")


def label(path: Path) -> str:
    """The file as a message names it when the caller gives no name: the path, with forward slashes."""
    return path.as_posix()


def exists(root: Path, name: str) -> bool:
    """Whether the input `name` (repository-relative) is there for an adapter to read: False when
    nothing is at that path in exactly that case, which the caller judges as missing; anything there
    that is not a file, such as a folder, and a folder on the way that cannot be read (which would
    hide the file) are a ReportError naming it."""
    try:
        if not present_exact(root, name, strict=True):
            return False
    except OSError as exc:
        raise ReportError(f"{name}: cannot read a folder on its path: {exc.strerror or exc}") from None
    if not exists_exact(root, name):
        raise ReportError(f"{name}: not a file")
    return True


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


def long_number(text: str, low: int) -> int:
    """The integer in `text`, from `low` to LONG_MAX: ASCII digits, with a leading minus only when
    `low` is negative. Any other text, and a number out of that range, is a ValueError whose
    message says what the text must be. The text is converted only once it is known to fit, as
    int() refuses a string of more than 4300 digits."""
    signed = low < 0
    if not (_SIGNED_RE if signed else _UNSIGNED_RE).fullmatch(text):
        raise ValueError(f"must be {'an integer' if signed else 'a non-negative integer'}, got {text!r}")
    digits = text.lstrip("-").lstrip("0") or "0"
    number = None if len(digits) > _LONG_DIGITS else -int(digits) if text.startswith("-") else int(digits)
    if number is None or not low <= number <= LONG_MAX:
        raise ValueError(f"must be from {low} to {LONG_MAX}")
    return number


def _not_a_number(text: str):
    raise ValueError(f"{text} is not a JSON number")


def _finite(text: str) -> float:
    """A JSON number with a fraction or an exponent; one too large for a float, such as 1e400, would
    be an infinity."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def strict_json(text: str):
    """The JSON document in `text`, with every number finite: NaN, Infinity and a number that
    overflows to an infinity are a ValueError, in any key. A document nested too deeply is a
    RecursionError."""
    return json.loads(text, parse_constant=_not_a_number, parse_float=_finite)


class SourceResolver:
    """Finds the file of a report name, such as `org/example/app/Main.java`, under the source roots.

    `files` is every file under every root, in exact case and without the include globs, as the
    walk of the policy check found them; without it the roots are walked here, once. `classified`
    is the classification set (include minus exclude minus generated).
    """

    def __init__(
        self, root: Path, roots: list[str], classified: frozenset[str], files: list[str] | None = None
    ) -> None:
        self.roots = list(dict.fromkeys(normalize(folder) for folder in roots))
        self.files = frozenset(walk(root, "source.roots", roots) if files is None else files)
        self.classified = classified

    def resolve(self, name: str) -> list[str]:
        """The repository-relative paths `<root>/<name>` that exist, over all roots, sorted."""
        found = {posixpath.join(folder, name) if folder else name for folder in self.roots}
        return sorted(path for path in found if path in self.files)
