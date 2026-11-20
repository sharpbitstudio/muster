"""Report output: workflow annotations, the verdict state of a check and the Markdown summary.

Annotations use the GitHub workflow command syntax; the summary is capped in bytes so that it fits
the step summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SUMMARY_CAP_BYTES = 900 * 1024


class ReportError(Exception):
    """An input problem that is reported as a finding, not as a crash."""


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

    def annotate(self, level: str, title: str, message: str) -> None:
        self.annotations.append(f"::{level} title={escape_property(title)}::{escape_data(message)}")

    def violated(self, title: str, message: str) -> None:
        """A gate violation: a warning without --strict, an error (and exit 1) under --strict."""
        self.violation = True
        self.annotate("error" if self.strict else "warning", title, message)

    def errored(self, title: str, message: str) -> None:
        """The report's own error (bad input): a warning, an error (and exit 1) under --strict or --fail-on-error."""
        self.error = True
        self.violation = self.violation or self.strict
        self.annotate("error" if self.strict or self.fail_on_error else "warning", title, message)


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
