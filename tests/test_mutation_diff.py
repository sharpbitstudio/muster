"""Tests of the mutation-diff command, end to end through cli.main: the options, the comparison of
two PIT reports mutant by mutant, the verdict, the killing tests of the lost mutants, the configured
known variation, the Markdown and the data, the input errors and the files no output may be.

Every run happens in temporary folders, with git's global and system configuration switched off.
The reports and the JUnit folders lie in the working directory, which is not the repository.
Control characters are built at run time with chr(); so is every line token, an L and its number.
"""

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape, quoteattr

from muster import cli, diffs, policy
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
TEST_FOLDER = "src/test/java"
ENGINE = f"{BASE}/billing/internal/ledger/Engine.java"
PARSER = f"{BASE}/codec/KeyParser.java"
LEDGER = "org.example.app.billing.internal.ledger"
ENGINE_CLASS = f"{LEDGER}.Engine"
PARSER_CLASS = "org.example.app.codec.KeyParser"
ENGINE_TEST = f"{LEDGER}.EngineTest"
PARSER_TEST = "org.example.app.codec.KeyParserTest"
ENGINE_TEST_FILE = f"{TEST_FOLDER}/org/example/app/billing/internal/ledger/EngineTest.java"
REPORT = "build/mutation/mutations.xml"  # mutation.report, which mutation-diff never reads
PROFILE = """\
schema = 1

[source]
roots = ["src/main/java"]
tests = ["src/test/java"]
include = ["**/*.java"]

[zones]
base = "src/main/java/org/example/app"
critical = "policy/critical.txt"
standard = "policy/standard.txt"

[mutation]
report = "build/mutation/mutations.xml"
"""
# The known variation of KeyParser.decode on line 12, and of a lambda of the nested class Engine$Cursor.
KNOWN = """
[[mutation.known_variation]]
class = "KeyParser"
method = "decode"
line = 12
mutator = "MathMutator"

[[mutation.known_variation]]
class = "Engine$Cursor"
method = "lambda$next$0"
line = 31
mutator = "NegateConditionalsMutator"
"""
CRITICAL = "# Zone 1: money.\nbilling/**\n"
STANDARD = "codec/*\n"
MUTATORS = "org.pitest.mutationtest.engine.gregor.mutators."
DETECTED = ("KILLED", "TIMED_OUT", "NON_VIABLE", "MEMORY_ERROR", "RUN_ERROR")
HEADING = "## Mutation diff\n\n"
ERROR_BLOCK = HEADING + "**ERROR**: the inputs could not be read, no verdict:\n\n"
SOURCES_LINE = (
    "A killing test hidden by a display name or a parameterized invocation in the JUnit XML is resolved"
    " against the test sources in `src/test/java` of the repository, the tree after the trim."
)
TOLERANCE = "Known variation (configured, a tolerance): the one mutant of the two runs that an entry of"
AMBIGUOUS = "No tolerance for an entry of `[[mutation.known_variation]]` that names more than one mutant: "


def mutant(cls: str = ENGINE_CLASS, status: str = "KILLED", line: int = 7, **fields) -> dict:
    """One mutant of a PIT report; `detected` defaults to whether the status is one of DETECTED."""
    return {"cls": cls, "status": status, "line": line, **fields}


def pit_report(*items: dict) -> str:
    """A PIT 1.x mutations.xml; the source file of a mutant is its top-level class, unless `file`."""
    rows = []
    for item in items:
        simple = item["cls"].rpartition(".")[2]
        detected = item.get("detected", item["status"] in DETECTED)
        indexes = "".join(f"<index>{index}</index>" for index in item.get("indexes", (5,)))
        killing = item.get("killing", "")
        rows.append(
            f"<mutation detected='{'true' if detected else 'false'}' status={quoteattr(item['status'])}"
            " numberOfTestsRun='2'>"
            f"<sourceFile>{escape(item.get('file', simple.partition('$')[0] + '.java'))}</sourceFile>"
            f"<mutatedClass>{escape(item['cls'])}</mutatedClass>"
            f"<mutatedMethod>{escape(item.get('method', 'post'))}</mutatedMethod>"
            f"<methodDescription>{escape(item.get('descriptor', '(I)I'))}</methodDescription>"
            f"<lineNumber>{item['line']}</lineNumber>"
            f"<mutator>{escape(MUTATORS + item.get('mutator', 'MathMutator'))}</mutator>"
            f"<indexes>{indexes}</indexes><blocks><block>0</block></blocks>"
            + (f"<killingTest>{escape(killing)}</killingTest>" if killing else "<killingTest/>")
            + "<description>replaced integer addition with subtraction</description></mutation>"
        )
    return '<?xml version="1.0" encoding="UTF-8"?>\n<mutations partial="true">\n' + "\n".join(rows) + "\n</mutations>\n"


def text(cls: str = ENGINE_CLASS, line: int = 7, **fields) -> str:
    """The text of a mutant as the command writes it, the mutator by its simple name."""
    mutator = fields.get("mutator", "MathMutator").rpartition(".")[2]
    indexes = ",".join(map(str, fields.get("indexes", (5,))))
    return f"{cls}.{fields.get('method', 'post')}{fields.get('descriptor', '(I)I')} L{line} {mutator} [{indexes}]"


def unique_id(cls: str, member: str, *nested: str, kind: str = "method") -> str:
    """A killing test entry as PIT writes it for a JUnit Platform test of the class `cls`."""
    segments = [f"[class:{cls}]", *(f"[nested-class:{name}]" for name in nested), f"[{kind}:{member}]"]
    if kind == "test-template":
        segments.append("[test-template-invocation:#1]")
    return f"{cls}.[engine:junit-jupiter]/" + "/".join(segments)


def junit_report(*cases) -> str:
    """A JUnit XML file of one suite; per case (classname, name), skipped with a third item True."""
    rows = "".join(
        f'<testcase name={quoteattr(case)} classname={quoteattr(classname)} time="0.01">'
        + ("<skipped/>" if skipped and skipped[0] else "")
        + "</testcase>"
        for classname, case, *skipped in cases
    )
    suite = f'<testsuite name="{ENGINE_TEST}" tests="{len(cases)}" hostname="localhost"'
    suite += ' timestamp="2026-11-10T08:00:00">'
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + suite + rows + "</testsuite>\n"


def java_class(cls: str, body: str) -> str:
    """The source of the top-level class `cls` with the class body `body`."""
    package, _, name = cls.rpartition(".")
    return f"package {package};\n\nclass {name} {{\n{body}}}\n"


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

    @property
    def killer(self) -> str:
        return self.data["lost"][0]["killing_test_after"]


class DiffCase(unittest.TestCase):
    """A repository with a profile, no git, and a separate working directory that holds the reports
    and the JUnit folders."""

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
        for path in (ENGINE, PARSER):
            self.write(path, "class Source {}\n")
        self.write(ENGINE_TEST_FILE, java_class(ENGINE_TEST, ""))
        self.write(REPORT, pit_report(mutant()))

    def write(self, path: str, content) -> Path:
        """A file of the repository."""
        return self.put(self.repo / path, content)

    def put(self, file: Path | str, content) -> Path:
        """A file, by default of the working directory."""
        file = self.work / file
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return file

    def put_source(self, cls: str, body: str, folder: str = TEST_FOLDER) -> Path:
        """The source of the top-level test class `cls` in a test folder of the repository."""
        return self.write(f"{folder}/{cls.replace('.', '/')}.java", java_class(cls, body))

    def junit(self, *cases) -> list[str]:
        """A JUnit folder of the after run with the test cases (classname, name)."""
        self.put("junit-after/TEST-one.xml", junit_report(*cases))
        return ["--junit-after", "junit-after"]

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def json_main(self, *argv: str) -> tuple[int, dict]:
        code, out, _ = self.main(*argv, "--format", "json")
        return code, json.loads(out)

    def run_diff(self, *options: str, reports: bool = True) -> Run:
        """mutation-diff with before.xml and after.xml of the working directory (with `reports`),
        and the envelope and the summary in the working directory too."""
        for file in ("out.json", "summary.md"):
            (self.work / file).unlink(missing_ok=True)
        names = ["--before", "before.xml", "--after", "after.xml"] if reports else []
        code, out, err = self.main(
            "mutation-diff", *names, "--json-out", "out.json", "--summary-file", "summary.md", *options
        )
        json_out, summary = self.work / "out.json", self.work / "summary.md"
        env = json.loads(json_out.read_bytes().decode("ascii")) if json_out.exists() else None
        return Run(code, out, err, env, summary.read_bytes().decode("utf-8") if summary.exists() else "")

    def diff(self, before: list[dict] | None, after: list[dict] | None, *options: str) -> Run:
        """Write the reports of `before` and `after` (None: no file), then run the command."""
        for file, items in (("before.xml", before), ("after.xml", after)):
            (self.work / file).unlink(missing_ok=True)
            if items is not None:
                self.put(file, pit_report(*items))
        return self.run_diff(*options)

    def judged(self, before: list[dict], after: list[dict], outcome: str, *options: str) -> Run:
        run = self.diff(before, after, *options)
        self.assertEqual((run.code, run.data["outcome"]), (0, outcome), run.out + run.err)
        return run

    def lost(self, killing: str, *options: str) -> Run:
        """One mutant of Engine, killed before by `killing` and surviving after."""
        return self.judged([mutant(killing=killing)], [mutant(status="SURVIVED")], "warn", *options)


class ConstantsTest(unittest.TestCase):
    def test_wiring(self):
        analysis = cli.ANALYSES["mutation-diff"]
        self.assertIs(analysis.protected, policy.protected)
        self.assertIs(analysis.inputs, named_files)
        self.assertIs(analysis.option_inputs, diffs.option_inputs)
        args = argparse.Namespace(before="before.xml", after="after.xml", junit_after=None)
        expected = [("--before before.xml", Path("before.xml")), ("--after after.xml", Path("after.xml"))]
        self.assertEqual(analysis.option_inputs(args), expected)

    def test_every_file_option_has_the_type_of_paths(self):
        parser = cli.build_parser()
        commands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        command = commands.choices["mutation-diff"]
        actions = {option: action for action in command._actions for option in action.option_strings}
        for option in ("--before", "--after", "--junit-after"):
            with self.subTest(option=option):
                self.assertIs(actions[option].type, file_argument)
        self.assertNotIn("--junit-before", actions)

    def test_identity_is_compared_as_a_whole(self):
        source = (REPO_ROOT / "src" / "muster" / "diffs.py").read_bytes().decode("ascii")
        self.assertIsNone(re.search(r"\bid\s*\[|\[\s*[\"']id[\"']\s*\]", source))
        self.assertIn("mutant.id", source)


class VerdictTest(DiffCase):
    def test_unchanged_reports_pass(self):
        report = [mutant(line=nr) for nr in (1, 2, 3)]
        report += [mutant(PARSER_CLASS, "SURVIVED", 1), mutant(PARSER_CLASS, "NO_COVERAGE", 2)]
        run = self.judged(report, report, "pass")
        verdict = "mutation-diff: PASS (0 lost, 0 gained); test strength 75.0 % (3/4) -> 75.0 % (3/4)"
        self.assertEqual(run.out.splitlines(), [verdict])
        self.assertEqual((run.section["verdict"], run.data["files"], run.data["unresolved"]), ("pass", {}, {}))
        self.assertEqual(run.data["totals"]["after"]["covered"], 4)
        self.assertIn(row("before", 5, 4, 1, 3, "75.0 %", "60.0 %", "`before.xml`") + "\n", run.summary)
        self.assertTrue(run.summary.startswith(HEADING + "Mutant key: class, method, descriptor, line, mutator,"))
        self.assertNotIn("###", run.summary)

    def test_verdict_table(self):
        """Annotation, violation, enum and exit of each outcome."""
        self.write("muster.toml", PROFILE + KNOWN)
        killer = unique_id(PARSER_TEST, "decodes()")
        known = (
            [mutant(PARSER_CLASS, line=12, method="decode", killing=killer)],
            [mutant(PARSER_CLASS, "SURVIVED", 12, method="decode")],
        )
        same, lost, other = [mutant()], [mutant(status="SURVIVED")], [mutant(line=8)]
        junit = self.junit((PARSER_TEST, "decodes()"))
        cases = (
            ("pass", same, same, [], (None, False, "pass", 0)),
            ("pass", same, same, ["--strict"], (None, False, "pass", 0)),
            ("pass", same, same, ["--fail-on-error"], (None, False, "pass", 0)),
            ("known variation", *known, junit, (None, False, "pass", 0)),
            ("known variation", *known, [*junit, "--strict"], (None, False, "pass", 0)),
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
                self.assertEqual(run.section["strict"], "--strict" in options)
                if case == "known variation":
                    self.assertEqual(run.data["outcome"], "known-variation")

    def test_lost_mutant_names_its_killer(self):
        killer = unique_id(ENGINE_TEST, "settles()")
        before = [mutant(killing=killer), mutant(PARSER_CLASS, line=3)]
        run = self.judged(before, [mutant(status="SURVIVED"), mutant(PARSER_CLASS, line=3)], "warn")
        verdict = "mutation-diff: WARN (1 lost, 0 gained); test strength 100.0 % (2/2) -> 50.0 % (1/2)"
        self.assertEqual(run.verdict, verdict)
        self.assertEqual(run.annotations, [f"::warning title=Mutation diff::1 lost mutant(s): {text()}"])
        lost = {
            "mutant": text(),
            "status": ["KILLED", "SURVIVED"],
            "killing_tests": [killer],
            "killing_test_after": "not checked",
            "known_variation": False,
        }
        self.assertEqual(json.loads(json.dumps(run.data))["lost"], [lost])
        self.assertEqual(list(run.data["files"]), [ENGINE])
        self.assertEqual(run.data["files"][ENGINE]["after"]["detected"], 0)
        self.assertIn(row(f"`{ENGINE}`", "100.0 % (1/1)", "0.0 % (0/1)") + "\n", run.summary)
        item = (
            f"- `{text()}`: `KILLED` -> `SURVIVED`; killed before by `{ENGINE_TEST}#settles()`"
            " - killing test after: not checked\n"
        )
        heading = "### Lost mutants (1; detected before, not after)\n\n"
        self.assertIn(heading + item + "\nKilling tests not checked (no `--junit-after`).\n", run.summary)

    def test_gain_alone_is_a_pass(self):
        run = self.judged([mutant(status="SURVIVED")], [mutant()], "pass")
        self.assertEqual(run.data["gained"], [{"mutant": text(), "status": ["SURVIVED", "KILLED"]}])
        self.assertEqual((run.annotations, run.data["lost"]), ([], []))
        self.assertTrue(run.verdict.startswith("mutation-diff: PASS (0 lost, 1 gained); "), run.verdict)
        gained = f"### Gained mutants (1; detected after, not before)\n\n- `{text()}`: `SURVIVED` -> `KILLED`\n"
        self.assertIn(gained, run.summary)

    def test_unknown_status_counts_by_detected(self):
        before = [mutant(status="TIMED_OUT", line=1), mutant(status="UNHEARD_OF", line=2, detected=True)]
        after = [mutant(status="UNHEARD_OF", line=1, detected=False), mutant(line=2)]
        run = self.judged(before, after, "warn")
        self.assertEqual([item["status"] for item in run.data["lost"]], [["TIMED_OUT", "UNHEARD_OF"]])
        self.assertEqual(run.data["gained"], [])
        self.assertEqual((run.data["totals"]["before"]["detected"], run.data["totals"]["after"]["covered"]), (2, 2))


class KnownVariationTest(DiffCase):
    KILLER = unique_id(PARSER_TEST, "decodes()")

    def setUp(self):
        super().setUp()
        self.write("muster.toml", PROFILE + KNOWN)

    def parser(self, status: str = "KILLED", line: int = 12, cls: str = PARSER_CLASS, **fields) -> dict:
        """A mutant of KeyParser.decode on line 12, killed by default by KILLER."""
        return mutant(cls, status, line, **({"method": "decode", "killing": self.KILLER} | fields))

    def test_known_variation_with_a_present_killer_passes_under_strict(self):
        cursor = {"method": "lambda$next$0", "mutator": "NegateConditionalsMutator"}
        cursor_killer = unique_id(ENGINE_TEST, "closes()", "Daily")
        before = [self.parser(), mutant(f"{ENGINE_CLASS}$Cursor", line=31, killing=cursor_killer, **cursor)]
        after = [self.parser("SURVIVED"), mutant(f"{ENGINE_CLASS}$Cursor", "SURVIVED", 31, **cursor)]
        junit = self.junit((PARSER_TEST, "decodes()"), (f"{ENGINE_TEST}$Daily", "closes()"))
        for options in ([], ["--strict"]):
            with self.subTest(options=options):
                run = self.judged(before, after, "known-variation", *junit, *options)
                status = "mutation-diff: PASS (known variation: 2 lost, 0 gained; configured known variation, killing"
                self.assertTrue(run.verdict.startswith(status + " test present); test strength "), run.verdict)
                self.assertEqual((run.annotations, run.section["violation"], run.code), ([], False, 0))
                self.assertEqual([item["known_variation"] for item in run.data["lost"]], [True, True])
                marked = "killing test after: present; **known variation** (configured)\n"
                self.assertEqual(run.summary.count(marked), 2)
                self.assertIn("\n" + TOLERANCE, run.summary)
                self.assertEqual(run.data["known_variation_ambiguous"], [])

    def test_known_variation_needs_a_present_killer(self):
        cases = (
            ("not checked", self.KILLER, None),
            ("unresolved", f"{PARSER_TEST}.decodes({PARSER_TEST})", (PARSER_TEST, "decodes()")),
            ("unresolved", self.KILLER, (PARSER_TEST, "decodes()", True)),  # skipped
            ("no killing test recorded", "", (PARSER_TEST, "decodes()")),
            ("deleted", self.KILLER, (ENGINE_TEST, "decodes()")),
        )
        for killer, killing, case in cases:
            with self.subTest(killer=killer, case=case):
                junit = [] if case is None else self.junit(case)
                run = self.judged([self.parser(killing=killing)], [self.parser("SURVIVED")], "warn", *junit)
                self.assertEqual((run.killer, run.data["lost"][0]["known_variation"]), (killer, False))
                self.assertNotIn("**known variation**", run.summary)
                self.assertIn("\n" + TOLERANCE, run.summary)
                self.assertEqual(self.run_diff(*junit, "--strict").code, 1)
        deleted = "mutation-diff: WARN (1 lost, 0 gained; 1 with a deleted killing test); test strength"
        self.assertEqual(run.verdict, deleted + " 100.0 % (1/1) -> 0.0 % (0/1)")
        self.assertIn("1 lost mutant(s); 1 with a deleted killing test: ", run.annotations[0])
        self.assertIn("\n**1 lost mutant(s) whose killing test was deleted** - never known variation.\n", run.summary)
        self.assertIn("killing test after: **deleted**\n", run.summary)

    def test_entry_names_the_simple_class_the_method_the_line_and_the_mutator(self):
        junit = self.junit((PARSER_TEST, "decodes()"))
        cases = (
            ("another line", {"line": 13}, "warn"),
            ("another method", {"method": "encode"}, "warn"),
            ("another mutator", {"mutator": "NegateConditionalsMutator"}, "warn"),
            ("the mutator in another package", {"mutator": "experimental.MathMutator"}, "known-variation"),
            ("another class", {"cls": "org.example.app.codec.KeyCodec"}, "warn"),
            ("the class in another package", {"cls": "org.example.app.legacy.KeyParser"}, "known-variation"),
        )
        for case, change, outcome in cases:
            with self.subTest(case=case):
                fields = {"cls": PARSER_CLASS, "status": "KILLED", "line": 12, "method": "decode", **change}
                before = mutant(killing=self.KILLER, **fields)
                after = mutant(**(fields | {"status": "SURVIVED"}))
                run = self.judged([before], [after], outcome, *junit)
                self.assertEqual(run.killer, "present")
                self.assertEqual("known variation" in run.verdict, outcome == "known-variation")

    def test_entry_that_names_two_mutants_gives_no_tolerance(self):
        junit = self.junit((PARSER_TEST, "decodes()"))
        legacy = "org.example.app.legacy.KeyParser"
        cases = (
            ("two classes of one simple name", [{}, {"cls": legacy}], [{}, {"cls": legacy}]),
            ("two indexes on one line", [{"indexes": (1,)}, {"indexes": (2,)}], [{"indexes": (1,)}, {"indexes": (2,)}]),
            ("one of them unchanged", [{}, {"cls": legacy}], [{}, {"cls": legacy, "status": "KILLED"}]),
        )
        for case, before, after in cases:
            with self.subTest(case=case):
                run = self.judged(
                    [self.parser(**fields) for fields in before],
                    [self.parser(**({"status": "SURVIVED"} | fields)) for fields in after],
                    "warn",
                    *junit,
                )
                entry = {"class": "KeyParser", "method": "decode", "line": 12, "mutator": "MathMutator", "matches": 2}
                self.assertEqual(run.data["known_variation_ambiguous"], [entry])
                self.assertEqual({item["killing_test_after"] for item in run.data["lost"]}, {"present"})
                self.assertNotIn(True, [item["known_variation"] for item in run.data["lost"]])
                self.assertIn(f"\n{AMBIGUOUS}`KeyParser.decode L{12} MathMutator` (2 mutants)\n", run.summary)
                self.assertNotIn("**known variation**", run.summary)

    def test_entry_that_names_two_mutants_is_shown_when_nothing_is_lost(self):
        report = [self.parser(indexes=(1,)), self.parser(indexes=(2,))]
        run = self.judged(report, report, "pass")
        entry = {"class": "KeyParser", "method": "decode", "line": 12, "mutator": "MathMutator", "matches": 2}
        self.assertEqual(run.data["known_variation_ambiguous"], [entry])
        line = f"\n{AMBIGUOUS}`KeyParser.decode L{12} MathMutator` (2 mutants)\n"
        self.assertIn(line, run.summary)
        self.assertLess(run.summary.index("| after |"), run.summary.index(line))
        self.assertNotIn("### Lost mutants", run.summary)

    def test_known_variation_with_a_killer_found_in_the_source(self):
        junit = self.junit((PARSER_TEST, "Decodes a padded key"))
        for body, killer, outcome in (
            ("    @Test\n    void decodes() {}\n", "present", "known-variation"),
            ("    @Test\n    void other() {\n        decodes();\n    }\n", "deleted", "warn"),
        ):
            with self.subTest(killer=killer):
                self.put_source(PARSER_TEST, body)
                run = self.judged([self.parser()], [self.parser("SURVIVED")], outcome, *junit)
                self.assertEqual((run.killer, run.data["lost"][0]["known_variation"]), (killer, killer == "present"))


class KillerTest(DiffCase):
    SETTLES = unique_id(ENGINE_TEST, "settles()")
    CLOSES = unique_id(ENGINE_TEST, "closes()", "Daily")
    CONVERTS = unique_id(ENGINE_TEST, "converts(java.lang.String)", kind="test-template")

    def test_status_by_the_junit_names(self):
        cases = (
            ("method present", self.SETTLES, [(ENGINE_TEST, "settles()")], "present"),
            ("nested method present", self.CLOSES, [(f"{ENGINE_TEST}$Daily", "closes()")], "present"),
            ("nested class gone", self.CLOSES, [(ENGINE_TEST, "closes()")], "deleted"),
            ("class gone", self.SETTLES, [(f"{ENGINE_TEST}Other", "settles()")], "deleted"),
            ("method gone, plain names", self.SETTLES, [(ENGINE_TEST, "refunds()")], "deleted"),
            ("template gone, plain names", self.CONVERTS, [(ENGINE_TEST, "refunds()")], "deleted"),
            # Kept on purpose: a template or factory is never present by a plain name, even its own.
            ("template of a plain name", self.CONVERTS, [(ENGINE_TEST, "converts(java.lang.String)")], "deleted"),
            # Kept on purpose: an exact plain name is present, whatever display names the class has.
            (
                "plain name beside a display name",
                self.SETTLES,
                [(ENGINE_TEST, "Settles a refund"), (ENGINE_TEST, "settles()")],
                "present",
            ),
            ("not a unique id", f"{ENGINE_TEST}.settles({ENGINE_TEST})", [(ENGINE_TEST, "settles()")], "unresolved"),
            ("only skipped test cases", self.SETTLES, [(ENGINE_TEST, "settles()", True)], "unresolved"),
            (
                "plain names beside a skipped one",
                self.SETTLES,
                [(ENGINE_TEST, "refunds()"), (ENGINE_TEST, "postpones()", True)],
                "unresolved",
            ),
            (
                "a plain name beside its skipped one",
                self.SETTLES,
                [(ENGINE_TEST, "settles()"), (ENGINE_TEST, "settles()", True)],
                "present",
            ),
        )
        for case, killing, junit, expected in cases:
            with self.subTest(case=case):
                run = self.lost(killing, *self.junit(*junit))
                self.assertEqual(run.killer, expected)
                hidden = "`unresolved`: the killing test could be neither confirmed nor ruled out."
                self.assertEqual(hidden in run.summary, expected == "unresolved")

    def test_statuses_of_several_killers_fold(self):
        gone = unique_id(ENGINE_TEST, "refunds()")
        cases = (
            ("one present", f"{gone}|{self.SETTLES}", "present", 2),
            ("all deleted", f"{gone} | {unique_id(ENGINE_TEST, 'postpones()')}", "deleted", 2),
            ("deleted and unresolved", f"{gone}|  |{ENGINE_TEST}.settles({ENGINE_TEST})|", "unresolved", 2),
            ("no killing test recorded", "", "no killing test recorded", 0),
        )
        junit = self.junit((ENGINE_TEST, "settles()"))
        for case, killing, expected, killers in cases:
            with self.subTest(case=case):
                run = self.lost(killing, *junit)
                self.assertEqual((run.killer, len(run.data["lost"][0]["killing_tests"])), (expected, killers))
        self.assertIn("; killed before by none - killing test after: no killing test recorded\n", run.summary)

    def test_hidden_name_is_read_from_the_test_source(self):
        self.put_source(
            ENGINE_TEST,
            "\n".join(
                [
                    "    @Test",
                    '    @DisplayName("Settles a refund")',
                    "    void settles() {",
                    "        helper();",
                    "    }",
                    "",
                    "    @ParameterizedTest",
                    '    @ValueSource(strings = {"EUR"})',
                    "    void converts(final String unit) {}",
                    "",
                    "    @Test",
                    "    void postpones() {}",
                    "",
                    "    @Nested",
                    "    class Daily {",
                    "        @Test",
                    "        void closes() {}",
                    "    }",
                    "",
                    "    private static List<String> helper() {",
                    "        return refunded();",
                    "    }",
                    "",
                ]
            ),
        )
        shown = [(ENGINE_TEST, "Settles a refund"), (f"{ENGINE_TEST}$Daily", "Closes the day")]
        cases = (
            ("method declared", self.SETTLES, shown, "present"),
            ("nested method declared in the file of the outer class", self.CLOSES, shown, "present"),
            ("parameterized method declared", self.CONVERTS, [(ENGINE_TEST, "[1] unit = EUR")], "present"),
            ("only called", unique_id(ENGINE_TEST, "refunded()"), shown, "deleted"),
            ("a method without a test annotation", unique_id(ENGINE_TEST, "helper()"), shown, "deleted"),
            # The whole classname is the class: a # in it is no separator.
            ("a # in the classname", self.SETTLES, [(f"{ENGINE_TEST}#other", "Settles a refund")], "deleted"),
            # Kept on purpose: one display name sends every killer of the class to the source.
            (
                "plain names and one display name",
                unique_id(ENGINE_TEST, "postpones()"),
                [(ENGINE_TEST, "Settles a refund"), (ENGINE_TEST, "refunds()")],
                "present",
            ),
        )
        for case, killing, junit, expected in cases:
            with self.subTest(case=case):
                run = self.lost(killing, *self.junit(*junit))
                self.assertEqual(run.killer, expected)
                self.assertNotIn("`unresolved`", run.summary)
                self.assertIn(f"\n{SOURCES_LINE}\n", run.summary)

    def test_hidden_name_without_a_java_source(self):
        # The class ran, so a source that is not found is a failed lookup, never a deleted test.
        junit = self.junit((ENGINE_TEST, "Settles a refund"))
        (self.repo / ENGINE_TEST_FILE).unlink()
        self.put_source(f"{LEDGER}.OtherTest", "    @Test\n    void settles() {}\n")
        self.assertEqual(self.lost(self.SETTLES, *junit).killer, "unresolved")
        self.write(ENGINE_TEST_FILE.removesuffix(".java") + ".kt", "class EngineTest {\n}\n")
        run = self.lost(self.SETTLES, *junit)
        self.assertEqual(run.killer, "unresolved")
        self.assertIn("no source of its class found", run.summary)

    def test_class_that_may_inherit_the_test(self):
        junit = self.junit((ENGINE_TEST, "Settles a refund"))
        headers = (
            ("class EngineTest extends LedgerContract {", "unresolved"),
            ("final class EngineTest implements LedgerChecks, AutoCloseable {", "unresolved"),
            ("class EngineTest /* extends LedgerContract */ {", "deleted"),
            ("class EngineTest {", "deleted"),
        )
        for header, expected in headers:
            with self.subTest(header=header):
                source = java_class(ENGINE_TEST, "    @Test\n    void refunds() {}\n")
                self.write(ENGINE_TEST_FILE, source.replace("class EngineTest {", header))
                self.assertEqual(self.lost(self.SETTLES, *junit).killer, expected)

    def test_dollar_sign_in_the_package(self):
        cls = "org.example.app$qa.EngineTest"
        nested = "    @Nested\n    class Daily {\n        @Test\n        void closes() {}\n    }\n"
        self.put_source(cls, "    @Test\n    void settles() {}\n" + nested)
        junit = self.junit((cls, "Settles a refund"), (f"{cls}$Daily", "Closes the day"))
        for killing in (unique_id(cls, "settles()"), unique_id(cls, "closes()", "Daily")):
            with self.subTest(killing=killing):
                self.assertEqual(self.lost(killing, *junit).killer, "present")

    def test_source_that_cannot_be_followed_is_unresolved(self):
        self.put_source(ENGINE_TEST, "    @Nested\n    class Daily {}\n    @Nested\n    class Daily {}\n")
        junit = self.junit((f"{ENGINE_TEST}$Daily", "Closes the day"))
        run = self.lost(self.CLOSES, *junit)
        self.assertEqual(run.killer, "unresolved")
        self.assertIn("its class body not found", run.summary)
        self.put_source(ENGINE_TEST, "    @Nested\n    class Daily {\n        @Test\n        void closes() {}\n    }\n")
        self.assertEqual(self.lost(self.CLOSES, *junit).killer, "present")
        read = Path.read_bytes

        def reading(path: Path) -> bytes:
            if path.suffix == ".java":
                raise PermissionError(13, "Permission denied", str(path))
            return read(path)

        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=reading):
            self.assertEqual(self.lost(self.CLOSES, *junit).killer, "unresolved")

    def test_test_folders_of_the_profile(self):
        junit = self.junit((ENGINE_TEST, "Settles a refund"))
        (self.repo / ENGINE_TEST_FILE).unlink()
        second = PROFILE.replace('tests = ["src/test/java"]', 'tests = ["src/test/java", "src/check/java"]')
        self.write("muster.toml", second)
        self.put_source(ENGINE_TEST, "    @Test\n    void settles() {}\n", "src/check/java")
        run = self.lost(self.SETTLES, *junit)
        self.assertEqual(run.killer, "present")
        self.assertIn("resolved against the test sources in `src/test/java`, `src/check/java` of the", run.summary)
        self.put_source(ENGINE_TEST, "    @Test\n    void settles() {}\n")
        self.assertEqual(self.lost(self.SETTLES, *junit).killer, "unresolved")
        self.write("muster.toml", PROFILE.replace('tests = ["src/test/java"]\n', ""))
        run = self.lost(self.SETTLES, *junit)
        self.assertEqual(run.killer, "unresolved")
        self.assertNotIn("resolved against the test sources", run.summary)
        self.write("muster.toml", PROFILE.replace('tests = ["src/test/java"]', 'tests = ["src/test/java", "src/gone"]'))
        run = self.diff([mutant()], [mutant()])
        error = "muster.toml: source.tests[1]: folder src/gone does not exist"
        self.assertEqual((run.code, run.env["error"]), (2, {"kind": "config", "message": error}))

    def test_class_name_that_names_no_file_is_never_read(self):
        body = "    @Test\n    void settles() {}\n    @Nested\n    class Daily {\n"
        body += "        @Test\n        void settles() {}\n    }\n"
        self.put_source(ENGINE_TEST, body)
        names = (
            f"{ENGINE_TEST}$$Daily",
            f"{ENGINE_TEST}$",
            "org.example.app..EngineTest",
            "org.example.app.billing/internal.EngineTest",
            "org.example.app.billing" + chr(92) + "internal.EngineTest",
            "org.example.app.billing:internal.EngineTest",
            ENGINE_TEST + chr(0x85),
        )
        read = Path.read_bytes
        opened = []

        def reading(path: Path) -> bytes:
            if path.suffix == ".java":
                opened.append(path)
            return read(path)

        for cls in names:
            with self.subTest(cls=cls):
                junit = self.junit((cls, "Settles a refund"))
                with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=reading):
                    run = self.lost(unique_id(cls, "settles()"), *junit)
                self.assertEqual((run.killer, opened), ("unresolved", []))


class ComparabilityTest(DiffCase):
    def test_mutant_sets_differ(self):
        before = [mutant(line=1), mutant(line=2, indexes=(5,))]
        after = [mutant(line=1), mutant(line=2, indexes=(6,))]
        for options, code in (([], 0), (["--strict"], 1)):
            with self.subTest(options=options):
                run = self.diff(before, after, *options)
                self.assertEqual((run.code, run.data["outcome"]), (code, "not-comparable"))
                only = (run.data["only_before"], run.data["only_after"])
                self.assertEqual(only, ([text(line=2, indexes=(5,))], [text(line=2, indexes=(6,))]))
                detail = "mutant sets differ (1 only before, 1 only after); 0 lost, 0 gained"
                self.assertTrue(run.verdict.startswith(f"mutation-diff: NOT COMPARABLE ({detail}); "), run.verdict)
                self.assertNotIn("PASS", run.out)
                self.assertIn(f"### Only before (1; not comparable)\n\n- `{text(line=2, indexes=(5,))}`\n", run.summary)
                message = "before and after are not comparable: mutant sets differ (1 only before, 1 only after)"
                self.assertEqual(run.section["violations"], [message])

    def test_identity_has_the_method_the_descriptor_and_the_indexes(self):
        changes = (("method", "post", "void"), ("descriptor", "(I)I", "(J)J"), ("indexes", (5,), (6,)))
        for field, first, second in changes:
            with self.subTest(field=field):
                run = self.judged([mutant(**{field: first})], [mutant(**{field: second})], "not-comparable")
                only = (run.data["only_before"], run.data["only_after"])
                self.assertEqual(only, ([text(**{field: first})], [text(**{field: second})]))

    def test_report_without_any_mutation(self):
        two = [mutant(line=1), mutant(line=2)]
        differ = "mutant sets differ (2 only before, 0 only after)"
        for before, empty, detail in (
            (two, ["after"], "empty report: after without any mutation; " + differ),
            ([], ["before", "after"], "empty report: before and after without any mutation"),
        ):
            with self.subTest(empty=empty):
                run = self.judged(before, [], "not-comparable")
                self.assertEqual(run.data["empty"], empty)
                self.assertTrue(run.verdict.startswith(f"mutation-diff: NOT COMPARABLE ({detail}; 0 lost, 0 gained)"))
                self.assertIn(f"\n**Empty report:** {' and '.join(empty)} without any mutation.\n", run.summary)
        self.assertTrue(run.verdict.endswith("; test strength n/a (0/0) -> n/a (0/0)"), run.verdict)

    def test_lists_follow_the_class_method_line_and_mutator(self):
        # The two MathMutator mutants on line 12 differ only by the package of the mutator, so their
        # texts are the same; their killing tests tell them apart.
        fields = [
            {"line": 12, "mutator": "NegateConditionalsMutator", "indexes": (1,)},
            {"line": 12, "mutator": "experimental.MathMutator", "indexes": (2,)},
            {"line": 12, "mutator": "MathMutator", "indexes": (2,)},
            {"line": 3, "mutator": "NegateConditionalsMutator"},
            {"line": 3, "method": "apply"},
        ]
        before = [mutant(killing=f"entry{nr}", **f) for nr, f in enumerate(fields)]
        run = self.judged(before, [mutant(status="SURVIVED", **f) for f in fields], "warn")
        order = [4, 3, 2, 1, 0]
        lost = [(item["mutant"], item["killing_tests"]) for item in run.data["lost"]]
        self.assertEqual(lost, [(text(**fields[nr]), [f"entry{nr}"]) for nr in order])


class InputErrorTest(DiffCase):
    def test_reports_that_cannot_be_read(self):
        good = pit_report(mutant())
        cases = (
            ("missing", None, "no PIT report at before.xml", True),
            ("a folder", "folder", "before.xml: not a file", True),
            ("not well-formed", "<mutations", "before.xml: not well-formed XML: ", False),
            ("another root", "<report/>", "before.xml: not a PIT report, the root element is <report>", True),
            (
                "a contradicting record",
                good.replace("detected='true'", "detected='false'"),
                "before.xml: mutation 1: status KILLED contradicts detected=false",
                True,
            ),
            (
                "the same mutant twice",
                pit_report(mutant(), mutant()),
                f"before.xml: the mutant {text()} occurs twice",
                True,
            ),
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
                self.assertEqual(run.verdict, f"mutation-diff: ERROR ({errors[0]})")
                self.assertEqual(run.annotations, [f"::warning title=Mutation diff::{errors[0]}"])
                self.assertEqual(run.summary, f"{ERROR_BLOCK}- `{errors[0]}`\n\n")

    def test_every_input_error_is_listed_in_order(self):
        twice = pit_report(mutant(line=2), mutant(line=1), mutant(line=2), mutant(line=1), mutant(line=2))
        self.put("after.xml", twice)
        self.put("junit-after/alpha.xml", "<report/>")
        self.put("junit-after/beta.xml", "<testsuite")
        run = self.run_diff("--junit-after", "junit-after")
        errors = run.section["errors"]
        expected = [
            "no PIT report at before.xml",
            f"after.xml: the mutant {text(line=2)} occurs twice",
            f"after.xml: the mutant {text(line=1)} occurs twice",
            "junit-after/alpha.xml: not a JUnit report, the root element is <report>",
        ]
        self.assertEqual(errors[:4], expected)
        self.assertTrue(errors[4].startswith("junit-after/beta.xml: not well-formed XML: "), errors)
        self.assertEqual((len(errors), len(run.annotations), run.code), (5, 5, 0))
        self.assertEqual(run.summary, ERROR_BLOCK + "".join(f"- `{error}`\n" for error in errors) + "\n")
        run = self.run_diff("--junit-after", "gone")
        self.assertEqual(run.section["errors"][-1], "no JUnit XML folder at gone")

    def test_config_error_wins_over_input_errors(self):
        profile = PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "gen/main/java"]')
        self.write("muster.toml", profile)
        self.write("gen/main/java/org/example/app/codec/KeyParser.java", "class KeyParser {}\n")
        self.put("before.xml", "<mutations")
        self.put("after.xml", pit_report(mutant(PARSER_CLASS)))
        run = self.run_diff()
        message = (
            "after.xml: org/example/app/codec/KeyParser.java is under more than one source root:"
            f" gen/main/java/org/example/app/codec/KeyParser.java, {PARSER}"
        )
        self.assertEqual((run.code, run.env["error"]), (2, {"kind": "config", "message": message}))
        self.assertEqual(run.env["sections"], [])

    def test_only_the_source_roots_are_required_and_no_policy_is_checked(self):
        self.write("policy/critical.txt", CRITICAL + "gone/**\n")
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]\n', ""))
        self.judged([mutant()], [mutant()], "pass")
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]\n', ""))
        code, env = self.json_main("mutation-diff", "--before", "before.xml", "--after", "after.xml")
        error = {"kind": "config", "message": "muster.toml: missing key source.roots"}
        self.assertEqual((code, env["error"]), (2, error))


class OptionTest(DiffCase):
    def test_before_and_after_are_required(self):
        for argv in (["--before", "before.xml"], ["--after", "after.xml"], [], ["--junit-before", "results"]):
            with self.subTest(argv=argv):
                code, env = self.json_main("mutation-diff", *argv)
                self.assertEqual((code, env["error"]["kind"], env["sections"]), (2, "usage", []))

    def test_option_paths_are_relative_to_the_working_directory(self):
        for file in ("before.xml", "after.xml"):
            self.write(file, pit_report(mutant()))  # in the repository, which is not the working directory
        run = self.run_diff()
        self.assertEqual(run.section["errors"], ["no PIT report at before.xml", "no PIT report at after.xml"])
        self.put("results/TEST-one.xml", junit_report((ENGINE_TEST, "settles()")))
        killer = unique_id(ENGINE_TEST, "settles()")
        self.put("before.xml", pit_report(mutant(killing=killer)))
        self.put("after.xml", pit_report(mutant(status="SURVIVED")))
        run = self.run_diff("--junit-after", str(self.work / "results"))
        self.assertEqual((run.code, run.killer), (0, "present"))
        self.assertIn(row("after", 1, 1, 0, 0, "0.0 %", "0.0 %", "`after.xml`"), run.summary)

    def test_strict_only_takes_the_command_name(self):
        lost = ([mutant()], [mutant(status="SURVIVED")])
        for option, strict, code in (("mutation-diff", True, 1), ("coverage-diff,mutation", False, 0)):
            with self.subTest(option=option):
                run = self.diff(*lost, "--strict-only", option)
                state = (run.code, run.section["strict"], run.section["verdict"])
                self.assertEqual(state, (code, strict, "fail" if strict else "warn"))
        self.write("muster.toml", PROFILE + '\n[check]\nsections = ["mutation-diff"]\n')
        code, env = self.json_main("mutation-diff", "--before", "before.xml", "--after", "after.xml")
        self.assertEqual((code, env["error"]["kind"]), (2, "config"))
        self.assertIn("check.sections", env["error"]["message"])


class MarkdownTest(DiffCase):
    def test_report_text_stays_in_its_span_and_cell(self):
        odd_file = "Odd|Na`me.java"
        method = "pa|y`s"
        killing = "Odd`entry" + chr(10) + "next"
        before = [mutant(PARSER_CLASS, file=odd_file, method=method, killing=killing)]
        run = self.judged(before, [mutant(PARSER_CLASS, "SURVIVED", file=odd_file, method=method)], "warn")
        self.assertEqual(list(run.data["unresolved"]), [f"org/example/app/codec/{odd_file}"])
        self.assertEqual(run.data["lost"][0]["killing_tests"], [killing])
        cell = "`org/example/app/codec/Odd\\|Na'me.java` (under no source root)"
        self.assertIn(row(cell, "100.0 % (1/1)", "0.0 % (0/1)") + "\n", run.summary)
        shown = text(PARSER_CLASS, method="pa|y's")
        self.assertIn(f"\n- `{shown}`: `KILLED` -> `SURVIVED`; killed before by `Odd'entry next` - ", run.summary)
        self.assertNotIn(chr(10) + "next", run.summary)

    def test_annotation_names_at_most_ten_mutants(self):
        lines = range(1, 12)
        before, after = [mutant(line=nr) for nr in lines], [mutant(status="SURVIVED", line=nr) for nr in lines]
        run = self.judged(before, after, "warn")
        self.assertEqual(len(run.data["lost"]), 11)
        shown = "; ".join(text(line=nr) for nr in range(1, 11))
        self.assertEqual(run.annotations, [f"::warning title=Mutation diff::11 lost mutant(s): {shown} (+1 more)"])

    def test_lists_are_capped(self):
        before = [mutant(line=1), mutant(line=2), mutant(PARSER_CLASS)]
        after = [mutant(status="SURVIVED", line=1), mutant(status="SURVIVED", line=2), mutant(PARSER_CLASS, "SURVIVED")]
        with mock.patch.object(diffs, "FILES_CAP", 1), mock.patch.object(diffs, "ITEMS_CAP", 2):
            run = self.judged(before, after, "warn")
        self.assertEqual((len(run.data["files"]), len(run.data["lost"])), (2, 3))
        self.assertIn(" |\n\n... 1 more file(s)\n\n### Lost mutants (3;", run.summary)
        self.assertIn(f"- `{text(line=2)}`: `KILLED`", run.summary)
        self.assertIn("\n- ... (+1 more)\n", run.summary)

    def test_unresolved_name_with_the_text_of_a_path(self):
        # The second class is under no source root; its report name is the repository path of Engine.java.
        shadow = "src.main.java." + ENGINE_CLASS
        report = [mutant(), mutant(shadow)]
        run = self.judged(report, [mutant(status="SURVIVED"), mutant(shadow, "SURVIVED")], "warn")
        self.assertEqual((list(run.data["files"]), list(run.data["unresolved"])), ([ENGINE], [ENGINE]))
        rows = [line for line in run.summary.splitlines() if line.startswith(f"| `{ENGINE}`")]
        cells = ("100.0 % (1/1)", "0.0 % (0/1)")
        self.assertEqual(rows, [row(f"`{ENGINE}`", *cells), row(f"`{ENGINE}` (under no source root)", *cells)])
        self.assertEqual([item["mutant"] for item in run.data["lost"]], [text(), text(shadow)])


class OutputTest(DiffCase):
    def setUp(self):
        super().setUp()
        report = pit_report(mutant())
        self.put("before.xml", report)
        self.put("after.xml", report)
        self.put("junit-after/TEST-one.xml", junit_report((ENGINE_TEST, "settles()")))

    def test_output_that_is_an_input_on_every_path(self):
        junit = ["--junit-after", "junit-after"]
        targets = (
            ("before.xml", "--before before.xml"),
            ("after.xml", "--after after.xml"),
            ("junit-after/TEST-one.xml", "the JUnit file junit-after/TEST-one.xml"),
            (str(self.repo / "policy/critical.txt"), "the file of zones.critical"),
            (str(self.repo / REPORT), "the file of mutation.report"),
            (str(self.repo / ENGINE), "under the source folder src/main/java"),
            (str(self.repo / ENGINE_TEST_FILE), "under the test folder src/test/java"),
        )
        both = ["--before", "before.xml", "--after", "after.xml"]
        paths = {
            # argparse fails: only the option that names the target, of --before and --after
            "a required option is missing": lambda target: (
                (both[2:] if target == "after.xml" else both[:2]) + junit,
                PROFILE,
            ),
            "the profile is invalid": lambda target: (both + junit, PROFILE + "\n[output]\nbogus = 1\n"),
            "the profile is valid": lambda target: (both + junit, PROFILE),
        }
        for path, case in paths.items():
            for target, label in targets:
                argv, profile = case(target)
                self.write("muster.toml", profile)
                for option in ("--json-out", "--summary-file"):
                    with self.subTest(path=path, target=target, option=option):
                        file = self.work / target
                        before = file.read_bytes()
                        code, _, err = self.main("mutation-diff", *argv, option, target)
                        self.assertEqual(code, 2, err)
                        self.assertIn(f"error: {option} {target} is {label}\n", err)
                        self.assertEqual(file.read_bytes(), before)

    def test_default_summary(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        code, _, err = self.main("mutation-diff", "--before", "before.xml", "--after", "after.xml")
        self.assertEqual(code, 0, err)
        summary = self.repo / "build/muster/mutation-diff.md"
        content = summary.read_bytes()
        self.assertTrue(content.startswith(HEADING.encode("ascii")), content)
        code, env = self.json_main("mutation-diff", "--before", str(summary), "--after", "after.xml")
        message = f"the summary build/muster/mutation-diff.md is --before {summary}"
        self.assertEqual((code, env["error"]), (2, {"kind": "usage", "message": message}))
        self.assertEqual(summary.read_bytes(), content)


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
        self.put("before.xml", pit_report(mutant(killing=unique_id(ENGINE_TEST, "settles()"))))
        self.put("after.xml", pit_report(mutant(status="SURVIVED")))
        self.put("junit-after/TEST-one.xml", junit_report((ENGINE_TEST, "Settles a refund")))
        self.put_source(ENGINE_TEST, "    @Test\n    void settles() {}\n")
        argv = ["mutation-diff", "--before", "before.xml", "--after", "after.xml", "--junit-after", "junit-after"]
        code, _, err = self.main(*argv, "--json-out", "out.json")
        self.assertEqual(code, 0, err)
        env = json.loads((self.work / "out.json").read_bytes())
        self.assertEqual(env["commit"], self.git("rev-parse", "HEAD"))
        data = env["sections"][0]["data"]
        self.assertEqual((list(data["files"]), data["lost"][0]["killing_test_after"]), ([ENGINE], "present"))
        report = (self.repo / "build/muster/mutation-diff.md").read_bytes().decode("ascii")
        self.assertTrue(report.startswith(HEADING), report)
        self.assertFalse((self.top / "build").exists())


if __name__ == "__main__":
    unittest.main()
