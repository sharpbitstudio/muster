"""The JaCoCo XML report: per-line coverage, the LINE and BRANCH counters of each source file and
the sessions of the run.

A source file is named in the report by its package and its file name; the source resolver finds
it under the source roots. A file found under no root is unresolved, one found under a root but
outside the classification set is excluded, and one found under two or more roots is a ConfigError,
as are two names found as the same file. Every number is an integer that a Java long holds; a
session that ends before it starts is an input error.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import LONG_MIN, SourceResolver, label, long_number, read_xml
from muster.config import ConfigError
from muster.report import ReportError

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


def _attribute(element: ET.Element, key: str, low: int, name: str) -> int:
    """An integer attribute from `low` to LONG_MAX, by the rule of long_number()."""
    value = element.get(key)
    if value is None:
        raise ReportError(f"{name}: <{element.tag}> has no attribute {key}")
    try:
        return long_number(value, low)
    except ValueError as exc:
        raise ReportError(f"{name}: <{element.tag}> attribute {key} {exc}") from None


def _count(element: ET.Element, key: str, name: str) -> int:
    return _attribute(element, key, 0, name)


def _epoch(element: ET.Element, key: str, name: str) -> int:
    return _attribute(element, key, LONG_MIN, name)


def _report(path: Path, name: str | None) -> tuple[ET.Element, str]:
    name = label(path) if name is None else name
    root = read_xml(path, name)
    if root.tag != "report":
        raise ReportError(f"{name}: not a JaCoCo report, the root element is <{root.tag}>")
    return root, name


def _sessions(root: ET.Element, name: str) -> list[tuple[int, int]]:
    sessions = []
    for session in root.iter("sessioninfo"):
        start, dump = _epoch(session, "start", name), _epoch(session, "dump", name)
        if dump < start:
            raise ReportError(f"{name}: <sessioninfo> dump {dump} is before start {start}")
        sessions.append((start, dump))
    return sessions


def _unsafe(text: str) -> bool:
    return "\\" in text or "\0" in text or text.startswith("/")


def _package_name(package: ET.Element, name: str) -> str:
    """The checked name of a package; an empty name is the default package."""
    folder = package.get("name")
    if folder is None:
        raise ReportError(f"{name}: <package> has no attribute name")
    if _unsafe(folder) or (folder and any(seg in ("", ".", "..") for seg in folder.split("/"))):
        raise ReportError(f"{name}: bad package name {folder!r}")
    return folder


def _report_name(folder: str, sourcefile: ET.Element, name: str) -> str:
    """`package/file`, or the file alone in the default package, after the file name is checked."""
    file = sourcefile.get("name")
    if file is None:
        raise ReportError(f"{name}: <sourcefile> in package {folder!r} has no attribute name")
    if _unsafe(file) or "/" in file or file in ("", ".", ".."):
        raise ReportError(f"{name}: bad sourcefile name {file!r} in package {folder!r}")
    return f"{folder}/{file}" if folder else file


def _coverage(sourcefile: ET.Element, report_name: str, name: str) -> FileCoverage:
    """The lines and the counters of a source file; every counter is checked, whatever its type, and
    the LINE and BRANCH counters are kept."""
    counters: dict[str, tuple[int, int]] = {}
    for counter in sourcefile.findall("counter"):
        kind = counter.get("type")
        if kind is None:
            raise ReportError(f"{name}: {report_name} has a <counter> without attribute type")
        if not kind:
            raise ReportError(f"{name}: {report_name} has a <counter> with an empty type")
        if kind in counters:
            raise ReportError(f"{name}: {report_name} has two counters of type {kind!r}")
        counters[kind] = (_count(counter, "missed", name), _count(counter, "covered", name))
    lines: dict[int, tuple[int, int, int, int]] = {}
    for line in sourcefile.findall("line"):
        nr = _count(line, "nr", name)
        if nr in lines:
            raise ReportError(f"{name}: {report_name} has line {nr} twice")
        mi, ci, mb, cb = (_count(line, key, name) for key in _LINE_ATTRIBUTES)
        lines[nr] = (mi, ci, mb, cb)
    return FileCoverage(lines, counters.get("LINE", (0, 0)), counters.get("BRANCH", (0, 0)))


def _check_counters(root: ET.Element, name: str) -> None:
    """Every <counter> of the document, at any level (the report, a group, a package, a class, a
    method), has a type that is not empty, no type twice in one element, and valid counts; only
    those of the source files are kept."""
    for element in root.iter():
        kinds: set[str] = set()
        for counter in element.findall("counter"):
            kind = counter.get("type")
            if kind is None:
                raise ReportError(f"{name}: <{element.tag}> has a <counter> without attribute type")
            if not kind:
                raise ReportError(f"{name}: <{element.tag}> has a <counter> with an empty type")
            if kind in kinds:
                raise ReportError(f"{name}: <{element.tag}> has two counters of type {kind!r}")
            kinds.add(kind)
            _count(counter, "missed", name)
            _count(counter, "covered", name)


def _read(path: Path, name: str | None) -> tuple[list[tuple[int, int]], list[tuple[str, FileCoverage]], str]:
    """The sessions and every source file (report name, coverage) of a report, after the whole
    document is checked; the packages are found at any depth, so groups may nest them, and the
    counters of every level are checked. Also returns the name that messages give the file."""
    root, name = _report(path, name)
    sessions = _sessions(root, name)
    entries: list[tuple[str, FileCoverage]] = []
    seen: set[str] = set()
    for package in root.iter("package"):
        folder = _package_name(package, name)  # also for a package without source files
        for sourcefile in package.findall("sourcefile"):
            report_name = _report_name(folder, sourcefile, name)
            if report_name in seen:
                raise ReportError(f"{name}: source file {report_name} occurs twice")
            seen.add(report_name)
            entries.append((report_name, _coverage(sourcefile, report_name, name)))
    _check_counters(root, name)
    return sessions, entries, name


def read_sessions(path: Path, *, name: str | None = None) -> list[tuple[int, int]]:
    """The (start, dump) pairs of every <sessioninfo>, in epoch ms. The whole report is checked as
    load() checks it, but no source is resolved. Messages name the file `name`, by default the
    path."""
    return _read(path, name)[0]


def _resolve(
    entries: list[tuple[str, FileCoverage]], resolver: SourceResolver, name: str
) -> list[tuple[str, str | None, FileCoverage]]:
    """(report name, repository-relative path or None under no source root, coverage) of every
    source file, in report order. A report name found under two or more source roots, and two
    report names found as one file (source roots that nest), are a ConfigError naming every path."""
    resolved: list[tuple[str, str | None, FileCoverage]] = []
    found: dict[str, str] = {}  # repository-relative path -> the report name found as it
    for report_name, coverage in entries:
        paths = resolver.resolve(report_name)
        if len(paths) > 1:
            raise ConfigError(f"{name}: {report_name} is under more than one source root: {', '.join(paths)}")
        path = paths[0] if paths else None
        if path is not None:
            if path in found:
                raise ConfigError(f"{name}: {found[path]} and {report_name} are both {path} (the source roots nest)")
            found[path] = report_name
        resolved.append((report_name, path, coverage))
    return resolved


def load(path: Path, resolver: SourceResolver, *, name: str | None = None) -> CoverageReport:
    """Every source file of the report with its per-line data and its counters, and the sessions.
    A report name found under two or more source roots, and two report names found as one file
    (source roots that nest), are a ConfigError naming every path. Messages name the file `name`, by
    default the path."""
    sessions, entries, name = _read(path, name)
    files: dict[str, FileCoverage] = {}
    unresolved: list[str] = []
    excluded: list[str] = []
    for report_name, found, coverage in _resolve(entries, resolver, name):
        if found is None:
            unresolved.append(report_name)
        elif found in resolver.classified:
            files[found] = coverage
        else:
            excluded.append(found)
    return CoverageReport(files, sessions, sorted(unresolved), sorted(excluded), len(entries))


def source_files(
    path: Path, resolver: SourceResolver, *, name: str | None = None
) -> list[tuple[str, str | None, bool, FileCoverage]]:
    """Every source file of the report, in report order, as (report name, repository-relative path
    or None under no source root, whether the path is in the classification set, coverage), with no
    file left out. The whole report is checked as load() checks it, and a name is resolved and
    refused as load() does: the same two ConfigErrors. Messages name the file `name`, by default the
    path."""
    _, entries, name = _read(path, name)
    return [
        (report_name, found, found is not None and found in resolver.classified, coverage)
        for report_name, found, coverage in _resolve(entries, resolver, name)
    ]
