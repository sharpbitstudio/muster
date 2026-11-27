"""JUnit XML reports: the start and the duration of each test suite, the window check of the
times of a run, and the test cases under a folder of reports.

The root element is <testsuite> or <testsuites>. A suite without a timestamp is skipped; a
timestamp without a zone is UTC.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import posixpath
import re
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import read_xml
from muster.paths import is_link
from muster.report import ReportError

TOLERANCE_MS = 1000  # clock rounding between the build, the JUnit XML and the coverage agent
_ROOTS = ("testsuite", "testsuites")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_MICROSECOND = dt.timedelta(microseconds=1)

TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?")

# A finite decimal number >= 0 in ASCII digits: `0`, `1.5`, `.5`, `2e-3`; no sign, no separators.
_SECONDS_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def parse_timestamp(text: str) -> dt.datetime:
    """ISO-8601 with or without a fraction and with or without `Z`/offset; naive means UTC."""
    match = TS_RE.fullmatch(text.strip())
    if not match:
        raise ValueError(f"bad timestamp '{text}'")
    date, hh, mm, ss, fraction, zone = match.groups()
    value = dt.datetime.fromisoformat(f"{date}T{hh}:{mm}:{ss}")
    if fraction:
        value += dt.timedelta(microseconds=round(float(fraction) * 1_000_000))
    tz = dt.timezone.utc
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        tz = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    return value.replace(tzinfo=tz)


@dataclass(frozen=True)
class Suite:
    name: str
    file: str  # the repository-relative report file
    start: dt.datetime
    seconds: float

    @property
    def end(self) -> dt.datetime:
        return self.start + dt.timedelta(seconds=self.seconds)


def _seconds(text: str | None, file: str, suite: str) -> float:
    if text is None:
        return 0.0
    value = float(text) if _SECONDS_RE.fullmatch(text) else math.nan
    if not math.isfinite(value):
        raise ReportError(f"{file}: suite {suite!r} has a bad time {text!r}")
    return value


def _file_suites(root: Path, file: str) -> list[Suite]:
    if not os.path.isfile(root / file):  # a literal path of tests.results may name a folder
        raise ReportError(f"{file}: not a file")
    document = read_xml(root / file, file)
    if document.tag not in _ROOTS:
        raise ReportError(f"{file}: not a JUnit report, the root element is <{document.tag}>")
    suites: list[Suite] = []
    for element in document.iter("testsuite"):
        stamp = element.get("timestamp")
        if stamp is None:
            continue
        name = element.get("name", "")
        try:  # in UTC, so that an end past the last date is caught below and no later step overflows
            start = parse_timestamp(stamp).astimezone(dt.timezone.utc)
        except (ValueError, OverflowError):
            raise ReportError(f"{file}: suite {name!r} has a bad timestamp {stamp!r}") from None
        suite = Suite(name, file, start, _seconds(element.get("time"), file, name))
        try:
            suite.end
        except OverflowError:
            raise ReportError(f"{file}: suite {name!r} ends past the last date") from None
        suites.append(suite)
    return suites


def read_suites(root: Path, files: list[str], errors: list[str] | None = None) -> list[Suite]:
    """Every <testsuite> with a timestamp, at any depth, of each repository-relative file, in file
    order and document order. Without `errors` the first file that cannot be read raises its
    ReportError; with it, each such file adds its message there and the other files are still read."""
    suites: list[Suite] = []
    for file in files:
        try:
            suites += _file_suites(root, file)
        except ReportError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
    return suites


def xml_files(folder: str, errors: list[str]) -> list[str]:
    """Every file under `folder`, a folder named on the command line, whose name ends in .xml in
    exactly that case, at any depth, sorted by its path relative to the folder; each is named
    `<folder>/<relative path>` (on Windows a bare drive such as `D:` gets no `/`), a path that opens
    it from the working directory too. A linked folder (a symbolic link or a junction) below it is
    not followed; a folder that cannot be read, and an entry ending in .xml that cannot be looked
    at, add their message to `errors` once the walk is done, folders and files together sorted by
    the relative path, and the walk goes on."""
    separators = tuple({os.sep, os.altsep or os.sep})
    drive, tail = os.path.splitdrive(folder)
    bare_drive = drive.endswith(":") and not tail
    prefix = folder if folder.endswith(separators) or bare_drive else folder + "/"
    found: list[str] = []
    problems: list[tuple[str, str]] = []  # (relative path, message)

    def unreadable(exc: OSError) -> None:
        try:
            rel = Path(exc.filename).relative_to(folder).as_posix()
        except (TypeError, ValueError):  # no file name, or one outside the folder
            rel = "."
        problems.append((rel, f"{folder if rel == '.' else prefix + rel}: cannot read: {exc.strerror or exc}"))

    for dirpath, dirnames, filenames in os.walk(folder, onerror=unreadable, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not is_link(os.path.join(dirpath, name)))
        here = Path(dirpath).relative_to(folder).as_posix()
        for name in sorted(filenames):
            if not name.endswith(".xml"):
                continue
            rel = name if here == "." else posixpath.join(here, name)
            try:
                mode = os.stat(os.path.join(dirpath, name)).st_mode
            except OSError as exc:
                problems.append((rel, f"{prefix + rel}: cannot read: {exc.strerror or exc}"))
                continue
            if stat.S_ISREG(mode):
                found.append(rel)
    errors.extend(message for _, message in sorted(problems, key=lambda problem: problem[0]))
    return [prefix + rel for rel in sorted(found)]


def _testcases(file: str) -> Counter[tuple[str, str, bool]]:
    document = read_xml(Path(file), file)
    if document.tag not in _ROOTS:
        raise ReportError(f"{file}: not a JUnit report, the root element is <{document.tag}>")
    found: Counter[tuple[str, str, bool]] = Counter()
    for case in document.iter("testcase"):
        name = case.get("name")
        if name is None:
            raise ReportError(f"{file}: <testcase> has no attribute name")
        if not name:
            raise ReportError(f"{file}: <testcase> has an empty name")
        found[case.get("classname", ""), name, case.find("skipped") is not None] += 1
    return found


def testcases(files: list[str], errors: list[str] | None = None) -> Counter[tuple[str, str, bool]]:
    """How often each test case, (classname, name, skipped), occurs in the files: every <testcase>
    at any depth, failed and errored ones too; it is skipped when it has a direct <skipped> child. A
    missing classname is empty, and a missing or empty name is an input error. Each file is a path
    from the working directory, as xml_files() names it, and messages name it so. Without `errors`
    the first file that cannot be read raises its ReportError; with it, each such file adds its
    message there and the other files are still read."""
    found: Counter[tuple[str, str, bool]] = Counter()
    for file in files:
        try:
            found += _testcases(file)
        except ReportError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
    return found


def identities(files: list[str], errors: list[str] | None = None) -> Counter[str]:
    """How often each test case identity, `<classname>#<name>`, occurs in the files: the test cases
    of testcases(), skipped ones too, each file read once."""
    found: Counter[str] = Counter()
    for (classname, name, _), count in testcases(files, errors).items():
        found[f"{classname}#{name}"] += count
    return found


def span(suites: list[Suite]) -> tuple[dt.datetime, dt.datetime, int] | None:
    """(earliest start, latest end, number of suites); None for no suites."""
    if not suites:
        return None
    return min(s.start for s in suites), max(s.end for s in suites), len(suites)


def micros(moment: dt.datetime) -> int:
    """The moment in whole microseconds since the epoch, exactly."""
    return (moment - _EPOCH) // _MICROSECOND


def within(start: dt.datetime, end: dt.datetime, first_ms: int, last_ms: int) -> bool:
    """Whether [start, end] lies within [first_ms, last_ms] (epoch ms), give or take TOLERANCE_MS.
    The times are compared as exact integers, in microseconds: a float would round them far from the
    epoch."""
    return (first_ms - TOLERANCE_MS) * 1000 <= micros(start) and micros(end) <= (last_ms + TOLERANCE_MS) * 1000
