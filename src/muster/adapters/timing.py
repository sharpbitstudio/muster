"""The timing record a build writes for its test task: a JSON object with the task, the start and
the end in epoch ms, the outcome and the failure. The start and the end are moments a datetime
holds, from the epoch to the end of the year 9999, whatever the outcome.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from muster.adapters import label
from muster.report import ReportError

EPOCH_MS_MAX = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z, the last millisecond a datetime holds


@dataclass(frozen=True)
class TimingRecord:
    task: str | None
    start_ms: int
    end_ms: int
    outcome: str | None
    failure: str | None

    def data(self) -> dict:
        """The record's five fields, never the raw document."""
        return {
            "task": self.task,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "outcome": self.outcome,
            "failure": self.failure,
        }


_JSON_TYPES = {
    bool: "a boolean",
    int: "an integer",
    float: "a number",
    str: "a string",
    list: "an array",
    dict: "an object",
}


def _kind(value) -> str:
    return "null" if value is None else _JSON_TYPES.get(type(value), type(value).__name__)


def _no_constant(text: str):
    raise ValueError(f"{text} is not a JSON number")


def _finite(text: str) -> float:
    """A JSON number with a fraction or an exponent; one too large for a float, such as 1e400, would
    be an infinity."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is not a finite number")
    return value


def _epoch_ms(document: dict, key: str, name: str) -> int:
    if key not in document:
        raise ReportError(f"{name}: missing key {key}")
    value = document[key]
    if type(value) is not int:
        raise ReportError(f"{name}: {key} must be an integer, got {_kind(value)}")
    if not 0 <= value <= EPOCH_MS_MAX:
        raise ReportError(f"{name}: {key} must be from 0 to {EPOCH_MS_MAX} (epoch ms)")
    return value


def _text(document: dict, key: str, name: str) -> str | None:
    value = document.get(key)
    if value is not None and not isinstance(value, str):
        raise ReportError(f"{name}: {key} must be a string or null, got {_kind(value)}")
    return value


def load(path: Path, task: str | None = None, *, name: str | None = None) -> TimingRecord:
    """The record of the file. Extra keys are ignored, but not a NaN or an infinity in one. Given
    `task`, a record of another task, or of no task, is a ReportError. Messages name the file
    `name`, by default the path."""
    name = label(path) if name is None else name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"{name}: cannot read: {exc.strerror or exc}") from None
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_no_constant, parse_float=_finite)
    except UnicodeDecodeError as exc:
        raise ReportError(f"{name}: not valid UTF-8 (byte {exc.start})") from None
    except RecursionError:
        raise ReportError(f"{name}: not valid JSON: nested too deeply") from None
    except ValueError as exc:
        raise ReportError(f"{name}: not valid JSON: {exc}") from None
    if not isinstance(document, dict):
        raise ReportError(f"{name}: the top level must be a JSON object")
    start_ms = _epoch_ms(document, "start_ms", name)
    end_ms = _epoch_ms(document, "end_ms", name)
    if end_ms < start_ms:
        raise ReportError(f"{name}: end_ms {end_ms} is before start_ms {start_ms}")
    record = TimingRecord(
        _text(document, "task", name),
        start_ms,
        end_ms,
        _text(document, "outcome", name),
        _text(document, "failure", name),
    )
    if task is not None and record.task != task:
        found = "no task" if record.task is None else f"task {record.task!r}"
        raise ReportError(f"{name}: a record of {found}, expected task {task!r}")
    return record
