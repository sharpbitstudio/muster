"""Tests of the duration command, end to end through cli.main on a synthetic git repository: the
keys it needs, the verdicts of a timing record and its staleness against the JUnit results and the
coverage sessions, the JUnit span as a source, the thresholds and strict mode, the input errors,
the output channels and the files no output may be.

Every run happens in temporary folders, with git's global and system configuration switched off.
Times are counted in ms from START_MS, the start of the recorded test task.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from muster import cli, duration
from muster.config import DEFAULT_ERROR_S, DEFAULT_WARN_S, Config

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)  # fmt: skip
TASK = ":app:test"
RECORD = "build/muster/test-task.json"
REPORT = "build/jacoco/report.xml"
RESULTS = "build/test-results"
SOURCE = "src/main/java/org/example/app/Engine.java"
TEST = "src/test/java/org/example/app/EngineTest.java"
SUITE = "org.example.app.EngineTest"
START_MS = int(datetime(2026, 5, 19, 3, 0, tzinfo=timezone.utc).timestamp()) * 1000
PROFILE = """\
schema = 1

[source]
roots = ["src/main/java"]
tests = ["src/test/java"]
include = ["**/*.java"]

[tests]
results = "build/test-results/TEST-*.xml"

[duration]
task_json = "build/muster/test-task.json"
task = ":app:test"
"""
SOURCE_TABLE = '[source]\nroots = ["src/main/java"]\ntests = ["src/test/java"]\ninclude = ["**/*.java"]\n\n'
WITH_REPORT = '\n[coverage]\nreport = "build/jacoco/report.xml"\n'
TASK_JSON_LINE = 'task_json = "build/muster/test-task.json"\n'
SPAN_PROFILE = PROFILE.replace(TASK_JSON_LINE, 'source = "junit-span"\n')
SPAN_NOTE = "; JUnit span, a lower bound: a file left over from an earlier run widens it"


def stamp(epoch_ms: int, zone: str = "") -> str:
    """A JUnit timestamp in UTC to the millisecond, with `zone` (empty: none, which is UTC)."""
    moment = datetime.fromtimestamp(epoch_ms // 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{epoch_ms % 1000:03d}" + zone


def suite_xml(name: str, start_ms: int | None, seconds: float, wrapped: bool = False, zone: str = "") -> str:
    """A JUnit file with one suite; without a start it has no timestamp."""
    when = "" if start_ms is None else f' timestamp="{stamp(start_ms, zone)}"'
    suite = (
        f'<testsuite name="{name}" tests="1" failures="0" errors="0" skipped="0"{when} hostname="localhost"'
        f' time="{seconds}"><testcase name="runs" classname="{name}" time="{seconds}"/></testsuite>'
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + (f"<testsuites>{suite}</testsuites>" if wrapped else suite)


def sessions_xml(sessions: list) -> str:
    """A JaCoCo report with these (start, dump) sessions in epoch ms and no source file."""
    infos = "".join(f'<sessioninfo id="localhost-5eed" start="{start}" dump="{dump}"/>' for start, dump in sessions)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<report name="app">{infos}</report>\n'


@dataclass
class Run:
    code: int
    out: str
    err: str
    env: dict | None
    summary: str

    @property
    def section(self) -> dict:
        return self.env["sections"][0]

    @property
    def data(self) -> dict:
        return self.section["data"]

    @property
    def annotations(self) -> list[str]:
        """The annotation lines, with the escaped percent sign read back."""
        return [line.replace("%25", "%") for line in self.out.splitlines() if line.startswith("::")]

    @property
    def verdict(self) -> str:
        return [line for line in self.out.splitlines() if not line.startswith("::")][-1]

    @property
    def state(self) -> tuple:
        """(exit code, section verdict, violation)."""
        return self.code, self.section["verdict"], self.section["violation"]


class DurationCase(unittest.TestCase):
    """A git repository with a source and a test file, a profile for the task-json source and a
    separate working directory."""

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        for key in [key for key in os.environ if key.startswith("GIT_") or key == "GITHUB_STEP_SUMMARY"]:
            del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.repo = self.root / "repo"
        self.work = self.root / "work"
        self.work.mkdir()
        self.enterContext(contextlib.chdir(self.work))
        self.json_out = self.work / "out.json"
        self.summary = self.work / "summary.md"
        self.write("muster.toml", PROFILE)
        self.write(SOURCE, "class Engine {}\n")
        self.write(TEST, "class EngineTest {}\n")
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "first")):
            subprocess.run(["git", *IDENTITY, *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, path: str, content) -> Path:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return file

    def replace(self, path: str, old: str, new: str) -> None:
        text = (self.repo / path).read_bytes().decode("utf-8")
        self.assertIn(old, text)
        self.write(path, text.replace(old, new, 1))

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def json_main(self, *argv: str) -> tuple[int, dict]:
        code, out, _ = self.main(*argv, "--format", "json")
        return code, json.loads(out)

    def suites(self, *suites: tuple) -> None:
        """Replace the JUnit files: one file per (name, start in ms from START_MS or None, seconds,
        wrapped, zone)."""
        for file in (self.repo / RESULTS).glob("*") if (self.repo / RESULTS).is_dir() else []:
            file.unlink()
        for name, start, seconds, *rest in suites:
            wrapped, zone = rest[0] if rest else False, rest[1] if len(rest) > 1 else ""
            when = None if start is None else START_MS + start
            self.write(f"{RESULTS}/TEST-{name}.xml", suite_xml(name, when, seconds, wrapped, zone))

    def timed_run(self, seconds: float, outcome: str | None = "success", junit: bool = True, **fields) -> None:
        """A timing record of the task from START_MS that took `seconds`, and by default one JUnit
        suite from one second after the start, ending at least a second before the end."""
        record = {"task": TASK, "start_ms": START_MS, "end_ms": START_MS + round(seconds * 1000)}
        if outcome is not None:
            record["outcome"] = outcome
        record.update(fields)
        self.write(RECORD, json.dumps(record))
        self.suites(*([(SUITE, 1000, max(0.5, seconds - 2))] if junit else []))

    def duration(self, *options: str) -> Run:
        self.json_out.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        code, out, err = self.main(
            "duration", "--json-out", str(self.json_out), "--summary-file", str(self.summary), *options
        )
        env = json.loads(self.json_out.read_bytes().decode("ascii")) if self.json_out.exists() else None
        summary = self.summary.read_bytes().decode("utf-8") if self.summary.exists() else ""
        return Run(code, out, err, env, summary)

    def not_measured(self, verdict: str) -> Run:
        """The run has no verdict: no annotation, not evaluated and exit 0, also under --strict."""
        for options in ([], ["--strict"]):
            run = self.duration(*options)
            self.assertEqual(run.state, (0, "not-evaluated", False), run.out + run.err)
            self.assertEqual((run.annotations, run.section["violations"]), ([], []))
            self.assertEqual(run.verdict, f"duration: {verdict}")
            self.assertIsNone(run.data["task_seconds"])
        return run


class VerdictTest(DurationCase):
    def test_run_within_the_budget_passes_quietly(self):
        self.timed_run(200)
        run = self.duration()
        self.assertEqual(run.state, (0, "pass", False))
        self.assertEqual(run.annotations, [])
        self.assertEqual(run.verdict, "duration: PASS 200.0 s (within budget)")
        self.assertEqual(run.data["task_seconds"], 200.0)
        self.assertTrue(run.summary.startswith("## Duration\n\n"), run.summary)
        self.assertIn("`:app:test` task: 3:20 (200.0 s) - within budget (warning > 4 min, error > 5 min).", run.summary)

    def test_run_over_the_warning_threshold_warns_once(self):
        self.timed_run(241)
        run = self.duration()
        self.assertEqual(run.state, (0, "warn", False))
        self.assertEqual(run.annotations, ["::warning title=Test duration:::app:test took 4:01 (241.0 s), > 4 min"])
        self.assertEqual(run.verdict, "duration: WARNING 241.0 s (> 4 min)")

    def test_run_over_the_error_threshold_fails_only_under_strict(self):
        self.timed_run(301)
        annotation = "::error title=Test duration:::app:test took 5:01 (301.0 s), > 5 min"
        run = self.duration()
        self.assertEqual((run.state, run.annotations), ((0, "warn", False), [annotation]))
        self.assertEqual(run.verdict, "duration: ERROR 301.0 s (> 5 min)")
        run = self.duration("--strict")
        self.assertEqual((run.state, run.annotations), ((1, "fail", True), [annotation]))
        self.assertEqual(run.section["violations"], [annotation.split("::")[-1]])

    def test_annotation_violation_verdict_and_exit_table(self):
        table = {
            (241, False): ("warning", False, "warn", 0),
            (241, True): ("warning", False, "warn", 0),
            (301, False): ("error", False, "warn", 0),
            (301, True): ("error", True, "fail", 1),
        }
        for (seconds, strict), expected in table.items():
            with self.subTest(seconds=seconds, strict=strict):
                self.timed_run(seconds)
                run = self.duration(*(["--strict"] if strict else []))
                self.assertEqual(len(run.annotations), 1)
                level = run.annotations[0][2:].split(" ", 1)[0]
                self.assertEqual((level, run.section["violation"], run.section["verdict"], run.code), expected)
                self.assertEqual((run.env["verdict"], run.env["exit_code"]), (expected[2], expected[3]))

    def test_a_threshold_is_crossed_only_above_it(self):
        for seconds, verdict in ((240, "PASS 240.0 s (within budget)"), (300, "WARNING 300.0 s (> 4 min)")):
            with self.subTest(seconds=seconds):
                self.timed_run(seconds)
                self.assertEqual(self.duration().verdict, f"duration: {verdict}")
        self.timed_run(240.001)
        self.assertEqual(self.duration().verdict, "duration: WARNING 240.0 s (> 4 min)")

    def test_failed_task_is_an_incomplete_run(self):
        self.timed_run(600, outcome="failed", failure="3 tests failed")
        run = self.not_measured("INCOMPLETE RUN (task failed: 3 tests failed)")
        line = "`:app:test` task failed or timed out - incomplete run, no verdict. Failure: 3 tests failed"
        self.assertIn(line, run.summary)
        self.assertEqual(run.data["task"]["failure"], "3 tests failed")

    def test_failed_task_without_a_message(self):
        self.timed_run(20, outcome="failed")
        self.not_measured("INCOMPLETE RUN (task failed: no message)")

    def test_failed_task_with_a_blank_message(self):
        self.timed_run(20, outcome="failed", failure="  " + chr(10) + chr(9) + " ")
        run = self.not_measured("INCOMPLETE RUN (task failed: no message)")
        self.assertIn("Failure: no message", run.summary)

    def test_task_that_did_not_run_is_not_measured(self):
        self.timed_run(20, outcome="up-to-date")
        run = self.not_measured("NOT MEASURED (task up-to-date)")
        self.assertIn("`:app:test` task did not run (up-to-date) - not measured.", run.summary)
        self.timed_run(20, outcome=None)
        self.not_measured("NOT MEASURED (task None)")

    def test_outcome_over_several_lines_stays_on_one_line(self):
        self.timed_run(20, outcome="up-to-date" + chr(10) + "  from cache")
        run = self.not_measured("NOT MEASURED (task up-to-date from cache)")
        self.assertIn("`:app:test` task did not run (up-to-date from cache) - not measured.", run.summary)
        self.assertEqual(run.section["verdict_text"], "duration: NOT MEASURED (task up-to-date from cache)")

    def test_missing_record_is_not_measured(self):
        self.suites((SUITE, 1000, 2.0))
        run = self.not_measured("NOT MEASURED (no task timing)")
        self.assertIn(f"`:app:test` task timing: missing ({RECORD}) - no verdict.", run.summary)
        self.assertIsNone(run.data["task"])

    def test_record_in_another_case_is_missing(self):
        self.timed_run(20)
        (self.repo / RECORD).rename(self.repo / "build/muster/Test-Task.json")
        self.not_measured("NOT MEASURED (no task timing)")

    def test_data_carries_the_checked_record_only(self):
        self.timed_run(20, worker="localhost", attempt=2)
        data = self.duration().data
        end_ms = START_MS + 20_000
        record = {"task": TASK, "start_ms": START_MS, "end_ms": end_ms, "outcome": "success", "failure": None}
        expected = {
            "task_seconds": 20.0,
            "junit_span_seconds": 18.0,
            "task": record,
            "source": "task-json",
            "thresholds": {"warn_s": DEFAULT_WARN_S, "error_s": DEFAULT_ERROR_S},
        }
        self.assertEqual(data, expected)

    def test_without_a_task_name_any_task_is_measured(self):
        self.write("muster.toml", PROFILE.replace('task = ":app:test"\n', ""))
        self.timed_run(241, task=":lib:test")
        run = self.duration()
        self.assertEqual(run.annotations, ["::warning title=Test duration::the test task took 4:01 (241.0 s), > 4 min"])
        self.assertIn("The test task: 4:01 (241.0 s) - > 4 min (warning > 4 min, error > 5 min).", run.summary)
        (self.repo / RECORD).unlink()
        self.assertIn(f"The test task timing: missing ({RECORD}) - no verdict.", self.duration().summary)

    def test_record_of_another_task_is_an_input_error(self):
        self.timed_run(20, task=":lib:test")
        for options, code in (([], 0), (["--strict"], 1)):
            run = self.duration(*options)
            self.assertEqual((run.code, run.section["verdict"]), (code, "error"))
            message = f"{RECORD}: a record of task ':lib:test', expected task ':app:test'"
            self.assertEqual(run.section["errors"], [message])

    def test_auxiliary_junit_span_over_two_files(self):
        self.timed_run(100, junit=False)
        self.suites(("org.example.app.FirstTest", 1000, 30.0, True, "Z"), ("org.example.app.SecondTest", 51000, 30.0))
        run = self.duration()
        self.assertEqual((run.data["task_seconds"], run.data["junit_span_seconds"]), (100.0, 80.0))
        self.assertIn("\nJUnit span (auxiliary): 1:20 (80.0 s) over 2 suite(s).\n", run.summary)


class StaleRecordTest(DurationCase):
    def test_junit_after_the_task_window(self):
        self.timed_run(100, junit=False)
        self.suites((SUITE, 3_600_000, 2.0))
        run = self.not_measured("NO CURRENT TIMING (the JUnit XML is not from the recorded task run)")
        line = "`:app:test` task timing is stale (the JUnit XML is not from the recorded task run) - no current timing"
        self.assertIn(line, run.summary)
        self.assertEqual(run.data["junit_span_seconds"], 2.0)

    def test_no_junit_xml_for_the_record(self):
        self.timed_run(100, junit=False)
        run = self.not_measured("NO CURRENT TIMING (no JUnit XML for the recorded run)")
        self.assertIn("\nJUnit span (auxiliary): no JUnit XML.\n", run.summary)

    def test_suite_without_a_timestamp_is_skipped(self):
        self.timed_run(100, junit=False)
        self.suites((SUITE, None, 2.0))
        self.not_measured("NO CURRENT TIMING (no JUnit XML for the recorded run)")

    def test_window_is_exact_to_the_millisecond(self):
        self.timed_run(100, junit=False)
        stale = "duration: NO CURRENT TIMING (the JUnit XML is not from the recorded task run)"
        for suite, verdict in (
            ((SUITE, -1000, 2.0), "duration: PASS 100.0 s (within budget)"),
            ((SUITE, -1001, 2.0), stale),
            ((SUITE, 99_000, 2.0), "duration: PASS 100.0 s (within budget)"),
            ((SUITE, 99_000, 2.001), stale),
        ):
            with self.subTest(suite=suite):
                self.suites(suite)
                self.assertEqual(self.duration().verdict, verdict)

    def test_coverage_sessions_outside_and_inside_the_window(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(100)
        sessions = [(START_MS + 500, START_MS + 99_000), (START_MS + 200_000, START_MS + 201_000)]
        self.write(REPORT, sessions_xml(sessions))
        self.not_measured("NO CURRENT TIMING (the JaCoCo sessions are not from the recorded task run)")
        self.write(REPORT, sessions_xml([(START_MS + 500, START_MS + 99_000)]))
        self.assertEqual(self.duration().verdict, "duration: PASS 100.0 s (within budget)")

    def test_session_window_is_exact_to_the_millisecond(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(100)
        stale = "duration: NO CURRENT TIMING (the JaCoCo sessions are not from the recorded task run)"
        for session, verdict in (
            ((START_MS - 1000, START_MS + 101_000), "duration: PASS 100.0 s (within budget)"),
            ((START_MS - 1001, START_MS + 50_000), stale),
            ((START_MS, START_MS + 101_001), stale),
        ):
            with self.subTest(session=session):
                self.write(REPORT, sessions_xml([session]))
                self.assertEqual(self.duration().verdict, verdict)

    def test_missing_report_is_not_checked(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(100)
        self.assertEqual(self.duration().verdict, "duration: PASS 100.0 s (within budget)")

    def test_report_is_read_only_for_a_successful_task(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.write(REPORT, "<report")
        self.timed_run(100, outcome="failed")
        self.not_measured("INCOMPLETE RUN (task failed: no message)")


class JunitSpanSourceTest(DurationCase):
    def setUp(self):
        super().setUp()
        self.write("muster.toml", SPAN_PROFILE)

    def test_span_is_the_time(self):
        self.suites(("org.example.app.FirstTest", 1000, 30.0, True, "Z"), ("org.example.app.SecondTest", 51000, 30.0))
        run = self.duration()
        self.assertEqual((run.state, run.annotations), ((0, "pass", False), []))
        self.assertEqual(run.verdict, f"duration: PASS 80.0 s (within budget{SPAN_NOTE})")
        self.assertEqual(
            {key: run.data[key] for key in ("task_seconds", "junit_span_seconds", "task", "source")},
            {"task_seconds": None, "junit_span_seconds": 80.0, "task": None, "source": "junit-span"},
        )
        line = "JUnit span: 1:20 (80.0 s) over 2 suite(s) - within budget (warning > 4 min, error > 5 min)."
        self.assertIn(line, run.summary)
        self.assertIn("The span is a lower bound; a JUnit file left over from an earlier run widens it.", run.summary)

    def test_no_junit_xml_is_not_measured(self):
        run = self.not_measured("NOT MEASURED (no JUnit XML)")
        self.assertIn("JUnit span: no JUnit XML - not measured.", run.summary)
        self.suites((SUITE, None, 2.0))
        self.not_measured("NOT MEASURED (no JUnit XML)")

    def test_span_over_the_error_threshold_fails_under_strict(self):
        self.suites(("org.example.app.FirstTest", 0, 1.0), ("org.example.app.SecondTest", 300_000, 1.0))
        annotation = "::error title=Test duration:::app:test took at least 5:01 (301.0 s) (JUnit span), > 5 min"
        run = self.duration()
        self.assertEqual((run.state, run.annotations), ((0, "warn", False), [annotation]))
        run = self.duration("--strict")
        self.assertEqual((run.state, run.annotations), ((1, "fail", True), [annotation]))
        self.assertEqual(run.verdict, f"duration: ERROR 301.0 s (> 5 min{SPAN_NOTE})")

    def test_record_is_not_read(self):
        self.write("muster.toml", SPAN_PROFILE + TASK_JSON_LINE + WITH_REPORT)
        self.write(RECORD, "{not json")
        self.write(REPORT, "<report")
        self.suites((SUITE, 0, 10.0))
        self.assertEqual(self.duration().verdict, f"duration: PASS 10.0 s (within budget{SPAN_NOTE})")

    def test_file_left_from_an_earlier_run_widens_the_span(self):
        self.suites(("org.example.app.FirstTest", -3_600_000, 1.0), ("org.example.app.SecondTest", 0, 10.0))
        self.assertEqual(self.duration().verdict, f"duration: ERROR 3610.0 s (> 5 min{SPAN_NOTE})")


class ThresholdTest(DurationCase):
    def test_thresholds_of_the_profile(self):
        self.write("muster.toml", PROFILE + "warn_s = 120\nerror_s = 150\n")
        for seconds, verdict, annotation in (
            (130, "WARNING 130.0 s (> 2 min)", "::warning title=Test duration:::app:test took 2:10 (130.0 s), > 2 min"),
            (151, "ERROR 151.0 s (> 150 s)", "::error title=Test duration:::app:test took 2:31 (151.0 s), > 150 s"),
        ):
            with self.subTest(seconds=seconds):
                self.timed_run(seconds)
                run = self.duration()
                self.assertEqual((run.verdict, run.annotations), (f"duration: {verdict}", [annotation]))
                self.assertIn("(warning > 2 min, error > 150 s).", run.summary)
                self.assertEqual(run.data["thresholds"], {"warn_s": 120, "error_s": 150})

    def test_equal_thresholds_give_an_error(self):
        self.write("muster.toml", PROFILE + "warn_s = 60\nerror_s = 60\n")
        self.timed_run(61)
        self.assertEqual(self.duration().verdict, "duration: ERROR 61.0 s (> 1 min)")

    def test_threshold_text(self):
        cases = ((240, "> 4 min"), (240.0, "> 4 min"), (250, "> 250 s"), (0.5, "> 0.5 s"), (90.25, "> 90.25 s"))
        for value, text in cases:
            with self.subTest(value=value):
                self.assertEqual(duration.threshold_text(value), text)

    def test_fmt_seconds(self):
        self.assertEqual(duration.fmt_seconds(241.0), "4:01 (241.0 s)")
        self.assertEqual(duration.fmt_seconds(59.96), "1:00 (60.0 s)")

    def test_error_below_the_warning_threshold_exits_2_for_every_command(self):
        for table, message in (
            ("error_s = 200\n", "duration.error_s: 200 is below duration.warn_s 240 (the default)"),
            ("warn_s = 400\n", "duration.error_s: 300 (the default) is below duration.warn_s 400"),
        ):
            self.write("muster.toml", PROFILE + table)
            for command in ("coverage", "duration", "policy"):
                with self.subTest(table=table, command=command):
                    code, env = self.json_main(command)
                    expected = {"kind": "config", "message": f"muster.toml: {message}"}
                    self.assertEqual((code, env["error"]), (2, expected))


class DurationKeyTest(DurationCase):
    def assert_missing(self, key: str) -> None:
        code, env = self.json_main("duration")
        self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": f"muster.toml: missing key {key}"}))

    def test_results_key_for_both_sources(self):
        no_results = '[tests]\nresults = "build/test-results/TEST-*.xml"\n'
        self.write("muster.toml", PROFILE.replace(no_results, ""))
        self.assert_missing("tests.results")
        self.write("muster.toml", SPAN_PROFILE.replace(no_results, ""))
        self.assert_missing("tests.results")

    def test_record_key_versus_missing_record_file(self):
        self.write("muster.toml", PROFILE.replace(TASK_JSON_LINE, ""))
        self.assert_missing("duration.task_json")
        self.write("muster.toml", PROFILE.replace(TASK_JSON_LINE, 'source = "task-json"\n'))
        self.assert_missing("duration.task_json")
        self.write("muster.toml", PROFILE)
        self.not_measured("NOT MEASURED (no task timing)")


class BrokenInputTest(DurationCase):
    """A timing record, a JUnit file or a coverage report that cannot be read: errors, no verdict;
    exit 0, and 1 with --strict or --fail-on-error, never 3."""

    TIMING = {
        "non-finite number": ('"end_ms": ', '"end_ms": NaN, "was": '),
        "wrong type": ('"end_ms": ', '"end_ms": "1", "was": '),
        "boolean": ('"end_ms": ', '"end_ms": true, "was": '),
        "missing key": ('"end_ms": ', '"end": '),
        "inverted times": ('"end_ms": ', f'"end_ms": {START_MS - 1}, "was": '),
        "too long a run": ('"end_ms": ', '"end_ms": 1' + "0" * 400 + ', "was": '),
        "end past the year 9999": ('"end_ms": ', '"end_ms": 253402300800000, "was": '),
        "an infinite number in an extra key": ('"end_ms": ', '"was": 1e9999, "end_ms": '),
        "not JSON": ("{", "{not json"),
    }
    JUNIT = {
        "non-finite time": ('time="2.0">', 'time="inf">'),
        "bad time": ('time="2.0">', 'time="2.0s">'),
        "negative time": ('time="2.0">', 'time="-1">'),
        "bad timestamp": ('timestamp="', 'timestamp="T'),
        # The suite ends at 23:00:02 of its own day, which is past the last date in UTC.
        "an offset past the last date": ('timestamp="', 'timestamp="9999-12-31T23:00:00-02:00" was="'),
        "not XML": ("</testsuite>", ""),
    }
    JACOCO = {
        "missing attribute": (' dump="', ' ended="'),
        "wrong type": (' start="', ' start="1.5'),
        "not XML": ("</report>", ""),
        "more digits than int() reads": (' start="', ' start="' + "9" * 4301),
        "a session that ends before it starts": (' start="', ' start="9'),
    }

    def assert_errored(self, prefix: str) -> None:
        for options, expected in (([], 0), (["--strict"], 1), (["--fail-on-error"], 1)):
            run = self.duration(*options)
            self.assertEqual(run.code, expected, run.out + run.err)
            self.assertEqual((run.section["verdict"], run.env["error"]), ("error", None))
            self.assertTrue(run.section["errors"][0].startswith(prefix), run.section["errors"])
            self.assertEqual(run.verdict, f"duration: ERROR ({run.section['errors'][0]})")
            self.assertIn("**ERROR**: the inputs could not be read, no verdict:", run.summary)
            self.assertNotIn("JUnit span", run.summary)
            self.assertIsNone(run.data["task_seconds"])
            level = "error" if options else "warning"
            self.assertTrue(run.annotations[0].startswith(f"::{level} title=Duration::"), run.annotations)

    def test_broken_record(self):
        for case, (old, new) in self.TIMING.items():
            with self.subTest(case=case):
                self.timed_run(20)
                self.replace(RECORD, old, new)
                self.assert_errored(f"{RECORD}: ")

    def test_broken_junit_file(self):
        for source, profile in (("task-json", PROFILE), ("junit-span", SPAN_PROFILE)):
            self.write("muster.toml", profile)
            for case, (old, new) in self.JUNIT.items():
                with self.subTest(source=source, case=case):
                    self.timed_run(20, junit=False)
                    self.suites((SUITE, 1000, 2.0))
                    self.replace(f"{RESULTS}/TEST-{SUITE}.xml", old, new)
                    self.assert_errored(f"{RESULTS}/TEST-{SUITE}.xml: ")

    def test_broken_coverage_report(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        for case, (old, new) in self.JACOCO.items():
            with self.subTest(case=case):
                self.timed_run(20)
                self.write(REPORT, sessions_xml([(START_MS, START_MS + 20_000)]))
                self.replace(REPORT, old, new)
                self.assert_errored(f"{REPORT}: ")

    def test_inverted_times_are_an_ordering_error(self):
        self.timed_run(20)
        self.replace(RECORD, '"end_ms": ', f'"end_ms": {START_MS - 1}, "was": ')
        self.assert_errored(f"{RECORD}: end_ms {START_MS - 1} is before start_ms {START_MS}")

    def test_huge_times_are_an_error_whatever_the_outcome(self):
        for outcome in ("failed", "skipped", None):
            with self.subTest(outcome=outcome):
                self.timed_run(20, outcome=outcome)
                self.replace(RECORD, '"end_ms": ', '"end_ms": 1' + "0" * 400 + ', "was": ')
                self.assert_errored(f"{RECORD}: end_ms must be from 0 to ")

    def test_inverted_session_inside_the_task(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(20)
        self.write(REPORT, sessions_xml([(START_MS + 5000, START_MS + 1000)]))
        self.assert_errored(f"{REPORT}: <sessioninfo> dump {START_MS + 1000} is before start {START_MS + 5000}")

    def test_broken_counter_of_the_coverage_report(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(20)
        counter = '<package name="org/example/app"><sourcefile name="Engine.java">'
        counter += '<counter type="LINE" missed="-1" covered="3"/></sourcefile></package></report>'
        self.write(REPORT, sessions_xml([(START_MS, START_MS + 20_000)]).replace("</report>", counter))
        self.assert_errored(f"{REPORT}: <counter> attribute missed")

    def test_broken_counter_outside_a_source_file(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(20)
        counter = '<counter type="LINE" missed="-1" covered="1"/></report>'
        self.write(REPORT, sessions_xml([(START_MS, START_MS + 20_000)]).replace("</report>", counter))
        self.assert_errored(f"{REPORT}: <counter> attribute missed")

    def test_literal_results_path_that_is_a_folder(self):
        literal = f"{RESULTS}/TEST-{SUITE}.xml"
        for source, profile in (("task-json", PROFILE), ("junit-span", SPAN_PROFILE)):
            with self.subTest(source=source):
                self.write("muster.toml", profile.replace(f"{RESULTS}/TEST-*.xml", literal))
                self.timed_run(20, junit=False)
                (self.repo / literal).mkdir(parents=True)
                self.assert_errored(f"{literal}: not a file")
                self.assertEqual(self.duration().section["errors"], [f"{literal}: not a file"])
                (self.repo / literal).rmdir()

    @unittest.skipIf(os.name == "nt", "chmod cannot make a folder unreadable on Windows")
    def test_record_behind_a_folder_that_cannot_be_read(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every folder")
        self.timed_run(20)
        folder = self.repo / "build" / "muster"
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        try:
            os.listdir(folder)
        except PermissionError:
            pass
        else:
            self.skipTest("chmod does not make a folder unreadable here")
        self.assert_errored(f"{RECORD}: cannot read a folder on its path: ")

    def test_junit_file_with_another_root(self):
        self.timed_run(20)
        suite = suite_xml(SUITE, START_MS + 1000, 2.0).split("?>", 1)[1]
        self.write(f"{RESULTS}/TEST-other.xml", f'<report name="app">{suite}</report>')
        self.assert_errored(f"{RESULTS}/TEST-other.xml: not a JUnit report")

    def test_every_broken_junit_file_is_listed(self):
        self.timed_run(20)
        self.write(f"{RESULTS}/TEST-other.xml", "<testsuite")
        self.replace(f"{RESULTS}/TEST-{SUITE}.xml", "</testsuite>", "")
        errors = self.duration().section["errors"]
        files = [f"{RESULTS}/TEST-{SUITE}.xml", f"{RESULTS}/TEST-other.xml"]
        self.assertEqual([error.split(":")[0] for error in errors], files)

    def test_record_or_report_that_is_a_folder(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(20)
        for path in (RECORD, REPORT):
            with self.subTest(path=path):
                (self.repo / path).unlink(missing_ok=True)
                (self.repo / path).mkdir(parents=True)
                self.assert_errored(f"{path}: not a file")
                self.assertEqual(self.duration().section["errors"], [f"{path}: not a file"])
                (self.repo / path).rmdir()
                if path == RECORD:
                    self.timed_run(20)

    def test_every_error_is_listed(self):
        self.timed_run(20)
        self.replace(f"{RESULTS}/TEST-{SUITE}.xml", "</testsuite>", "")
        self.replace(RECORD, "{", "[")
        run = self.duration()
        errors = run.section["errors"]
        self.assertEqual([error.split(":")[0] for error in errors], [f"{RESULTS}/TEST-{SUITE}.xml", RECORD])
        for error in errors:
            self.assertIn(f"- {error}", run.summary)
        self.assertEqual(len(run.annotations), 2)


class OutputChannelTest(DurationCase):
    def test_github_summary_is_appended_to(self):
        step = self.work / "step.md"
        step.write_bytes(b"previous step\n")
        os.environ["GITHUB_STEP_SUMMARY"] = str(step)
        self.timed_run(20)
        self.assertEqual(self.main("duration", "--github")[0], 0)
        self.assertTrue(step.read_bytes().decode("utf-8").startswith("previous step\n## Duration\n"))

    def test_report_in_the_output_folder_is_rewritten_per_run(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        self.timed_run(20)
        for _ in range(2):
            code, out, _ = self.main("duration")
            self.assertEqual((code, out), (0, "duration: PASS 20.0 s (within budget)\n"))
        text = (self.repo / "build/muster/duration.md").read_bytes().decode("utf-8")
        self.assertEqual(text.count("## Duration"), 1)

    def test_failure_over_several_lines_stays_on_one_line(self):
        self.timed_run(20, outcome="failed", failure="first line\nsecond\r\n  third")
        code, out, _ = self.main("duration", "--summary-file", str(self.summary))
        self.assertEqual((code, out), (0, "duration: INCOMPLETE RUN (task failed: first line second third)\n"))
        self.assertIn("Failure: first line second third\n", self.summary.read_bytes().decode("utf-8"))

    def test_record_that_is_not_json_is_an_error_not_a_crash(self):
        self.timed_run(20)
        self.write(RECORD, "{not json")
        for options, code in (([], 0), (["--strict"], 1)):
            run = self.duration(*options)
            self.assertEqual((run.code, run.section["verdict"], run.env["error"]), (code, "error", None))
            self.assertTrue(run.section["errors"][0].startswith(f"{RECORD}: not valid JSON"))
            self.assertEqual(run.err, "")

    def test_bad_arguments_are_a_usage_error(self):
        code, env = self.json_main("duration", "--no-such-option")
        self.assertEqual((code, env["error"]["kind"], env["command"]), (2, "usage", "duration"))
        self.assertEqual(self.json_main("duration", "--base", "main")[0], 2)

    def test_isolated_run_from_another_folder(self):
        self.timed_run(20)
        self.write(RECORD, "{not json")
        proc = subprocess.run(
            [sys.executable, "-I", str(REPO_ROOT / "src"), "--repo", str(self.repo), "duration"],
            cwd=self.work,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"::warning title=Duration::{RECORD}: not valid JSON".encode("ascii"), proc.stdout)
        self.assertIn(b"duration: ERROR (", proc.stdout)


class OutputClashTest(DurationCase):
    """An output that is a source, test, JUnit, record or report file: exit 2, nothing is removed or
    written."""

    def assert_refused(self, target: Path, label: str) -> None:
        before = target.read_bytes()
        code, env = self.json_main("duration", "--json-out", str(target))
        self.assertEqual((code, env["error"]["kind"]), (2, "usage"))
        self.assertEqual(env["error"]["message"], f"--json-out {target} is {label}")
        self.assertEqual(target.read_bytes(), before)

    def test_source_test_and_junit_files(self):
        self.timed_run(20)
        self.assert_refused(self.repo / SOURCE, "under the source folder src/main/java")
        self.assert_refused(self.repo / TEST, "under the test folder src/test/java")
        junit_file = f"{RESULTS}/TEST-{SUITE}.xml"
        self.assert_refused(self.repo / junit_file, f"the JUnit file {junit_file}")

    def test_record_and_report(self):
        self.write("muster.toml", PROFILE + WITH_REPORT)
        self.timed_run(20)
        self.write(REPORT, sessions_xml([]))
        self.assert_refused(self.repo / RECORD, "the file of duration.task_json")
        self.assert_refused(self.repo / REPORT, "the file of coverage.report")

    def test_files_under_the_folders_that_no_glob_matches(self):
        self.timed_run(20)
        for path, label in (
            ("src/main/java/org/example/app/notes.txt", "under the source folder src/main/java"),
            ("src/test/java/org/example/app/fixture.txt", "under the test folder src/test/java"),
        ):
            with self.subTest(path=path):
                self.write(path, "data\n")
                self.assert_refused(self.repo / path, label)

    def test_files_the_profile_names_for_other_commands(self):
        zones = '\n[zones]\ncritical = "policy/critical.txt"\n\n[policy]\nsnapshot = "build/muster/snapshot.json"\n'
        self.write("muster.toml", PROFILE + zones)
        self.write("policy/critical.txt", "billing/**\n")
        self.write("build/muster/snapshot.json", "{}\n")
        self.timed_run(20)
        self.assert_refused(self.repo / "policy/critical.txt", "the file of zones.critical")
        self.assert_refused(self.repo / "build/muster/snapshot.json", "the file of policy.snapshot")

    def test_junit_file_without_a_source_table(self):
        self.write("muster.toml", PROFILE.replace(SOURCE_TABLE, ""))
        self.timed_run(20)
        junit_file = f"{RESULTS}/TEST-{SUITE}.xml"
        self.assert_refused(self.repo / junit_file, f"the JUnit file {junit_file}")

    def test_output_under_a_source_or_test_folder_on_every_run(self):
        self.timed_run(20)
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "src/test/java/build"\n')
        default = self.repo / "src/test/java/build/duration.md"
        for options, target, message in (
            (
                ["--json-out", str(self.repo / "src/main/java/build/duration.json")],
                self.repo / "src/main/java/build/duration.json",
                f"--json-out {self.repo / 'src/main/java/build/duration.json'} is under the source folder"
                " src/main/java",
            ),
            (
                ["--summary-file", str(self.repo / "src/test/java/build/summary.md")],
                self.repo / "src/test/java/build/summary.md",
                f"--summary-file {self.repo / 'src/test/java/build/summary.md'} is under the test folder src/test/java",
            ),
            ([], default, "the summary src/test/java/build/duration.md is under the test folder src/test/java"),
        ):
            for run in ("first", "second"):
                with self.subTest(target=target.name, run=run):
                    if run == "second":  # as an earlier run would have left it
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b"left\n")
                    code, env = self.json_main("duration", *options)
                    self.assertEqual((code, env["error"]), (2, {"kind": "usage", "message": message}))
                    if run == "first":
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_bytes(), b"left\n")
            target.unlink()

    def test_inputs_never_raise_on_an_unchecked_profile(self):
        inputs = cli.ANALYSES["duration"].inputs
        data = {"duration": {"task_json": ["not", "a", "path"]}, "coverage": {"report": "build/x" + chr(0) + ".xml"}}
        self.assertEqual(inputs(Config(self.repo, self.repo / "muster.toml", data)), [])
        data = {"duration": "a string", "coverage": 7}
        self.assertEqual(inputs(Config(self.repo, self.repo / "muster.toml", data)), [])


class PolicyTextTest(unittest.TestCase):
    def test_policy_text_names_the_default_thresholds(self):
        text = (REPO_ROOT / "POLICY.md").read_bytes().decode("ascii")
        point = text.split("## 5. ", 1)[1].split("\n## ", 1)[0]
        found = re.findall(r"a warning above (\d+) seconds and an error above (\d+) seconds", point)
        self.assertEqual(found, [(str(DEFAULT_WARN_S), str(DEFAULT_ERROR_S))])
        self.assertIn("By default a run gets", point)

    def test_policy_text_scopes_the_failed_run_to_the_timing_record(self):
        text = (REPO_ROOT / "POLICY.md").read_bytes().decode("ascii")
        point = text.split("## 5. ", 1)[1].split("\n## ", 1)[0]
        rule = "When the time comes from a timing record of the test task, a failed or interrupted run has no"
        self.assertIn(rule + " duration verdict.", point)
        self.assertIn("is a lower bound of the wall time", point)
        self.assertNotIn("A failed or interrupted run has no duration verdict.", point)


if __name__ == "__main__":
    unittest.main()
