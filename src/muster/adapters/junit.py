"""JUnit XML reports: the start and the duration of each test suite.

A suite without a timestamp is skipped; a timestamp without a zone is UTC.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import read_xml
from muster.report import ReportError

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


def read_suites(root: Path, files: list[str]) -> list[Suite]:
    """Every <testsuite> with a timestamp, at any depth, of each repository-relative file, in file
    order and document order."""
    suites: list[Suite] = []
    for file in files:
        for element in read_xml(root / file, file).iter("testsuite"):
            stamp = element.get("timestamp")
            if stamp is None:
                continue
            name = element.get("name", "")
            try:
                start = parse_timestamp(stamp)
            except (ValueError, OverflowError):
                raise ReportError(f"{file}: suite {name!r} has a bad timestamp {stamp!r}") from None
            suite = Suite(name, file, start, _seconds(element.get("time"), file, name))
            try:
                suite.end
            except OverflowError:
                raise ReportError(f"{file}: suite {name!r} ends past the last date") from None
            suites.append(suite)
    return suites


def span(suites: list[Suite]) -> tuple[dt.datetime, dt.datetime, int] | None:
    """(earliest start, latest end, number of suites); None for no suites."""
    if not suites:
        return None
    return min(s.start for s in suites), max(s.end for s in suites), len(suites)
