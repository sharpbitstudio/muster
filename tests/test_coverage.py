"""Tests of the coverage command, end to end through cli.main on a synthetic git repository: the
keys it needs, the policy check, the freshness of the report against the snapshot, the floor and
the zones, the exemptions, the delta gate, the input errors and the files no output may be.

Every run happens in temporary folders, with git's global and system configuration switched off.
A "test run" writes the snapshot with `policy --snapshot` first, then a JaCoCo report and the JUnit
files whose times are counted from the snapshot's creation.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from muster import cli, coverage
from muster.config import (
    DEFAULT_DELTA_BRANCH,
    DEFAULT_DELTA_LINE,
    DEFAULT_FLOOR_BRANCH,
    DEFAULT_FLOOR_LINE,
    Config,
)
from muster.report import ReportError

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)  # fmt: skip
BASE = "src/main/java/org/example/app"
TESTS = "src/test/java/org/example/app"
ENGINE = f"{BASE}/billing/internal/ledger/Engine.java"  # zone 1
TOKEN = f"{BASE}/auth/Token.java"  # zone 2, exempt until 2026-05-30
PARSER = f"{BASE}/codec/KeyParser.java"  # standard
DISPATCHER = f"{BASE}/queue/Dispatcher.java"  # standard
JOURNAL = f"{BASE}/billing/internal/ledger/Journal.java"  # a new critical file
ROUTER = f"{BASE}/queue/Router.java"  # a new standard file
PACKAGE_INFO = f"{BASE}/package-info.java"  # excluded from the classification set
PARSER_TEST = f"{TESTS}/codec/KeyParserTest.java"
SUITE = "org.example.app.codec.KeyParserTest"
REPORT = "build/jacoco/report.xml"
SNAPSHOT = "build/muster/snapshot.json"
RESULTS = "build/test-results"
TODAY = date(2026, 5, 19)
PROFILE = """\
schema = 1

[source]
roots = ["src/main/java"]
tests = ["src/test/java"]
include = ["**/*.java"]
exclude = ["**/package-info.java"]

[zones]
base = "src/main/java/org/example/app"
critical = "policy/critical.txt"
standard = "policy/standard.txt"
exemptions = "policy/exemptions.txt"

[policy]
snapshot = "build/muster/snapshot.json"

[coverage]
report = "build/jacoco/report.xml"

[tests]
results = "build/test-results/TEST-*.xml"
"""
FULL_RUN = f'\n[[coverage.full_run_suites]]\nname = "{SUITE}"\nfile = "{PARSER_TEST}"\n'
MUTATION = '\n[mutation]\ntargets = "policy/mutation.txt"\nown_test = "{stem}Test{ext}"\n'
CRITICAL = "# Zone 1: money.\nbilling/**\n# Zone 2: access.\nauth/**\n"
STANDARD = "codec/*\nqueue/*\n"
TOKEN_EXEMPTION = "auth/Token.java | rename only | 2026-05-30\n"
EXEMPTIONS = "# path | reason | valid-until\n" + TOKEN_EXEMPTION
COVERED = (0, 3, 0, 0)  # (missed instructions, covered instructions, missed branches, covered branches)
MISSED = (3, 0, 0, 0)
BRANCHED = (0, 3, 0, 2)
NO_SNAPSHOT = 'no snapshot: run "muster policy --snapshot" before the tests'
SKIPPED_SUITE = f"full-run suite {SUITE} has no result in this run (filtered run, or the suite was renamed)"
STALE_RUN = "the snapshot is newer than the test run: run the tests again after writing the snapshot"


def lines_of(*numbers: int, data: tuple = COVERED) -> dict:
    return {nr: data for nr in numbers}


def source_text(count: int = 10) -> str:
    return "".join(f"    int field{nr};\n" for nr in range(1, count + 1))


def stamp(epoch_ms: int) -> str:
    """A JUnit timestamp in UTC without a zone, to the millisecond."""
    moment = datetime.fromtimestamp(epoch_ms // 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{epoch_ms % 1000:03d}"


def jacoco_xml(files: dict, sessions: list, counters: dict | None = None) -> str:
    """A JaCoCo report: per file (a path under src/main/java) its lines; the LINE and BRANCH counters
    are summed from the lines unless `counters` gives them."""
    packages: dict[str, list[str]] = {}
    for path, lines in sorted(files.items()):
        folder, _, name = path.removeprefix("src/main/java/").rpartition("/")
        rows = "".join(
            f'<line nr="{nr}" mi="{mi}" ci="{ci}" mb="{mb}" cb="{cb}"/>'
            for nr, (mi, ci, mb, cb) in sorted(lines.items())
        )
        line = (sum(1 for row in lines.values() if row[1] == 0), sum(1 for row in lines.values() if row[1] > 0))
        branch = (sum(row[2] for row in lines.values()), sum(row[3] for row in lines.values()))
        line, branch = (counters or {}).get(path, (line, branch))
        rows += f'<counter type="LINE" missed="{line[0]}" covered="{line[1]}"/>'
        if branch != (0, 0):
            rows += f'<counter type="BRANCH" missed="{branch[0]}" covered="{branch[1]}"/>'
        packages.setdefault(folder, []).append(f'<sourcefile name="{name}">{rows}</sourcefile>')
    infos = "".join(f'<sessioninfo id="localhost-c0de" start="{start}" dump="{dump}"/>' for start, dump in sessions)
    body = "".join(f'<package name="{folder}">{"".join(items)}</package>' for folder, items in packages.items())
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<report name="app">{infos}{body}</report>\n'


def junit_xml(name: str, start_ms: int, seconds: float, wrapped: bool = False) -> str:
    suite = (
        f'<testsuite name="{name}" tests="1" failures="0" errors="0" skipped="0" timestamp="{stamp(start_ms)}"'
        f' hostname="localhost" time="{seconds}"><testcase name="parses" classname="{name}" time="{seconds}"/>'
        "</testsuite>"
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + (f"<testsuites>{suite}</testsuites>" if wrapped else suite)


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


class CoverageCase(unittest.TestCase):
    """A git repository with a valid policy on branch `feature`, cut from `main` with nothing
    committed since, a separate working directory and a fixed today, 2026-05-19."""

    SUBFOLDER = ""  # the folder of the profile inside the git work tree

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        self.enterContext(mock.patch("muster.policy.utc_today", return_value=TODAY))
        for key in [key for key in os.environ if key.startswith("GIT_") or key == "GITHUB_STEP_SUMMARY"]:
            del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.top = self.root / "repo"
        self.repo = self.top / self.SUBFOLDER if self.SUBFOLDER else self.top
        self.work = self.root / "work"
        self.work.mkdir()
        self.enterContext(contextlib.chdir(self.work))
        self.json_out = self.work / "out.json"
        self.summary = self.work / "summary.md"
        self.write("muster.toml", PROFILE)
        self.write("policy/critical.txt", CRITICAL)
        self.write("policy/standard.txt", STANDARD)
        self.write("policy/exemptions.txt", EXEMPTIONS)
        for path in (ENGINE, TOKEN, PARSER, DISPATCHER):
            self.write(path, source_text())
        self.write(PACKAGE_INFO, "package org.example.app;\n")
        self.write(PARSER_TEST, "class KeyParserTest {}\n")
        self.write(f"{TESTS}/support/BaseIT.java", "class BaseIT {}\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.git("checkout", "-q", "-b", "feature")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *IDENTITY, *args], cwd=self.top, check=True, capture_output=True)
        return proc.stdout.decode("utf-8").strip()

    def write(self, path: str, content) -> Path:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return file

    def edit(self, path: str, changes: dict[int, str]) -> None:
        lines = (self.repo / path).read_bytes().decode("utf-8").split("\n")
        for nr, text in changes.items():
            lines[nr - 1] = text
        self.write(path, "\n".join(lines))

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

    def measure(
        self,
        lines: dict | None = None,
        *,
        only: bool = False,
        drop: tuple = (),
        sessions: list | None = None,
        counters: dict | None = None,
        suites: list | None = None,
        snapshot: bool = True,
    ) -> int:
        """`policy --snapshot`, then a test run: a JaCoCo report with every source covered and a
        hundred lines of Dispatcher with covered branches, updated by `lines` (or only `lines`),
        and one JUnit file per suite. Sessions (start, dump) and suites (name, start, seconds,
        wrapped) are in ms from the creation of the snapshot. Returns that creation time."""
        if snapshot:
            self.assertEqual(self.main("policy", "--snapshot")[0], 0)
        path = self.repo / SNAPSHOT
        created = json.loads(path.read_bytes())["created_epoch_ms"] if path.exists() else time.time_ns() // 10**6
        files = {}
        if not only:
            files = {ENGINE: lines_of(*range(1, 11)), TOKEN: lines_of(*range(1, 11)), PARSER: lines_of(*range(1, 11))}
            files[DISPATCHER] = lines_of(*range(1, 101), data=BRANCHED)
        files.update(lines or {})
        for path_dropped in drop:
            del files[path_dropped]
        sessions = [(100, 60_000)] if sessions is None else sessions
        absolute = [(created + start, created + dump) for start, dump in sessions]
        self.write(REPORT, jacoco_xml(files, absolute, counters))
        shutil.rmtree(self.repo / RESULTS, ignore_errors=True)
        for name, start, seconds, *wrapped in [(SUITE, 1000, 2.0)] if suites is None else suites:
            self.write(f"{RESULTS}/TEST-{name}.xml", junit_xml(name, created + start, seconds, bool(wrapped)))
        return created

    def coverage(self, *options: str) -> Run:
        self.json_out.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        code, out, err = self.main(
            "coverage", "--json-out", str(self.json_out), "--summary-file", str(self.summary), *options
        )
        env = json.loads(self.json_out.read_bytes().decode("ascii")) if self.json_out.exists() else None
        summary = self.summary.read_bytes().decode("utf-8") if self.summary.exists() else ""
        return Run(code, out, err, env, summary)

    def passed(self, *options: str) -> Run:
        run = self.coverage(*options)
        self.assertEqual((run.code, run.data["outcome"]), (0, "pass"), run.out + run.err)
        return run

    def stale(self, run: Run, *reasons: str) -> None:
        """The section is not evaluated for exactly these reasons, with exit 0."""
        self.assertEqual(run.code, 0, run.out + run.err)
        self.assertEqual((run.data["outcome"], run.data["reasons"]), ("not-evaluated", list(reasons)))
        self.assertEqual((run.section["verdict"], run.section["violation"]), ("not-evaluated", False))
        self.assertEqual(run.section["violations"], [])
        self.assertEqual(run.annotations, ["::warning title=Coverage::coverage not evaluated: " + "; ".join(reasons)])
        self.assertIn("coverage: NOT EVALUATED (" + "; ".join(reasons), run.verdict)
        self.assertNotIn("Global floor", run.summary)

    def exempt_engine(self, until: date) -> None:
        line = f"billing/internal/ledger/Engine.java | moved only | {until.isoformat()}\n"
        self.write("policy/exemptions.txt", EXEMPTIONS + line)


class DefaultsTest(unittest.TestCase):
    def config(self, data: dict) -> Config:
        return Config(Path("."), Path("muster.toml"), data)

    def test_thresholds_without_a_table(self):
        cfg = self.config({})
        self.assertEqual(cfg.coverage_floor, {"line": 0.80, "branch": 0.65})
        self.assertEqual(cfg.coverage_delta, {"line": 0.80, "branch": 0.80})

    def test_partial_table_is_completed_key_by_key(self):
        cfg = self.config({"coverage": {"floor": {"branch": 0.7}, "delta": {"line": 0.9}}})
        self.assertEqual(cfg.coverage_floor, {"line": 0.80, "branch": 0.7})
        self.assertEqual(cfg.coverage_delta, {"line": 0.9, "branch": 0.80})

    def test_formats(self):
        self.assertEqual((self.config({}).coverage_format, self.config({}).tests_format), ("jacoco", "junit"))
        cfg = self.config({"coverage": {"format": "jacoco"}, "tests": {"format": "junit"}})
        self.assertEqual((cfg.coverage_format, cfg.tests_format), ("jacoco", "junit"))

    def test_policy_text_names_the_defaults_of_the_code(self):
        text = (REPO_ROOT / "POLICY.md").read_bytes().decode("ascii")
        point = text.split("## 4. ", 1)[1].split("\n## ", 1)[0]
        found = re.findall(r"(\d+)% line and (\d+)% branch coverage", point)
        expected = [(DEFAULT_FLOOR_LINE, DEFAULT_FLOOR_BRANCH), (DEFAULT_DELTA_LINE, DEFAULT_DELTA_BRANCH)]
        self.assertEqual(found, [(f"{round(a * 100)}", f"{round(b * 100)}") for a, b in expected])
        self.assertIn("By default the floor is", point)


class ArgumentTest(CoverageCase):
    def test_option_shaped_or_empty_base_is_a_usage_error(self):
        for argv, problem in (
            (["--base=-x"], "the ref '-x' starts with -"),
            (["--base=--all"], "the ref '--all' starts with -"),
            (["--base="], "the ref '' is empty"),
            (["--base", "-x"], None),
        ):
            with self.subTest(argv=argv):
                code, env = self.json_main("coverage", *argv)
                self.assertEqual((code, env["error"]["kind"], env["base"]), (2, "usage", None))
                if problem:
                    self.assertIn(problem, env["error"]["message"])

    def test_nul_in_the_base_is_a_usage_error(self):
        code, env = self.json_main("coverage", "--base", "main" + chr(0))
        self.assertEqual(code, 2)
        self.assertIn("has a NUL", env["error"]["message"])

    def test_base_with_a_diff_file_is_a_usage_error(self):
        (self.work / "change.diff").write_bytes(b"")
        code, env = self.json_main("coverage", "--base", "main", "--diff-file", "change.diff")
        self.assertEqual(code, 2)
        self.assertEqual(env["error"], {"kind": "usage", "message": "--base and --diff-file cannot be used together"})

    def test_bad_today_is_a_usage_error(self):
        for value in ("2026-5-19", "2026-02-30", "20260519", "2026-05-19T00:00", "", "2026-05-1" + chr(0x0661)):
            with self.subTest(value=value):
                code, env = self.json_main("coverage", f"--today={value}")
                self.assertEqual((code, env["error"]["kind"]), (2, "usage"))

    def test_empty_diff_file_is_a_usage_error(self):
        code, env = self.json_main("coverage", "--diff-file=")
        self.assertEqual((code, env["error"]["kind"]), (2, "usage"))
        self.assertIn("the file name is empty", env["error"]["message"])

    def test_diff_file_that_cannot_be_read_is_a_usage_error(self):
        self.measure()
        code, env = self.json_main("coverage", "--diff-file", "missing.diff")
        self.assertEqual((code, env["error"]["kind"]), (2, "usage"))
        self.assertTrue(env["error"]["message"].startswith("cannot read --diff-file missing.diff: "))

    def test_hidden_options_are_not_in_the_help(self):
        code, out, _ = self.main("coverage", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--base", out)
        for option in ("--diff-file", "--no-freshness", "--today"):
            self.assertNotIn(option, out)

    def test_today_is_in_the_data_only_when_given(self):
        self.measure()
        self.assertNotIn("today", self.passed().data)
        run = self.coverage("--today", "2026-05-25")
        self.assertEqual(run.data["today"], "2026-05-25")
        self.assertEqual(run.data["exemptions"]["expiring"], [TOKEN])  # 5 days ahead of the given day


class RequiredKeyTest(CoverageCase):
    def assert_missing(self, key: str, *options: str) -> None:
        code, env = self.json_main("coverage", *options)
        self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": f"muster.toml: missing key {key}"}))

    def test_policy_keys(self):
        self.write("muster.toml", PROFILE.replace('critical = "policy/critical.txt"\n', ""))
        self.assert_missing("zones.critical")

    def test_report_key_versus_missing_report_file(self):
        self.measure()
        (self.repo / REPORT).unlink()
        self.stale(self.coverage(), f"no JaCoCo XML at {REPORT}")
        self.write("muster.toml", PROFILE.replace('[coverage]\nreport = "build/jacoco/report.xml"\n', ""))
        self.assert_missing("coverage.report")
        self.assert_missing("coverage.report", "--no-freshness")

    def test_snapshot_key_unless_freshness_is_skipped(self):
        self.measure()
        self.write("muster.toml", PROFILE.replace('[policy]\nsnapshot = "build/muster/snapshot.json"\n', ""))
        self.assert_missing("policy.snapshot")
        self.passed("--no-freshness")

    def test_results_key_only_for_full_run_suites(self):
        self.measure()
        self.write("muster.toml", PROFILE.replace('[tests]\nresults = "build/test-results/TEST-*.xml"\n', ""))
        self.passed()
        self.write("muster.toml", PROFILE.replace('[tests]\nresults = "build/test-results/TEST-*.xml"\n', FULL_RUN))
        self.assert_missing("tests.results")
        self.passed("--no-freshness")


class PolicyGateTest(CoverageCase):
    """An invalid policy exits 2 in coverage as in policy: with or without a report, and with
    --no-freshness."""

    def assert_refused(self) -> None:
        self.assertEqual(self.main("policy")[0], 2)
        for options in ([], ["--no-freshness"], ["--strict"]):
            with self.subTest(options=options):
                run = self.coverage(*options)
                self.assertEqual((run.code, run.env["error"]["kind"]), (2, "policy"))
                self.assertEqual(run.env["error"]["message"], "the policy is invalid: 1 violation")
                self.assertEqual(run.section["verdict_text"], "coverage: NOT EVALUATED (invalid policy: 1 violation)")
                self.assertEqual(run.section["verdict"], "error")
                heading = "## Coverage\n\n**NOT EVALUATED**: invalid policy, 1 violation.\n"
                self.assertTrue(run.summary.startswith(heading), run.summary)
                self.assertTrue(run.annotations[0].startswith("::error title=Invalid policy::"), run.out)
        (self.repo / REPORT).unlink()
        for options in ([], ["--no-freshness"]):
            with self.subTest(options=options, report=False):
                self.assertEqual(self.coverage(*options).code, 2)

    def test_dead_glob(self):
        self.measure()
        self.write("policy/critical.txt", CRITICAL + "gone/**\n")
        self.assert_refused()

    def test_bad_mutation_target(self):
        self.measure()
        self.write("muster.toml", PROFILE + MUTATION)
        self.write("policy/mutation.txt", "queue/Dispatcher.java\n")
        self.assert_refused()

    def test_expired_exemption(self):
        self.measure()
        self.write("policy/exemptions.txt", "auth/Token.java | rename only | 2026-05-18\n")
        self.assert_refused()

    def test_today_option_judges_the_exemptions(self):
        self.measure()
        self.assertEqual(self.coverage("--today", "2026-05-31").code, 2)


class SnapshotFreshnessTest(CoverageCase):
    def test_fresh_run_is_evaluated(self):
        self.measure()
        run = self.passed()
        self.assertEqual((run.data["freshness"], run.section["verdict"]), ("checked", "pass"))
        self.assertEqual(run.annotations, [])

    def test_missing_snapshot(self):
        self.measure()
        (self.repo / SNAPSHOT).unlink()
        self.stale(self.coverage(), NO_SNAPSHOT)

    def test_missing_report_and_snapshot_give_both_reasons(self):
        self.measure()
        (self.repo / SNAPSHOT).unlink()
        (self.repo / REPORT).unlink()
        self.stale(self.coverage(), f"no JaCoCo XML at {REPORT}", NO_SNAPSHOT)

    def test_policy_changed_after_the_snapshot(self):
        self.measure()
        self.write("policy/exemptions.txt", EXEMPTIONS + "# reviewed\n")
        self.stale(self.coverage(), "the policy changed after the snapshot (policy_sha256 differs)")

    def test_profile_table_changed_after_the_snapshot(self):
        self.measure()
        profile = PROFILE.replace('exclude = ["**/package-info.java"]', 'exclude = ["**/package-*.java"]')
        self.write("muster.toml", profile)
        self.stale(self.coverage(), "the policy changed after the snapshot (policy_sha256 differs)")

    def test_source_added_after_the_snapshot(self):
        self.measure()
        self.write(ROUTER, source_text())
        self.stale(self.coverage(), f"the source set changed after the snapshot: 1 added (e.g. {ROUTER})")

    def test_source_gone_after_the_snapshot(self):
        self.write(ROUTER, source_text())
        self.measure()
        (self.repo / ROUTER).unlink()
        self.stale(self.coverage(), f"the source set changed after the snapshot: 1 gone (e.g. {ROUTER})")

    def test_sources_added_and_gone(self):
        self.write(ROUTER, source_text())
        self.measure()
        (self.repo / ROUTER).unlink()
        self.write(f"{BASE}/queue/Relay.java", source_text())
        self.write(f"{BASE}/queue/Broker.java", source_text())
        reason = (
            f"the source set changed after the snapshot: 2 added (e.g. {BASE}/queue/Broker.java),"
            f" 1 gone (e.g. {ROUTER})"
        )
        self.stale(self.coverage(), reason)

    def test_report_name_under_no_source_root(self):
        self.measure({f"{BASE}/report/Gone.java": lines_of(1)})
        reason = "1 JaCoCo source file(s) under no source root (e.g. org/example/app/report/Gone.java)"
        self.stale(self.coverage(), reason)

    def test_unresolved_names_are_counted_without_freshness(self):
        self.measure({f"{BASE}/report/Gone.java": lines_of(1, data=MISSED)})
        run = self.passed("--no-freshness")
        self.assertEqual(run.data["unresolved"], {"count": 1, "names": ["org/example/app/report/Gone.java"]})
        self.assertEqual(run.data["global"]["files"], 4)

    def test_report_without_sessions(self):
        self.measure(sessions=[])
        self.stale(self.coverage(), "the JaCoCo XML has no sessioninfo")

    def test_snapshot_newer_than_the_run(self):
        self.measure(sessions=[(-1001, 60_000)])
        self.stale(self.coverage(), STALE_RUN)

    def test_snapshot_up_to_a_second_after_the_run_start(self):
        self.measure(sessions=[(-1000, 60_000)])
        self.passed()

    def test_earliest_session_counts(self):
        self.measure(sessions=[(500, 2000), (-900, 60_000)])
        self.passed()
        self.measure(sessions=[(500, 70_000), (-1500, 2000)])
        self.stale(self.coverage(), STALE_RUN)

    def test_second_snapshot_without_change_keeps_the_run_fresh(self):
        self.measure()
        before = (self.repo / SNAPSHOT).read_bytes()
        time.sleep(0.01)
        self.assertEqual(self.main("policy", "--snapshot")[0], 0)
        self.assertEqual((self.repo / SNAPSHOT).read_bytes(), before)
        self.passed()

    def test_not_evaluated_is_a_violation_under_strict(self):
        self.measure(sessions=[(-5000, 60_000)])
        run = self.coverage("--strict")
        self.assertEqual((run.code, run.section["verdict"], run.section["violation"]), (1, "not-evaluated", True))
        self.assertEqual(run.section["violations"], [STALE_RUN])
        self.assertEqual(run.annotations, [f"::warning title=Coverage::coverage not evaluated: {STALE_RUN}"])

    def test_skipped_freshness(self):
        self.measure(sessions=[(-5000, 60_000)])
        (self.repo / SNAPSHOT).unlink()
        self.write(ROUTER, source_text())
        run = self.passed("--no-freshness")
        self.assertEqual(run.data["freshness"], "skipped")
        self.assertTrue(run.verdict.endswith("; freshness not checked)"), run.verdict)

    def test_skipped_freshness_is_said_when_not_evaluated(self):
        self.measure()
        (self.repo / REPORT).unlink()
        run = self.coverage("--no-freshness")
        self.assertEqual(run.data["reasons"], [f"no JaCoCo XML at {REPORT}"])
        self.assertEqual(run.verdict, f"coverage: NOT EVALUATED (no JaCoCo XML at {REPORT}; freshness not checked)")

    def test_report_without_sourcefile(self):
        self.measure({}, only=True)
        self.stale(self.coverage(), "the JaCoCo XML has no <sourcefile>")

    def test_report_without_a_classified_file(self):
        self.measure({PACKAGE_INFO: lines_of(1)}, only=True)
        self.stale(self.coverage("--no-freshness"), "no JaCoCo source file is in the classification set")


class FullRunSuiteTest(CoverageCase):
    def setUp(self):
        super().setUp()
        self.write("muster.toml", PROFILE + FULL_RUN)

    def test_suite_inside_the_run(self):
        self.measure()
        self.passed()

    def test_suite_missing(self):
        self.measure(suites=[])
        self.stale(self.coverage(), SKIPPED_SUITE)

    def test_suite_renamed(self):
        self.measure(suites=[("org.example.app.codec.KeyParserSpec", 1000, 2.0)])
        self.stale(self.coverage(), SKIPPED_SUITE)

    def test_suite_after_the_last_dump(self):
        self.measure(suites=[(SUITE, 59_000, 2.001)])
        self.stale(self.coverage(), SKIPPED_SUITE)
        self.measure(suites=[(SUITE, 59_000, 2.0)])  # ends exactly a second after the dump
        self.passed()

    def test_suite_before_the_first_session(self):
        self.measure(suites=[(SUITE, -1001, 0.5)])
        self.stale(self.coverage(), SKIPPED_SUITE)
        self.measure(suites=[(SUITE, -900, 0.5)])
        self.passed()

    def test_window_spans_every_session(self):
        self.measure(sessions=[(100, 2000), (5000, 60_000)], suites=[(SUITE, 30_000, 2.0)])
        self.passed()

    def test_suite_inside_a_wrapper(self):
        self.measure(suites=[(SUITE, 1000, 2.0, True)])
        self.passed()


class FloorTest(CoverageCase):
    def test_zone_table_and_no_delta(self):
        self.measure()
        run = self.passed()
        self.assertNotIn("delta", run.data)
        self.assertNotIn("Critical delta", run.summary)
        self.assertIn("| Z1 | 1 | 100.0% | 10/10 | n/a | 0/0 |", run.summary)
        self.assertIn("| global | 4 | 100.0% | 130/130 | 100.0% | 200/200 |", run.summary)
        self.assertIn("no delta (no --base)", run.verdict)
        self.assertEqual(
            run.verdict, "coverage: PASS (global line 100.0% branch 100.0%, floor ok; no delta (no --base))"
        )
        self.assertEqual(list(run.data["zones"]), ["Z1", "Z2"])
        self.assertEqual(run.data["critical"], {"files": 2, "line": [0, 20], "branch": [0, 0]})
        self.assertEqual(run.data["standard"], {"files": 2, "line": [0, 110], "branch": [0, 200]})
        self.assertTrue(run.summary.startswith("## Coverage\n\n**Global floor** (80% line / 65% branch): line"))

    def test_zones_sort_by_number(self):
        self.write("policy/critical.txt", "# Zone 2: money.\nbilling/**\n# Zone 10: access.\nauth/**\n")
        self.measure()
        run = self.passed()
        self.assertEqual(list(run.data["zones"]), ["Z2", "Z10"])
        rows = [line.split(" | ")[0] for line in run.summary.splitlines() if line.startswith("| ")]
        self.assertEqual(rows[1:], ["| Z2", "| Z10", "| critical (all zones)", "| standard", "| global"])

    def test_below_the_floor_warns_and_fails_only_under_strict(self):
        self.measure({ENGINE: lines_of(*range(1, 11), data=MISSED)}, only=True)
        run = self.coverage()
        self.assertEqual(run.code, 0)
        message = "global coverage below the floor: line 0.0% < 80%"
        self.assertEqual(run.annotations, [f"::warning title=Coverage floor::{message}"])
        state = (run.data["outcome"], run.data["floor_ok"], run.section["verdict"])
        self.assertEqual(state, ("violation", False, "warn"))
        self.assertIn("coverage: WARN (global line 0.0% branch n/a, BELOW FLOOR;", run.verdict)
        self.assertIn("- below the floor", run.summary)
        run = self.coverage("--strict")
        self.assertEqual((run.code, run.section["verdict"]), (1, "fail"))
        self.assertEqual(run.annotations, [f"::error title=Coverage floor::{message}"])
        self.assertIn("coverage: FAIL", run.verdict)

    def test_floor_holds_at_the_threshold(self):
        engine = {**lines_of(*range(1, 8)), 8: MISSED, 9: MISSED}
        self.measure({ENGINE: engine, TOKEN: {1: (0, 1, 7, 13)}}, only=True)
        run = self.passed()
        self.assertEqual((run.data["global"]["line"], run.data["global"]["branch"]), ([2, 8], [7, 13]))
        self.assertIn("- pass", run.summary)

    def test_branch_below_the_floor(self):
        self.measure({TOKEN: lines_of(1, data=(0, 1, 2, 1))}, only=True)
        run = self.coverage()
        message = "global coverage below the floor: branch 33.3% < 65%"
        self.assertEqual(run.annotations, [f"::warning title=Coverage floor::{message}"])

    def test_floor_uses_the_counters_and_the_delta_uses_the_lines(self):
        self.edit(ENGINE, {3: "    int changed;"})
        self.measure(counters={ENGINE: ((500, 0), (0, 0))})
        run = self.coverage("--base", "main")
        self.assertEqual(run.data["global"]["line"], [500, 120])
        self.assertFalse(run.data["floor_ok"])
        self.assertEqual((run.data["delta"]["line"], run.data["delta"]["fails"]), ([0, 1], []))

    def test_excluded_file_is_counted_and_not_measured(self):
        self.measure({PACKAGE_INFO: lines_of(1, data=MISSED)})
        run = self.passed()
        self.assertEqual(run.data["excluded"], {"count": 1, "paths": [PACKAGE_INFO]})
        self.assertEqual(run.data["global"]["files"], 4)
        self.assertEqual(run.data["unresolved"], {"count": 0, "names": []})

    def test_thresholds_of_the_profile(self):
        thresholds = (
            '[coverage]\nreport = "build/jacoco/report.xml"\nfloor = { line = 0.825 }\ndelta = { branch = 0.5 }\n'
        )
        self.write("muster.toml", PROFILE.replace('[coverage]\nreport = "build/jacoco/report.xml"\n', thresholds))
        self.edit(ENGINE, {3: "    int changed;"})
        self.measure()
        run = self.passed("--base", "main")
        expected = {"floor": {"line": 0.825, "branch": 0.65}, "delta": {"line": 0.8, "branch": 0.5}}
        self.assertEqual(run.data["thresholds"], expected)
        self.assertIn("**Global floor** (82.5% line / 65% branch)", run.summary)
        self.assertIn("Gate: 80% line / 50% branch over changed lines of critical files", run.summary)

    def test_default_thresholds_in_the_data(self):
        self.measure()
        expected = {"floor": {"line": 0.8, "branch": 0.65}, "delta": {"line": 0.8, "branch": 0.8}}
        self.assertEqual(self.passed().data["thresholds"], expected)


class ExemptionWindowTest(CoverageCase):
    def changed_engine(self) -> None:
        self.edit(ENGINE, {3: "    int changed;"})

    def test_exempt_file_is_out_of_the_gate(self):
        self.exempt_engine(TODAY + timedelta(days=20))
        self.changed_engine()
        self.measure({ENGINE: lines_of(*range(1, 11), data=MISSED)})
        run = self.coverage("--base", "main")
        delta = run.data["delta"]
        self.assertEqual((delta["exempt"], delta["fails"], delta["files"]), ([ENGINE], [], []))
        self.assertEqual(run.annotations, [])
        self.assertIn(f"Exempt changed critical file(s): `{ENGINE}` (until 2026-06-08)", run.summary)

    def test_exemption_holds_through_its_last_day(self):
        self.exempt_engine(TODAY)
        self.changed_engine()
        self.measure()
        run = self.coverage("--base", "main")
        self.assertEqual(run.data["delta"]["exempt"], [ENGINE])
        self.assertEqual(run.data["exemptions"], {"active": [TOKEN, ENGINE], "expired": [], "expiring": [ENGINE]})

    def test_exemption_ending_within_seven_days_warns(self):
        self.exempt_engine(TODAY + timedelta(days=7))
        self.measure()
        run = self.coverage()
        self.assertEqual(run.code, 0)
        message = f"exemption(s) expiring within 7 days: {ENGINE} (until 2026-05-26)"
        self.assertEqual(run.annotations, [f"::warning title=Coverage exemptions::{message}"])
        self.assertEqual((run.section["verdict"], run.section["violation"]), ("warn", False))
        self.assertEqual(self.coverage("--strict").code, 0)

    def test_exemption_eight_days_ahead_is_quiet(self):
        self.exempt_engine(TODAY + timedelta(days=8))
        self.measure()
        run = self.passed()
        self.assertEqual((run.data["exemptions"]["expiring"], run.annotations), ([], []))

    def test_exemptions_in_the_summary(self):
        self.measure()
        run = self.passed()
        section = f"**Coverage exemptions** (valid through the date, UTC):\n\n- `{TOKEN}` until 2026-05-30\n"
        self.assertIn(section, run.summary)
        self.assertEqual(run.data["exemptions"], {"active": [TOKEN], "expired": [], "expiring": []})


class DeltaGateTest(CoverageCase):
    def test_changed_lines_below_the_gate_warn_once(self):
        self.edit(ENGINE, {3: "    int three;", 4: "    int four;"})
        engine = {**lines_of(*range(1, 11)), 3: (1, 4, 1, 1), 4: (3, 0, 0, 0), 9: (0, 2, 0, 0)}
        self.measure({ENGINE: engine})
        run = self.coverage("--base", "main")
        delta = run.data["delta"]
        self.assertEqual((run.code, delta["line"], delta["branch"], len(delta["fails"])), (0, [1, 1], [1, 1], 2))
        missed, partial = "L" + str(4), "L" + str(3) + " (1/2 branches missed)"  # line 4 missed, line 3 partly
        self.assertEqual(delta["uncovered"], {ENGINE: [missed, partial]})
        row = {"path": ENGINE, "zone": "Z1", "changed": 2, "line": [1, 1], "branch": [1, 1], "note": ""}
        self.assertEqual(delta["files"], [row])
        message = "changed critical lines below the gate: line 50.0% (1/2) < 80%; branch 50.0% (1/2) < 80% (vs main)"
        self.assertEqual(run.annotations, [f"::warning title=Critical delta coverage::{message}"])
        self.assertIn("coverage: WARN", run.verdict)
        self.assertIn("critical delta WOULD FAIL: line 50.0% (1/2) < 80%; branch 50.0% (1/2) < 80%", run.verdict)
        self.assertIn("would fail", run.summary)
        self.assertIn(f"- `{ENGINE}`: {missed}, {partial}", run.summary)
        head = self.git("merge-base", "main", "HEAD")
        self.assertIn(f"### Critical delta vs `main` (merge-base `{head[:10]}`)", run.summary)
        self.assertEqual((delta["source"], delta["unclassified"]), ("git", []))

    def test_change_without_branches_is_measured_by_line(self):
        self.edit(ENGINE, {nr: f"    int edited{nr};" for nr in range(1, 6)})
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 5: MISSED}})
        run = self.passed("--base", "main")
        delta = run.data["delta"]
        self.assertEqual((delta["line"], delta["branch"], delta["fails"]), ([1, 4], [0, 0], []))
        self.assertEqual(run.annotations, [])
        self.assertIn("coverage: PASS", run.verdict)
        self.assertIn("critical delta line 80.0% (4/5), branch n/a (0/0)", run.verdict)

    def test_change_of_lines_without_coverage_data_is_not_applicable(self):
        self.edit(ENGINE, {2: "    // a comment"})
        self.measure({ENGINE: {nr: COVERED for nr in range(1, 11) if nr != 2}})
        run = self.passed("--base", "main")
        self.assertEqual(run.data["delta"]["fails"], [])
        self.assertIn("Result: n/a (no coverable changed critical lines) - pass.", run.summary)
        self.assertIn("n/a, no coverable changed lines", run.verdict)
        self.assertEqual(run.annotations, [])

    def test_changed_critical_file_without_coverage_data_is_named(self):
        self.edit(ENGINE, {3: "    int changed;"})
        self.measure(drop=(ENGINE,))
        run = self.coverage("--base", "main")
        self.assertEqual((run.code, run.data["delta"]["no_data"]), (0, [ENGINE]))
        message = f"no coverage data for 1 changed critical file(s): {ENGINE}"
        self.assertEqual(run.annotations, [f"::warning title=Critical delta coverage::{message}"])
        self.assertIn("**No coverage data** for changed critical file(s)", run.summary)
        self.assertIn(f"- `{ENGINE}` (Z1)", run.summary)
        self.assertEqual((run.section["verdict"], run.section["violation"]), ("warn", False))
        self.assertIn("critical delta n/a, no coverable changed lines)", run.verdict)
        self.assertIn("Result: n/a (no coverable changed critical lines) - pass.", run.summary)
        self.assertNotIn("No changed critical lines with coverage data", run.summary)

    def test_untracked_critical_file_is_wholly_added(self):
        self.write(JOURNAL, source_text(3))
        self.measure({JOURNAL: {1: COVERED, 2: MISSED}})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual(delta["untracked"], [JOURNAL])
        row = {"path": JOURNAL, "zone": "Z1", "changed": 3, "line": [1, 1], "branch": [0, 0], "note": "untracked (new)"}
        self.assertEqual(delta["files"], [row])

    def test_untracked_file_has_a_line_per_line_feed(self):
        text = "int one;" + chr(0x0C) + "int two;\nint three;" + chr(0x2028) + "int four;\n"
        text += "int five;\rint six;\nint seven;"
        self.write(JOURNAL, text)
        self.measure({JOURNAL: {1: COVERED, 2: COVERED}})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual((delta["files"][0]["changed"], delta["line"]), (4, [0, 2]))

    def test_untracked_file_outside_the_classification_set_is_left_out(self):
        self.write(f"{BASE}/billing/notes.txt", "draft\n")
        self.write(f"{BASE}/billing/package-info.java", "package org.example.app.billing;\n")
        self.measure()
        delta = self.passed("--base", "main").data["delta"]
        self.assertEqual((delta["untracked"], delta["files"]), ([], []))

    def test_changed_standard_file_stays_out_of_the_gate(self):
        self.edit(PARSER, {1: "    int one;", 2: "    int two;"})
        self.measure({PARSER: lines_of(*range(1, 11), data=MISSED)})
        run = self.passed("--base", "main")
        self.assertEqual((run.data["delta"]["standard_files"], run.data["delta"]["files"]), (1, []))
        self.assertEqual(run.annotations, [])
        self.assertIn("1 changed standard file(s): outside the delta gate.", run.summary)

    def test_base_without_changes(self):
        self.measure()
        run = self.passed("--base", "main")
        self.assertEqual(run.data["delta"]["files"], [])
        self.assertIn("critical delta no critical changes", run.verdict)
        self.assertIn("No changed critical lines with coverage data - pass.", run.summary)
        self.assertEqual(run.annotations, [])

    def test_unknown_base_without_strict(self):
        self.measure()
        run = self.coverage("--base", "no-such-ref")
        self.assertEqual((run.code, run.section["verdict"], run.section["violation"]), (0, "not-evaluated", False))
        self.assertEqual(run.data["delta"], {"evaluated": False})
        self.assertEqual(run.data["outcome"], "incomplete")
        message = "no merge-base with no-such-ref; delta not evaluated"
        self.assertEqual(run.annotations, [f"::warning title=Critical delta coverage::{message}"])
        self.assertIn("coverage: INCOMPLETE", run.verdict)
        self.assertNotIn("PASS", run.verdict)
        self.assertIn("delta not evaluated (no merge-base)", run.verdict)
        self.assertIn("**Critical delta:** no merge-base with `no-such-ref` - not evaluated.", run.summary)
        self.assertEqual(run.env["base"], {"ref": "no-such-ref", "commit": None})
        self.assertEqual(run.section["violations"], [])

    def test_unknown_base_under_strict(self):
        self.measure()
        run = self.coverage("--base", "no-such-ref", "--strict")
        self.assertEqual((run.code, run.section["verdict"], run.section["violation"]), (1, "not-evaluated", True))
        self.assertEqual(run.data["delta"], {"evaluated": False})
        self.assertIn("coverage: FAIL", run.verdict)
        self.assertEqual(run.env["verdict"], "not-evaluated")
        self.assertEqual(run.section["violations"], ["no merge-base with no-such-ref; delta not evaluated"])

    def test_diff_file_keeps_a_lone_carriage_return_in_its_line(self):
        diff = (
            f"diff --git a/{ENGINE} b/{ENGINE}\nindex 1111111..2222222 100644\n--- a/{ENGINE}\n+++ b/{ENGINE}\n"
            "@@ -3,0 +4,3 @@\n+    int four;\n+    int five;\r    int still_five;\n+    int six;\n"
        )
        (self.work / "change.diff").write_bytes(diff.encode("ascii"))
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 6: MISSED}})
        run = self.coverage("--diff-file", "change.diff")
        delta = run.data["delta"]
        self.assertEqual((delta["files"][0]["changed"], delta["line"], delta["source"]), (3, [1, 2], "diff-file"))
        self.assertIn("### Critical delta vs diff file `change.diff`", run.summary)
        self.assertIsNone(run.env["base"])

    def test_diff_file_failing_the_gate_names_it(self):
        diff = f"--- a/{ENGINE}\n+++ b/{ENGINE}\n@@ -1 +1 @@\n-    int field1;\n+    int one;\n"
        (self.work / "change.diff").write_bytes(diff.encode("ascii"))
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 1: MISSED}})
        run = self.coverage("--diff-file", "change.diff")
        self.assertTrue(run.annotations[0].endswith("< 80% (vs diff file)"), run.annotations)

    def test_added_line_that_looks_like_a_file_header(self):
        self.edit(ENGINE, {3: "++ x", 8: "    int eight;"})
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 8: MISSED}})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual((delta["files"][0]["changed"], delta["line"]), (2, [1, 1]))

    def test_java_marked_binary_stays_in_the_delta(self):
        self.write(".gitattributes", "*.java -diff\n")
        self.edit(ENGINE, {3: "    int three;", 8: "    int eight;"})
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 8: MISSED}})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual((delta["files"][0]["changed"], delta["line"]), (2, [1, 1]))

    def test_nul_byte_in_a_change_stays_in_the_delta(self):
        self.edit(ENGINE, {3: "    int nul" + chr(0) + ";", 8: "    int eight;"})
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 8: MISSED}})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual((delta["files"][0]["changed"], delta["line"]), (2, [1, 1]))

    def test_uncovered_lines_are_capped(self):
        count = 45
        self.write(JOURNAL, source_text(2 * count))
        self.measure({JOURNAL: {nr: MISSED if nr % 2 else COVERED for nr in range(1, 2 * count + 1)}})
        run = self.coverage("--base", "main")
        items = run.data["delta"]["uncovered"][JOURNAL]
        self.assertEqual(len(items), count)
        self.assertIn(", ... (+5 more)", run.summary)


class GitFailureTest(CoverageCase):
    def failed_run(self, *options: str) -> Run:
        self.edit(ENGINE, {3: "    int changed;"})
        self.measure()
        failure = ReportError("git diff failed: boom")
        with mock.patch("muster.coverage.diff_since", side_effect=failure):
            return self.coverage("--base", "main", *options)

    def test_git_error_keeps_the_floor_and_the_zones(self):
        run = self.failed_run()
        self.assertEqual(run.code, 0)
        self.assertIn("Global floor", run.summary)
        self.assertIn("| Z1 | 1 |", run.summary)
        self.assertIn("**Critical delta:** delta not evaluated: git diff failed: boom", run.summary)
        self.assertEqual(run.data["delta"], {"evaluated": False})
        message = "delta not evaluated: git diff failed: boom"
        self.assertEqual(run.annotations, [f"::warning title=Critical delta coverage::{message}"])
        self.assertIn("coverage: INCOMPLETE", run.verdict)
        self.assertIn("delta not evaluated (git error)", run.verdict)
        self.assertNotIn("PASS", run.verdict)
        self.assertEqual(run.section["verdict"], "not-evaluated")
        self.assertEqual(run.env["base"], {"ref": "main", "commit": self.git("merge-base", "main", "HEAD")})

    def test_git_error_under_strict(self):
        run = self.failed_run("--strict")
        self.assertEqual((run.code, run.section["verdict"], run.section["violation"]), (1, "not-evaluated", True))
        self.assertIn("coverage: FAIL", run.verdict)
        self.assertEqual(run.section["violations"], ["delta not evaluated: git diff failed: boom"])

    def test_git_that_cannot_run(self):
        self.edit(ENGINE, {3: "    int changed;"})
        self.measure()
        with mock.patch("muster.gitio.subprocess.run", side_effect=FileNotFoundError("git")):
            run = self.coverage("--base", "main")
        self.assertEqual(run.code, 0)
        self.assertEqual(run.env["base"], {"ref": "main", "commit": None})
        self.assertEqual(run.data["delta"], {"evaluated": False})


class SubfolderProfileTest(CoverageCase):
    SUBFOLDER = "service"

    def test_changed_critical_line_is_seen(self):
        self.edit(ENGINE, {3: "    int changed;"})
        self.write(JOURNAL, source_text(2))
        self.measure({ENGINE: {**lines_of(*range(1, 11)), 3: MISSED}, JOURNAL: lines_of(1, 2)})
        delta = self.coverage("--base", "main").data["delta"]
        self.assertEqual([item["path"] for item in delta["files"]], [ENGINE, JOURNAL])
        self.assertEqual((delta["files"][0]["line"], delta["untracked"]), ([1, 0], [JOURNAL]))
        self.assertEqual(len(delta["fails"]), 1)


class InputErrorTest(CoverageCase):
    """A report, a JUnit file or a snapshot that cannot be read: errors, no verdict; exit 0, and 1
    with --strict or --fail-on-error, never 3."""

    JACOCO = {
        "non-finite number": ('mi="0" ci="3"', 'mi="NaN" ci="3"'),
        "wrong type": ('mi="0" ci="3"', 'mi="0" ci="1.5"'),
        "boolean": ('mi="0" ci="3"', 'mi="0" ci="true"'),
        "missing attribute": ('mi="0" ci="3"', 'mi="0"'),
        "negative value": ('mi="0" ci="3"', 'mi="-1" ci="3"'),
        "unparseable": ("</report>", ""),
    }
    JUNIT = {
        "non-finite number": ('time="2.0">', 'time="inf">'),
        "wrong type": ('time="2.0">', 'time="2.0s">'),
        "boolean": ('time="2.0">', 'time="true">'),
        "unparseable timestamp": ('timestamp="', 'timestamp="T'),  # a suite without one is skipped
        "negative value": ('time="2.0">', 'time="-1">'),
        "unparseable": ("</testsuite>", ""),
    }
    SNAPSHOT = {
        "non-finite number": ('"created_epoch_ms": ', '"created_epoch_ms": NaN, "was": '),
        "wrong type": ('"schema": 1', '"schema": "1"'),
        "boolean": ('"schema": 1', '"schema": true'),
        "missing attribute": ('"sources": [', '"source_list": ['),
        "negative value": ('"created_epoch_ms": ', '"created_epoch_ms": -1, "was": '),
        "unparseable": ("{", "["),
    }

    def assert_errored(self, prefix: str) -> None:
        for options, expected in (([], 0), (["--strict"], 1), (["--fail-on-error"], 1)):
            run = self.coverage(*options)
            self.assertEqual(run.code, expected, run.out + run.err)
            self.assertEqual((run.section["verdict"], run.data["outcome"], run.env["error"]), ("error", "error", None))
            self.assertTrue(run.section["errors"][0].startswith(prefix), run.section["errors"])
            self.assertEqual(run.verdict, f"coverage: ERROR ({run.section['errors'][0]})")
            self.assertNotIn("Global floor", run.summary)
            self.assertNotIn("| scope |", run.summary)
            level = "error" if options else "warning"
            self.assertTrue(run.annotations[0].startswith(f"::{level} title=Coverage::"), run.annotations)

    def test_broken_jacoco_report(self):
        for case, (old, new) in self.JACOCO.items():
            with self.subTest(case=case):
                self.measure()
                self.replace(REPORT, old, new)
                self.assert_errored(f"{REPORT}: ")

    def test_broken_junit_file(self):
        self.write("muster.toml", PROFILE + FULL_RUN)
        for case, (old, new) in self.JUNIT.items():
            with self.subTest(case=case):
                self.measure()
                self.replace(f"{RESULTS}/TEST-{SUITE}.xml", old, new)
                self.assert_errored(f"{RESULTS}/TEST-{SUITE}.xml: ")

    def test_broken_snapshot(self):
        for case, (old, new) in self.SNAPSHOT.items():
            with self.subTest(case=case):
                self.measure()
                self.replace(SNAPSHOT, old, new)
                self.assert_errored(f"{SNAPSHOT}: ")

    def test_every_error_is_listed(self):
        self.measure()
        self.replace(REPORT, "</report>", "")
        self.replace(SNAPSHOT, "{", "[")
        run = self.coverage()
        errors = run.section["errors"]
        self.assertEqual([error.split(":")[0] for error in errors], [REPORT, SNAPSHOT])
        self.assertIn("**ERROR**: the inputs could not be read, no verdict:", run.summary)
        for error in errors:
            self.assertIn(f"- {error}", run.summary)
        self.assertEqual(len(run.annotations), 2)

    def test_junit_files_are_read_only_for_full_run_suites(self):
        self.measure()
        self.replace(f"{RESULTS}/TEST-{SUITE}.xml", "</testsuite>", "")
        self.passed()

    def test_name_under_two_roots_is_a_config_error(self):
        profile = PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "gen/main/java"]')
        profile = profile.replace('exclude = ["**/package-info.java"]', 'exclude = ["**/package-info.java", "gen/**"]')
        self.write("muster.toml", profile)
        self.write("gen/main/java/org/example/app/auth/Token.java", source_text())
        self.measure()
        run = self.coverage()
        self.assertEqual((run.code, run.env["error"]["kind"], run.env["sections"]), (2, "config", []))
        message = (
            f"{REPORT}: org/example/app/auth/Token.java is under more than one source root:"
            " gen/main/java/org/example/app/auth/Token.java, src/main/java/org/example/app/auth/Token.java"
        )
        self.assertEqual(run.env["error"]["message"], message)


class ProtectedFileTest(CoverageCase):
    """An output that is a source, test, JUnit, diff, report or snapshot file: exit 2, nothing is
    removed or written."""

    def assert_refused(self, target: Path, label: str, *options: str) -> None:
        before = target.read_bytes()
        code, env = self.json_main("coverage", "--json-out", str(target), *options)
        self.assertEqual((code, env["error"]["kind"]), (2, "usage"))
        self.assertEqual(env["error"]["message"], f"--json-out {target} is {label}")
        self.assertEqual(target.read_bytes(), before)

    def test_source_file(self):
        self.measure()
        self.assert_refused(self.repo / ENGINE, f"the source file {ENGINE}")

    def test_test_file(self):
        self.measure()
        self.assert_refused(self.repo / PARSER_TEST, f"the test file {PARSER_TEST}")

    def test_junit_file(self):
        self.measure()
        path = f"{RESULTS}/TEST-{SUITE}.xml"
        self.assert_refused(self.repo / path, f"the JUnit file {path}")

    def test_diff_file(self):
        self.measure()
        diff = self.work / "change.diff"
        diff.write_bytes(b"")
        self.assert_refused(diff, f"--diff-file {diff}", "--diff-file", str(diff))
        code, env = self.json_main("coverage", "--diff-file", "change.diff", "--summary-file", "change.diff")
        self.assertEqual(env["error"]["message"], "--summary-file change.diff is --diff-file change.diff")
        self.assertEqual((code, diff.read_bytes()), (2, b""))

    def test_report_and_snapshot(self):
        self.measure()
        self.assert_refused(self.repo / REPORT, "the file of coverage.report")
        self.assert_refused(self.repo / SNAPSHOT, "the file of policy.snapshot")

    def test_report_of_a_profile_that_fails_its_check(self):
        self.measure()
        self.write("muster.toml", PROFILE + "\n[output]\nbogus = 1\n")
        self.assert_refused(self.repo / REPORT, "the file of coverage.report")

    def test_inputs_never_raise_on_an_unchecked_profile(self):
        data = {"coverage": {"report": ["not", "a", "path"]}, "policy": {"snapshot": "build/x" + chr(0) + ".json"}}
        self.assertEqual(coverage.inputs(Config(self.repo, self.repo / "muster.toml", data)), [])
        data = {"coverage": "a string", "policy": 7}
        self.assertEqual(coverage.inputs(Config(self.repo, self.repo / "muster.toml", data)), [])


class BaseInEnvelopeTest(CoverageCase):
    def test_ref_and_merge_base(self):
        self.measure()
        run = self.passed("--base", "main")
        self.assertEqual(run.env["base"], {"ref": "main", "commit": self.git("merge-base", "main", "HEAD")})

    def test_null_without_base(self):
        self.measure()
        self.assertIsNone(self.passed().env["base"])

    def test_kept_on_a_config_error(self):
        self.write("muster.toml", PROFILE.replace('[coverage]\nreport = "build/jacoco/report.xml"\n', ""))
        code, env = self.json_main("coverage", "--base", "main")
        self.assertEqual((code, env["error"]["kind"]), (2, "config"))
        self.assertEqual(env["base"], {"ref": "main", "commit": self.git("merge-base", "main", "HEAD")})

    def test_kept_on_an_invalid_policy(self):
        self.write("policy/critical.txt", CRITICAL + "gone/**\n")
        code, env = self.json_main("coverage", "--base", "no-such-ref")
        self.assertEqual((code, env["error"]["kind"]), (2, "policy"))
        self.assertEqual(env["base"], {"ref": "no-such-ref", "commit": None})


class ConsoleTest(CoverageCase):
    def test_non_ascii_name_reaches_a_console_that_is_not_utf8(self):
        path = f"{BASE}/billing/" + chr(0x65E5) + chr(0x672C) + ".java"
        self.write(path, source_text(3))
        self.measure()
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        env["PYTHONIOENCODING"] = "ascii"
        argv = ["--repo", str(self.repo), "coverage", "--base", "main", "--today", "2026-05-19"]
        # -I runs as the CI does but ignores PYTHONIOENCODING; -s keeps the variable in force.
        for flag in ("-I", "-s"):
            with self.subTest(flag=flag):
                self.summary.unlink(missing_ok=True)
                proc = subprocess.run(
                    [sys.executable, flag, "src", *argv, "--summary-file", str(self.summary)],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"changed critical file(s): {path}".encode("utf-8"), proc.stdout)
                self.assertIn(f"- `{path}` (Z1)", self.summary.read_bytes().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
