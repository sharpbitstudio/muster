"""Tests of the loc command, end to end through cli.main on a synthetic git repository: the lines of
code by group, the languages, the change split against a base, the files without a language or that
cannot be read, the keys it needs, the output channels and the files no output may be.

Every run happens in temporary folders, with git's global and system configuration switched off. The
repository is on branch `feature`, cut from `main` with nothing committed since.
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from muster import cli, gitio, loc, policy
from muster.config import Config, load
from muster.report import Report, ReportError

IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)  # fmt: skip
MAIN = "src/main/java/org/example/app"
TESTS = "src/test/java/org/example/app"
ENGINE = f"{MAIN}/Engine.java"
LEDGER = f"{MAIN}/Ledger.java"
ENGINE_TEST = f"{TESTS}/EngineTest.java"
MOVED = f"{TESTS}/Archive.java"
CLOCK = f"{TESTS}/support/Clock.java"
PROFILE = """\
schema = 1

[source]
roots = ["src/main/java"]
tests = ["src/test/java"]
include = ["**/*.java"]
generated = ["**/gen/**"]

[loc]
test_support = ["src/test/java/org/example/app/support/**", "**/*Fixture.java"]
"""
ROOTS_LINE = 'roots = ["src/main/java"]\n'
INCLUDE_LINE = 'include = ["**/*.java"]\n'
# A profile like muster's own: Python files under src and tests, with a language for them.
OWN_PROFILE = """\
schema = 1

[source]
roots   = ["src"]
tests   = ["tests"]
include = ["**/*.py"]

[[language]]
name = "python"
extensions = [".py"]
line_comment = "#"
"""
JAVA_SAMPLE = """\
package org.example.app;

/* A block comment
   over two lines. */
public class Probe {
    // a line comment
    String path = "first//second";
    int width = 3; /* trailing */
    /* leading */ int height = 4;
    String text = \"\"\"
        // inside a text block
        \"\"\";
    \t
}
"""
JAVA_SAMPLE_LOC = 9  # package, class, path, width, height, the three text block lines, the brace


def java_lines(count: int, name: str = "field") -> str:
    """A Java body of `count` lines, each of them code."""
    return "".join(f"    int {name}{nr};\n" for nr in range(1, count + 1))


@dataclass
class Outcome:
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
        return [line for line in self.out.splitlines() if line.startswith("::")]

    @property
    def verdict(self) -> str:
        return [line for line in self.out.splitlines() if not line.startswith("::")][-1]

    def counts(self) -> dict:
        """{group: (files, loc)}."""
        return {name: (acc["files"], acc["loc"]) for name, acc in self.data["groups"].items()}


class LocCase(unittest.TestCase):
    """A git repository with a main and a test file, a profile with a test-support glob and a
    separate working directory."""

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
        self.write("muster.toml", PROFILE)
        self.write(ENGINE, "class Engine {\n" + java_lines(8) + "}\n")  # 10 lines
        self.write(ENGINE_TEST, "class EngineTest {\n}\n")
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.git("checkout", "-q", "-b", "feature")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *IDENTITY, *args], cwd=self.top, check=True, capture_output=True)
        return proc.stdout.decode("utf-8").strip()

    def commit(self) -> None:
        """Commit the work tree on main and cut feature from it again."""
        self.git("checkout", "-q", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "more")
        self.git("checkout", "-q", "-B", "feature")

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

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def json_main(self, *argv: str) -> tuple[int, dict]:
        code, out, _ = self.main(*argv, "--format", "json")
        return code, json.loads(out)

    def loc(self, *options: str) -> Outcome:
        self.json_out.unlink(missing_ok=True)
        self.summary.unlink(missing_ok=True)
        code, out, err = self.main(
            "loc", "--json-out", str(self.json_out), "--summary-file", str(self.summary), *options
        )
        env = json.loads(self.json_out.read_bytes().decode("ascii")) if self.json_out.exists() else None
        summary = self.summary.read_bytes().decode("utf-8") if self.summary.exists() else ""
        return Outcome(code, out, err, env, summary)

    def quiet(self, *options: str) -> Outcome:
        """A run that passes: exit 0, no annotation, no error, no violation."""
        outcome = self.loc(*options)
        self.assertEqual(outcome.code, 0, outcome.out + outcome.err)
        self.assertEqual((outcome.annotations, outcome.section["errors"]), ([], []))
        self.assertEqual((outcome.section["verdict"], outcome.section["violation"]), ("pass", False))
        return outcome


class CountTest(LocCase):
    def test_java_sample_counts_code_lines_only(self):
        self.write(f"{MAIN}/Probe.java", JAVA_SAMPLE)
        outcome = self.quiet()
        self.assertEqual(outcome.counts()["main"], (2, 10 + JAVA_SAMPLE_LOC))

    def test_report_without_a_base(self):
        outcome = self.quiet()
        self.assertEqual(outcome.counts(), {"main": (1, 10), "test": (1, 2), "test-support": (0, 0)})
        self.assertEqual((outcome.data["test_ratio"], outcome.data["all_ratio"]), (0.2, 0.2))
        self.assertEqual((outcome.data["change"], outcome.data["uncounted"]), (None, {}))
        self.assertEqual(outcome.verdict, "loc: main 10, test 2, test-support 0, test/main 0.20")
        self.assertEqual(outcome.env["base"], None)
        expected = (
            "## Lines of code\n\n"
            "Lines of code (without blank and comment-only lines); a trend, not a gate.\n\n"
            "| group | files | LOC |\n|---|---:|---:|\n"
            "| main | 1 | 10 |\n| test | 1 | 2 |\n| test-support | 0 | 0 |\n\n"
            "test/main 0.20; (test + test-support)/main 0.20.\n\n"
        )
        self.assertEqual(outcome.summary, expected)

    def test_no_main_code_leaves_the_ratios_out(self):
        self.write(ENGINE, "// only a comment\n")
        outcome = self.quiet()
        self.assertEqual(outcome.counts()["main"], (1, 0))
        self.assertEqual((outcome.data["test_ratio"], outcome.data["all_ratio"]), (None, None))
        self.assertEqual(outcome.verdict, "loc: main 0, test 2, test-support 0")
        self.assertNotIn("test/main", outcome.summary)

    def test_set_leaves_out_other_files_and_generated_ones(self):
        self.write(f"{MAIN}/notes.txt", "not code\n")
        self.write(f"{MAIN}/gen/Schema.java", java_lines(5))
        self.write("build/Other.java", java_lines(5))
        self.assertEqual(self.quiet().counts()["main"], (1, 10))

    def test_loc_exclude_applies_and_source_exclude_does_not(self):
        self.write(f"{MAIN}/package-info.java", "package org.example.app;\n")
        self.write(f"{MAIN}/legacy/Old.java", java_lines(5))
        text = PROFILE.replace(INCLUDE_LINE, INCLUDE_LINE + 'exclude = ["**/package-info.java"]\n')
        self.write("muster.toml", text + 'exclude = ["**/legacy/**"]\n')
        self.assertEqual(self.quiet().counts()["main"], (2, 11))

    def test_new_language_counted_by_the_generic_counter(self):
        language = '\n[[language]]\nname = "query"\nextensions = [".sql"]\nline_comment = "--"\n'
        language += 'block_comment = ["/*", "*/"]\n'
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, 'include = ["**/*.java", "**/*.sql"]\n') + language)
        query = "-- header\nSELECT 1; -- trailing\n/* a block\n   over lines */\n\nSELECT 2; /* x */ SELECT 3;\n"
        self.write(f"{MAIN}/report.sql", query)
        outcome = self.quiet()
        self.assertEqual(outcome.counts()["main"], (2, 12))
        self.assertEqual(outcome.data["uncounted"], {})

    def test_run_walks_the_set_itself_without_the_walk_of_the_command_line(self):
        self.write(f"{MAIN}/gen/Schema.java", java_lines(5))
        self.write(CLOCK, java_lines(4))
        rep = Report("loc", strict=False, fail_on_error=False)
        loc.run(argparse.Namespace(base=None), load(self.repo), rep)
        self.assertEqual(rep.data["groups"], self.quiet().data["groups"])
        self.assertEqual(rep.data["groups"]["test-support"], {"files": 1, "loc": 4})


class GroupTest(LocCase):
    def test_main_test_and_test_support(self):
        self.write(f"{MAIN}/EngineFixture.java", java_lines(3))  # the glob does not move a main file
        self.write(CLOCK, java_lines(4))
        self.write(f"{TESTS}/ClockFixture.java", java_lines(5))
        outcome = self.quiet()
        self.assertEqual(outcome.counts(), {"main": (2, 13), "test": (1, 2), "test-support": (2, 9)})
        self.assertEqual(outcome.data["all_ratio"], 11 / 13)
        self.assertIn("test/main 0.15; (test + test-support)/main 0.85.", outcome.summary)

    def test_tests_folder_nested_under_a_root(self):
        self.write("muster.toml", PROFILE.replace(ROOTS_LINE, 'roots = ["src"]\n'))
        self.write("src/tools/Build.java", java_lines(2))
        self.write(CLOCK, java_lines(4))
        outcome = self.quiet()
        self.assertEqual(outcome.counts(), {"main": (2, 12), "test": (1, 2), "test-support": (1, 4)})


class ChangeSplitTest(LocCase):
    def setUp(self):
        super().setUp()
        self.write(LEDGER, java_lines(3, "entry"))
        self.write(MOVED, java_lines(4, "kept"))
        self.write(f"{MAIN}/notes.txt", "a\nb\n")
        self.commit()

    def test_report_and_split_together(self):
        self.edit(ENGINE, {3: "    int changed;"})
        self.write(ENGINE_TEST, "class EngineTest {\n" + java_lines(3) + "}\n")
        (self.repo / LEDGER).unlink()
        self.git("mv", self.top_path(MOVED), self.top_path(f"{MAIN}/Archive.java"))
        self.write(CLOCK, java_lines(2))  # untracked
        self.write(f"{MAIN}/notes.txt", "a\nb\nc\n")  # outside the LOC set
        self.write(f"{MAIN}/gen/Schema.java", java_lines(6))  # generated, untracked
        outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.counts(), {"main": (2, 14), "test": (1, 5), "test-support": (1, 2)})
        # main: Engine 1/1, Ledger deleted, Archive added; test: EngineTest +3, Archive deleted
        self.assertEqual(outcome.data["change"], {"main": [5, 4], "test": [3, 4], "test-support": [2, 0]})
        self.assertIn("This change vs `main` (raw lines, `git diff --numstat` + untracked):\n\n", outcome.summary)
        self.assertIn("| group | added | deleted |\n|---|---:|---:|\n| main | 5 | 4 |\n", outcome.summary)
        self.assertEqual(outcome.verdict, "loc: main 14, test 5, test-support 2, test/main 0.36")
        self.assertEqual(outcome.env["base"], {"ref": "main", "commit": self.git("merge-base", "main", "HEAD")})

    def top_path(self, path: str) -> str:
        return f"{self.SUBFOLDER}/{path}" if self.SUBFOLDER else path

    def test_file_git_takes_as_binary_keeps_its_line_counts(self):
        for case in ("attribute", "nul byte"):
            with self.subTest(case=case):
                self.git("checkout", "-q", "--", ".")
                if case == "attribute":
                    self.write(".gitattributes", "*.java -diff\n")
                    self.edit(ENGINE, {3: "    int three;", 8: "    int eight;"})
                else:
                    (self.repo / ".gitattributes").unlink()
                    self.edit(ENGINE, {3: "    int nul" + chr(0) + ";", 8: "    int eight;"})
                with mock.patch("muster.loc.binary_counts_since", wraps=gitio.binary_counts_since) as spy:
                    outcome = self.quiet("--base", "main")
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(outcome.data["change"]["main"], [2, 2])
                self.assertEqual(outcome.counts()["main"], (2, 13))

    def test_lines_end_at_a_line_feed_only(self):
        separators = "\f" + chr(0x2028) + chr(0x2029)
        text = "class Probe {" + separators + " int one;\n  int two;\r int three;\n" + chr(0x2028) + "\n}\n"
        self.write(f"{TESTS}/Probe.java", text)  # untracked
        outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.counts()["test"], (3, 2 + 4 + 3))
        self.assertEqual(outcome.data["change"]["test"], [4, 0])

    def test_unknown_base_under_strict_passes_without_a_split(self):
        outcome = self.quiet("--base", "no-such-ref", "--strict")
        self.assertEqual(outcome.data["change"], None)
        self.assertEqual(outcome.section["violations"], [])
        self.assertEqual(outcome.env["base"], {"ref": "no-such-ref", "commit": None})
        self.assertIn("No merge-base with `no-such-ref`: change split not computed.\n", outcome.summary)
        self.assertNotIn("| added |", outcome.summary)
        self.assertEqual(outcome.verdict, "loc: main 13, test 6, test-support 0, test/main 0.46")

    def test_git_error_keeps_the_table(self):
        failure = ReportError("git diff failed: boom")
        for options in ([], ["--strict"]):
            with self.subTest(options=options):
                with mock.patch("muster.loc.numstat_since", side_effect=failure):
                    outcome = self.quiet("--base", "main", *options)
                self.assertEqual(outcome.data["change"], None)
                self.assertIn("| main | 2 | 13 |", outcome.summary)
                self.assertIn("LOC change split not computed: git diff failed: boom\n", outcome.summary)
                self.assertTrue(outcome.verdict.startswith("loc: main 13, "), outcome.verdict)
                self.assertEqual(outcome.env["base"]["commit"], self.git("merge-base", "main", "HEAD"))

    def test_git_that_cannot_run_keeps_the_table(self):
        real_run = subprocess.run

        def numstat_cannot_run(command, *args, **kwargs):
            if "--numstat" in command:
                raise FileNotFoundError(2, "synthetic reason")
            return real_run(command, *args, **kwargs)

        with mock.patch("muster.gitio.subprocess.run", side_effect=numstat_cannot_run):
            outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.data["change"], None)
        self.assertEqual(outcome.env["base"], {"ref": "main", "commit": self.git("merge-base", "main", "HEAD")})
        self.assertIn("LOC change split not computed: git diff could not run: ", outcome.summary)
        self.assertNotIn("| added |", outcome.summary)
        self.assertEqual(outcome.counts()["main"], (2, 13))

    def test_untracked_file_that_cannot_be_read_leaves_the_split_out(self):
        self.write(CLOCK, java_lines(2))
        denied = PermissionError(13, "Permission denied")
        with mock.patch("muster.loc.count_file_lines", side_effect=denied):
            outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.data["change"], None)
        self.assertIn(f"LOC change split not computed: {CLOCK}: cannot read: Permission denied\n", outcome.summary)

    def link(self, target: str, path: str, folder: bool = False) -> None:
        """A symbolic link at `path` to `target` (a folder with `folder`), relative to the link's folder."""
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, self.repo / path, target_is_directory=folder)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symbolic link here")

    def test_untracked_link_to_no_file_adds_nothing(self):
        self.link("Missing.java", f"{MAIN}/Dangling.java")
        self.write(CLOCK, java_lines(2))  # untracked
        outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.data["change"], {"main": [0, 0], "test": [0, 0], "test-support": [2, 0]})
        self.assertEqual(outcome.counts()["main"], (2, 13))

    def test_untracked_link_to_a_folder_adds_nothing(self):
        self.write(f"{MAIN}/assets/readme.txt", "a\nb\n")
        self.link("assets", f"{MAIN}/Linked.java", folder=True)
        self.write(CLOCK, java_lines(2))  # untracked
        outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.data["change"], {"main": [0, 0], "test": [0, 0], "test-support": [2, 0]})
        self.assertEqual(outcome.counts()["main"], (2, 13))

    def test_ignored_file_is_in_the_table_and_not_in_the_split(self):
        self.write(".gitignore", "Ignored.java\n")
        self.write(f"{MAIN}/Ignored.java", java_lines(5))
        self.write(CLOCK, java_lines(2))  # untracked
        outcome = self.quiet("--base", "main")
        self.assertEqual(outcome.counts()["main"], (3, 18))
        self.assertEqual(outcome.data["change"], {"main": [0, 0], "test": [0, 0], "test-support": [2, 0]})

    def test_input_error_keeps_the_split_and_the_ratio(self):
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, 'include = ["**/*.java", "**/*.txt"]\n'))
        self.edit(ENGINE, {3: "    int changed;", 4: "    int again;"})
        error = f".txt: 1 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/notes.txt)"
        for options, violation in ((["--strict"], True), (["--fail-on-error"], False)):
            with self.subTest(options=options):
                outcome = self.loc("--base", "main", *options)
                self.assertEqual(outcome.code, 1, outcome.out + outcome.err)
                self.assertEqual((outcome.section["verdict"], outcome.section["errors"]), ("error", [error]))
                self.assertEqual(outcome.section["violation"], violation)
                self.assertEqual(outcome.data["change"], {"main": [2, 2], "test": [0, 0], "test-support": [0, 0]})
                self.assertIn("| main | 2 | 2 |\n", outcome.summary)
                self.assertIn("test/main 0.46; (test + test-support)/main 0.46.\n", outcome.summary)
                self.assertEqual(outcome.data["test_ratio"], 6 / 13)

    def test_link_replaced_by_a_binary_file_counts_once(self):
        self.link("Engine.java", f"{MAIN}/Shape.java")
        self.commit()
        (self.repo / f"{MAIN}/Shape.java").unlink()
        self.write(f"{MAIN}/Shape.java", "int one;" + chr(0) + "\nint two;\n")
        with mock.patch("muster.loc.binary_counts_since", wraps=gitio.binary_counts_since) as spy:
            outcome = self.quiet("--base", "main")
        self.assertEqual(spy.call_count, 1)
        # the link's one line deleted, the file's two lines added
        self.assertEqual(outcome.data["change"], {"main": [2, 1], "test": [0, 0], "test-support": [0, 0]})

    def test_file_without_a_language_is_left_out_of_the_split(self):
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, 'include = ["**/*.java", "**/*.txt"]\n'))
        self.write(f"{MAIN}/notes.txt", "a\nb\nc\nd\n")
        self.write(f"{TESTS}/more.txt", "a\n")
        outcome = self.loc("--base", "main")
        self.assertEqual(outcome.data["change"], {"main": [0, 0], "test": [0, 0], "test-support": [0, 0]})
        self.assertEqual(outcome.data["uncounted"], {".txt": 2})


class SubfolderTest(ChangeSplitTest):
    """The profile in a subfolder of the git work tree: git's paths are taken relative to it."""

    SUBFOLDER = "service"


class InputErrorTest(LocCase):
    """A file without a language or that cannot be read is an input error: listed, not counted;
    exit 0, and 1 with --strict or --fail-on-error, never 3. The table stays; the verdict names the first error."""

    def assert_errored(self, errors: list[str]) -> Outcome:
        for options, expected in (([], 0), (["--strict"], 1), (["--fail-on-error"], 1)):
            outcome = self.loc(*options)
            self.assertEqual(outcome.code, expected, outcome.out + outcome.err)
            self.assertEqual((outcome.section["verdict"], outcome.env["error"]), ("error", None))
            self.assertEqual(outcome.section["errors"], errors)
            self.assertEqual(outcome.section["violations"], [])
            level = "error" if options else "warning"
            titled = [f"::{level} title=Lines of code::{error}" for error in errors]
            self.assertEqual(outcome.annotations, titled)
            self.assertIn("**ERROR**: some files of the LOC set are not counted:\n\n", outcome.summary)
            for error in errors:
                self.assertIn(f"- {error}\n", outcome.summary)
            self.assertIn("| group | files | LOC |", outcome.summary)
            self.assertEqual(outcome.verdict, f"loc: ERROR ({errors[0]})")
        return outcome

    def test_files_without_a_language_per_extension(self):
        include = 'include = ["**/*.java", "**/*.txt", "**/*.tar.gz", "**/NOTICE", "**/.keep"]\n'
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, include))
        self.write(f"{MAIN}/notes.txt", "a\n")
        self.write(f"{TESTS}/more.txt", "a\n")
        self.write(f"{MAIN}/data.tar.gz", b"\x1f\x8b")
        self.write(f"{MAIN}/NOTICE", "a\n")
        self.write(f"{TESTS}/.keep", "")  # a dot-file
        self.write(f"{TESTS}/support/.java", "int a;\n")  # a name that is only the extension
        outcome = self.assert_errored(
            [
                f"(no extension): 3 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/NOTICE)",
                f".gz: 1 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/data.tar.gz)",
                f".txt: 2 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/notes.txt)",
            ]
        )
        self.assertEqual(outcome.data["uncounted"], {"(no extension)": 3, ".gz": 1, ".txt": 2})
        self.assertEqual(outcome.counts(), {"main": (1, 10), "test": (1, 2), "test-support": (0, 0)})

    def test_name_that_is_exactly_an_extension_has_no_extension(self):
        languages = '\n[[language]]\nname = "script"\nextensions = [".ts"]\nline_comment = "//"\n'
        languages += '\n[[language]]\nname = "declarations"\nextensions = [".d.ts"]\n'
        include = 'include = ["**/*.java", "**/*.ts", "**/.d.ts", "**/.ts"]\n'
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, include) + languages)
        self.write(f"{MAIN}/.d.ts", "let a;\n")
        self.write(f"{MAIN}/.ts", "let b;\n")
        self.write(f"{MAIN}/api.d.ts", "let c;\n")
        self.write(f"{TESTS}/.java", "int a;\n")
        outcome = self.assert_errored(
            [f"(no extension): 3 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/.d.ts)"]
        )
        self.assertEqual(outcome.data["uncounted"], {"(no extension)": 3})
        self.assertEqual(outcome.counts(), {"main": (2, 11), "test": (1, 2), "test-support": (0, 0)})

    def test_file_that_cannot_be_read_by_the_counter(self):
        denied = PermissionError(13, "Permission denied")
        with mock.patch("muster.lang.open", side_effect=denied, create=True):
            outcome = self.assert_errored(
                [f"{ENGINE}: cannot read: Permission denied", f"{ENGINE_TEST}: cannot read: Permission denied"]
            )
        self.assertEqual(outcome.counts(), {"main": (0, 0), "test": (0, 0), "test-support": (0, 0)})

    @unittest.skipIf(os.name == "nt", "chmod cannot make a file unreadable on Windows")
    def test_file_that_cannot_be_read_on_disk(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every file")
        file = self.repo / ENGINE
        file.chmod(0)
        self.addCleanup(file.chmod, 0o644)
        try:
            file.open("rb").close()
        except PermissionError as exc:
            reason = exc.strerror
        else:
            self.skipTest("chmod does not make a file unreadable here")
        outcome = self.assert_errored([f"{ENGINE}: cannot read: {reason}"])
        self.assertEqual(outcome.counts()["main"], (0, 0))


class ProfileTest(LocCase):
    def assert_missing(self, key: str) -> None:
        code, env = self.json_main("loc")
        self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": f"muster.toml: missing key {key}"}))

    def test_required_keys(self):
        self.write("muster.toml", PROFILE.replace(ROOTS_LINE, ""))
        self.assert_missing("source.roots")
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, ""))
        self.assert_missing("source.include")

    def test_no_zones_and_no_tests_folder_needed(self):
        self.write("muster.toml", PROFILE.replace('tests = ["src/test/java"]\n', ""))
        outcome = self.quiet()
        self.assertEqual(outcome.counts(), {"main": (1, 10), "test": (0, 0), "test-support": (0, 0)})

    def test_missing_folder_is_a_config_error(self):
        self.write("muster.toml", PROFILE.replace('tests = ["src/test/java"]', 'tests = ["src/test/kotlin"]'))
        code, env = self.json_main("loc")
        message = "muster.toml: source.tests[0]: folder src/test/kotlin does not exist"
        self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": message}))

    def test_set_without_a_main_file_is_a_config_error(self):
        typo = PROFILE.replace(INCLUDE_LINE, 'include = ["**/*.jav"]\n')
        every_main_file = PROFILE + 'exclude = ["src/main/**"]\n'  # in [loc]; the test files stay
        # The tests folder nested under the root: its files are under source.roots, but not main.
        nested = PROFILE.replace(ROOTS_LINE, 'roots = ["src"]\n')
        only_tests = nested.replace(INCLUDE_LINE, 'include = ["**/*Test.java"]\n')
        for profile in (typo, every_main_file, nested + 'exclude = ["src/main/**"]\n', only_tests):
            with self.subTest(profile=profile):
                self.write("muster.toml", profile)
                code, env = self.json_main("loc")
                self.assertEqual((code, env["error"]), (2, {"kind": "config", "message": self.NO_MAIN_FILE}))

    NO_MAIN_FILE = (
        "muster.toml: no main file is in the LOC set"
        " (source.include minus source.generated and [loc].exclude, under source.roots)"
    )

    def test_main_files_without_a_language_are_an_input_error(self):
        self.write("muster.toml", PROFILE.replace(INCLUDE_LINE, 'include = ["**/*.txt", "**/*Test.java"]\n'))
        self.write(f"{MAIN}/notes.txt", "a\n")
        outcome = self.loc()
        self.assertEqual((outcome.code, outcome.env["error"]), (0, None))
        error = f".txt: 1 file(s) of the LOC set have no language and are not counted (e.g. {MAIN}/notes.txt)"
        self.assertEqual(outcome.section["errors"], [error])
        self.assertEqual(outcome.counts(), {"main": (0, 0), "test": (1, 2), "test-support": (0, 0)})

    def test_option_shaped_base_is_a_usage_error(self):
        for argv in (["--base=-x"], ["--base="], ["--base", "-x"], ["--base", "main" + chr(0)]):
            with self.subTest(argv=argv):
                code, env = self.json_main("loc", *argv)
                self.assertEqual((code, env["error"]["kind"], env["base"]), (2, "usage", None))

    def test_wiring(self):
        analysis = cli.ANALYSES["loc"]
        self.assertIs(analysis.protected, policy.protected)
        data = {"duration": {"task_json": ["not", "a", "path"]}, "coverage": {"report": "build/x" + chr(0) + ".xml"}}
        self.assertEqual(analysis.inputs(Config(self.repo, self.repo / "muster.toml", data)), [])


class OutputFileTest(LocCase):
    def assert_refused(self, target: Path, label: str) -> None:
        before = target.read_bytes()
        code, env = self.json_main("loc", "--json-out", str(target))
        self.assertEqual((code, env["error"]), (2, {"kind": "usage", "message": f"--json-out {target} {label}"}))
        self.assertEqual(target.read_bytes(), before)

    def test_source_and_test_files_are_never_outputs(self):
        self.assert_refused(self.repo / ENGINE, "is under the source folder src/main/java")
        self.assert_refused(self.repo / ENGINE_TEST, "is under the test folder src/test/java")

    def test_default_report_in_the_output_folder(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        for _ in range(2):
            code, out, _ = self.main("loc")
            self.assertEqual((code, out), (0, "loc: main 10, test 2, test-support 0, test/main 0.20\n"))
        text = (self.repo / "build/muster/loc.md").read_bytes().decode("utf-8")
        self.assertEqual(text.count("## Lines of code"), 1)

    def test_github_summary_is_appended_to(self):
        step = self.work / "step.md"
        step.write_bytes(b"previous step\n")
        os.environ["GITHUB_STEP_SUMMARY"] = str(step)
        self.assertEqual(self.main("loc", "--github")[0], 0)
        self.assertTrue(step.read_bytes().decode("utf-8").startswith("previous step\n## Lines of code\n"))


class OwnProfileTest(unittest.TestCase):
    """loc over a copy of a profile like muster's own, as CI runs it with --fail-on-error."""

    def setUp(self):
        self.repo = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.repo)}))
        files = {
            "muster.toml": OWN_PROFILE,
            "src/tool/__init__.py": '"""A package."""\n',
            "src/tool/core.py": "# a comment\n\ndef run():\n    return 1  # inline\n",
            "tests/test_core.py": "import unittest\n\n\nclass CoreTest(unittest.TestCase):\n    pass\n",
        }
        for path, text in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_bytes(text.encode("ascii"))

    def checked_run(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--repo", str(self.repo), "loc", "--fail-on-error"])
        return code, out.getvalue()

    def test_own_profile_passes(self):
        self.assertEqual(self.checked_run(), (0, "loc: main 3, test 3, test-support 0, test/main 1.00\n"))

    def test_file_type_without_a_language_fails(self):
        text = OWN_PROFILE.replace('include = ["**/*.py"]', 'include = ["**/*.py", "**/*.cfg"]')
        (self.repo / "muster.toml").write_bytes(text.encode("ascii"))
        (self.repo / "src/tool/setup.cfg").write_bytes(b"[tool]\n")
        code, out = self.checked_run()
        self.assertEqual(code, 1)
        self.assertIn("::error title=Lines of code::.cfg: 1 file(s) of the LOC set have no language", out)


if __name__ == "__main__":
    unittest.main()
