"""Tests of the mutation command, end to end through cli.main on a synthetic git repository: the
keys it needs, the policy check, the counts and the verdict, the cross-check with the allowlist,
the zones and their freshness, the Markdown and the data, the input errors and the outputs.

Every run happens in temporary folders, with git's global and system configuration switched off.
A "test run" writes the snapshot with `policy --snapshot` first, then a PIT report.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape, quoteattr

from muster import cli, mutation, policy
from muster.adapters import LONG_MAX
from muster.config import DEFAULT_MUTATION_TARGET, DEFAULT_MUTATION_THRESHOLD, named_files
from muster.report import percent_digits

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)  # fmt: skip
MAIN = "src/main/java"
BASE = f"{MAIN}/org/example/app"
TESTS = "src/test/java/org/example/app"
ENGINE = f"{BASE}/billing/internal/ledger/Engine.java"  # zone 1, listed
TOKEN = f"{BASE}/auth/Token.java"  # zone 2, listed, exempt
PARSER = f"{BASE}/codec/KeyParser.java"  # standard, not listed
DISPATCHER = f"{BASE}/queue/Dispatcher.java"  # standard, not listed
JOURNAL = f"{BASE}/billing/internal/ledger/Journal.java"  # a critical file added in some tests
PACKAGE_INFO = f"{BASE}/package-info.java"  # excluded from the classification set
REPORT = "build/mutation/mutations.xml"
TARGETS = "policy/mutation.txt"
SNAPSHOT = "build/muster/snapshot.json"
VERSIONS = "build/versions.toml"
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

[mutation]
report = "build/mutation/mutations.xml"
targets = "policy/mutation.txt"
own_test = "{stem}Test{ext}"
"""
CRITICAL = "# Zone 1: money.\nbilling/**\n# Zone 2: access.\nauth/**\n"
STANDARD = "codec/*\nqueue/*\n"
ALLOWLIST = "# Pure logic, one source file per line.\n\nbilling/internal/ledger/Engine.java\nauth/Token.java\n"
MUTATORS = "org.pitest.mutationtest.engine.gregor.mutators."
DETECTING = ("KILLED", "TIMED_OUT", "NON_VIABLE", "MEMORY_ERROR", "RUN_ERROR")  # detected="true" by default
NO_SNAPSHOT = 'no snapshot: run "muster policy --snapshot" before the tests'
THRESHOLDS = {"threshold": DEFAULT_MUTATION_THRESHOLD, "target": DEFAULT_MUTATION_TARGET}
TOOL_VERSIONS = 'tool_versions = { file = "build/versions.toml", keys = ["mutation-plugin", "mutation-tool"] }\n'


def mutants(path: str, status: str | None = "KILLED", count: int = 1, **fields) -> list[dict]:
    """`count` mutants of the source file `path`, on lines 1 to count."""
    return [{"path": path, "status": status, "line": nr, **fields} for nr in range(1, count + 1)]


def mutation_xml(items: list[dict]) -> str:
    """A PIT 1.x mutations.xml. Per item: the package and the file come from its path unless
    `package` or `file` is given; `cls` is the class name inside the package (the file's stem by
    default); `detected` defaults to whether the status is one of DETECTING."""
    rows = []
    for item in items:
        folder, _, name = item["path"].removeprefix(MAIN + "/").rpartition("/")
        package = item.get("package", folder.replace("/", "."))
        name = item.get("file", name)
        cls = item.get("cls", name.rpartition(".")[0])
        detected = item.get("detected", item["status"] in DETECTING)
        status = "" if item["status"] is None else f" status={quoteattr(item['status'])}"
        indexes = "".join(f"<index>{index}</index>" for index in item.get("indexes", (5,)))
        killing = item.get("killing", "")
        rows.append(
            f"<mutation detected='{'true' if detected else 'false'}'{status} numberOfTestsRun='1'>"
            f"<sourceFile>{escape(name)}</sourceFile>"
            f"<mutatedClass>{escape(f'{package}.{cls}' if package else cls)}</mutatedClass>"
            f"<mutatedMethod>{escape(item.get('method', 'apply'))}</mutatedMethod>"
            "<methodDescription>(I)I</methodDescription>"
            f"<lineNumber>{item['line']}</lineNumber>"
            f"<mutator>{escape(MUTATORS + item.get('mutator', 'MathMutator'))}</mutator>"
            f"<indexes>{indexes}</indexes><blocks><block>0</block></blocks>"
            + (f"<killingTest>{escape(killing)}</killingTest>" if killing else "<killingTest/>")
            + f"<description>{escape(item.get('description', 'replaced addition with subtraction'))}</description>"
            "</mutation>"
        )
    return '<?xml version="1.0" encoding="UTF-8"?>\n<mutations partial="true">\n' + "\n".join(rows) + "\n</mutations>\n"


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


class MutationCase(unittest.TestCase):
    """A git repository with a valid policy and allowlist on branch `feature`, cut from `main` with
    nothing committed since, and a separate working directory. Token is exempt until ten days after
    today."""

    SUBFOLDER = ""  # the folder of the profile inside the git work tree

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
        self.json_out = self.work / "out.json"
        self.summary = self.work / "summary.md"
        until = (policy.utc_today() + timedelta(days=10)).isoformat()
        self.write("muster.toml", PROFILE)
        self.write("policy/critical.txt", CRITICAL)
        self.write("policy/standard.txt", STANDARD)
        self.write("policy/exemptions.txt", f"# path | reason | valid-until\nauth/Token.java | rename only | {until}\n")
        self.write(TARGETS, ALLOWLIST)
        for path in (ENGINE, TOKEN, PARSER, DISPATCHER):
            self.write(path, "class Source {}\n")
        self.write(PACKAGE_INFO, "package org.example.app;\n")
        for name in ("billing/internal/ledger/EngineTest.java", "auth/TokenTest.java", "codec/KeyParserTest.java"):
            self.write(f"{TESTS}/{name}", "class OwnTest {}\n")
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

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def json_main(self, *argv: str) -> tuple[int, dict]:
        code, out, _ = self.main(*argv, "--format", "json")
        return code, json.loads(out)

    def measure(self, items: list[dict] | None = None, *, snapshot: bool = True) -> None:
        """`policy --snapshot`, then a PIT report of `items`: by default one killed mutant of each
        listed file."""
        if snapshot:
            self.assertEqual(self.main("policy", "--snapshot")[0], 0)
        self.write(REPORT, mutation_xml(mutants(ENGINE) + mutants(TOKEN) if items is None else items))

    def mutation(self, *options: str, keep_summary: bool = False) -> Run:
        self.json_out.unlink(missing_ok=True)
        if not keep_summary:
            self.summary.unlink(missing_ok=True)
        code, out, err = self.main(
            "mutation", "--json-out", str(self.json_out), "--summary-file", str(self.summary), *options
        )
        env = json.loads(self.json_out.read_bytes().decode("ascii")) if self.json_out.exists() else None
        summary = self.summary.read_bytes().decode("utf-8") if self.summary.exists() else ""
        return Run(code, out, err, env, summary)

    def passed(self, *options: str) -> Run:
        run = self.mutation(*options)
        self.assertEqual((run.code, run.data["outcome"]), (0, "pass"), run.out + run.err)
        return run

    def assert_rows(self, run: Run, *rows: str) -> None:
        for row in rows:
            self.assertIn(row + "\n", run.summary)


def row(name: str, *cells) -> str:
    """A table row of the file, class or zone table; the name of a file or a class is a code span."""
    return "| " + " | ".join([name, *map(str, cells)]) + " |"


class DefaultsTest(unittest.TestCase):
    def test_policy_text_names_the_default_thresholds(self):
        text = (REPO_ROOT / "POLICY.md").read_bytes().decode("ascii")
        point = text.split("## 6. ", 1)[1].split("\n## ", 1)[0]
        pattern = r"By default the threshold is ([0-9.]+)% and the target ([0-9.]+)%; the profile changes them\."
        found = re.findall(pattern, point)
        expected = (DEFAULT_MUTATION_THRESHOLD, DEFAULT_MUTATION_TARGET)
        self.assertEqual(found, [tuple(percent_digits(value) for value in expected)])

    def test_limits_of_the_lists(self):
        limits = (mutation.SURVIVED_FILES_CAP, mutation.SURVIVED_ITEMS_CAP, mutation.SHOWN_NAMES)
        self.assertEqual(limits, (50, 40, 10))

    def test_command_never_reads_the_identity_or_the_known_variation(self):
        source = (REPO_ROOT / "src" / "muster" / "mutation.py").read_bytes().decode("ascii")
        self.assertIsNone(re.search(r"\.id\b|\[\"id\"\]|known_variation", source))

    def test_wiring(self):
        analysis = cli.ANALYSES["mutation"]
        self.assertIs(analysis.protected, policy.protected)
        self.assertIs(analysis.inputs, named_files)
        self.assertEqual(analysis.option_inputs(None), [])


class ArgumentTest(MutationCase):
    def test_no_options_of_its_own(self):
        for argv in (["--base", "main"], ["--today", "2026-05-19"], ["--stri"], ["--fail-on"]):
            with self.subTest(argv=argv):
                code, env = self.json_main("mutation", *argv)
                self.assertEqual((code, env["error"]["kind"], env["sections"]), (2, "usage", []))

    def test_target_below_the_threshold_stops_every_command_that_reads_a_profile(self):
        self.write("muster.toml", PROFILE + "threshold = 0.75\n")
        message = "muster.toml: mutation.target: 0.7 (the default) is below mutation.threshold 0.75"
        for command in ("policy", "coverage", "duration", "loc", "mutation"):
            with self.subTest(command=command):
                code, env = self.json_main(command)
                self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": message}))
        self.assertEqual(self.json_main("version")[0], 0)


class RequiredKeyTest(MutationCase):
    def test_missing_keys(self):
        for line, key in (
            ('roots = ["src/main/java"]\n', "source.roots"),
            ('include = ["**/*.java"]\n', "source.include"),
            ('base = "src/main/java/org/example/app"\n', "zones.base"),
            ('critical = "policy/critical.txt"\n', "zones.critical"),
            ('standard = "policy/standard.txt"\n', "zones.standard"),
            ('report = "build/mutation/mutations.xml"\n', "mutation.report"),
            ('targets = "policy/mutation.txt"\n', "mutation.targets"),
            ('own_test = "{stem}Test{ext}"\n', "mutation.own_test"),
            ('[policy]\nsnapshot = "build/muster/snapshot.json"\n', "policy.snapshot"),
        ):
            with self.subTest(key=key):
                self.write("muster.toml", PROFILE.replace(line, ""))
                code, env = self.json_main("mutation")
                error = {"kind": "config", "message": f"muster.toml: missing key {key}"}
                self.assertEqual((code, env["error"]), (2, error))


class PolicyGateTest(MutationCase):
    def test_invalid_policy_with_or_without_a_report(self):
        self.measure()
        self.write("policy/critical.txt", CRITICAL + "gone/**\n")
        for report in (True, False):
            if not report:
                (self.repo / REPORT).unlink()
            for options in ([], ["--strict"]):
                with self.subTest(report=report, options=options):
                    run = self.mutation(*options)
                    self.assertEqual((run.code, run.env["error"]["kind"]), (2, "policy"))
                    self.assertEqual(run.env["error"]["message"], "the policy is invalid: 1 violation")
                    verdict = "mutation: NOT EVALUATED (invalid policy: 1 violation)"
                    self.assertEqual((run.section["verdict_text"], run.section["verdict"]), (verdict, "error"))
                    heading = "## Mutation testing\n\n**NOT EVALUATED**: invalid policy, 1 violation.\n"
                    self.assertTrue(run.summary.startswith(heading), run.summary)
                    self.assertTrue(run.annotations[0].startswith("::error title=Invalid policy::"), run.out)

    def test_listed_file_that_is_not_a_source_file(self):
        self.measure()
        self.write(TARGETS, ALLOWLIST + "auth/Ghost.java\n")
        run = self.mutation("--fail-on-error")
        self.assertEqual((run.code, run.env["error"]["kind"]), (2, "policy"))
        self.assertEqual(run.section["violations"], [f"{TARGETS}: 'auth/Ghost.java' is not a source file"])

    def test_missing_allowlist(self):
        self.measure()
        (self.repo / TARGETS).unlink()
        code, env = self.json_main("mutation")
        message = f"{TARGETS}: not found (mutation.targets)"
        self.assertEqual((code, env["error"], env["sections"]), (2, {"kind": "policy", "message": message}, []))


class CountTest(MutationCase):
    def test_counts_follow_detected_and_leave_no_coverage_out(self):
        items = mutants(ENGINE, "KILLED", 2)
        for status in ("TIMED_OUT", "RUN_ERROR", "MEMORY_ERROR", "NON_VIABLE"):
            items += mutants(ENGINE, status)
        items += mutants(ENGINE, "LATER_STATUS", detected=True)  # a status this muster does not know
        items += mutants(ENGINE, "SURVIVED", 3) + mutants(ENGINE, "NO_COVERAGE", 4) + mutants(TOKEN)
        self.measure(items)
        run = self.passed()
        totals = run.data["totals"]
        counts = tuple(totals[key] for key in ("total", "covered", "no_coverage", "detected", "killed", "survived"))
        self.assertEqual(counts, (15, 11, 4, 8, 3, 3))
        self.assertEqual((totals["strength"], totals["score"]), (8 / 11, 8 / 15))
        self.assertEqual(run.annotations, [])
        self.assertEqual(run.verdict, "mutation: PASS test strength 72.7 % (8/11, >= 60 %)")
        self.assertEqual(run.section["verdict_text"], run.verdict)
        self.assert_rows(run, "| 15 | 3 | 3 | 4 | 5 | 72.7 % | 53.3 % |")
        self.assertEqual(run.data["statuses"]["LATER_STATUS"], 1)
        statuses = "`NO_COVERAGE` 4, `KILLED` 3, `SURVIVED` 3, `LATER_STATUS` 1, `MEMORY_ERROR` 1, `NON_VIABLE` 1"
        self.assertIn(f"\nStatuses: {statuses}, `RUN_ERROR` 1, `TIMED_OUT` 1\n", run.summary)

    def test_threshold_holds_on_equality_and_is_named_as_configured(self):
        for profile, items, verdict in (
            ("", mutants(ENGINE, "KILLED", 3) + mutants(TOKEN, "SURVIVED", 2), "(3/5, >= 60 %)"),
            (
                "threshold = 0.625\n",
                mutants(ENGINE, "KILLED", 5) + mutants(TOKEN, "SURVIVED", 3),
                "(5/8, >= 62.5 %)",
            ),
        ):
            with self.subTest(profile=profile):
                self.write("muster.toml", PROFILE + profile)
                self.measure(items)
                run = self.passed()
                self.assertEqual(run.annotations, [])
                self.assertTrue(run.verdict.startswith("mutation: PASS test strength "), run.verdict)
                self.assertTrue(run.verdict.endswith(verdict), run.verdict)
                threshold = float(profile.split("=")[1]) if profile else DEFAULT_MUTATION_THRESHOLD
                self.assertEqual(run.data["thresholds"], {"threshold": threshold, "target": DEFAULT_MUTATION_TARGET})
        self.write("muster.toml", PROFILE + "threshold = 0.625\n")
        self.measure(mutants(ENGINE, "KILLED", 3) + mutants(TOKEN, "SURVIVED", 2))
        run = self.mutation()
        self.assertEqual(run.verdict, "mutation: BELOW THRESHOLD test strength 60.0 % (3/5, < 62.5 %)")
        self.assertIn("### By file (allowlist entries, ascending by test strength; mark `< 62.5 %`)", run.summary)

    def test_threshold_is_compared_exactly_on_a_float_boundary(self):
        threshold = 0.8333333333333334
        self.assertEqual(5 / 6, threshold)  # the float quotient is the threshold; 5/6 itself is below it
        self.write("muster.toml", PROFILE + f"threshold = {threshold!r}\ntarget = 1.0\n")
        self.measure(mutants(ENGINE, "KILLED", 5) + mutants(TOKEN, "SURVIVED"))
        run = self.mutation()
        self.assertEqual((run.code, run.data["outcome"]), (0, "below"))
        self.assertEqual(run.verdict, "mutation: BELOW THRESHOLD test strength 83.3 % (5/6, < 83.33333333333334 %)")
        self.assertEqual(run.data["thresholds"], {"threshold": threshold, "target": 1.0})

    def test_target_is_reached_on_equality(self):
        for items, text in (
            (mutants(ENGINE, "KILLED", 13) + mutants(TOKEN, "SURVIVED", 7), "pass (>= 60 %); target 70 %: not reached"),
            (mutants(ENGINE, "KILLED", 7) + mutants(TOKEN, "SURVIVED", 3), "pass (>= 60 %); target 70 %: reached"),
        ):
            with self.subTest(text=text):
                self.measure(items)
                self.assertIn(text, self.passed().summary)

    def test_verdict_table(self):
        """Annotation, violation, enum and exit of each outcome."""
        below = mutants(ENGINE, "KILLED", 5) + mutants(TOKEN, "SURVIVED", 4)
        not_covered = mutants(ENGINE) + mutants(TOKEN, "NO_COVERAGE", 2)
        cases = (
            ("pass", mutants(ENGINE) + mutants(TOKEN), [], (None, False, "pass", 0)),
            ("zones not evaluated", None, [], ("warning", False, "warn", 0)),
            ("zones not evaluated", None, ["--strict"], ("warning", False, "warn", 0)),
            ("zones not evaluated", None, ["--fail-on-error"], ("warning", False, "warn", 0)),
            ("listed file without a mutant", mutants(ENGINE), [], ("warning", False, "warn", 0)),
            ("listed file without a mutant", mutants(ENGINE), ["--strict"], ("warning", False, "warn", 0)),
            ("listed file without a mutant", mutants(ENGINE), ["--fail-on-error"], ("warning", False, "warn", 0)),
            ("listed file without a covered mutant", not_covered, [], ("warning", False, "warn", 0)),
            ("listed file without a covered mutant", not_covered, ["--strict"], ("warning", False, "warn", 0)),
            ("listed file without a covered mutant", not_covered, ["--fail-on-error"], ("warning", False, "warn", 0)),
            ("below", below, [], ("warning", True, "warn", 0)),
            ("below", below, ["--strict"], ("error", True, "fail", 1)),
            ("below", below, ["--fail-on-error"], ("warning", True, "warn", 0)),
            ("input error", [], [], ("warning", False, "error", 0)),
            ("input error", [], ["--strict"], ("error", True, "error", 1)),
            ("input error", [], ["--fail-on-error"], ("error", False, "error", 1)),
        )
        for case, items, options, expected in cases:
            with self.subTest(case=case, options=options):
                self.measure(mutants(ENGINE) + mutants(TOKEN) if items is None else items)
                if case == "zones not evaluated":
                    (self.repo / SNAPSHOT).unlink()
                if case == "input error":
                    (self.repo / REPORT).unlink()
                run = self.mutation(*options)
                level = run.annotations[0].split(" ", 1)[0][2:] if run.annotations else None
                state = (level, run.section["violation"], run.section["verdict"], run.code)
                self.assertEqual(state, expected, run.out)
                self.assertEqual(run.env["exit_code"], run.code)

    def test_below_the_threshold_names_the_counts(self):
        self.measure(mutants(ENGINE, "KILLED", 5) + mutants(TOKEN, "SURVIVED", 4))
        run = self.mutation()
        self.assertEqual(run.data["outcome"], "below")
        self.assertEqual(run.verdict, "mutation: BELOW THRESHOLD test strength 55.6 % (5/9, < 60 %)")
        message = "test strength 55.6 % (5/9 covered mutations detected) below 60 %"
        self.assertEqual(run.annotations, [f"::warning title=Mutation testing::{message}"])
        self.assertEqual(run.section["violations"], [message])
        self.assertIn("- below the 60 % threshold; target 70 %: not reached.", run.summary)


class AllowlistTest(MutationCase):
    def test_nested_and_secondary_types_count_for_their_file(self):
        items = mutants(ENGINE, "KILLED", 4)
        items += mutants(ENGINE, "SURVIVED", cls="Engine$Settlement")
        items += mutants(ENGINE, "KILLED", cls="Engine$Settlement")
        items += mutants(ENGINE, cls="LedgerRules") + mutants(TOKEN)
        self.measure(items)
        run = self.passed()
        self.assertEqual(list(run.data["files"]), [ENGINE, TOKEN])
        self.assertEqual((run.data["files"][ENGINE]["total"], run.data["outside"]), (7, []))
        package = "org.example.app.billing.internal.ledger"
        classes = run.data["classes"]
        self.assertEqual(classes[f"{package}.Engine"]["strength"], 1.0)
        self.assertEqual(classes[f"{package}.Engine$Settlement"]["strength"], 0.5)
        self.assertEqual(classes[f"{package}.LedgerRules"]["total"], 1)
        self.assertIn("<details><summary>By class (4; information, not a verdict)</summary>", run.summary)
        self.assert_rows(
            run,
            row(f"`{package}.Engine$Settlement`", 2, 2, 0, 1, "50.0 %", "50.0 %", "< 60 %"),
            row(f"`{package}.Engine`", 4, 4, 0, 4, "100.0 %", "100.0 %", ""),
        )

    def test_file_rows_ascend_by_strength_with_their_notes(self):
        self.measure(mutants(ENGINE, "KILLED", 9) + mutants(TOKEN) + mutants(TOKEN, "SURVIVED", 2))
        run = self.passed()
        rows = [line for line in run.summary.splitlines() if line.startswith("| `src/")]
        self.assertEqual(
            rows,
            [
                row(f"`{TOKEN}`", 3, 3, 0, 1, "33.3 %", "33.3 %", "< 60 %"),
                row(f"`{ENGINE}`", 9, 9, 0, 9, "100.0 %", "100.0 %", ""),
            ],
        )
        self.assertEqual(run.data["files"][TOKEN]["notes"], ["< 60 %"])

    def test_comment_and_blank_lines_of_the_allowlist_are_skipped(self):
        self.write(TARGETS, "# Pure logic.\n\n  billing/internal/ledger/Engine.java  \n# auth/Token.java\n")
        self.measure(mutants(ENGINE))
        run = self.passed()
        self.assertEqual((list(run.data["files"]), run.annotations), ([ENGINE], []))

    def test_listed_file_without_a_mutant_warns_and_keeps_its_row(self):
        self.measure(mutants(TOKEN, "KILLED", 2))
        run = self.passed()
        self.assertEqual(run.data["no_mutations"], [ENGINE])
        message = f"1 allowlisted file(s) without any mutation: {ENGINE}"
        self.assertEqual(run.annotations, [f"::warning title=Mutation allowlist::{message}"])
        # The n/a row comes first, though its name sorts after the other one.
        rows = [line for line in run.summary.splitlines() if line.startswith("| `src/")]
        self.assertEqual(
            rows,
            [
                row(f"`{ENGINE}`", 0, 0, 0, 0, "n/a", "n/a", "0 mutations"),
                row(f"`{TOKEN}`", 2, 2, 0, 2, "100.0 %", "100.0 %", ""),
            ],
        )

    def test_listed_file_without_a_covered_mutant_warns_with_strength_na(self):
        self.measure(mutants(ENGINE, "NO_COVERAGE", 3) + mutants(TOKEN, "KILLED", 2))
        run = self.passed()
        self.assertEqual(run.data["not_covered"], [ENGINE])
        self.assertIsNone(run.data["files"][ENGINE]["strength"])
        message = f"1 allowlisted file(s) not covered by tests (no covered mutant): {ENGINE}"
        self.assertEqual(run.annotations, [f"::warning title=Mutation allowlist::{message}"])
        # The n/a row comes first, though its name sorts after the other one.
        rows = [line for line in run.summary.splitlines() if line.startswith("| `src/")]
        self.assertEqual(
            rows,
            [
                row(f"`{ENGINE}`", 3, 0, 3, 0, "n/a", "0.0 %", "not covered by tests"),
                row(f"`{TOKEN}`", 2, 2, 0, 2, "100.0 %", "100.0 %", ""),
            ],
        )

    def test_mutated_file_off_the_allowlist_is_an_error_and_keeps_its_row(self):
        self.measure(mutants(ENGINE, "KILLED", 2) + mutants(TOKEN) + mutants(PARSER))
        message = f"1 mutated file(s) not on the allowlist: {PARSER}"
        for options, code, level in (([], 0, "warning"), (["--fail-on-error"], 1, "error"), (["--strict"], 1, "error")):
            with self.subTest(options=options):
                run = self.mutation(*options)
                self.assertEqual(run.code, code)
                self.assertEqual(run.annotations, [f"::{level} title=Mutation allowlist::{message}"])
                self.assertEqual((run.section["errors"], run.data["outcome"]), ([message], "error"))
                self.assertEqual(run.verdict, f"mutation: ERROR ({message})")
        self.assertEqual((run.data["outside"], run.data["totals"]["total"]), ([PARSER], 4))
        self.assertEqual(run.data["zones"]["standard"]["files"], 1)
        self.assertIn(f"**ERROR**: no verdict:\n\n- `{message}`\n", run.summary)
        self.assertIn("- no verdict (input error); target 70 %: reached.", run.summary)
        self.assert_rows(run, row(f"`{PARSER}`", 1, 1, 0, 1, "100.0 %", "100.0 %", "not on the allowlist"))

    def test_report_without_a_covered_mutant_is_an_error(self):
        self.measure(mutants(ENGINE, "NO_COVERAGE", 2) + mutants(TOKEN, "NO_COVERAGE"))
        message = "no covered mutant among 3 mutation(s): test strength n/a"
        for options, code, level in (([], 0, "warning"), (["--fail-on-error"], 1, "error"), (["--strict"], 1, "error")):
            with self.subTest(options=options):
                run = self.mutation(*options)
                self.assertEqual((run.code, run.data["outcome"]), (code, "error"))
                self.assertEqual(run.verdict, f"mutation: ERROR ({message})")
                self.assertNotIn("PASS", run.out)
                self.assertEqual(run.annotations[-1], f"::{level} title=Mutation testing::{message}")
        self.assertIn(f"**ERROR**: no verdict:\n\n- `{message}`\n", run.summary)
        gate = "**Test strength n/a** (0/0 covered mutations detected) - no verdict (no covered mutant);"
        self.assertIn(gate, run.summary)
        self.assertIn("### By file", run.summary)

    def test_unlisted_file_and_no_covered_mutant_are_two_errors(self):
        self.measure(mutants(ENGINE, "NO_COVERAGE") + mutants(TOKEN, "NO_COVERAGE") + mutants(PARSER, "NO_COVERAGE"))
        run = self.mutation()
        first = f"1 mutated file(s) not on the allowlist: {PARSER}"
        second = "no covered mutant among 3 mutation(s): test strength n/a"
        self.assertEqual((run.code, run.data["outcome"]), (0, "error"))
        self.assertEqual((run.section["errors"], run.section["violations"]), ([first, second], []))
        self.assertEqual(run.verdict, f"mutation: ERROR ({first})")
        self.assertIn(f"**ERROR**: no verdict:\n\n- `{first}`\n- `{second}`\n\n", run.summary)
        self.assertIn("- no verdict (input error); target 70 %: not reached.", run.summary)

    def test_unlisted_file_and_a_strength_below_the_threshold_are_one_error(self):
        self.measure(mutants(ENGINE) + mutants(TOKEN, "SURVIVED", 2) + mutants(PARSER))
        run = self.mutation()
        message = f"1 mutated file(s) not on the allowlist: {PARSER}"
        self.assertEqual((run.code, run.data["outcome"]), (0, "error"))
        self.assertEqual((run.section["errors"], run.section["violations"]), ([message], []))
        self.assertEqual(run.verdict, f"mutation: ERROR ({message})")
        gate = "**Test strength 50.0 %** (2/4 covered mutations detected) - no verdict (input error);"
        self.assertIn(gate, run.summary)

    def test_allowlist_message_names_at_most_ten_files(self):
        drafts = [f"Draft{nr:02}.java" for nr in range(1, 13)]
        items = mutants(ENGINE) + mutants(TOKEN)
        for name in drafts:
            items += mutants(ENGINE, package="org.example.app.drafts", file=name)
        self.measure(items)
        run = self.mutation()
        names = [f"org/example/app/drafts/{name}" for name in drafts]
        message = "12 mutated file(s) not on the allowlist: " + ", ".join(names[:10]) + " (+2 more)"
        self.assertEqual(run.section["errors"], [message])
        self.assertEqual(run.annotations[-1], f"::warning title=Mutation allowlist::{message}")
        self.assertEqual(run.verdict, f"mutation: ERROR ({message})")
        self.assertEqual(sorted(run.data["unresolved"]), names)

    def test_unresolved_name_keeps_its_own_key_space(self):
        # The report name of the second mutant has the text of the path of the first one.
        twin = mutants(ENGINE, package="src.main.java.org.example.app.billing.internal.ledger", detected=True)
        self.measure(mutants(ENGINE) + twin + mutants(TOKEN))
        run = self.mutation()
        self.assertEqual(run.data["files"][ENGINE]["total"], 1)
        self.assertEqual(run.data["unresolved"][ENGINE]["total"], 1)
        self.assertEqual(run.data["unresolved"][ENGINE]["notes"], ["not on the allowlist", "under no source root"])
        self.assertEqual(run.data["outside"], [])
        self.assertEqual(run.section["errors"], [f"1 mutated file(s) not on the allowlist: {ENGINE}"])
        reason = f"1 mutated source file(s) not in the classification set (e.g. {ENGINE})"
        self.assertEqual((run.data["zones"], run.data["zone_reasons"]), (None, [reason]))
        rows = [line for line in run.summary.splitlines() if line.startswith(f"| `{ENGINE}`")]
        self.assertEqual(len(rows), 2)
        notes = "not on the allowlist; under no source root"
        self.assertIn(row(f"`{ENGINE}`", 1, 1, 0, 1, "100.0 %", "100.0 %", notes), rows)
        self.assertIn(row(f"`{ENGINE}`", 1, 1, 0, 1, "100.0 %", "100.0 %", ""), rows)


class ZoneTest(MutationCase):
    def test_zone_table_counts_the_listed_files_of_each_zone(self):
        self.measure(mutants(ENGINE, "KILLED", 3) + mutants(ENGINE, "NO_COVERAGE") + mutants(TOKEN, "SURVIVED", 2))
        run = self.passed()
        self.assertEqual(run.data["zone_reasons"], [])
        zones = run.data["zones"]
        self.assertEqual(list(zones), ["Z1", "Z2", "standard"])
        counts = [(zones[zone]["files"], zones[zone]["total"], zones[zone]["detected"]) for zone in zones]
        self.assertEqual(counts, [(1, 4, 3), (1, 2, 0), (0, 0, 0)])
        self.assertIn("### By zone\n\n| zone | mutations | covered | NO_COVERAGE | detected |", run.summary)
        self.assert_rows(
            run,
            row("Z1", 4, 3, 1, 3, "100.0 %", "75.0 %", 1),
            row("Z2", 2, 2, 0, 0, "0.0 %", "0.0 %", 1),
            row("standard", 0, 0, 0, 0, "n/a", "n/a", 0),
        )

    def test_zones_are_ordered_by_number_past_nine(self):
        self.write("policy/critical.txt", CRITICAL + "# Zone 10: parsing.\ncodec/**\n")
        self.write("policy/standard.txt", "queue/*\n")
        self.measure()
        run = self.passed()
        self.assertEqual(list(run.data["zones"]), ["Z1", "Z2", "Z10", "standard"])
        names = re.findall(r"^\| (Z\d+|standard) \|", run.summary, re.MULTILINE)
        self.assertEqual(names, ["Z1", "Z2", "Z10", "standard"])

    def test_freshness_problem_is_one_warning_in_every_mode(self):
        """Checks 1 and 2, and a snapshot that is missing or broken: the zones are left out with
        one warning, never an error or a violation; the totals and the file table stay."""

        def policy_changed():
            self.write("policy/critical.txt", CRITICAL + "# The zones are reviewed every quarter.\n")

        def source_added():
            self.write(JOURNAL, "class Journal {}\n")

        def broken():
            text = (self.repo / SNAPSHOT).read_bytes().decode("ascii")
            self.write(SNAPSHOT, text.replace('"schema": 1', '"schema": 2'))

        def folder():
            (self.repo / SNAPSHOT).unlink()
            (self.repo / SNAPSHOT).mkdir()

        changed = "the source set changed after the snapshot"
        cases = (
            ("policy changed", None, policy_changed, "the policy changed after the snapshot (policy_sha256 differs)"),
            ("source added", None, source_added, f"{changed}: 1 added (e.g. {JOURNAL})"),
            (
                "source gone",
                lambda: self.write(JOURNAL, "class Journal {}\n"),
                lambda: (self.repo / JOURNAL).unlink(),
                f"{changed}: 1 gone (e.g. {JOURNAL})",
            ),
            ("missing snapshot", None, lambda: (self.repo / SNAPSHOT).unlink(), NO_SNAPSHOT),
            ("broken snapshot", None, broken, f"{SNAPSHOT}: not a snapshot: schema 2 is not supported (only 1)"),
            ("snapshot is a folder", None, folder, f"{SNAPSHOT}: not a file"),
        )
        for case, before, after, reason in cases:
            if before:
                before()
            self.measure(mutants(ENGINE, "KILLED", 3) + mutants(TOKEN, "SURVIVED"))
            after()
            for options in ([], ["--strict"], ["--fail-on-error"]):
                with self.subTest(case=case, options=options):
                    run = self.mutation(*options)
                    self.assertEqual((run.code, run.data["outcome"], run.section["verdict"]), (0, "pass", "warn"))
                    self.assertEqual((run.data["zones"], run.data["zone_reasons"]), (None, [reason]))
                    warning = f"::warning title=Mutation zones::zones not evaluated: {reason}"
                    self.assertEqual(run.annotations, [warning])
                    self.assertEqual((run.section["errors"], run.section["violations"]), ([], []))
                    self.assertEqual(run.verdict, "mutation: PASS test strength 75.0 % (3/4, >= 60 %)")
                    self.assertIn(f"Zones not evaluated (stale or missing snapshot):\n\n- `{reason}`\n", run.summary)
                    self.assertEqual((run.data["totals"]["total"], run.data["files"][ENGINE]["detected"]), (4, 3))
                    self.assertIn("### By file", run.summary)
            if (self.repo / SNAPSHOT).is_dir():
                (self.repo / SNAPSHOT).rmdir()
            self.write("policy/critical.txt", CRITICAL)
            (self.repo / JOURNAL).unlink(missing_ok=True)

    def test_allowlist_and_mutation_table_are_not_part_of_the_snapshot(self):
        self.measure(mutants(ENGINE))
        self.write(TARGETS, "billing/internal/ledger/Engine.java\n")
        self.write("muster.toml", PROFILE + "threshold = 0.5\ntarget = 0.9\n")
        run = self.passed()
        self.assertEqual(run.data["zone_reasons"], [])
        self.assertIsNotNone(run.data["zones"])

    def test_mutated_file_outside_the_classification_set_is_a_zone_reason_and_an_error(self):
        ghost = {"package": "org.example.app.ghost", "file": "Ghost.java"}
        for case, extra, name in (
            ("under no source root", mutants(ENGINE, **ghost), "org/example/app/ghost/Ghost.java"),
            ("excluded", mutants(PACKAGE_INFO, cls="package-info"), PACKAGE_INFO),
        ):
            for snapshot in (True, False):
                with self.subTest(case=case, snapshot=snapshot):
                    self.measure(mutants(ENGINE) + mutants(TOKEN) + extra)
                    if not snapshot:
                        (self.repo / SNAPSHOT).unlink()
                    run = self.mutation()
                    reason = f"1 mutated source file(s) not in the classification set (e.g. {name})"
                    self.assertEqual(run.data["zone_reasons"], ([] if snapshot else [NO_SNAPSHOT]) + [reason])
                    self.assertEqual(run.section["errors"], [f"1 mutated file(s) not on the allowlist: {name}"])
                    self.assertEqual((run.code, run.data["outcome"]), (0, "error"))

    def test_name_under_two_roots_or_nested_roots_is_a_config_error(self):
        two = PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "gen/main/java"]')
        two = two.replace('exclude = ["**/package-info.java"]', 'exclude = ["**/package-info.java", "gen/**"]')
        nested = PROFILE.replace('roots = ["src/main/java"]', f'roots = ["src/main/java", "{BASE}/codec"]')
        for case, profile, items, message in (
            (
                "two roots",
                two,
                mutants(TOKEN),
                f"{REPORT}: org/example/app/auth/Token.java is under more than one source root:"
                f" gen/main/java/org/example/app/auth/Token.java, {TOKEN}",
            ),
            (
                "nested roots",
                nested,
                mutants(PARSER) + mutants(PARSER, package=""),
                f"{REPORT}: org/example/app/codec/KeyParser.java and KeyParser.java are both {PARSER}"
                " (the source roots nest)",
            ),
        ):
            with self.subTest(case=case):
                self.write("muster.toml", profile)
                self.write("gen/main/java/org/example/app/auth/Token.java", "class Token {}\n")
                self.measure(items, snapshot=False)
                code, env = self.json_main("mutation")
                self.assertEqual((code, env["error"], env["sections"]), (2, {"kind": "config", "message": message}, []))


class InputErrorTest(MutationCase):
    """A report that is missing or cannot be read: errors, only the heading; exit 0, and 1 with
    --strict or --fail-on-error, never 3. No snapshot is written, so a zone warning would show."""

    def test_report_that_cannot_be_read(self):
        valid = mutation_xml(mutants(ENGINE) + mutants(TOKEN))
        first = f"{REPORT}: mutation 1"
        cases = {
            "missing": (None, f"no PIT report at {REPORT}"),
            "a folder": (None, f"{REPORT}: not a file"),
            "not well-formed": (("</mutations>", ""), f"{REPORT}: not well-formed XML: "),
            "no mutations": (None, f"{REPORT} has no mutations"),
            "other root": (None, f"{REPORT}: not a PIT report, the root element is <report>"),
            "no detected": (("detected='true' ", ""), f"{first} has no attribute detected"),
            "contradicting record": (
                ("detected='true'", "detected='false'"),
                f"{first}: status KILLED contradicts detected=false",
            ),
            "line beyond a long": (
                ("<lineNumber>1<", f"<lineNumber>{LONG_MAX + 1}<"),
                f"{first}: <lineNumber> must be from 0 to {LONG_MAX}",
            ),
            "repeated element": (
                ("</sourceFile>", "</sourceFile><sourceFile>Other.java</sourceFile>"),
                f"{first} has <sourceFile> twice",
            ),
            "bad name component": (("<sourceFile>Engine.java<", "<sourceFile>..<"), f"{first}: bad <sourceFile> '..'"),
            "control character": (
                ("<mutatedMethod>apply<", "<mutatedMethod>ap" + chr(0x85) + "ply<"),
                f"{first}: <mutatedMethod> has a control character",
            ),
            "no simple mutator name": (
                (MUTATORS + "MathMutator", "org.pitest."),
                f"{first}: <mutator> 'org.pitest.' has no simple name",
            ),
            "empty index": (
                ("<index>5</index>", "<index/>"),
                f"{first}: <index> must be a non-negative integer, got ''",
            ),
        }
        for case, (change, error) in cases.items():
            if (self.repo / REPORT).is_dir():
                (self.repo / REPORT).rmdir()
            (self.repo / REPORT).unlink(missing_ok=True)
            if case == "a folder":
                (self.repo / REPORT).mkdir(parents=True)
            elif case == "no mutations":
                self.write(REPORT, '<?xml version="1.0" encoding="UTF-8"?>\n<mutations partial="true">\n</mutations>\n')
            elif case == "other root":
                self.write(REPORT, "<report/>\n")
            elif change is not None:
                self.assertIn(change[0], valid)
                self.write(REPORT, valid.replace(change[0], change[1], 1))
            for options, code in (([], 0), (["--strict"], 1), (["--fail-on-error"], 1)):
                with self.subTest(case=case, options=options):
                    run = self.mutation(*options)
                    self.assertEqual(run.code, code, run.out + run.err)
                    errors = run.section["errors"]
                    self.assertEqual(len(errors), 1)
                    exact = case != "not well-formed"  # the rest of that message is the XML parser's
                    self.assertTrue(errors[0] == error if exact else errors[0].startswith(error), errors)
                    data = {"outcome": "error", "thresholds": THRESHOLDS}
                    self.assertEqual((run.section["verdict"], run.data), ("error", data))
                    self.assertEqual(run.verdict, f"mutation: ERROR ({errors[0]})")
                    level = "error" if options else "warning"
                    self.assertEqual(run.annotations, [f"::{level} title=Mutation testing::{errors[0]}"])
                    heading = "## Mutation testing\n\n**ERROR**: the inputs could not be read, no verdict:\n\n"
                    self.assertEqual(run.summary, f"{heading}- `{errors[0]}`\n\n")

    def test_bug_in_the_adapter_exits_three(self):
        self.measure()
        with mock.patch("muster.adapters.pit.load", side_effect=RuntimeError("boom")):
            run = self.mutation()
        self.assertEqual((run.code, run.env["error"]), (3, {"kind": "internal", "message": "RuntimeError: boom"}))


class MarkdownTest(MutationCase):
    def test_summary_is_appended_with_the_head_versions_and_mutators(self):
        self.write("muster.toml", PROFILE + TOOL_VERSIONS)
        self.write(VERSIONS, '[versions]\nmutation-plugin = "9.8.7"\nmutation-tool = "4.5.6"\nother = "1.0"\n')
        self.measure(mutants(ENGINE, "KILLED", 2) + mutants(TOKEN, "SURVIVED", mutator="returns.NullReturnValsMutator"))
        self.summary.write_bytes(b"earlier step\n")
        run = self.mutation(keep_summary=True)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(run.env["commit"], head)
        self.assertTrue(run.summary.startswith("earlier step\n## Mutation testing\n\n"), run.summary)
        self.assertIn(f"\nCommit `{head}` - mutation-plugin `9.8.7` - mutation-tool `4.5.6`\n", run.summary)
        self.assertIn("\nMutators observed (2): `MathMutator` 2, `NullReturnValsMutator` 1\n", run.summary)
        self.assertEqual(run.data["versions"], {"mutation-plugin": "9.8.7", "mutation-tool": "4.5.6"})
        self.assertEqual(run.data["mutators"], {"MathMutator": 2, "NullReturnValsMutator": 1})

    def test_commit_is_unknown_without_a_head(self):
        self.git("checkout", "-q", "--orphan", "empty")
        self.measure()
        run = self.passed()
        self.assertIsNone(run.env["commit"])
        self.assertIn("\nCommit `unknown`\n", run.summary)
        self.assertEqual(run.data["versions"], {})

    def test_tool_versions(self):
        self.write("muster.toml", PROFILE + TOOL_VERSIONS)
        self.measure()
        unknown = {"mutation-plugin": "unknown", "mutation-tool": "unknown"}
        good = '[versions]\nmutation-plugin = "9.8.7"\nmutation-tool = "4.5.6"\n'
        cases = (
            ("read", good.encode("ascii"), {"mutation-plugin": "9.8.7", "mutation-tool": "4.5.6"}),
            ("missing", None, unknown),
            ("a folder", "folder", unknown),
            ("another case", "case", unknown),
            ("not UTF-8", b'[versions]\nmutation-plugin = "9.8.7"\n# ' + bytes([0xFF]) + b"\n", unknown),
            ("not TOML", b"[versions\n", unknown),
            ("nested too deeply", b"[versions]\nmutation-plugin = " + b"[" * 2000 + b"]" * 2000 + b"\n", unknown),
            ("versions not a table", b'versions = "9.8.7"\n', unknown),
            ("no versions", b'[plugins]\nmutation-plugin = "9.8.7"\n', unknown),
            (
                "values that are no version",
                b'[versions]\nmutation-plugin = { strictly = "9.8.7" }\nmutation-tool = 2026-05-19\n',
                unknown,
            ),
            (
                "one empty, one with a control character",
                b'[versions]\nmutation-plugin = ""\nmutation-tool = "4.5\\u0007.6"\n',
                unknown,
            ),
            ("one key missing", b'[versions]\nmutation-tool = "4.5.6"\n', {**unknown, "mutation-tool": "4.5.6"}),
        )
        file = self.repo / VERSIONS
        for case, content, expected in cases:
            with self.subTest(case=case):
                if file.is_dir():
                    file.rmdir()
                file.unlink(missing_ok=True)
                (self.repo / "build/Versions.toml").unlink(missing_ok=True)
                if content == "folder":
                    file.mkdir(parents=True)
                elif content == "case":
                    self.write("build/Versions.toml", good)
                elif content is not None:
                    self.write(VERSIONS, content)
                run = self.passed()
                self.assertEqual(run.data["versions"], expected)
                line = " - ".join([f"Commit `{run.env['commit']}`", *(f"{k} `{v}`" for k, v in expected.items())])
                self.assertIn(f"\n{line}\n", run.summary)
        self.write("muster.toml", PROFILE + 'tool_versions = { file = "build/versions.toml" }\n')
        self.assertEqual(self.passed().data["versions"], {})

    def test_report_text_stays_on_one_line_and_inside_its_cell(self):
        odd = "Token$Odd|Part"
        description = "replaced `a`" + chr(10) + "with | b"
        weird = {"package": "org.example.app.auth", "file": "We|rd.java"}
        self.measure(
            mutants(ENGINE)
            + mutants(TOKEN, "SURVIVED", cls=odd, description=description)
            + mutants(TOKEN, "KILLED")
            + mutants(ENGINE, "SURVIVED", **weird)
        )
        run = self.mutation()
        notes = "not on the allowlist; under no source root; < 60 %"
        self.assert_rows(
            run,
            row("`org.example.app.auth.Token$Odd\\|Part`", 1, 1, 0, 0, "0.0 %", "0.0 %", "< 60 %"),
            row("`org/example/app/auth/We\\|rd.java`", 1, 1, 0, 0, "0.0 %", "0.0 %", notes),
        )
        self.assertIn(f"- `{TOKEN}` (1)\n  - `{odd}` L{1} `MathMutator`: `replaced 'a' with | b`\n", run.summary)
        self.assertIn("- `org/example/app/auth/We|rd.java` (1)\n", run.summary)
        self.assertIn(f"org.example.app.auth.{odd}", run.data["classes"])
        self.assertNotIn(chr(10) + "with", run.summary)

    def test_error_and_reason_bullets_are_code_spans(self):
        draft = {"package": "org.example.app.drafts", "file": "__Draft__*.java"}
        self.measure(mutants(ENGINE) + mutants(TOKEN) + mutants(ENGINE, **draft))
        run = self.mutation()
        name = "org/example/app/drafts/__Draft__*.java"
        error = f"1 mutated file(s) not on the allowlist: {name}"
        reason = f"1 mutated source file(s) not in the classification set (e.g. {name})"
        self.assertEqual((run.section["errors"], run.data["zone_reasons"]), ([error], [reason]))
        self.assertEqual(run.annotations[-1], f"::warning title=Mutation allowlist::{error}")
        self.assertIn(f"**ERROR**: no verdict:\n\n- `{error}`\n\n", run.summary)
        self.assertIn(f"Zones not evaluated (stale or missing snapshot):\n\n- `{reason}`\n\n", run.summary)
        self.assertNotIn(f"- {error}", run.summary)
        self.assertNotIn(f"- {reason}", run.summary)

    def test_survivor_without_a_description_has_no_empty_span(self):
        self.measure(mutants(ENGINE, "KILLED", 3) + mutants(TOKEN, "SURVIVED", 2, description=""))
        run = self.passed()
        item = "  - `Token` L{} `MathMutator`\n"
        self.assertIn(f"- `{TOKEN}` (2)\n" + item.format(1) + item.format(2), run.summary)
        self.assertNotIn("``", run.summary)

    def test_text_of_only_white_space_is_a_span_of_one_space(self):
        self.measure(mutants(ENGINE, "KILLED", 3) + mutants(TOKEN, " ", mutator=" ", description=" "))
        run = self.passed()
        self.assertIn("Mutators observed (2): `MathMutator` 3, ` ` 1\n", run.summary)
        self.assertIn("Statuses: `KILLED` 3, ` ` 1\n", run.summary)
        self.assertIn(f"- `{TOKEN}` (1)\n  - `Token` L{1} ` `\n", run.summary)
        self.assertNotIn("``", run.summary)

    def test_survivors_are_listed_by_file_with_a_cap(self):
        fields = {"cls": "Token$Inner", "mutator": "VoidMethodCallMutator", "description": "removed call"}
        items = mutants(TOKEN, "SURVIVED", 5, **fields)
        items += mutants(ENGINE, "KILLED", 20) + mutants(ENGINE, "SURVIVED")
        self.measure(items)
        with mock.patch.object(mutation, "SURVIVED_ITEMS_CAP", 2), mock.patch.object(mutation, "SURVIVED_FILES_CAP", 1):
            run = self.mutation()
        item = "`Token$Inner` L{} `VoidMethodCallMutator`: `removed call`"
        self.assertIn(
            f"### Survived mutants (6)\n\n- `{TOKEN}` (5)\n  - {item.format(1)}\n  - {item.format(2)}\n"
            "  - ... (+3 more)\n- ... 1 more file(s)\n\n",
            run.summary,
        )
        self.assertNotIn(item.format(3), run.summary)
        self.assertNotIn(f"- `{ENGINE}`", run.summary)

    def test_survivors_are_the_undetected_mutants_a_test_reached(self):
        self.measure(
            mutants(ENGINE, "KILLED", 4)
            + mutants(ENGINE, "NOT_STARTED", line=5, detected=False)
            + mutants(ENGINE, "NO_COVERAGE", line=6)
            + mutants(TOKEN)
        )
        run = self.passed()
        item = f"`Engine` L{5} `MathMutator`: `replaced addition with subtraction`"
        self.assertIn(f"### Survived mutants (1)\n\n- `{ENGINE}` (1)\n  - {item}\n\n", run.summary)
        self.assertNotIn(f"`Engine` L{6}", run.summary)

    def test_data_has_no_killing_tests_or_indexes(self):
        killer = "org.example.app.billing.internal.ledger.EngineTest.[engine:junit-jupiter]/[method:settles()]"
        self.measure(mutants(ENGINE, killing=killer, indexes=(1, 2)) + mutants(TOKEN, "SURVIVED"))
        run = self.mutation()
        self.assertEqual(run.verdict, "mutation: BELOW THRESHOLD test strength 50.0 % (1/2, < 60 %)")
        text = json.dumps(run.data)
        self.assertEqual(json.loads(text), run.data)
        for word in ("killing", "indexes", "EngineTest", "settles"):
            self.assertNotIn(word, text)
            self.assertNotIn(word, run.summary.replace("killed", ""))
        self.assertEqual(
            sorted(run.data),
            sorted(
                "outcome totals statuses mutators files classes zones zone_reasons no_mutations not_covered"
                " outside unresolved versions thresholds".split()
            ),
        )

    def test_known_variation_changes_nothing(self):
        self.measure(mutants(ENGINE, "KILLED", 2) + mutants(TOKEN, "SURVIVED", method="check"))
        before = self.mutation()
        entry = '\n[[mutation.known_variation]]\nclass = "Token"\nmethod = "check"\nline = 1\nmutator = "MathMutator"\n'
        self.write("muster.toml", PROFILE + entry)
        after = self.mutation()
        self.assertEqual((after.data, after.summary, after.verdict), (before.data, before.summary, before.verdict))


class OutputTest(MutationCase):
    def test_output_that_is_a_file_the_command_reads(self):
        self.write("muster.toml", PROFILE + TOOL_VERSIONS)
        self.write(VERSIONS, '[versions]\nmutation-tool = "4.5.6"\n')
        self.measure()
        files = (("mutation.report", REPORT), ("mutation.targets", TARGETS), ("mutation.tool_versions.file", VERSIONS))
        for key, path in files:
            target = self.repo / path
            before = target.read_bytes()
            for option in ("--json-out", "--summary-file"):
                for bad_argument in (False, True):
                    with self.subTest(key=key, option=option, bad_argument=bad_argument):
                        extra = ["--bogus"] if bad_argument else []
                        code, _, err = self.main("mutation", option, str(target), *extra)
                        self.assertEqual(code, 2)
                        self.assertIn(f"{option} {target} is the file of {key}", err)
                        self.assertEqual(target.read_bytes(), before)

    def test_source_file_is_no_output(self):
        self.measure()
        before = (self.repo / ENGINE).read_bytes()
        code, env = self.json_main("mutation", "--json-out", str(self.repo / ENGINE))
        message = f"--json-out {self.repo / ENGINE} is under the source folder src/main/java"
        self.assertEqual((code, env["error"]), (2, {"kind": "usage", "message": message}))
        self.assertEqual((self.repo / ENGINE).read_bytes(), before)


class SubfolderProfileTest(MutationCase):
    SUBFOLDER = "service"

    def test_profile_paths_and_the_default_report_are_anchored_at_the_profile(self):
        self.write("muster.toml", PROFILE.replace("[policy]", '[output]\ndir = "build/muster"\n\n[policy]'))
        self.measure()
        code, _, err = self.main("mutation", "--json-out", "out.json")
        self.assertEqual(code, 0, err)
        env = json.loads((self.work / "out.json").read_bytes())
        data = env["sections"][0]["data"]
        self.assertEqual((data["outcome"], data["zone_reasons"]), ("pass", []))
        self.assertEqual(env["commit"], self.git("rev-parse", "HEAD"))
        report = (self.repo / "build/muster/mutation.md").read_bytes().decode("ascii")
        self.assertTrue(report.startswith(f"## Mutation testing\n\nCommit `{env['commit']}`\n"), report)
        self.assertFalse((self.top / "build").exists())


if __name__ == "__main__":
    unittest.main()
