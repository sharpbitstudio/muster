"""Report output: workflow annotations, the verdict state of a check, the Markdown summary and the
JSON envelope.

Annotations use the GitHub workflow command syntax; the summary is capped in bytes so that it fits
the step summary. The envelope has the same shape for every command and every exit code.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from muster import __version__

SUMMARY_CAP_BYTES = 900 * 1024
ENVELOPE_SCHEMA_VERSION = 1
# Worst last: the top-level verdict of several sections is the one with the highest rank.
VERDICT_RANK = {"pass": 0, "not-evaluated": 1, "warn": 2, "fail": 3, "error": 4}


class ReportError(Exception):
    """An input problem that is reported as a finding, not as a crash."""


class FatalError(Exception):
    """A usage, configuration or policy error: exit 2 whatever the strict mode. `kind` names it in
    the envelope."""

    kind = "usage"


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


@dataclass
class Report:
    name: str
    strict: bool = False
    md: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    verdict: str = ""
    violation: bool = False
    fail_on_error: bool = False
    error: bool = False
    data: dict = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    evaluated: bool = True  # False when the section could not judge (a missing base, no measurement)
    warned: bool = False  # an annotation of level warning or error was made

    def annotate(self, level: str, title: str, message: str) -> None:
        self.warned = self.warned or level in ("warning", "error")
        self.annotations.append(f"::{level} title={escape_property(title)}::{escape_data(message)}")

    def violated(self, title: str, message: str) -> None:
        """A gate violation: a warning without --strict, an error (and exit 1) under --strict."""
        self.violation = True
        self.violations.append(message)
        self.annotate("error" if self.strict else "warning", title, message)

    def errored(self, title: str, message: str) -> None:
        """The report's own error (bad input): a warning, an error (and exit 1) under --strict or --fail-on-error."""
        self.error = True
        self.errors.append(message)
        self.violation = self.violation or self.strict
        self.annotate("error" if self.strict or self.fail_on_error else "warning", title, message)

    def section_verdict(self, exit_code: int = 0) -> str:
        """The section's verdict from its state, never from its text. The first rule that holds wins:
        errors or exit 2/3, not evaluated, a strict violation, a violation or a warning, else pass."""
        if self.errors or exit_code in (2, 3):
            return "error"
        if not self.evaluated:
            return "not-evaluated"
        if self.violation and self.strict:
            return "fail"
        if self.violation or self.warned:
            return "warn"
        return "pass"

    def section(self, exit_code: int = 0) -> dict:
        """The section as the envelope carries it."""
        return {
            "name": self.name,
            "strict": self.strict,
            "violation": self.violation,
            "verdict": self.section_verdict(exit_code),
            "verdict_text": self.verdict,
            "violations": list(self.violations),
            "errors": list(self.errors),
            "data": self.data,
        }


def one_line(text: str) -> str:
    """Collapse whitespace runs (incl. newlines) to one space: the verdict stays one stdout line."""
    return " ".join(text.split())


def cap_markdown(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    notice = "\n\n_Report truncated: the step summary is capped at ~900 KiB._\n"
    keep = max(0, budget - len(notice.encode("utf-8")))
    cut = raw[:keep].decode("utf-8", "ignore")
    cut = cut[: cut.rfind("\n") + 1] if "\n" in cut else ""
    return cut + notice


def write_markdown(path: Path, text: str, append: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    used = path.stat().st_size if append and path.exists() else 0
    with open(path, "a" if append else "w", encoding="utf-8", newline="\n") as fh:
        fh.write(cap_markdown(text, max(0, SUMMARY_CAP_BYTES - used)))


def md_path(text: str) -> str:
    """An inline code span (a path, a class or a mutant description); a backtick becomes a quote."""
    return "`" + text.replace("`", "'") + "`"


def percent_digits(fraction: float) -> str:
    """A threshold as the number of a percentage, exactly as the profile gives it: 0.8 is 80, 0.825
    is 82.5 and 0.800000000001 is 80.0000000001. The decimal point of the value's shortest form is
    moved, so no float error of a product shows and no digit is rounded away; -0.0 is 0."""
    digits = (Decimal(repr(fraction)) * 100).normalize()
    return f"{digits.copy_abs() if digits.is_zero() else digits:f}"


def envelope(
    command: str | None,
    *,
    exit_code: int,
    commit: str | None = None,
    sections: Sequence[Report] = (),
    error: tuple[str, str] | None = None,
    base: dict | None = None,
) -> dict:
    """The JSON envelope of one run. `error` is (kind, message) on exit 2 or 3; `base` is the
    {"ref", "commit"} of --base, or None without it."""
    items = [rep.section(exit_code) for rep in sections]
    if exit_code in (2, 3) or error is not None:
        verdict = "error"
    else:
        verdict = max((item["verdict"] for item in items), key=VERDICT_RANK.__getitem__, default="pass")
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "tool": {"name": "muster", "version": __version__},
        "command": command,
        "commit": commit,
        "base": base,
        "verdict": verdict,
        "exit_code": exit_code,
        "error": None if error is None else {"kind": error[0], "message": error[1]},
        "sections": items,
        "flags": [],
    }


def json_text(document: dict) -> str:
    """Indented ASCII JSON with a final line feed. A NaN or an infinity is a ValueError: JSON has no
    such numbers."""
    return json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def write_json(path: Path, text: str) -> None:
    """Replace the file with `text`, UTF-8 with LF line ends; missing parent folders are created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
