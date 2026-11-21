"""The JaCoCo XML report: per-line coverage, the LINE and BRANCH counters of each source file and
the sessions of the run.

A source file is named in the report by its package and its file name; the source resolver finds
it under the source roots. A file found under no root is unresolved, one found under a root but
outside the classification set is excluded, and one found under two or more roots is a ConfigError.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import SourceResolver, label, read_xml
from muster.config import ConfigError
from muster.report import ReportError

_COUNT_RE = re.compile(r"[0-9]+")
_EPOCH_RE = re.compile(r"-?[0-9]+")
_COUNTERS = ("LINE", "BRANCH")
_LINE_ATTRIBUTES = ("mi", "ci", "mb", "cb")


@dataclass(frozen=True)
class FileCoverage:
    # line nr -> (missed instructions, covered instructions, missed branches, covered branches)
    lines: dict[int, tuple[int, int, int, int]]
    line: tuple[int, int]  # LINE counter of the sourcefile element (missed, covered); (0, 0) when absent
    branch: tuple[int, int]  # BRANCH counter, same


@dataclass(frozen=True)
class CoverageReport:
    files: dict[str, FileCoverage]  # measured files, by repository-relative path
    sessions: list[tuple[int, int]]  # (start, dump) in epoch ms, one per <sessioninfo>
    unresolved: list[str]  # report names with no file under any source root, sorted
    excluded: list[str]  # repository-relative paths under a root but outside the classification set, sorted
    sourcefiles: int  # number of <sourcefile> elements in the report


def _attribute(element: ET.Element, key: str, pattern: re.Pattern[str], what: str, name: str) -> int:
    value = element.get(key)
    if value is None:
        raise ReportError(f"{name}: <{element.tag}> has no attribute {key}")
    if not pattern.fullmatch(value):
        raise ReportError(f"{name}: <{element.tag}> attribute {key} must be {what}, got {value!r}")
    return int(value)


def _count(element: ET.Element, key: str, name: str) -> int:
    return _attribute(element, key, _COUNT_RE, "a non-negative integer", name)


def _report(path: Path, name: str | None) -> tuple[ET.Element, str]:
    name = label(path) if name is None else name
    root = read_xml(path, name)
    if root.tag != "report":
        raise ReportError(f"{name}: not a JaCoCo report, the root element is <{root.tag}>")
    return root, name


def _sessions(root: ET.Element, name: str) -> list[tuple[int, int]]:
    return [
        (
            _attribute(session, "start", _EPOCH_RE, "an integer", name),
            _attribute(session, "dump", _EPOCH_RE, "an integer", name),
        )
        for session in root.iter("sessioninfo")
    ]


def read_sessions(path: Path, *, name: str | None = None) -> list[tuple[int, int]]:
    """The (start, dump) pairs of every <sessioninfo>, in epoch ms, without resolving any source.
    Messages name the file `name`, by default the path."""
    root, name = _report(path, name)
    return _sessions(root, name)


def _unsafe(text: str) -> bool:
    return "\\" in text or "\0" in text or text.startswith("/")


def _report_name(package: ET.Element, sourcefile: ET.Element, name: str) -> str:
    """`package/file`, or the file alone in the default package, after each part is checked."""
    folder = package.get("name")
    if folder is None:
        raise ReportError(f"{name}: <package> has no attribute name")
    if _unsafe(folder) or (folder and any(seg in ("", ".", "..") for seg in folder.split("/"))):
        raise ReportError(f"{name}: bad package name {folder!r}")
    file = sourcefile.get("name")
    if file is None:
        raise ReportError(f"{name}: <sourcefile> in package {folder!r} has no attribute name")
    if _unsafe(file) or "/" in file or file in ("", ".", ".."):
        raise ReportError(f"{name}: bad sourcefile name {file!r} in package {folder!r}")
    return f"{folder}/{file}" if folder else file


def _coverage(sourcefile: ET.Element, report_name: str, name: str) -> FileCoverage:
    counters: dict[str, tuple[int, int]] = {}
    for counter in sourcefile.findall("counter"):
        kind = counter.get("type")
        if kind not in _COUNTERS:
            continue
        if kind in counters:
            raise ReportError(f"{name}: {report_name} has two {kind} counters")
        counters[kind] = (_count(counter, "missed", name), _count(counter, "covered", name))
    lines: dict[int, tuple[int, int, int, int]] = {}
    for line in sourcefile.findall("line"):
        nr = _count(line, "nr", name)
        if nr in lines:
            raise ReportError(f"{name}: {report_name} has line {nr} twice")
        mi, ci, mb, cb = (_count(line, key, name) for key in _LINE_ATTRIBUTES)
        lines[nr] = (mi, ci, mb, cb)
    return FileCoverage(lines, counters.get("LINE", (0, 0)), counters.get("BRANCH", (0, 0)))


def load(path: Path, resolver: SourceResolver, *, name: str | None = None) -> CoverageReport:
    """Every source file of the report with its per-line data and its counters, and the sessions.
    The packages are found at any depth, so groups may nest them. A report name found under two or
    more source roots is a ConfigError naming every path. Messages name the file `name`, by default
    the path."""
    root, name = _report(path, name)
    sessions = _sessions(root, name)
    files: dict[str, FileCoverage] = {}
    seen: set[str] = set()
    unresolved: list[str] = []
    excluded: list[str] = []
    sourcefiles = 0
    for package in root.iter("package"):
        for sourcefile in package.findall("sourcefile"):
            sourcefiles += 1
            report_name = _report_name(package, sourcefile, name)
            if report_name in seen:
                raise ReportError(f"{name}: source file {report_name} occurs twice")
            seen.add(report_name)
            coverage = _coverage(sourcefile, report_name, name)
            paths = resolver.resolve(report_name)
            if len(paths) > 1:
                raise ConfigError(f"{name}: {report_name} is under more than one source root: {', '.join(paths)}")
            if not paths:
                unresolved.append(report_name)
            elif paths[0] in resolver.classified:
                files[paths[0]] = coverage
            else:
                excluded.append(paths[0])
    return CoverageReport(files, sessions, sorted(unresolved), sorted(excluded), sourcefiles)
