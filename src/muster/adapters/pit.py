"""The PIT XML report, `mutations.xml` of PIT 1.x: one <mutation> element per mutant.

A mutant names its source file by the package of its class and the file name; the source resolver
finds it under the source roots. The package is everything before the last dot of the class name,
so a nested class `Outer$Inner` belongs to the file of `Outer`. A name found under no root is
unresolved, one found under a single root is that file, inside or outside the classification set,
and one found under two or more roots is a ConfigError, as are two names found as the same file.
`detected` is read from its attribute, never derived from the status; a record whose two disagree
is an input error. The same mutant twice in a report is two mutants.
"""

from __future__ import annotations

import dataclasses
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import SourceResolver, label, long_number, read_xml
from muster.config import ConfigError
from muster.report import ReportError

# Every element that is read; each may occur once in a <mutation>, and each but <indexes> holds one
# value, without a child element. The <index> elements of <indexes> repeat and hold one value each;
# numberOfTestsRun, <blocks> and any other element are not read.
_SINGLE = (
    "sourceFile",
    "mutatedClass",
    "mutatedMethod",
    "methodDescription",
    "lineNumber",
    "mutator",
    "indexes",
    "killingTest",
    "description",
)
_STATUSES = {"KILLED": "killed", "SURVIVED": "survived", "NO_COVERAGE": "no-coverage"}
_DETECTED = {"true": True, "false": False}
_XML_SPACE = " \t\r\n"


@dataclass(frozen=True)
class Mutant:
    # (mutatedClass, mutatedMethod, methodDescription, lineNumber, full mutator name, indexes): an
    # opaque identity, to compare mutants across reports
    id: tuple[str, str, str, int, str, tuple[int, ...]]
    file: str | None  # repository-relative path of the source file; None when unresolved
    line: int
    mutator: str  # the simple name, after the last dot of the full name
    status_raw: str  # the status attribute as given; "?" when it is missing or empty
    status: str  # "killed", "survived", "no-coverage" or "other"
    detected: bool
    killing_tests: list[str]  # the <killingTest> text; [] when it is absent or empty
    description: str
    # "class", "method", "descriptor", "indexes", "mutator_full" and "report_name", the
    # `package/file` name the source is resolved by
    meta: dict[str, object]


@dataclass(frozen=True)
class MutationReport:
    mutants: list[Mutant]  # in the order of the report
    unresolved: list[str]  # report names with no file under any source root, sorted, each once


def _has_control(text: str) -> bool:
    """A control character: Unicode category Cc, which is C0, DEL and C1."""
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _bad_component(text: str) -> bool:
    """Whether `text` cannot be one component of a path: `.`, `..`, or a /, backslash, colon or
    control character in it."""
    return text in (".", "..") or any(ch in "/\\:" for ch in text) or _has_control(text)


def _single(mutation: ET.Element, tag: str, where: str) -> ET.Element | None:
    found = mutation.findall(tag)
    if len(found) > 1:
        raise ReportError(f"{where} has <{tag}> twice")
    return found[0] if found else None


def _scalar(element: ET.Element, tag: str, where: str) -> str:
    """The text of an element that holds one value, which may not have a child element."""
    if len(element):
        raise ReportError(f"{where}: <{tag}> has a child element")
    return element.text or ""


def _text(mutation: ET.Element, tag: str, where: str, *, required: bool = False) -> str:
    """The text of the element `tag`, "" when it is absent or empty; a required element must be
    there and not empty."""
    element = _single(mutation, tag, where)
    text = "" if element is None else _scalar(element, tag, where)
    if required and not text:
        raise ReportError(f"{where} has no <{tag}>" if element is None else f"{where} has an empty <{tag}>")
    return text


def _number(text: str, tag: str, where: str) -> int:
    """A line number or an index: the text without XML white space at its ends, from 0 to
    LONG_MAX, by the rule of long_number()."""
    try:
        return long_number(text.strip(_XML_SPACE), 0)
    except ValueError as exc:
        raise ReportError(f"{where}: <{tag}> {exc}") from None


def _no_control(text: str, what: str, where: str) -> None:
    if _has_control(text):
        raise ReportError(f"{where}: {what} has a control character")


def _report_name(cls: str, file: str, where: str) -> str:
    """`package/file`, the package being the class name before its last dot with / for the dots,
    or the file alone in the default package, after both are checked."""
    segments = cls.split(".")
    for segment in segments:
        if not segment:
            raise ReportError(f"{where}: <mutatedClass> {cls!r} has an empty segment")
        if _bad_component(segment):
            raise ReportError(f"{where}: <mutatedClass> {cls!r} has a bad segment {segment!r}")
    if _bad_component(file):
        raise ReportError(f"{where}: bad <sourceFile> {file!r}")
    return "/".join(segments[:-1] + [file])


def _mutant(mutation: ET.Element, where: str) -> Mutant:
    """The mutant of one <mutation>, with `file` not yet resolved."""
    for tag in _SINGLE:
        _single(mutation, tag, where)
    detected_raw = mutation.get("detected")
    if detected_raw is None:
        raise ReportError(f"{where} has no attribute detected")
    if detected_raw not in _DETECTED:
        raise ReportError(f"{where}: attribute detected must be true or false, got {detected_raw!r}")
    detected = _DETECTED[detected_raw]
    status_raw = mutation.get("status") or "?"
    _no_control(status_raw, "attribute status", where)
    status = _STATUSES.get(status_raw, "other")
    if (status == "killed" and not detected) or (status in ("survived", "no-coverage") and detected):
        raise ReportError(f"{where}: status {status_raw} contradicts detected={detected_raw}")
    cls = _text(mutation, "mutatedClass", where, required=True)
    file = _text(mutation, "sourceFile", where, required=True)
    report_name = _report_name(cls, file, where)
    method = _text(mutation, "mutatedMethod", where)
    _no_control(method, "<mutatedMethod>", where)
    descriptor = _text(mutation, "methodDescription", where)
    _no_control(descriptor, "<methodDescription>", where)
    line = _number(_text(mutation, "lineNumber", where, required=True), "lineNumber", where)
    mutator_full = _text(mutation, "mutator", where, required=True)
    _no_control(mutator_full, "<mutator>", where)
    mutator = mutator_full.rpartition(".")[2]
    if not mutator:
        raise ReportError(f"{where}: <mutator> {mutator_full!r} has no simple name")
    indexes_element = _single(mutation, "indexes", where)
    indexes = tuple(
        _number(_scalar(index, "index", where), "index", where)
        for index in ([] if indexes_element is None else indexes_element.findall("index"))
    )
    killing = _text(mutation, "killingTest", where)
    meta: dict[str, object] = {
        "class": cls,
        "method": method,
        "descriptor": descriptor,
        "indexes": indexes,
        "mutator_full": mutator_full,
        "report_name": report_name,
    }
    return Mutant(
        id=(cls, method, descriptor, line, mutator_full, indexes),
        file=None,
        line=line,
        mutator=mutator,
        status_raw=status_raw,
        status=status,
        detected=detected,
        killing_tests=[killing] if killing else [],
        description=_text(mutation, "description", where),
        meta=meta,
    )


def load(path: Path, resolver: SourceResolver, *, name: str | None = None) -> MutationReport:
    """Every mutant of the report, after the whole document is checked, with its source file
    resolved. A report name found under two or more source roots, and two report names found as one
    file (source roots that nest), are a ConfigError naming every path. Messages name the file
    `name`, by default the path, and a mutation by its position in the report, from 1."""
    name = label(path) if name is None else name
    root = read_xml(path, name)
    if root.tag != "mutations":
        raise ReportError(f"{name}: not a PIT report, the root element is <{root.tag}>")
    mutations = root.findall("mutation")
    if not mutations:
        raise ReportError(f"{name} has no mutations")
    mutants = [_mutant(mutation, f"{name}: mutation {nr}") for nr, mutation in enumerate(mutations, 1)]
    files: dict[str, str | None] = {}  # report name -> repository-relative path, None when unresolved
    found: dict[str, str] = {}  # repository-relative path -> the report name found as it
    for report_name in dict.fromkeys(str(mutant.meta["report_name"]) for mutant in mutants):
        paths = resolver.resolve(report_name)
        if len(paths) > 1:
            raise ConfigError(f"{name}: {report_name} is under more than one source root: {', '.join(paths)}")
        if not paths:
            files[report_name] = None
            continue
        file = paths[0]
        if file in found:
            raise ConfigError(f"{name}: {found[file]} and {report_name} are both {file} (the source roots nest)")
        found[file] = report_name
        files[report_name] = file
    unresolved = sorted(report_name for report_name, file in files.items() if file is None)
    return MutationReport(
        [dataclasses.replace(mutant, file=files[str(mutant.meta["report_name"])]) for mutant in mutants],
        unresolved,
    )
