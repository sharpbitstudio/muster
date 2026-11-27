"""Tests of the coverage-diff command, end to end through cli.main: the options, the policy, the
comparison of two JaCoCo reports file by file and line by line, the verdict, the JUnit identities,
the Markdown and the data, the input errors and the files no output may be.

Every run happens in temporary folders, with git's global and system configuration switched off.
The reports and the JUnit folders lie in the working directory, which is not the repository.
Non-ASCII and control characters are built at run time with chr(); so is every line token, an L
and its number.
"""

import argparse
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
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr

from muster import cli, diffs, policy
from muster.adapters import junit
from muster.config import named_files
from muster.paths import file_argument

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)  # fmt: skip
MAIN = "src/main/java"
BASE = f"{MAIN}/org/example/app"
TESTS = "src/test/java/org/example/app"
ENGINE = f"{BASE}/billing/internal/ledger/Engine.java"  # zone 1
TOKEN = f"{BASE}/auth/Token.java"  # zone 2
PARSER = f"{BASE}/codec/KeyParser.java"  # standard
DISPATCHER = f"{BASE}/queue/Dispatcher.java"  # standard
PACKAGE_INFO = f"{BASE}/package-info.java"  # under the source root, outside the classification set
ENGINE_TEST = f"{TESTS}/billing/internal/ledger/EngineTest.java"
DRAFTS = "org/example/app/drafts"  # a package under no source root
REPORT = "build/jacoco/report.xml"  # coverage.report, which coverage-diff never reads
SUITE = "org.example.app.billing.internal.ledger.EngineTest"
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

[coverage]
report = "build/jacoco/report.xml"
"""
CRITICAL = "# Zone 1: money.\nbilling/**\n# Zone 2: access.\nauth/**\n"
STANDARD = "codec/*\nqueue/*\n"
PAIR = "--junit-before and --junit-after go together (only one given)"
HEADING = "## Coverage diff\n\n"
ERROR_BLOCK = HEADING + "**ERROR**: the inputs could not be read, no verdict:\n\n"
WITHIN = "file(s) lost coverage within the tolerance or outside the critical zones"
LOST = "**lost critical coverage**"


def name(path: str) -> str:
    """The report name of a source file under the source root."""
    return path.removeprefix(MAIN + "/")


def runs(*parts) -> str:
    """Line runs as the command writes them: a number, or a (first, last) pair."""
    return ", ".join(f"L{part}" if isinstance(part, int) else f"L{part[0]}-{part[1]}" for part in parts)


def line_data(covered=(), missed=(), branches: dict | None = None) -> dict:
    """Per line (mi, ci, mb, cb): a covered line has two covered instructions and a missed line two
    missed ones, so a line keeps its totals when it flips; `branches` sets (mb, cb) of a line."""
    data = {nr: (0, 2, 0, 0) for nr in covered} | {nr: (2, 0, 0, 0) for nr in missed}
    for nr, (mb, cb) in (branches or {}).items():
        data[nr] = data[nr][:2] + (mb, cb)
    return data


def jacoco_report(files: dict) -> str:
    """A JaCoCo report: per report name (package/file) its lines (mi, ci, mb, cb)."""
    packages: dict[str, list[str]] = {}
    for report_name, lines in files.items():
        folder, _, file = report_name.rpartition("/")
        rows = "".join(
            f'<line nr="{nr}" mi="{mi}" ci="{ci}" mb="{mb}" cb="{cb}"/>'
            for nr, (mi, ci, mb, cb) in sorted(lines.items())
        )
        covered = sum(1 for counters in lines.values() if counters[1] > 0)
        rows += f'<counter type="LINE" missed="{len(lines) - covered}" covered="{covered}"/>'
        packages.setdefault(folder, []).append(f"<sourcefile name={quoteattr(file)}>{rows}</sourcefile>")
    body = "".join(
        f"<package name={quoteattr(folder)}>{''.join(items)}</package>" for folder, items in packages.items()
    )
    session = '<sessioninfo id="localhost-5eed" start="1000" dump="2000"/>'
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<report name="app">{session}{body}</report>\n'


def junit_report(*cases) -> str:
    """A JUnit XML file of one suite; per case (classname, name), skipped with a third item True."""
    rows = []
    for classname, case, *skipped in cases:
        body = "<skipped/>" if skipped and skipped[0] else ""
        rows.append(f'<testcase name={quoteattr(case)} classname={quoteattr(classname)} time="0.01">{body}</testcase>')
    suite = f'<testsuite name="{SUITE}" tests="{len(cases)}" hostname="localhost" timestamp="2026-11-10T08:00:00">'
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + suite + "".join(rows) + "</testsuite>\n"


def row(*cells) -> str:
    return "| " + " | ".join(map(str, cells)) + " |"


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


class DiffCase(unittest.TestCase):
    """A repository with a valid policy, no git, and a separate working directory that holds the
    reports and the JUnit folders."""

    SUBFOLDER = ""  # the folder of the profile inside the repository folder

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
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
        self.write("muster.toml", PROFILE)
        self.write("policy/critical.txt", CRITICAL)
        self.write("policy/standard.txt", STANDARD)
        for path in (ENGINE, TOKEN, PARSER, DISPATCHER):
            self.write(path, "class Source {}\n")
        self.write(PACKAGE_INFO, "package org.example.app;\n")
        self.write(ENGINE_TEST, "class EngineTest {}\n")
        self.write(REPORT, jacoco_report({}))

    def write(self, path: str, content) -> Path:
        """A file of the repository."""
        return self.put(self.repo / path, content)

    def put(self, file: Path | str, content) -> Path:
        """A file, by default of the working directory."""
        file = self.work / file
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return file

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def json_main(self, *argv: str) -> tuple[int, dict]:
        code, out, _ = self.main(*argv, "--format", "json")
        return code, json.loads(out)

    def run_diff(self, *options: str, reports: bool = True) -> Run:
        """coverage-diff with before.xml and after.xml of the working directory (with `reports`), and
        the envelope and the summary in the working directory too."""
        for file in ("out.json", "summary.md"):
            (self.work / file).unlink(missing_ok=True)
        names = ["--before", "before.xml", "--after", "after.xml"] if reports else []
        code, out, err = self.main(
            "coverage-diff", *names, "--json-out", "out.json", "--summary-file", "summary.md", *options
        )
        json_out, summary = self.work / "out.json", self.work / "summary.md"
        env = json.loads(json_out.read_bytes().decode("ascii")) if json_out.exists() else None
        return Run(code, out, err, env, summary.read_bytes().decode("utf-8") if summary.exists() else "")

    def diff(self, before: dict | None, after: dict | None, *options: str) -> Run:
        """Write the reports of `before` and `after` (None: no file), then run the command."""
        for file, files in (("before.xml", before), ("after.xml", after)):
            (self.work / file).unlink(missing_ok=True)
            if files is not None:
                self.put(file, jacoco_report(files))
        return self.run_diff(*options)

    def judged(self, before: dict, after: dict, outcome: str, *options: str) -> Run:
        run = self.diff(before, after, *options)
        self.assertEqual((run.code, run.data["outcome"]), (0, outcome), run.out + run.err)
        return run


class ConstantsTest(unittest.TestCase):
    def test_policy_text_names_the_branch_tolerance(self):
        text = (REPO_ROOT / "POLICY.md").read_bytes().decode("ascii")
        point = text.split("## 8. ", 1)[1].split("\n## ", 1)[0]
        sentence = r"A critical file does not lose a covered line, or more than ([0-9]+) covered branch in total"
        self.assertEqual(re.findall(sentence, point), [str(diffs.BRANCH_TOLERANCE)])
        self.assertIn("in total (the losses of its lines summed), without an explanation in review;", point)

    def test_limits_of_the_lists(self):
        limits = (diffs.BRANCH_TOLERANCE, diffs.TEST_IDS_CAP, diffs.FILES_CAP, diffs.ITEMS_CAP, diffs.SHOWN_NAMES)
        self.assertEqual(limits, (1, 100, 100, 40, 10))

    def test_wiring(self):
        analysis = cli.ANALYSES["coverage-diff"]
        self.assertIs(analysis.protected, policy.protected)
        self.assertIs(analysis.inputs, named_files)
        self.assertIs(analysis.option_inputs, diffs.option_inputs)
        args = argparse.Namespace(before="before.xml", after="after.xml", junit_before=None, junit_after=None)
        expected = [("--before before.xml", Path("before.xml")), ("--after after.xml", Path("after.xml"))]
        self.assertEqual(analysis.option_inputs(args), expected)

    def test_every_file_option_has_the_type_of_paths(self):
        parser = cli.build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        wanted = {
            "coverage": ["--diff-file"],
            "coverage-diff": ["--before", "--after", "--junit-before", "--junit-after"],
        }
        for command, options in wanted.items():
            command_parser = commands.choices[command]
            actions = {option: action for action in command_parser._actions for option in action.option_strings}
            for option in options:
                with self.subTest(command=command, option=option):
                    self.assertIs(actions[option].type, file_argument)


class VerdictTest(DiffCase):
    def test_unchanged_reports_pass_without_rows(self):
        report = {
            name(ENGINE): line_data((1, 2, 3), (4,), branches={2: (1, 3)}),
            name(PARSER): line_data((1,), (2,)),
        }
        run = self.judged(report, report, "pass")
        self.assertEqual((run.annotations, run.data["files"], run.data["info"]), ([], [], []))
        verdict = "coverage-diff: PASS (no critical coverage lost); line 66.7% -> 66.7%, branch 75.0% -> 75.0%"
        self.assertEqual(run.out.splitlines(), [verdict])
        self.assertEqual(run.section["verdict_text"], verdict)
        self.assertEqual(run.data["totals"]["after"], {"line": [2, 4], "branch": [1, 3]})
        self.assertIn(row("lines covered", "4/6 (66.7%)", "4/6 (66.7%)") + "\n", run.summary)
        self.assertIn(row("source files", 2, 2) + "\n", run.summary)
        self.assertIn("\nNo file changed its covered lines or branches.\n", run.summary)
        self.assertTrue(run.summary.startswith(HEADING + "Before `before.xml` - after `after.xml`; "), run.summary)

    def test_verdict_table(self):
        """Annotation, violation, enum and exit of each outcome."""
        same = {name(ENGINE): line_data((1, 2))}
        lost = {name(ENGINE): line_data((1,), (2,))}
        other = {name(ENGINE): line_data((1,))}
        cases = (
            ("pass", same, same, [], (None, False, "pass", 0)),
            ("pass", same, same, ["--strict"], (None, False, "pass", 0)),
            ("pass", same, same, ["--fail-on-error"], (None, False, "pass", 0)),
            ("warn", same, lost, [], ("warning", True, "warn", 0)),
            ("warn", same, lost, ["--strict"], ("error", True, "fail", 1)),
            ("warn", same, lost, ["--fail-on-error"], ("warning", True, "warn", 0)),
            ("not comparable", same, other, [], ("warning", True, "not-evaluated", 0)),
            ("not comparable", same, other, ["--strict"], ("error", True, "not-evaluated", 1)),
            ("not comparable", same, other, ["--fail-on-error"], ("warning", True, "not-evaluated", 0)),
            ("input error", None, same, [], ("warning", False, "error", 0)),
            ("input error", None, same, ["--strict"], ("error", True, "error", 1)),
            ("input error", None, same, ["--fail-on-error"], ("error", False, "error", 1)),
        )
        for case, before, after, options, expected in cases:
            with self.subTest(case=case, options=options):
                run = self.diff(before, after, *options)
                level = run.annotations[0].split(" ", 1)[0][2:] if run.annotations else None
                state = (level, run.section["violation"], run.section["verdict"], run.code)
                self.assertEqual(state, expected, run.out)
                self.assertEqual((run.env["exit_code"], len(run.annotations)), (run.code, int(level is not None)))
                self.assertEqual(run.section["strict"], options == ["--strict"])

    def test_lost_critical_lines_name_their_runs(self):
        before = {name(ENGINE): line_data((1, 2, 3, 4, 7)), name(PARSER): line_data((1,))}
        after = {name(ENGINE): line_data((1, 2, 7), (3, 4)), name(PARSER): line_data((1,))}
        run = self.judged(before, after, "warn")
        self.assertEqual((run.data["warned"], run.data["files"][0]["lost_lines"]), ([ENGINE], [runs((3, 4))]))
        verdict = "coverage-diff: WARN (1 critical file(s) lost coverage: Engine.java); line "
        self.assertTrue(run.verdict.startswith(verdict), run.verdict)
        message = f"1 critical file(s) lost coverage: {ENGINE} (lost lines or more than 1 branch)"
        self.assertEqual(run.annotations, [f"::warning title=Coverage diff::{message}"])
        self.assertEqual(run.section["violations"], [message])
        self.assertIn("### Changed files (1; critical first)\n\n" + row("file", "zone", "lines covered"), run.summary)
        cells = (f"`{ENGINE}`", "Z1", "5 -> 3 (-2)", "0 -> 0 (+0)", runs((3, 4)), "", LOST)
        self.assertIn(row(*cells) + "\n", run.summary)

    def test_branch_loss_is_summed_per_line(self):
        """A branch gained on one line never offsets one lost on another; one lost branch is within
        the tolerance."""
        before = {name(TOKEN): line_data((1, 2, 3), branches={1: (0, 2), 2: (2, 0), 3: (0, 2)})}
        cases = (
            ("a single branch lost", {1: (1, 1), 2: (2, 0), 3: (0, 2)}, "pass", 3, (1,), 1),
            ("one branch lost on each of two lines", {1: (1, 1), 2: (2, 0), 3: (1, 1)}, "warn", 2, (1, 3), 2),
            ("two lost on a line, two gained on another", {1: (2, 0), 2: (0, 2), 3: (0, 2)}, "warn", 4, (1,), 2),
            ("two lost, one gained", {1: (2, 0), 2: (1, 1), 3: (0, 2)}, "warn", 3, (1,), 2),
        )
        for case, branches, outcome, covered, lost, loss in cases:
            with self.subTest(case=case):
                run = self.judged(before, {name(TOKEN): line_data((1, 2, 3), branches=branches)}, outcome)
                file = run.data["files"][0]
                self.assertEqual((file["branches"], file["lost_branch_lines"]), ([4, covered], runs(*lost).split(", ")))
                self.assertEqual((file["branch_loss"], file["lost_lines"], file["loss"]), (loss, [], True))
                note = "loss, information" if outcome == "pass" else LOST
                cells = (f"`{TOKEN}`", "Z2", "3 -> 3 (+0)", f"4 -> {covered} ({covered - 4:+d})", "")
                self.assertIn(row(*cells, f"-{loss} ({runs(*lost)})", note) + "\n", run.summary)
                if outcome == "pass":
                    self.assertEqual((run.data["info"], run.data["warned"], run.annotations), ([TOKEN], [], []))
                    status = f"coverage-diff: PASS (no critical coverage lost; 1 {WITHIN}); line "
                    self.assertTrue(run.verdict.startswith(status), run.verdict)
                else:
                    self.assertEqual((run.data["warned"], run.data["info"]), ([TOKEN], []))

    def test_standard_file_loss_is_information(self):
        run = self.judged({name(PARSER): line_data((1, 2, 3))}, {name(PARSER): line_data((), (1, 2, 3))}, "pass")
        self.assertEqual((run.annotations, run.data["info"], run.data["warned"]), ([], [PARSER], []))
        cells = (f"`{PARSER}`", "standard", "3 -> 0 (-3)", "0 -> 0 (+0)", runs((1, 3)), "", "loss, information")
        self.assertIn(row(*cells) + "\n", run.summary)
        self.assertIn(f"\nInformation: 1 {WITHIN}: `{PARSER}`\n", run.summary)

    def test_rows_put_critical_files_first(self):
        files = (PARSER, PACKAGE_INFO, TOKEN, ENGINE)
        before = {name(path): line_data((1,), (2,)) for path in files}
        run = self.judged(before, {name(path): line_data((1, 2)) for path in files}, "pass")
        order = [(file["path"], file["zone"], file["critical"]) for file in run.data["files"]]
        expected = [(ENGINE, "Z1", True), (TOKEN, "Z2", True), (PACKAGE_INFO, "unknown", True)]
        self.assertEqual(order, expected + [(PARSER, "standard", False)])
        gained = r"^\| `([^`]+)` \| (\S+) \| 1 -> 2 \(\+1\) \| 0 -> 0 \(\+0\) \|  \|  \|  \|$"
        self.assertEqual(re.findall(gained, run.summary, re.M), [(path, zone) for path, zone, _ in order])
        self.assertEqual((run.data["info"], run.data["warned"], run.annotations), ([], [], []))


class ComparabilityTest(DiffCase):
    def test_reasons_a_file_cannot_be_compared(self):
        bytecode = line_data((1, 2), (3,), branches={2: (1, 1)})
        totals = "line bytecode differs (1 line(s) with other instruction or branch totals)"
        cases = (
            (
                "another line set",
                {name(ENGINE): line_data((1, 2, 3))},
                {name(ENGINE): line_data((1, 2, 3, 4))},
                [(ENGINE, "line set differs (-0/+1 line(s) with bytecode)")],
            ),
            (
                "another instruction total on a line",
                {name(ENGINE): bytecode},
                {name(ENGINE): {**bytecode, 1: (0, 3, 0, 0)}},
                [(ENGINE, totals)],
            ),
            (
                "another branch total on a line",
                {name(ENGINE): bytecode},
                {name(ENGINE): {**bytecode, 2: (0, 2, 2, 2)}},
                [(ENGINE, totals)],
            ),
            (
                "a file of one report only",
                {name(ENGINE): line_data((1,)), name(PARSER): line_data((1,))},
                {name(ENGINE): line_data((1,)), name(TOKEN): line_data((1,))},
                [(TOKEN, "only in the after report"), (PARSER, "only in the before report")],
            ),
        )
        for case, before, after, reasons in cases:
            with self.subTest(case=case):
                run = self.judged(before, after, "not-comparable")
                expected = [{"path": path, "unresolved": False, "reason": why} for path, why in reasons]
                self.assertEqual((run.data["not_comparable"], run.data["files"]), (expected, []))
                part = f"{len(reasons)} file(s) not comparable (main source changed)"
                self.assertTrue(run.verdict.startswith(f"coverage-diff: NOT COMPARABLE ({part}); line "), run.verdict)
                message = f"before and after are not comparable: {part}"
                self.assertEqual(run.annotations, [f"::warning title=Coverage diff::{message}"])
                self.assertNotIn("PASS", run.out)
                items = "".join(f"- `{path}`: {why}\n" for path, why in reasons)
                heading = f"### Not comparable ({len(reasons)}; main source changed between the runs)"
                self.assertIn(f"{heading}\n\n{items}\n", run.summary)

    def test_two_names_of_one_file_under_nested_roots(self):
        nested = PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "src/main/java/org"]')
        self.write("muster.toml", nested)
        after = {name(ENGINE).removeprefix("org/"): line_data((1,), (2,))}
        run = self.judged({name(ENGINE): line_data((1, 2))}, after, "warn")
        self.assertEqual(run.data["not_comparable"], [])
        self.assertEqual([(file["path"], file["warn"]) for file in run.data["files"]], [(ENGINE, True)])

    def test_report_without_any_sourcefile(self):
        one = {name(ENGINE): line_data((1,))}
        changed = "1 file(s) not comparable (main source changed)"
        for before, empty, detail in (
            (one, ["after"], f"empty report: after without any sourcefile; {changed}"),
            ({}, ["before", "after"], "empty report: before and after without any sourcefile"),
        ):
            with self.subTest(empty=empty):
                run = self.judged(before, {}, "not-comparable")
                self.assertEqual(run.data["empty"], empty)
                self.assertTrue(run.verdict.startswith(f"coverage-diff: NOT COMPARABLE ({detail}); line "), run.verdict)
                self.assertIn(f"\n**Empty report:** {' and '.join(empty)} without any `<sourcefile>`.\n", run.summary)
                self.assertIn(row("source files", len(before), 0) + "\n", run.summary)
        self.assertTrue(run.verdict.endswith("; line n/a -> n/a, branch n/a -> n/a"), run.verdict)

    def test_critical_loss_is_the_last_part_of_not_comparable(self):
        # The totals count every file of each report, KeyParser of the before report only too.
        before = {name(ENGINE): line_data((1, 2)), name(PARSER): line_data((), (1,))}
        run = self.judged(before, {name(ENGINE): line_data((1,), (2,))}, "not-comparable")
        detail = "1 file(s) not comparable (main source changed); 1 critical file(s) lost coverage"
        trend = "line 66.7% -> 50.0%, branch n/a -> n/a"
        self.assertEqual(run.verdict, f"coverage-diff: NOT COMPARABLE ({detail}); {trend}")
        # (missed, covered) per counter
        totals = {"before": {"line": [1, 2], "branch": [0, 0]}, "after": {"line": [1, 1], "branch": [0, 0]}}
        self.assertEqual(run.data["totals"], totals)
        message = f"before and after are not comparable: {detail}"
        self.assertEqual(run.annotations, [f"::warning title=Coverage diff::{message}"])
        self.assertEqual((run.data["warned"], [file["path"] for file in run.data["files"]]), ([ENGINE], [ENGINE]))
        self.assertLess(run.summary.index("### Changed files"), run.summary.index("### Not comparable"))


class ZoneTest(DiffCase):
    def test_file_without_a_zone_is_critical(self):
        draft = f"{DRAFTS}/Draft.java"
        files = (draft, name(PACKAGE_INFO), name(PARSER))
        before = {key: line_data((1, 2)) for key in files}
        run = self.judged(before, {key: line_data((1,), (2,)) for key in files}, "warn")
        self.assertEqual((run.data["warned"], run.data["info"]), ([draft, PACKAGE_INFO], [PARSER]))
        zones = [(file["path"], file["zone"], file["unresolved"]) for file in run.data["files"]]
        expected = [(draft, "unknown", True), (PACKAGE_INFO, "unknown", False), (PARSER, "standard", False)]
        self.assertEqual(zones, expected)
        for path, note in ((draft, f"{LOST}; under no source root"), (PACKAGE_INFO, LOST)):
            self.assertIn(row(f"`{path}`", "unknown", "2 -> 1 (-1)", "0 -> 0 (+0)", runs(2), "", note), run.summary)
        within = ({draft: line_data((1,), branches={1: (0, 2)})}, {draft: line_data((1,), branches={1: (1, 1)})})
        run = self.judged(*within, "pass")
        self.assertIn(f"\nInformation: 1 {WITHIN}: `{draft}` (under no source root)\n", run.summary)

    def test_unresolved_name_with_the_text_of_a_path(self):
        # The second report name is the text of the repository path of the first one.
        before = {key: line_data((1, 2)) for key in (name(ENGINE), ENGINE)}
        run = self.judged(before, {key: line_data((1,), (2,)) for key in before}, "warn")
        rows = [(file["path"], file["zone"], file["unresolved"]) for file in run.data["files"]]
        self.assertEqual(rows, [(ENGINE, "Z1", False), (ENGINE, "unknown", True)])
        self.assertEqual(run.data["warned"], [ENGINE, ENGINE])
        notes = [line.rsplit(" | ", 1)[1] for line in run.summary.splitlines() if line.startswith(f"| `{ENGINE}` |")]
        self.assertEqual(notes, [f"{LOST} |", f"{LOST}; under no source root |"])
        run = self.judged(before, {name(ENGINE): line_data((1, 2))}, "not-comparable")
        reason = {"path": ENGINE, "unresolved": True, "reason": "only in the before report"}
        self.assertEqual(run.data["not_comparable"], [reason])
        self.assertIn(f"\n- `{ENGINE}` (under no source root): only in the before report\n", run.summary)

    def test_invalid_policy_exits_two_with_or_without_reports(self):
        self.write("policy/critical.txt", CRITICAL + "gone/**\n")
        for reports in (True, False):
            with self.subTest(reports=reports):
                if reports:
                    run = self.diff({}, {})
                else:
                    run = self.run_diff("--before", "gone.xml", "--after", "gone.xml", reports=False)
                self.assertEqual((run.code, run.env["error"]["kind"]), (2, "policy"))
                self.assertEqual(run.env["error"]["message"], "the policy is invalid: 1 violation")
                verdict = "coverage-diff: NOT EVALUATED (invalid policy: 1 violation)"
                self.assertEqual((run.section["verdict_text"], run.section["verdict"]), (verdict, "error"))
                self.assertTrue(run.summary.startswith(HEADING + "**NOT EVALUATED**: invalid policy, 1 violation.\n"))

    def test_missing_keys(self):
        for line, key in (
            ('roots = ["src/main/java"]\n', "source.roots"),
            ('include = ["**/*.java"]\n', "source.include"),
            ('base = "src/main/java/org/example/app"\n', "zones.base"),
            ('critical = "policy/critical.txt"\n', "zones.critical"),
            ('standard = "policy/standard.txt"\n', "zones.standard"),
        ):
            with self.subTest(key=key):
                self.write("muster.toml", PROFILE.replace(line, ""))
                code, env = self.json_main("coverage-diff", "--before", "before.xml", "--after", "after.xml")
                error = {"kind": "config", "message": f"muster.toml: missing key {key}"}
                self.assertEqual((code, env["error"]), (2, error))


class OptionTest(DiffCase):
    def test_before_and_after_are_required(self):
        for argv in (["--before", "before.xml"], ["--after", "after.xml"], []):
            with self.subTest(argv=argv):
                code, env = self.json_main("coverage-diff", *argv)
                self.assertEqual((code, env["error"]["kind"], env["sections"]), (2, "usage", []))

    def test_junit_option_without_its_pair_is_a_usage_error(self):
        self.put("results/TEST-one.xml", junit_report((SUITE, "settles()")))
        no_base = PROFILE.replace('base = "src/main/java/org/example/app"\n', "")
        for option in ("--junit-before", "--junit-after"):
            for case, profile, extra in (
                ("plain", PROFILE, []),
                ("strict", PROFILE, ["--strict"]),
                ("before the required keys", no_base, []),
            ):
                with self.subTest(option=option, case=case):
                    self.write("muster.toml", profile)
                    argv = ["coverage-diff", "--before", "before.xml", "--after", "after.xml", option, "results"]
                    code, env = self.json_main(*argv, *extra)
                    self.assertEqual((code, env["error"], env["sections"]), (2, {"kind": "usage", "message": PAIR}, []))

    def test_empty_or_nul_name_is_a_usage_error(self):
        for option in ("--before", "--after", "--junit-before", "--junit-after"):
            for value, problem in (("", "the file name is empty"), (f"re{chr(0)}sults", "the file name has a NUL")):
                with self.subTest(option=option, value=value):
                    argv = ["--before", "before.xml", "--after", "after.xml", f"{option}={value}"]
                    code, env = self.json_main("coverage-diff", *argv)
                    self.assertEqual((code, env["error"]["kind"]), (2, "usage"))
                    self.assertIn(problem, env["error"]["message"])

    def test_option_paths_are_relative_to_the_working_directory(self):
        report = jacoco_report({name(ENGINE): line_data((1,))})
        for file in ("before.xml", "after.xml"):
            self.write(file, report)  # in the repository, which is not the working directory
        run = self.run_diff()
        self.assertEqual(run.section["errors"], ["no JaCoCo XML at before.xml", "no JaCoCo XML at after.xml"])
        self.put("results/TEST-one.xml", junit_report((SUITE, "settles()")))
        self.put("before.xml", report)
        self.put("after.xml", report)
        run = self.run_diff("--junit-before", "results", "--junit-after", str(self.work / "results"))
        self.assertEqual((run.code, run.data["outcome"], run.data["junit"]["before"]), (0, "pass", 1))
        self.assertIn("Before `before.xml` - after `after.xml`; ", run.summary)

    def test_strict_only_takes_the_command_name(self):
        lost = ({name(ENGINE): line_data((1, 2))}, {name(ENGINE): line_data((1,), (2,))})
        for option, strict, code in (("coverage-diff", True, 1), ("coverage,loc", False, 0)):
            with self.subTest(option=option):
                run = self.diff(*lost, "--strict-only", option)
                self.assertEqual((run.code, run.section["strict"]), (code, strict))
                self.assertEqual(run.section["verdict"], "fail" if strict else "warn")
        self.write("muster.toml", PROFILE + '\n[check]\nsections = ["coverage-diff"]\n')
        code, env = self.json_main("coverage-diff", "--before", "before.xml", "--after", "after.xml")
        self.assertEqual((code, env["error"]["kind"]), (2, "config"))
        self.assertIn("check.sections", env["error"]["message"])


class InputErrorTest(DiffCase):
    def test_reports_that_cannot_be_read(self):
        good = jacoco_report({name(ENGINE): line_data((1,))})
        twice = good.replace("</package>", '<sourcefile name="Engine.java"/></package>')
        cases = (
            ("missing", None, "no JaCoCo XML at before.xml", True),
            ("a folder", "folder", "before.xml: not a file", True),
            ("not well-formed", "<report", "before.xml: not well-formed XML: ", False),
            ("another root", "<testsuite/>", "before.xml: not a JaCoCo report, the root element is <testsuite>", True),
            (
                "a bad count",
                good.replace('ci="2"', 'ci="two"'),
                "before.xml: <line> attribute ci must be a non-negative integer, got 'two'",
                True,
            ),
            ("a file twice", twice, f"before.xml: source file {name(ENGINE)} occurs twice", True),
        )
        self.put("after.xml", good)
        for case, content, error, exact in cases:
            with self.subTest(case=case):
                file = self.work / "before.xml"
                if file.is_dir():
                    file.rmdir()
                file.unlink(missing_ok=True)
                if content == "folder":
                    file.mkdir()
                elif content is not None:
                    self.put(file, content)
                run = self.run_diff()
                errors = run.section["errors"]
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0] == error if exact else errors[0].startswith(error), errors)
                self.assertEqual((run.code, run.section["verdict"], run.data), (0, "error", {"outcome": "error"}))
                self.assertEqual(run.verdict, f"coverage-diff: ERROR ({errors[0]})")
                self.assertEqual(run.annotations, [f"::warning title=Coverage diff::{errors[0]}"])
                self.assertEqual(run.summary, f"{ERROR_BLOCK}- `{errors[0]}`\n\n")

    def test_every_input_error_is_listed_in_order(self):
        self.put("after.xml", "<report")
        self.put("junit-after/beta.xml", "<testsuite")
        self.put("junit-after/alpha.xml", "<report/>")
        self.put("junit-after/locked/TEST-hidden.xml", junit_report((SUITE, "settles()")))
        denied, scandir = os.path.join("junit-after", "locked"), os.scandir

        def listing(top="."):
            if os.path.normpath(os.fspath(top)) == denied:
                raise PermissionError(13, "Permission denied", top)
            return scandir(top)

        with mock.patch("os.scandir", side_effect=listing):
            run = self.run_diff("--junit-before", "gone", "--junit-after", "junit-after")
        errors = run.section["errors"]
        self.assertEqual(errors[0], "no JaCoCo XML at before.xml")
        self.assertTrue(errors[1].startswith("after.xml: not well-formed XML: "), errors)
        expected = [
            "no JUnit XML folder at gone",
            "junit-after/locked: cannot read: Permission denied",
            "junit-after/alpha.xml: not a JUnit report, the root element is <report>",
        ]
        self.assertEqual(errors[2:5], expected)
        self.assertTrue(errors[5].startswith("junit-after/beta.xml: not well-formed XML: "), errors)
        self.assertEqual((len(errors), len(run.annotations), run.code), (6, 6, 0))
        self.assertEqual(run.verdict, "coverage-diff: ERROR (no JaCoCo XML at before.xml)")
        self.assertEqual(run.summary, ERROR_BLOCK + "".join(f"- `{error}`\n" for error in errors) + "\n")

    def test_report_and_folder_that_cannot_be_looked_at(self):
        report = jacoco_report({name(ENGINE): line_data((1,))})
        self.put("before.xml", report)
        self.put("after.xml", report)
        for folder in ("junit-before", "junit-after"):
            self.put(f"{folder}/TEST-one.xml", junit_report((SUITE, "settles()")))
        denied, inspect = {"before.xml", "junit-before"}, os.stat

        def looking(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and os.path.normpath(os.fspath(path)) in denied:
                raise PermissionError(13, "Permission denied", path)
            return inspect(path, *args, **kwargs)

        with mock.patch("os.stat", side_effect=looking):
            run = self.run_diff("--junit-before", "junit-before", "--junit-after", "junit-after")
        expected = ["before.xml: cannot read: Permission denied", "junit-before: cannot read: Permission denied"]
        self.assertEqual((run.section["errors"], run.code), (expected, 0))

    def test_config_error_wins_over_input_errors(self):
        profile = PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "gen/main/java"]')
        profile = profile.replace('exclude = ["**/package-info.java"]', 'exclude = ["**/package-info.java", "gen/**"]')
        self.write("muster.toml", profile)
        self.write("gen/main/java/org/example/app/auth/Token.java", "class Token {}\n")
        self.put("before.xml", "<report")
        self.put("after.xml", jacoco_report({name(TOKEN): line_data((1,))}))
        run = self.run_diff()
        message = (
            "after.xml: org/example/app/auth/Token.java is under more than one source root:"
            f" gen/main/java/org/example/app/auth/Token.java, {TOKEN}"
        )
        self.assertEqual((run.code, run.env["error"]), (2, {"kind": "config", "message": message}))
        self.assertEqual(run.env["sections"], [])

    def test_configured_report_and_snapshot_are_never_read(self):
        before, after = {name(ENGINE): line_data((1, 2))}, {name(ENGINE): line_data((1,), (2,))}
        valid = self.judged(before, after, "warn")
        (self.repo / REPORT).unlink()
        self.write("muster.toml", PROFILE + '\n[policy]\nsnapshot = "build/muster/snapshot.json"\n')
        missing = self.judged(before, after, "warn")
        self.assertEqual((missing.out, missing.env, missing.summary), (valid.out, valid.env, valid.summary))


class JunitFolderTest(DiffCase):
    def setUp(self):
        super().setUp()
        same = jacoco_report({name(ENGINE): line_data((1,))})
        self.put("before.xml", same)
        self.put("after.xml", same)

    def identities(self, before: str = "junit-before", after: str = "junit-after") -> Run:
        return self.run_diff("--junit-before", before, "--junit-after", after)

    def test_identities_are_compared_as_multisets(self):
        nested = SUITE + "$Rounding"
        shown = "Settles a na" + chr(0xEF) + "ve refund"
        case = "[2] currency = EUR"
        kept = [(SUITE, "settles()"), (SUITE, shown), (SUITE, case), (SUITE, "postponed()", True)]
        before = [*kept, (SUITE, case)]
        after = [*kept, (SUITE, "refunds()")]
        self.put("junit-before/TEST-EngineTest.xml", junit_report(*before))
        self.put("junit-before/deeper/inner/TEST-Rounding.xml", junit_report((nested, "roundsHalfUp()")))
        self.put("junit-before/notes.txt", "not read\n")
        self.put("junit-after/TEST-EngineTest.xml", junit_report(*after))
        run = self.identities()
        self.assertEqual((run.code, run.data["outcome"], run.annotations), (0, "pass", []), run.out)
        removed = [f"{SUITE}#{case}", f"{nested}#roundsHalfUp()"]
        expected = {"before": 6, "after": 5, "removed": removed, "added": [f"{SUITE}#refunds()"]}
        self.assertEqual(run.data["junit"], expected)
        block = (
            "### Test identities (JUnit XML; evidence, not a verdict)\n\n"
            "`classname#name` testcases over `*.xml`, skipped included: 6 -> 5; 2 removed, 1 added.\n\n"
            f"Removed (2):\n\n- `{removed[0]}`\n- `{removed[1]}`\n\nAdded (1):\n\n- `{SUITE}#refunds()`\n"
        )
        self.assertIn(block, run.summary)

    def test_identity_lists_are_capped(self):
        self.put("junit-before/TEST-many.xml", junit_report(*[(SUITE, f"case{nr}()") for nr in range(5)]))
        self.put("junit-after/TEST-many.xml", junit_report((SUITE, "other()")))
        with mock.patch.object(diffs, "TEST_IDS_CAP", 2):
            run = self.identities()
        self.assertEqual(len(run.data["junit"]["removed"]), 5)
        items = f"- `{SUITE}#case0()`\n- `{SUITE}#case1()`\n- ... (+3 more)\n"
        self.assertIn(f"Removed (5):\n\n{items}\nAdded (1):", run.summary)

    def test_folder_problems_are_input_errors(self):
        self.put("junit-after/TEST-one.xml", junit_report((SUITE, "settles()")))
        self.put("plain.txt", "a file\n")
        self.put("quiet/notes.txt", "no report\n")
        self.put("quiet/upper.XML", junit_report((SUITE, "settles()")))
        self.put("foreign/foreign-report.xml", "<foreign-report/>")
        self.put("unnamed/TEST-one.xml", junit_report((SUITE, "settles()")).replace(' name="settles()"', ""))
        self.put("blank/TEST-one.xml", junit_report((SUITE, "")))
        cases = (
            ("gone", "no JUnit XML folder at gone"),
            ("plain.txt", "no JUnit XML folder at plain.txt"),
            ("quiet", "no JUnit XML under quiet"),
            ("foreign", "foreign/foreign-report.xml: not a JUnit report, the root element is <foreign-report>"),
            ("unnamed", "unnamed/TEST-one.xml: <testcase> has no attribute name"),
            ("blank", "blank/TEST-one.xml: <testcase> has an empty name"),
        )
        for folder, error in cases:
            with self.subTest(folder=folder):
                run = self.identities(folder)
                self.assertEqual((run.section["errors"], run.data, run.code), ([error], {"outcome": "error"}, 0))

    def test_same_folder_twice_is_walked_and_read_once(self):
        self.put("results/TEST-one.xml", junit_report((SUITE, "settles()")))
        self.put("results/TEST-two.xml", "<report/>")
        with mock.patch.object(junit, "xml_files", wraps=junit.xml_files) as walked:
            run = self.identities("results", "./results/")
        self.assertEqual(walked.call_count, 1)
        error = "results/TEST-two.xml: not a JUnit report, the root element is <report>"
        self.assertEqual(run.section["errors"], [error])
        (self.work / "results/TEST-two.xml").unlink()
        run = self.identities("results", "./results/")
        self.assertEqual(run.data["junit"], {"before": 1, "after": 1, "removed": [], "added": []})

    def test_name_that_cannot_be_looked_at_hides_no_folder(self):
        # On POSIX missing/../results names nothing, as missing does not exist; results is still
        # walked, read and protected, in either order of the options.
        self.put("results/TEST-one.xml", junit_report((SUITE, "settles()")))
        target = "results/TEST-one.xml"
        content = (self.work / target).read_bytes()
        argv = ["coverage-diff", "--before", "before.xml", "--after", "after.xml"]
        for first, second in (("missing/../results", "results"), ("results", "missing/../results")):
            with self.subTest(first=first):
                options = ["--junit-before", first, "--junit-after", second, "--json-out", target]
                code, _, err = self.main(*argv, *options)
                self.assertEqual(code, 2, err)
                self.assertIn(f"error: --json-out {target} is the JUnit file ", err)
                self.assertEqual((self.work / target).read_bytes(), content)

    @unittest.skipIf(sys.platform == "win32", "Windows reads a .. by name, before any link")
    def test_link_before_a_parent_folder_is_the_folder_it_leads_to(self):
        self.put("results/TEST-one.xml", junit_report((SUITE, "settles()")))
        self.put("elsewhere/results/TEST-two.xml", junit_report((SUITE, "refunds()")))
        (self.work / "elsewhere" / "child").mkdir()
        try:
            os.symlink(self.work / "elsewhere" / "child", self.work / "alias", target_is_directory=True)
        except OSError:
            self.skipTest("cannot create a link here")
        run = self.identities("alias/../results", "results")
        junit_data = {"before": 1, "after": 1, "removed": [f"{SUITE}#refunds()"], "added": [f"{SUITE}#settles()"]}
        self.assertEqual(run.data["junit"], junit_data)
        options = ["--before", "before.xml", "--after", "after.xml", "--junit-before", "alias/../results"]
        for target in ("results/TEST-one.xml", "alias/../results/TEST-two.xml"):
            with self.subTest(target=target):
                before = (self.work / target).read_bytes()
                code, _, err = self.main("coverage-diff", *options, "--junit-after", "results", "--json-out", target)
                self.assertEqual(code, 2, err)
                self.assertIn(f"error: --json-out {target} is the JUnit file {target}\n", err)
                self.assertEqual((self.work / target).read_bytes(), before)


class MarkdownTest(DiffCase):
    def test_report_text_stays_in_its_span_and_cell(self):
        odd = f"{DRAFTS}/Odd|Na`me" + chr(10) + "Part.java"
        run = self.judged({odd: line_data((1, 2))}, {odd: line_data((1,), (2,))}, "warn")
        self.assertEqual(run.data["warned"], [odd])
        self.assertIn(f"| `{DRAFTS}/Odd\\|Na'me Part.java` | unknown | 2 -> 1 (-1) |", run.summary)
        self.assertNotIn(chr(10) + "Part.java", run.summary)
        odd_case = "a|b `c`" + chr(10) + "d"
        self.put("junit-before/TEST-one.xml", junit_report((SUITE, odd_case)))
        self.put("junit-after/TEST-one.xml", junit_report((SUITE, "settles()")))
        run = self.diff({odd: line_data((1,))}, {}, "--junit-before", "junit-before", "--junit-after", "junit-after")
        self.assertEqual(run.data["junit"]["removed"], [f"{SUITE}#{odd_case}"])
        self.assertIn(f"\n- `{SUITE}#a|b 'c' d`\n", run.summary)
        item = f"- `{DRAFTS}/Odd|Na'me Part.java` (under no source root): only in the before report"
        self.assertIn(f"\n{item}\n", run.summary)

    def test_annotation_names_at_most_ten_files(self):
        drafts = [f"{DRAFTS}/Draft{nr:02}.java" for nr in range(1, 12)]
        before = {key: line_data((1,)) for key in drafts}
        run = self.judged(before, {key: line_data((), (1,)) for key in drafts}, "warn")
        self.assertEqual(run.data["warned"], drafts)
        shown = ", ".join(drafts[:10])
        message = f"11 critical file(s) lost coverage: {shown} (+1 more) (lost lines or more than 1 branch)"
        self.assertEqual(run.annotations, [f"::warning title=Coverage diff::{message}"])
        names = "Draft01.java, Draft02.java, Draft03.java, ... (+8 more)"
        verdict = f"coverage-diff: WARN (11 critical file(s) lost coverage: {names}); line "
        self.assertTrue(run.verdict.startswith(verdict), run.verdict)

    def test_lists_are_capped(self):
        files = {name(path): line_data((1, 3, 5)) for path in (PARSER, DISPATCHER)}
        before = {**files, name(ENGINE): line_data((1,)), name(TOKEN): line_data((1,))}
        after = {key: line_data((), (1, 3, 5)) for key in files}
        with mock.patch.object(diffs, "FILES_CAP", 1), mock.patch.object(diffs, "ITEMS_CAP", 2):
            run = self.judged(before, after, "not-comparable")
        self.assertIn(f"| {runs(1, 3)}, ... (+1 more) |  | loss, information |\n\n... 1 more file(s)\n\n", run.summary)
        self.assertIn(f"\nInformation: 2 {WITHIN}: `{PARSER}`, ... (+1 more)\n", run.summary)
        self.assertIn(f"\n- `{TOKEN}`: only in the before report\n- ... (+1 more)\n", run.summary)


class OutputTest(DiffCase):
    def setUp(self):
        super().setUp()
        report = jacoco_report({name(ENGINE): line_data((1,))})
        self.put("before.xml", report)
        self.put("after.xml", report)
        self.put("junit-before/TEST-one.xml", junit_report((SUITE, "settles()")))
        self.put("junit-after/TEST-one.xml", junit_report((SUITE, "settles()")))

    def test_output_that_is_an_input_on_every_path(self):
        junit_options = ["--junit-before", "junit-before", "--junit-after", "junit-after"]
        targets = (
            ("before.xml", "--before before.xml"),
            ("after.xml", "--after after.xml"),
            ("junit-before/TEST-one.xml", "the JUnit file junit-before/TEST-one.xml"),
            (str(self.repo / "policy/critical.txt"), "the file of zones.critical"),
            (str(self.repo / REPORT), "the file of coverage.report"),
            (str(self.repo / ENGINE), "under the source folder src/main/java"),
            (str(self.repo / ENGINE_TEST), "under the test folder src/test/java"),
        )
        both = ["--before", "before.xml", "--after", "after.xml"]
        paths = {
            # argparse fails: only the option that names the target, of --before and --after
            "a required option is missing": lambda target: (
                (both[2:] if target == "after.xml" else both[:2]) + junit_options,
                PROFILE,
            ),
            "a JUnit option without its pair": lambda target: (both + junit_options[:2], PROFILE),
            "the profile is invalid": lambda target: (both + junit_options, PROFILE + "\n[output]\nbogus = 1\n"),
        }
        for path, case in paths.items():
            for target, label in targets:
                argv, profile = case(target)
                self.write("muster.toml", profile)
                for option in ("--json-out", "--summary-file"):
                    with self.subTest(path=path, target=target, option=option):
                        file = self.work / target
                        before = file.read_bytes()
                        code, _, err = self.main("coverage-diff", *argv, option, target)
                        self.assertEqual(code, 2, err)
                        self.assertIn(f"error: {option} {target} is {label}\n", err)
                        self.assertEqual(file.read_bytes(), before)

    def test_junit_file_is_looked_up_not_compared_one_by_one(self):
        # A JUnit folder can hold thousands of files: an output is looked up among them by its
        # resolved path, as among the protected files, and never compared with each one.
        self.put("junit-after/deeper/TEST-two.xml", junit_report((SUITE, "settles()")))
        target = "junit-after/deeper/TEST-two.xml"
        content = (self.work / target).read_bytes()
        argv = ["coverage-diff", "--before", "before.xml", "--after", "after.xml"]
        argv += ["--junit-before", "junit-before", "--junit-after", "junit-after", "--json-out", target]
        for case, extra in (("a run", []), ("argparse fails", ["--strict-only", "bogus"])):
            with self.subTest(case=case), mock.patch.object(cli, "_same_file", wraps=cli._same_file) as same:
                code, _, err = self.main(*argv, *extra)
                self.assertEqual(code, 2, err)
                self.assertIn(f"error: --json-out {target} is the JUnit file {target}\n", err)
                self.assertEqual((self.work / target).read_bytes(), content)
                compared = [Path(call.args[1]).name for call in same.call_args_list]
                self.assertIn("before.xml", compared)
                self.assertEqual([name for name in compared if name.startswith("TEST-")], [])

    def test_default_summary(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        code, _, err = self.main("coverage-diff", "--before", "before.xml", "--after", "after.xml")
        self.assertEqual(code, 0, err)
        summary = self.repo / "build/muster/coverage-diff.md"
        text = summary.read_bytes()
        self.assertTrue(text.startswith(HEADING.encode("ascii")), text)
        code, env = self.json_main("coverage-diff", "--before", str(summary), "--after", "after.xml")
        message = f"the summary build/muster/coverage-diff.md is --before {summary}"
        self.assertEqual((code, env["error"]), (2, {"kind": "usage", "message": message}))
        self.assertEqual(summary.read_bytes(), text)


class SubfolderProfileTest(DiffCase):
    SUBFOLDER = "service"

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *IDENTITY, *args], cwd=self.top, check=True, capture_output=True)
        return proc.stdout.decode("utf-8").strip()

    def test_paths_and_the_default_summary_are_anchored_at_the_profile(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.put("before.xml", jacoco_report({name(ENGINE): line_data((1, 2))}))
        self.put("after.xml", jacoco_report({name(ENGINE): line_data((1,), (2,))}))
        argv = ["coverage-diff", "--before", "before.xml", "--after", "after.xml", "--json-out", "out.json"]
        code, _, err = self.main(*argv)
        self.assertEqual(code, 0, err)
        env = json.loads((self.work / "out.json").read_bytes())
        self.assertEqual(env["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(env["sections"][0]["data"]["warned"], [ENGINE])
        report = (self.repo / "build/muster/coverage-diff.md").read_bytes().decode("ascii")
        self.assertTrue(report.startswith(HEADING), report)
        self.assertFalse((self.top / "build").exists())


if __name__ == "__main__":
    unittest.main()
