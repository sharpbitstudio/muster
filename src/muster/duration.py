"""The duration command: the wall time of the test run against a warning and an error threshold.

With duration.source = "task-json", the default, the time is the one of the timing record that the
build writes for its test task (duration.task_json). It is judged only when the task succeeded and
the record is from the run whose results are on disk: every JUnit suite of tests.results, and every
session of the coverage report when coverage.report names one that exists, lies within the start
and the end of the task, give or take a second. Otherwise the run is not measured (no record, or a
task that did not run), incomplete (the task failed) or has no current timing (the record is
stale). The span of the JUnit suites is shown beside the time.

With duration.source = "junit-span" the time is the span of the JUnit suites, from the first start
to the last end: a lower bound of the run, with no check that the files come from one run. A known
limitation: a JUnit file left over from an earlier run widens the span.

Above the warning threshold the section gets a warning, never a violation; above the error
threshold an error annotation, which is a violation only under --strict. A run that is not measured
is never a violation. A file that cannot be read, or a configured path that is there but is not a
file, is an input error, and the section then has no verdict.
"""

from __future__ import annotations

import argparse

from muster.adapters import exists, jacoco, junit, timing
from muster.adapters.timing import TimingRecord
from muster.config import Config, require
from muster.paths import resolve_glob
from muster.report import Report, ReportError, md_path, one_line

HELP = "test task duration against the budget"
SPAN_NOTE = "; JUnit span, a lower bound: a file left over from an earlier run widens it"


def fmt_seconds(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d} ({seconds:.1f} s)"


def threshold_text(seconds: float) -> str:
    """A threshold as the report names it: `> 4 min` for 240, `> 250 s` for 250, `> 0.5 s` for 0.5."""
    if seconds == int(seconds):
        seconds = int(seconds)
        if seconds % 60 == 0:
            return f"> {seconds // 60} min"
    return f"> {seconds} s"


def _span(suites: list[junit.Suite]) -> tuple[float, int] | None:
    """(seconds from the first start to the last end, number of suites); None for no suites."""
    bounds = junit.span(suites)
    if bounds is None:
        return None
    first, last, count = bounds
    return (last - first).total_seconds(), count


def _stale(record: TimingRecord, suites: list[junit.Suite], sessions: list[tuple[int, int]] | None) -> str | None:
    """Why a successful task record is not from the run whose results are on disk, or None: the
    JUnit suites, and the coverage sessions when there is a report, lie within the start and the end
    of the task, give or take junit.TOLERANCE_MS. The times are compared as exact integers."""
    if not suites:
        return "no JUnit XML for the recorded run"
    start, end, _ = junit.span(suites)
    if not junit.within(start, end, record.start_ms, record.end_ms):
        return "the JUnit XML is not from the recorded task run"
    first, last = record.start_ms - junit.TOLERANCE_MS, record.end_ms + junit.TOLERANCE_MS
    if sessions is not None and any(begin < first or dump > last for begin, dump in sessions):
        return "the JaCoCo sessions are not from the recorded task run"
    return None


def _level(cfg: Config, seconds: float) -> tuple[str | None, str]:
    """The level of the annotation, None within the budget, and the text of the rating; a threshold
    is crossed only when the time is above it."""
    if seconds > cfg.duration_error_s:
        return "error", threshold_text(cfg.duration_error_s)
    if seconds > cfg.duration_warn_s:
        return "warning", threshold_text(cfg.duration_warn_s)
    return None, "within budget"


def _limits(cfg: Config) -> str:
    return f"warning {threshold_text(cfg.duration_warn_s)}, error {threshold_text(cfg.duration_error_s)}"


def _annotate(rep: Report, level: str | None, message: str) -> None:
    """An annotation above a threshold. Above the error threshold it is an error annotation also
    without --strict, and a violation only under --strict: Report.violated would give a warning and
    a violation without --strict."""
    if level is None:
        return
    rep.annotate(level, "Test duration", message)
    if level == "error" and rep.strict:
        rep.violation = True
        rep.violations.append(message)


def _errored(rep: Report, errors: list[str], data: dict) -> None:
    """An input could not be read: every error is reported, and no verdict is given."""
    for error in errors:
        rep.errored("Duration", error)
    rep.evaluated = False
    rep.data = data
    rep.md = ["## Duration", "", "**ERROR**: the inputs could not be read, no verdict:", ""]
    rep.md.extend(f"- {error}" for error in errors)
    rep.verdict = f"duration: ERROR ({errors[0]})"


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Judge the duration of the test run; policy.protected() has resolved args.junit_files."""
    source = cfg.duration_source
    require(cfg, "tests.results", *(("duration.task_json",) if source == "task-json" else ()))
    data: dict = {
        "task_seconds": None,
        "junit_span_seconds": None,
        "task": None,
        "source": source,
        "thresholds": {"warn_s": cfg.duration_warn_s, "error_s": cfg.duration_error_s},
    }
    files = getattr(args, "junit_files", None)
    files = resolve_glob(cfg.root, cfg.get("tests.results")) if files is None else files
    errors: list[str] = []
    suites = junit.read_suites(cfg.root, files, errors)
    if source == "junit-span":
        if errors:
            _errored(rep, errors, data)
        else:
            _junit_span(cfg, rep, suites, data)
        return
    name = cfg.get("duration.task_json")
    record = sessions = seconds = None
    try:
        if exists(cfg.root, name):
            record = timing.load(cfg.root / name, cfg.duration_task, name=name)
    except ReportError as exc:
        errors.append(str(exc))
    if record is not None and record.outcome == "success":
        seconds = (record.end_ms - record.start_ms) / 1000  # the record holds times a datetime holds
        report = cfg.get("coverage.report")
        try:
            if report is not None and exists(cfg.root, report):
                sessions = jacoco.read_sessions(cfg.root / report, name=report)
        except ReportError as exc:
            errors.append(str(exc))
    if errors:
        _errored(rep, errors, data)
        return
    _task_json(cfg, rep, name, record, seconds, suites, sessions, data)


def _task_json(
    cfg: Config,
    rep: Report,
    name: str,
    record: TimingRecord | None,
    seconds: float | None,
    suites: list[junit.Suite],
    sessions: list[tuple[int, int]] | None,
    data: dict,
) -> None:
    """The time of the timing record, judged when the record is from the run on disk."""
    task = cfg.duration_task
    who = md_path(task) if task is not None else "The test"
    label = task if task is not None else "the test task"
    rep.md = ["## Duration", ""]
    rep.evaluated = False
    if record is None:
        verdict = "NOT MEASURED (no task timing)"
        rep.md.append(f"{who} task timing: missing ({name}) - no verdict.")
    elif record.outcome == "failed":
        failure = one_line(record.failure or "") or "no message"
        verdict = f"INCOMPLETE RUN (task failed: {failure})"
        rep.md.append(f"{who} task failed or timed out - incomplete run, no verdict. Failure: {failure}")
    elif record.outcome != "success":
        verdict = f"NOT MEASURED (task {record.outcome})"
        rep.md.append(f"{who} task did not run ({record.outcome}) - not measured.")
    elif reason := _stale(record, suites, sessions):
        verdict = f"NO CURRENT TIMING ({reason})"
        rep.md.append(f"{who} task timing is stale ({reason}) - no current timing, no verdict.")
    else:
        rep.evaluated = True
        level, text = _level(cfg, seconds)
        rep.md.append(f"{who} task: {fmt_seconds(seconds)} - {text} ({_limits(cfg)}).")
        _annotate(rep, level, f"{label} took {fmt_seconds(seconds)}, {text}")
        verdict = f"{(level or 'pass').upper()} {seconds:.1f} s ({text})"
        data["task_seconds"] = seconds
    rep.md.append("")
    span = _span(suites)
    if span is not None:
        rep.md.append(f"JUnit span (auxiliary): {fmt_seconds(span[0])} over {span[1]} suite(s).")
        data["junit_span_seconds"] = span[0]
    else:
        rep.md.append("JUnit span (auxiliary): no JUnit XML.")
    rep.md.append("")
    data["task"] = None if record is None else record.data()
    rep.data = data
    rep.verdict = f"duration: {verdict}"


def _junit_span(cfg: Config, rep: Report, suites: list[junit.Suite], data: dict) -> None:
    """The span of the JUnit suites as the time: a lower bound of the run, which a JUnit file left
    over from an earlier run widens."""
    task = cfg.duration_task
    label = task if task is not None else "the test task"
    rep.md = ["## Duration", ""]
    span = _span(suites)
    if span is None:
        rep.evaluated = False
        verdict = "NOT MEASURED (no JUnit XML)"
        rep.md.append("JUnit span: no JUnit XML - not measured.")
    else:
        seconds, count = span
        level, text = _level(cfg, seconds)
        rep.md.append(f"JUnit span: {fmt_seconds(seconds)} over {count} suite(s) - {text} ({_limits(cfg)}).")
        rep.md.append("")
        rep.md.append("The span is a lower bound; a JUnit file left over from an earlier run widens it.")
        _annotate(rep, level, f"{label} took at least {fmt_seconds(seconds)} (JUnit span), {text}")
        verdict = f"{(level or 'pass').upper()} {seconds:.1f} s ({text}{SPAN_NOTE})"
        data["junit_span_seconds"] = seconds
    rep.md.append("")
    rep.data = data
    rep.verdict = f"duration: {verdict}"
