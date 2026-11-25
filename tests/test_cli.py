"""Tests of muster.cli and of the entry point: the commands, exit codes, the JSON envelope on every
exit and the output of an analysis command.

The analysis command here is a stand-in registered for the test only; its state is set by each test.
Every run happens in temporary folders, with git's global and system configuration switched off.
"""

import argparse
import ast
import contextlib
import io
import json
import math
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster import __version__, cli
from muster.report import FatalError, envelope, json_text

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)


def version_envelope(code: int = 0, error=None, command="version") -> dict:
    return envelope(command, exit_code=code, error=error)


def option_strings(parser: argparse.ArgumentParser) -> tuple[set[str], set[str]]:
    """Every option string of the parser and of its commands, and those of the parser itself that
    take a value."""
    every, values = set(), set()
    for action in parser._actions:
        every.update(action.option_strings)
        if action.option_strings and action.nargs != 0:
            values.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for command in action.choices.values():
                every |= option_strings(command)[0]
    return every, values


class CliCase(unittest.TestCase):
    """A repository folder with an empty muster.toml, a separate working directory and a stand-in
    analysis command named coverage."""

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        for key in [key for key in os.environ if key.startswith("GIT_") or key == "GITHUB_STEP_SUMMARY"]:
            del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.root)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config = self.repo / "muster.toml"
        self.config.write_bytes(b"")
        self.work = self.root / "work"
        self.work.mkdir()
        self.enterContext(contextlib.chdir(self.work))
        self.behave = lambda args, cfg, rep: None
        stand_in = cli.Analysis(lambda args, cfg, rep: self.behave(args, cfg, rep), "stand-in section")
        self.enterContext(mock.patch.dict(cli.ANALYSES, {"coverage": stand_in}))

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def read_json(self, path: Path) -> dict:
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        return json.loads(raw.decode("ascii"))


class VersionTest(CliCase):
    def test_text(self):
        self.assertEqual(self.run_main("version"), (0, f"muster {__version__}\n", ""))

    def test_json(self):
        code, out, err = self.run_main("version", "--format", "json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json_text(version_envelope()))
        self.assertEqual(json.loads(out)["verdict"], "pass")

    def test_json_out_with_text(self):
        code, out, _ = self.run_main("version", "--json-out", "version.json")
        self.assertEqual((code, out), (0, f"muster {__version__}\n"))
        self.assertEqual(self.read_json(self.work / "version.json"), version_envelope())

    def test_needs_no_repository_or_config(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(self.run_main("--repo", str(empty), "--config", "missing.toml", "version")[0], 0)

    def test_takes_no_analysis_options(self):
        code, out, err = self.run_main("version", "--strict")
        self.assertEqual(code, 2)
        self.assertIn("muster version: error: unrecognized arguments: --strict", err)
        self.assertEqual(out, "::error title=muster version::unrecognized arguments: --strict\n")

    def test_help_exits_zero_without_envelope(self):
        code, out, _ = self.run_main("--help", "--json-out", "help.json")
        self.assertEqual(code, 0)
        self.assertIn("usage: muster", out)
        self.assertFalse((self.work / "help.json").exists())


class EntryPointTest(unittest.TestCase):
    """`python -I src` puts the src folder on sys.path[0] and runs the command line."""

    def run_src(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", "src", *argv], cwd=REPO_ROOT, capture_output=True)

    def test_version_text(self):
        proc = self.run_src("version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.decode("ascii").replace("\r\n", "\n"), f"muster {__version__}\n")

    def test_version_json(self):
        proc = self.run_src("version", "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.decode("ascii")), version_envelope())

    def test_usage_error_exits_two(self):
        proc = self.run_src("no-such-command")
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"invalid choice", proc.stderr)

    def entry_point(self) -> dict:
        return runpy.run_path(str(REPO_ROOT / "src" / "__main__.py"), run_name="entry_point")

    def test_scanners_know_the_options_of_the_parser(self):
        every, global_values = option_strings(cli.build_parser())
        entry = self.entry_point()
        self.assertEqual((set(cli.OPTIONS), set(entry["OPTIONS"])), (every, every))
        self.assertEqual((set(cli.GLOBAL_OPTIONS), set(entry["GLOBAL_OPTIONS"])), (global_values, global_values))

    def test_entry_point_knows_the_commands(self):
        self.assertEqual(set(self.entry_point()["COMMANDS"]), {"version", *cli.ANALYSES})

    def test_entry_point_is_python_3_8_syntax(self):
        source = (REPO_ROOT / "src" / "__main__.py").read_text(encoding="ascii")
        ast.parse(source, filename="__main__.py", feature_version=(3, 8))


class OldPythonTest(unittest.TestCase):
    """src/__main__.py on a Python older than 3.11, run with the version patched."""

    def setUp(self):
        self.work = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(contextlib.chdir(self.work))

    def run_old(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "version_info", (3, 10, 4, "final", 0)),
            mock.patch.object(sys, "argv", ["src", *argv]),
            mock.patch("muster.cli.main", side_effect=AssertionError("the command line must not run")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(str(REPO_ROOT / "src" / "__main__.py"), run_name="__main__")
        return caught.exception.code, out.getvalue(), err.getvalue()

    MESSAGE = "Python 3.11 or newer is needed, this is 3.10"

    def expected(self, command) -> dict:
        document = envelope(command, exit_code=2, error=("usage", self.MESSAGE))
        return document

    def test_text_mode(self):
        code, out, err = self.run_old("policy", "--strict")
        self.assertEqual((code, out), (2, f"::error title=muster::{self.MESSAGE}\n"))
        self.assertEqual(err, f"muster: error: {self.MESSAGE}\n")

    def test_json_format_and_json_out(self):
        code, out, err = self.run_old("--repo", "repo", "policy", "--format=json", "--json-out", "out/env.json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out), self.expected("policy"))
        raw = (self.work / "out" / "env.json").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertEqual(json.loads(raw), self.expected("policy"))
        self.assertEqual(err, f"muster: error: {self.MESSAGE}\n")

    def test_scan_follows_the_command_line_rules(self):
        code, out, _ = self.run_old("--config", "x.toml", "version", "--json-out", "first.json", "--json-out=-1",
                                    "--", "--json-out=ignored.json", "--format=json")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error title=muster::"), out)
        self.assertEqual(sorted(os.listdir(self.work)), ["-1"])
        self.assertEqual(json.loads((self.work / "-1").read_bytes())["command"], "version")

    def test_no_command(self):
        code, out, _ = self.run_old("--format=json", "--json-out=")
        self.assertEqual((code, json.loads(out)["command"]), (2, None))
        self.assertEqual(os.listdir(self.work), [])

    def test_option_value_is_not_the_command(self):
        code, out, _ = self.run_old("--format", "json", "--summary-file", "summary.md")
        self.assertEqual((code, json.loads(out)["command"]), (2, None))

    def test_unwritable_json_out(self):
        (self.work / "taken").mkdir()
        code, _, err = self.run_old("version", "--json-out", "taken")
        self.assertEqual(code, 2)
        self.assertIn("muster: error: cannot write --json-out taken", err)

    def test_option_with_a_value_is_no_value_of_the_option_before(self):
        code, _, _ = self.run_old("policy", "--json-out", "--summary-file=my dir/summary.md")
        self.assertEqual(code, 2)
        self.assertEqual(os.listdir(self.work), [])

    def test_unknown_command_is_none(self):
        code, out, _ = self.run_old("--config", "other.toml", "lint", "--format", "json")
        self.assertEqual((code, json.loads(out)["command"]), (2, None))


class BrokenInstallTest(unittest.TestCase):
    """src/__main__.py when the command line cannot be imported."""

    MESSAGE = "ModuleNotFoundError: import of muster.cli halted; None in sys.modules"

    def setUp(self):
        self.work = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(contextlib.chdir(self.work))

    def run_broken(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(sys.modules, {"muster.cli": None}),
            mock.patch.object(sys, "argv", ["src", *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(str(REPO_ROOT / "src" / "__main__.py"), run_name="__main__")
        return caught.exception.code, out.getvalue(), err.getvalue()

    def test_text_mode(self):
        code, out, err = self.run_broken("policy", "--strict")
        self.assertEqual((code, out), (3, f"::error title=muster::{self.MESSAGE}\n"))
        self.assertIn("Traceback", err)
        self.assertTrue(err.endswith(f"muster: error: {self.MESSAGE}\n"), err)

    def test_json_format_and_json_out(self):
        code, out, _ = self.run_broken("policy", "--format", "json", "--json-out", "out/env.json")
        expected = envelope("policy", exit_code=3, error=("internal", self.MESSAGE))
        self.assertEqual((code, json.loads(out)), (3, expected))
        self.assertEqual(json.loads((self.work / "out" / "env.json").read_bytes()), expected)


class UsageErrorTest(CliCase):
    def test_unknown_command(self):
        code, out, err = self.run_main("lint")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error title=muster::argument command: invalid choice: 'lint'"), out)
        self.assertIn("muster: error: argument command: invalid choice: 'lint'", err)
        self.assertIn("usage: muster", err)

    def test_no_command(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("muster: error: the following arguments are required: command", err)

    def test_abbreviation_rejected(self):
        self.assertEqual(self.run_main("version", "--form", "json")[0], 2)

    def test_envelope_replaces_existing_json_out(self):
        out_file = self.work / "out.json"
        out_file.write_bytes(b"stale content\n")
        code, _, _ = self.run_main("version", "--bogus", "--json-out", str(out_file))
        self.assertEqual(code, 2)
        message = "unrecognized arguments: --bogus"
        self.assertEqual(self.read_json(out_file), version_envelope(2, ("usage", message)))

    def test_unknown_command_envelope_has_no_command(self):
        code, _, _ = self.run_main("lint", "--json-out", "out.json")
        env = self.read_json(self.work / "out.json")
        self.assertEqual((code, env["command"], env["verdict"], env["exit_code"]), (2, None, "error", 2))
        self.assertEqual(env["error"]["kind"], "usage")

    def test_format_json_prints_only_the_envelope(self):
        for argv in (["version", "--bogus", "--format", "json"], ["version", "--format=json", "--bogus"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv)
                self.assertEqual(code, 2)
                env = json.loads(out)
                self.assertEqual(env["error"], {"kind": "usage", "message": "unrecognized arguments: --bogus"})
                self.assertIn("muster version: error:", err)

    def test_last_format_wins(self):
        code, out, _ = self.run_main("version", "--format", "json", "--format", "text", "--bogus")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error"), out)

    def test_json_out_equals_form(self):
        self.assertEqual(self.run_main("lint", "--json-out=eq.json")[0], 2)
        self.assertEqual(self.read_json(self.work / "eq.json")["exit_code"], 2)

    def test_last_json_out_wins(self):
        self.assertEqual(self.run_main("version", "--json-out", "first.json", "--json-out=second.json")[0], 0)
        self.assertFalse((self.work / "first.json").exists())
        self.assertEqual(self.read_json(self.work / "second.json"), version_envelope())

    def test_last_json_out_wins_on_usage_error(self):
        self.run_main("version", "--json-out=first.json", "--json-out", "second.json", "--bogus")
        self.assertFalse((self.work / "first.json").exists())
        self.assertEqual(self.read_json(self.work / "second.json")["exit_code"], 2)

    def test_relative_json_out_is_relative_to_the_working_directory(self):
        self.assertEqual(self.run_main("--repo", str(self.repo), "coverage", "--json-out", "rel.json")[0], 0)
        self.assertTrue((self.work / "rel.json").exists())
        self.assertFalse((self.repo / "rel.json").exists())

    def test_json_out_without_value(self):
        code, _, err = self.run_main("version", "--json-out")
        self.assertEqual(code, 2)
        self.assertIn("expected one argument", err)
        self.assertEqual(sorted(os.listdir(self.work)), [])

    def test_strict_with_strict_only(self):
        code, _, err = self.run_main("--repo", str(self.repo), "coverage", "--strict", "--strict-only", "coverage")
        self.assertEqual(code, 2)
        self.assertIn("argument --strict-only: not allowed with argument --strict", err)

    def test_unknown_strict_only_section(self):
        code, _, err = self.run_main("--repo", str(self.repo), "coverage", "--strict-only", "coverage,lint")
        self.assertEqual(code, 2)
        self.assertIn("unknown section 'lint'", err)

    def test_bad_format_value(self):
        self.assertEqual(self.run_main("version", "--format", "xml")[0], 2)


class ArgumentScanTest(CliCase):
    """The parsed --json-out is the destination; argv is read only when argparse fails."""

    def test_value_that_looks_like_a_negative_number(self):
        self.assertEqual(self.run_main("version", "--json-out", "-1")[0], 0)
        self.assertEqual(self.read_json(self.work / "-1"), version_envelope())

    def test_nothing_after_the_option_terminator_counts(self):
        code, out, _ = self.run_main("version", "--", "--json-out=ignored.json", "--format=json")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error title=muster version::"), out)
        self.assertEqual(os.listdir(self.work), [])

    def test_help_exits_zero_before_any_output_check(self):
        with contextlib.chdir(self.repo):
            code, out, _ = self.run_main("version", "--help", "--json-out", "muster.toml")
        self.assertEqual(code, 0)
        self.assertIn("usage: muster version", out)
        self.assertEqual(self.config.read_bytes(), b"")

    def test_empty_json_out(self):
        coverage = ["--repo", str(self.repo), "coverage", "--json-out="]
        for argv in (["version", "--json-out", ""], ["version", "--json-out="], coverage):
            with self.subTest(argv=argv):
                code, out, err = self.run_main(*argv, "--format", "json")
                self.assertEqual(code, 2)
                message = "--json-out: the file name is empty"
                self.assertEqual(json.loads(out)["error"], {"kind": "usage", "message": message})
                self.assertEqual(os.listdir(self.work), [])

    def test_dash_is_no_json_out(self):
        code, _, err = self.run_main("version", "--json-out", "-")
        self.assertEqual(code, 2)
        self.assertIn("--json-out: '-' is not a file; use --format json to print the envelope", err)
        self.assertEqual(os.listdir(self.work), [])

    def test_empty_summary_file_still_gets_the_envelope(self):
        self.config.write_bytes(b'[output]\ndir = "build"\n')
        (self.work / "out.json").write_bytes(b"stale\n")
        argv = ["--repo", str(self.repo), "coverage", "--json-out", "out.json", "--summary-file", ""]
        code, _, err = self.run_main(*argv)
        self.assertEqual(code, 2)
        message = "--summary-file: the file name is empty"
        self.assertIn(message, err)
        self.assertEqual(self.read_json(self.work / "out.json")["error"], {"kind": "usage", "message": message})
        self.assertFalse((self.repo / "build").exists())

    def test_empty_json_out_still_gets_the_summary_line(self):
        argv = ["--repo", str(self.repo), "coverage", "--json-out", "", "--summary-file", "summary.md"]
        self.assertEqual(self.run_main(*argv)[0], 2)
        line = b"muster coverage: error: --json-out: the file name is empty\n\n"
        self.assertEqual((self.work / "summary.md").read_bytes(), line)

    def test_global_option_after_the_command_is_not_the_config_file(self):
        with contextlib.chdir(self.repo):
            code, _, err = self.run_main("coverage", "--config", "other.toml", "--json-out", "muster.toml")
        self.assertEqual(code, 2)
        self.assertIn("--json-out muster.toml is the config file", err)
        self.assertEqual(self.config.read_bytes(), b"")

    def test_default_profile_is_kept_with_another_config_file(self):
        with contextlib.chdir(self.repo):
            code, _, err = self.run_main("--config", "other.toml", "coverage", "--bogus", "--json-out", "muster.toml")
        self.assertEqual(code, 2)
        self.assertIn("--json-out muster.toml is the default config file", err)
        self.assertEqual(self.config.read_bytes(), b"")

    def test_option_with_a_value_is_no_value_of_the_option_before(self):
        code, _, err = self.run_main("coverage", "--json-out", "--summary-file=my dir/summary.md")
        self.assertEqual(code, 2)
        self.assertIn("argument --json-out: expected one argument", err)
        self.assertEqual(os.listdir(self.work), ["my dir"])
        self.assertTrue((self.work / "my dir" / "summary.md").read_bytes().startswith(b"muster coverage: error:"))

    def test_summary_file_on_a_usage_error_is_checked_against_the_config_file(self):
        with contextlib.chdir(self.repo):
            argv = ["version", "--bogus", "--summary-file", "muster.toml", "--json-out", "out.json"]
            code, _, err = self.run_main(*argv)
        self.assertEqual(code, 2)
        self.assertIn("--summary-file muster.toml is the config file", err)
        self.assertEqual(self.config.read_bytes(), b"")
        self.assertFalse((self.repo / "out.json").exists())

    def test_parsed_json_out_is_checked_against_the_config_file(self):
        with contextlib.chdir(self.repo):
            code, _, err = self.run_main("version", "--json-out", "-1", "--json-out", "muster.toml")
        self.assertEqual(code, 2)
        self.assertIn("--json-out muster.toml is the config file", err)
        self.assertEqual(self.config.read_bytes(), b"")


class ScannedOutputClashTest(CliCase):
    """After an argument error, and for version, the outputs are checked against the profile as far
    as it parses: no output is a file under a source or test folder or a file the profile names."""

    PROFILE = (
        b'schema = 1\n\n[source]\nroots = ["src/main/java"]\ntests = ["src/test/java"]\n\n'
        b'[zones]\ncritical = "policy/critical.txt"\n\n[policy]\nsnapshot = "build/muster/snapshot.json"\n'
    )
    # Argument errors: argparse refuses the value, an unknown option, an empty file name.
    ERRORS = (("loc", "--base="), ("coverage", "--bogus"), ("loc", "--summary-file", ""))

    def setUp(self):
        super().setUp()
        self.config.write_bytes(self.PROFILE)
        self.source = self.repo / "src/main/java/org/example/app/Engine.java"
        self.test = self.repo / "src/test/java/org/example/app/EngineTest.java"
        for path in (self.source, self.test):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"class Engine {}\n")

    def assert_refused(self, argv: list[str], target: Path, message: str) -> None:
        before = target.read_bytes()
        code, _, err = self.run_main("--repo", str(self.repo), *argv)
        self.assertEqual(code, 2, err)
        self.assertIn(f"error: {message}\n", err)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(os.listdir(self.work), [])

    def under_folders(self) -> tuple[tuple[Path, str], ...]:
        return ((self.source, "the source folder src/main/java"), (self.test, "the test folder src/test/java"))

    def test_json_out_under_a_source_or_test_folder(self):
        for error in self.ERRORS:
            for target, folder in self.under_folders():
                with self.subTest(error=error, folder=folder):
                    argv = [*error, "--json-out", str(target)]
                    self.assert_refused(argv, target, f"--json-out {target} is under {folder}")

    def test_summary_file_under_a_source_or_test_folder(self):
        for error in self.ERRORS[:2]:
            for target, folder in self.under_folders():
                with self.subTest(error=error, folder=folder):
                    argv = [*error, "--json-out", "out.json", "--summary-file", str(target)]
                    self.assert_refused(argv, target, f"--summary-file {target} is under {folder}")

    def test_version_json_out_under_a_source_folder(self):
        argv = ["version", "--json-out", str(self.source)]
        self.assert_refused(argv, self.source, f"--json-out {self.source} is under the source folder src/main/java")
        argv = ["--config", "muster.toml", "version", "--format", "json", "--json-out", str(self.test)]
        self.assert_refused(argv, self.test, f"--json-out {self.test} is under the test folder src/test/java")

    def test_file_the_profile_names(self):
        named = (("policy/critical.txt", "zones.critical"), ("build/muster/snapshot.json", "policy.snapshot"))
        for path, key in named:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"kept\n")
            for argv in (["loc", "--base=", "--json-out", str(target)], ["version", "--json-out", str(target)]):
                with self.subTest(key=key, command=argv[0]):
                    self.assert_refused(argv, target, f"--json-out {target} is the file of {key}")

    def test_profile_that_cannot_be_read_or_is_not_toml_keeps_the_envelope(self):
        for profile in (None, b"[source\n"):
            with self.subTest(profile=profile):
                self.config.unlink(missing_ok=True)
                if profile is not None:
                    self.config.write_bytes(profile)
                code, _, _ = self.run_main("--repo", str(self.repo), "loc", "--base=", "--json-out", "out.json")
                self.assertEqual(code, 2)
                self.assertEqual(self.read_json(self.work / "out.json")["error"]["kind"], "usage")
                (self.work / "out.json").unlink()

    def test_repo_and_config_read_from_argv_may_be_anything(self):
        missing, nul = str(self.root / "missing"), chr(0)  # no command line carries a NUL, cli.main() may
        cases = (
            (missing, "muster.toml"),
            (str(self.config), "muster.toml"),
            (str(self.repo), "src"),
            (str(self.repo) + nul, "muster.toml"),
            (str(self.repo), "muster" + nul + ".toml"),
        )
        for repo, config in cases:
            for command, expected in ((["loc", "--base="], 2), (["version"], 0)):
                with self.subTest(repo=repo, config=config, command=command[0]):
                    argv = ["--repo", repo, "--config", config, *command, "--json-out", "out.json"]
                    code, _, err = self.run_main(*argv)
                    self.assertEqual(code, expected, err)
                    self.assertEqual(self.read_json(self.work / "out.json")["exit_code"], expected)
                    (self.work / "out.json").unlink()

    def test_files_the_options_name(self):
        kept = self.work / "kept.xml"
        kept.write_bytes(b"kept\n")
        found = self.work / "results" / "deeper" / "TEST-one.xml"
        found.parent.mkdir(parents=True)
        found.write_bytes(b"<testsuite/>\n")
        junit_file = "results/deeper/TEST-one.xml"
        cases = [([option, "kept.xml"], "kept.xml", f"{option} kept.xml") for option in ("--before", "--after")]
        cases.append((["--diff-file", "kept.xml"], "kept.xml", "--diff-file kept.xml"))
        for option in ("--junit-before", "--junit-after"):
            cases.append(([option, "results"], junit_file, f"the JUnit file {junit_file}"))
        for options, target, label in cases:
            for output in ("--json-out", "--summary-file"):
                with self.subTest(options=options, output=output):
                    before = (self.work / target).read_bytes()
                    code, _, err = self.run_main("--repo", str(self.repo), "loc", "--base=", *options, output, target)
                    self.assertEqual(code, 2, err)
                    self.assertIn(f"error: {output} {target} is {label}\n", err)
                    self.assertEqual((self.work / target).read_bytes(), before)
        nul = ["--before", f"kept{chr(0)}.xml", "--junit-after", f"results{chr(0)}"]
        code, _, err = self.run_main("--repo", str(self.repo), "loc", "--base=", *nul, "--json-out", "out.json")
        self.assertEqual((code, self.read_json(self.work / "out.json")["exit_code"]), (2, 2), err)

    def test_every_value_of_an_input_option_is_an_input(self):
        kept = self.work / "kept.xml"
        kept.write_bytes(b"kept\n")
        for command in ("coverage-diff", "mutation-diff"):
            with self.subTest(command=command):
                argv = [command, "--before=kept.xml", "--before=", "--after=after.xml", "--json-out=kept.xml"]
                code, _, err = self.run_main("--repo", str(self.repo), *argv)
                self.assertEqual(code, 2, err)
                self.assertIn("error: --json-out kept.xml is --before kept.xml\n", err)
                self.assertEqual(kept.read_bytes(), b"kept\n")

    def test_nul_in_repo_still_refuses_two_outputs_that_are_one_file(self):
        argv = ["--repo", str(self.repo) + chr(0), "loc", "--base=", "--json-out", "same.out"]
        code, _, err = self.run_main(*argv, "--summary-file", "same.out")
        self.assertEqual(code, 2, err)
        self.assertIn("error: --summary-file same.out is the same file as --json-out same.out\n", err)
        self.assertEqual(os.listdir(self.work), [])

    def test_nul_in_config_keeps_the_default_config_file(self):
        argv = ["--config", "muster" + chr(0) + ".toml", "loc", "--base=", "--json-out", str(self.config)]
        self.assert_refused(argv, self.config, f"--json-out {self.config} is the default config file")

    def test_step_summary_under_a_source_or_test_folder(self):
        for error in self.ERRORS[:2]:
            for target, folder in self.under_folders():
                with self.subTest(error=error, folder=folder):
                    os.environ["GITHUB_STEP_SUMMARY"] = str(target)
                    argv = [*error, "--github", "--json-out", "out.json"]
                    self.assert_refused(argv, target, f"$GITHUB_STEP_SUMMARY {target} is under {folder}")

    def test_config_that_is_no_regular_file_is_not_read(self):
        (self.repo / "profile").mkdir()
        for command, expected in ((["loc", "--base="], 2), (["version"], 0)):
            with self.subTest(command=command[0]), mock.patch.object(cli, "load_unchecked") as load:
                argv = ["--repo", str(self.repo), "--config", "profile", *command, "--json-out", "out.json"]
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, expected, err)
                load.assert_not_called()
                self.assertEqual(self.read_json(self.work / "out.json")["exit_code"], expected)
                (self.work / "out.json").unlink()

    def test_path_that_cannot_be_resolved_is_no_clash(self):
        # os.path.realpath raises for a busy named pipe on Windows; here it raises for one path.
        realpath = os.path.realpath
        for unresolved in (self.config, self.work / "out.json", self.repo / "src/main/java"):
            for command, expected in ((["loc", "--base="], 2), (["version"], 0)):

                def raising(path, *args, **kwargs):
                    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(unresolved)):
                        raise OSError(231, "All pipe instances are busy")
                    return realpath(path, *args, **kwargs)

                with self.subTest(path=unresolved, command=command[0]):
                    with mock.patch("os.path.realpath", side_effect=raising) as patched:
                        argv = ["--repo", str(self.repo), *command, "--json-out", "out.json"]
                        code, _, err = self.run_main(*argv)
                    self.assertTrue(patched.called)
                    self.assertEqual(code, expected, err)
                    self.assertEqual(self.read_json(self.work / "out.json")["exit_code"], expected)
                    (self.work / "out.json").unlink()

    # The best-effort paths (version, an argument error) and a run of a command.
    REPO_COMMANDS = ((["version"], 0), (["loc", "--base="], 2), (["loc"], 2))

    # Resolving a folder raises OSError for a busy named pipe on Windows, and RuntimeError for a
    # link loop on Python 3.11 and 3.12.
    RESOLVE_FAILURES = (OSError(231, "All pipe instances are busy"), RuntimeError("a synthetic link loop"))

    def resolve_raising_for(self, folder: Path, failure: Exception):
        """Path.resolve patched to raise `failure` for `folder` only."""
        resolve = Path.resolve

        def raising(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(folder)):
                raise failure
            return resolve(path, *args, **kwargs)

        return mock.patch.object(Path, "resolve", autospec=True, side_effect=raising)

    def test_repo_that_cannot_be_resolved_is_never_a_bug(self):
        for failure in self.RESOLVE_FAILURES:
            for command, expected in self.REPO_COMMANDS:
                with self.subTest(failure=type(failure).__name__, command=command):
                    with self.resolve_raising_for(self.repo, failure) as patched:
                        code, _, err = self.run_main("--repo", str(self.repo), *command, "--json-out", "out.json")
                    self.assertTrue(patched.called)
                    self.assertEqual(code, expected, err)
                    env = self.read_json(self.work / "out.json")
                    self.assertEqual(env["exit_code"], expected)
                    if command == ["loc"]:
                        self.assertEqual(env["error"]["kind"], "usage")
                        prefix = f"--repo {self.repo}: cannot resolve the folder: "
                        self.assertTrue(env["error"]["message"].startswith(prefix), env["error"])
                    (self.work / "out.json").unlink()

    def test_working_directory_that_cannot_be_resolved_is_never_a_bug(self):
        for failure in self.RESOLVE_FAILURES:
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(cli, "find_repo_root", side_effect=failure) as found,
                    self.resolve_raising_for(self.work, failure) as patched,
                ):
                    code, _, err = self.run_main("loc", "--json-out", "out.json")
                self.assertTrue(found.called and patched.called)
                self.assertEqual(code, 2, err)
                env = self.read_json(self.work / "out.json")
                self.assertEqual((env["exit_code"], env["error"]["kind"]), (2, "usage"))
                prefix = "the working directory: cannot resolve the folder: "
                self.assertTrue(env["error"]["message"].startswith(prefix), env["error"])
                (self.work / "out.json").unlink()

    def test_repo_that_is_a_link_loop(self):
        loop = self.root / "loop"
        try:
            os.symlink(loop, loop, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symbolic link here")
        for command, expected in self.REPO_COMMANDS:
            with self.subTest(command=command):
                code, _, err = self.run_main("--repo", str(loop), *command, "--json-out", "out.json")
                self.assertEqual(code, expected, err)
                env = self.read_json(self.work / "out.json")
                self.assertEqual(env["exit_code"], expected)
                if command == ["loc"]:  # cannot resolve (Python 3.11, 3.12), else cannot read the profile
                    self.assertIn(env["error"]["kind"], ("usage", "config"))
                (self.work / "out.json").unlink()


class OutputOrderTest(CliCase):
    """A normal run compares each output in turn: an earlier clash is refused before a later output
    can fail to resolve."""

    def test_clash_before_an_output_that_cannot_be_resolved(self):
        self.config.write_bytes(b'schema = 1\n\n[source]\nroots = ["src/main/java"]\n')
        source = self.repo / "src/main/java/org/example/app/Engine.java"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"class Engine {}\n")
        summary = self.work / "summary.md"
        summary.write_bytes(b"earlier step\n")
        realpath = os.path.realpath

        def raising(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(summary)):
                raise OSError(231, "All pipe instances are busy")
            return realpath(path, *args, **kwargs)

        argv = ["--repo", str(self.repo), "coverage", "--json-out", str(source), "--summary-file", str(summary)]
        with mock.patch("os.path.realpath", side_effect=raising):
            code, _, err = self.run_main(*argv)
        self.assertEqual(code, 2, err)
        self.assertIn(f"error: --json-out {source} is under the source folder src/main/java\n", err)
        self.assertEqual(source.read_bytes(), b"class Engine {}\n")
        self.assertEqual(summary.read_bytes(), b"earlier step\n")
        self.assertEqual(os.listdir(self.work), ["summary.md"])


class InternalErrorTest(CliCase):
    def test_bug_exits_three_with_envelope(self):
        with mock.patch.object(cli, "run_version", side_effect=RuntimeError("boom")):
            code, out, err = self.run_main("version", "--json-out", "out.json")
        self.assertEqual(code, 3)
        expected = version_envelope(3, ("internal", "RuntimeError: boom"))
        self.assertEqual(self.read_json(self.work / "out.json"), expected)
        self.assertEqual(out, "::error title=muster version::RuntimeError: boom\n")
        self.assertIn("Traceback", err)
        self.assertTrue(err.endswith("muster version: error: RuntimeError: boom\n"), err)

    def test_bug_with_format_json(self):
        with mock.patch.object(cli, "run_version", side_effect=KeyError("x")):
            code, out, _ = self.run_main("version", "--format", "json")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["error"], {"kind": "internal", "message": "KeyError: 'x'"})

    def test_bug_in_analysis_writes_a_summary_line(self):
        def crash(args, cfg, rep):
            raise ZeroDivisionError("division by zero")

        self.behave = crash
        summary = self.work / "summary.md"
        code, _, _ = self.run_main("--repo", str(self.repo), "coverage", "--summary-file", str(summary))
        self.assertEqual(code, 3)
        self.assertEqual(summary.read_bytes(), b"muster coverage: error: ZeroDivisionError: division by zero\n\n")

    def test_unwritable_json_out_keeps_exit_three(self):
        denied = PermissionError(13, "Permission denied")
        with (
            mock.patch.object(cli, "run_version", side_effect=RuntimeError("boom")),
            mock.patch.object(cli, "write_json", side_effect=denied),
        ):
            code, _, err = self.run_main("version", "--json-out", "out.json")
        self.assertEqual(code, 3)
        self.assertIn("muster: error: cannot write --json-out out.json: Permission denied", err)
        self.assertIn("muster version: error: RuntimeError: boom", err)

    def test_data_that_json_cannot_carry_is_a_bug(self):
        self.behave = lambda args, cfg, rep: rep.data.update(when=object())
        code, out, _ = self.run_main("--repo", str(self.repo), "coverage", "--format", "json")
        self.assertEqual(code, 3)
        env = json.loads(out)
        self.assertEqual((env["error"]["kind"], env["sections"]), ("internal", []))

    def test_data_nested_too_deeply_for_json_is_a_bug(self):
        # How deep the encoder goes before a RecursionError depends on the Python version and on
        # the stack, so the encoder raises it here for any envelope that carries a section.
        encode = cli.json_text

        def too_deep(document):
            if document["sections"]:
                raise RecursionError("maximum recursion depth exceeded while encoding a JSON object")
            return encode(document)

        self.behave = lambda args, cfg, rep: rep.data.update(nested=[[[]]])
        with mock.patch.object(cli, "json_text", side_effect=too_deep):
            code, out, _ = self.run_main("--repo", str(self.repo), "coverage", "--format", "json")
        self.assertEqual(code, 3)
        env = json.loads(out)
        self.assertEqual((env["error"]["kind"], env["sections"]), ("internal", []))

    def test_number_that_json_has_not_is_a_bug(self):
        for value in (math.nan, math.inf):
            with self.subTest(value=value):
                self.behave = lambda args, cfg, rep: rep.data.update(ratio=value)
                code, out, _ = self.run_main("--repo", str(self.repo), "coverage", "--format", "json")
                self.assertEqual(code, 3)
                env = json.loads(out)
                self.assertEqual((env["error"]["kind"], env["sections"]), ("internal", []))


class OutputCollisionTest(CliCase):
    def test_json_out_is_the_config_file(self):
        before = b"[output]\ndir = \"build\"\n"
        self.config.write_bytes(before)
        code, out, err = self.run_main("--repo", str(self.repo), "coverage", "--json-out", str(self.config))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn("is the config file", err)
        self.assertTrue(out.startswith("::error title=muster coverage::"), out)

    def test_relative_json_out_and_default_config(self):
        with contextlib.chdir(self.repo):
            code, out, _ = self.run_main("version", "--format", "json", "--json-out", "muster.toml")
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_bytes(), b"")
        self.assertEqual(json.loads(out)["error"]["kind"], "usage")

    def test_custom_config_path(self):
        profile = self.repo / "profile" / "muster.toml"
        profile.parent.mkdir()
        profile.write_bytes(b"schema = 1\n")
        argv = ["--repo", str(self.repo), "--config", "profile/muster.toml", "version", "--json-out", str(profile)]
        self.assertEqual(self.run_main(*argv)[0], 2)
        self.assertEqual(profile.read_bytes(), b"schema = 1\n")

    def test_also_on_a_usage_error(self):
        code, _, _ = self.run_main("--repo", str(self.repo), "version", "--bogus", "--json-out", str(self.config))
        self.assertEqual(code, 2)
        self.assertEqual(self.config.read_bytes(), b"")

    def test_json_out_that_fails_at_the_end(self):
        with mock.patch.object(cli, "write_json", side_effect=PermissionError(13, "Permission denied")):
            code, out, err = self.run_main("version", "--json-out", "out.json")
        self.assertEqual(code, 2)
        self.assertEqual(out, f"muster {__version__}\n::error title=muster version::"
                         "cannot write --json-out out.json: Permission denied\n")
        self.assertIn("muster version: error: cannot write --json-out out.json: Permission denied", err)

    def test_unwritable_json_out(self):
        (self.work / "taken").mkdir()
        code, out, err = self.run_main("version", "--format", "json", "--json-out", "taken")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["kind"], "usage")
        self.assertIn("cannot write --json-out taken", err)
        self.assertTrue((self.work / "taken").is_dir())


class FailedRunOutputTest(CliCase):
    """An output flagged as removed when the run fails goes on every exit 2 or 3 after the clash check."""

    def setUp(self):
        super().setUp()
        self.kept = self.work / "kept.json"
        self.kept.write_bytes(b"old\n")
        self.behave = lambda args, cfg, rep: None
        stand_in = cli.Analysis(
            lambda args, cfg, rep: self.behave(args, cfg, rep),
            "stand-in section",
            outputs=lambda args, cfg: ([("the kept file", self.kept, False, True)], None),
        )
        self.enterContext(mock.patch.dict(cli.ANALYSES, {"coverage": stand_in}))

    def analyse(self, *options: str) -> int:
        return self.run_main("--repo", str(self.repo), "coverage", *options)[0]

    def test_kept_when_the_run_passes(self):
        self.assertEqual(self.analyse(), 0)
        self.assertTrue(self.kept.exists())

    def test_removed_on_a_fatal_error_and_on_a_bug(self):
        for error, code in ((FatalError("bad input"), 2), (RuntimeError("boom"), 3)):
            with self.subTest(code=code):
                self.kept.write_bytes(b"old\n")

                def fail(args, cfg, rep, error=error):
                    raise error

                self.behave = fail
                self.assertEqual(self.analyse(), code)
                self.assertFalse(self.kept.exists())

    def test_removed_when_the_json_out_cannot_be_cleared(self):
        (self.work / "taken").mkdir()
        self.assertEqual(self.analyse("--json-out", "taken"), 2)
        self.assertFalse(self.kept.exists())

    def test_every_output_is_removed_when_one_cannot_be(self):
        stale = self.work / "stale.tsv"
        stale.write_bytes(b"old\n")
        outputs = [("the stale file", stale, True, False)]
        stand_in = cli.Analysis(lambda args, cfg, rep: None, "stand-in", outputs=lambda args, cfg: (outputs, None))
        self.enterContext(mock.patch.dict(cli.ANALYSES, {"coverage": stand_in}))
        (self.work / "taken").mkdir()
        code, _, err = self.run_main("--repo", str(self.repo), "coverage", "--json-out", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write --json-out taken", err)
        self.assertFalse(stale.exists())

    def test_kept_when_the_profile_cannot_be_read(self):
        self.config.write_bytes(b"[coverage\n")
        self.assertEqual(self.analyse(), 2)
        self.assertTrue(self.kept.exists())

    def test_kept_after_a_clash(self):
        self.assertEqual(self.analyse("--json-out", str(self.kept)), 2)
        self.assertEqual(self.kept.read_bytes(), b"old\n")


class AnalysisTest(CliCase):
    def judge(self, *, violation=False, error=False, md=("## Coverage",)):
        def run(args, cfg, rep):
            rep.md.extend(md)
            rep.data["seen"] = True
            if violation:
                rep.violated("Floor", "line 50% below 80%")
            if error:
                rep.errored("Input", "report missing")
            rep.verdict = "coverage: " + ("FAIL" if rep.violation else "PASS") + "\n(details)"

        self.behave = run

    def analyse(self, *options: str) -> tuple[int, str, str]:
        return self.run_main("--repo", str(self.repo), "coverage", *options)

    def test_pass(self):
        self.judge()
        code, out, err = self.analyse("--json-out", "out.json")
        self.assertEqual((code, out, err), (0, "coverage: PASS (details)\n", ""))
        env = self.read_json(self.work / "out.json")
        state = (env["command"], env["verdict"], env["exit_code"], env["commit"])
        self.assertEqual(state, ("coverage", "pass", 0, None))
        self.assertEqual(env["sections"][0]["data"], {"seen": True})

    def test_violation_warns_without_strict(self):
        self.judge(violation=True)
        code, out, _ = self.analyse()
        self.assertEqual(code, 0)
        self.assertEqual(out, "::warning title=Floor::line 50%25 below 80%25\ncoverage: FAIL (details)\n")

    def test_violation_fails_under_strict(self):
        self.judge(violation=True)
        code, out, _ = self.analyse("--strict", "--json-out", "out.json")
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("::error title=Floor::"), out)
        self.assertEqual(self.read_json(self.work / "out.json")["verdict"], "fail")

    def test_strict_only_names_the_section(self):
        self.judge(violation=True)
        self.assertEqual(self.analyse("--strict-only", "policy, coverage")[0], 1)
        self.assertEqual(self.analyse("--strict-only", "policy")[0], 0)

    def test_input_error(self):
        self.judge(error=True)
        self.assertEqual(self.analyse()[0], 0)
        self.assertEqual(self.analyse("--fail-on-error")[0], 1)
        self.assertEqual(self.analyse("--strict")[0], 1)

    def test_format_json_prints_only_the_envelope(self):
        self.judge(violation=True)
        code, out, err = self.analyse("--format", "json")
        self.assertEqual((code, err), (0, ""))
        env = json.loads(out)
        self.assertEqual(env["sections"][0]["violations"], ["line 50% below 80%"])
        self.assertEqual(env["verdict"], "warn")

    def test_commit_in_a_repository(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        commit = ["git", *IDENTITY, "commit", "-q", "--allow-empty", "-m", "first"]
        subprocess.run(commit, cwd=self.repo, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, check=True).stdout
        self.judge()
        code, out, _ = self.analyse("--format", "json")
        self.assertEqual((code, json.loads(out)["commit"]), (0, head.decode("ascii").strip()))

    def test_summary_file_is_appended(self):
        self.judge(md=("## Coverage", "all good"))
        summary = self.work / "summary.md"
        summary.write_bytes(b"# Earlier step\n\n")
        self.assertEqual(self.analyse("--summary-file", str(summary))[0], 0)
        self.assertEqual(summary.read_bytes(), b"# Earlier step\n\n## Coverage\nall good\n\n")

    def test_github_summary(self):
        self.judge()
        summary = self.work / "step-summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        self.assertEqual(self.analyse("--github")[0], 0)
        self.assertEqual(summary.read_bytes(), b"## Coverage\n\n")

    def test_output_dir_summary_is_removed_and_gets_the_failure_line(self):
        def fail(args, cfg, rep):
            raise FatalError("no report")

        self.behave = fail
        self.config.write_bytes(b"[output]\ndir = \"build/muster\"\n")
        target = self.repo / "build" / "muster" / "coverage.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old report\n")
        self.assertEqual(self.analyse()[0], 2)
        self.assertEqual(target.read_bytes(), b"muster coverage: error: no report\n\n")

    def test_output_dir_is_overwritten(self):
        self.judge()
        self.config.write_bytes(b"[output]\ndir = \"build/muster\"\n")
        target = self.repo / "build" / "muster" / "coverage.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old report\n")
        self.assertEqual(self.analyse()[0], 0)
        self.assertEqual(target.read_bytes(), b"## Coverage\n\n")

    def test_summary_file_wins_over_output_dir(self):
        self.judge()
        self.config.write_bytes(b"[output]\ndir = \"build/muster\"\n")
        self.analyse("--summary-file", "summary.md")
        self.assertTrue((self.work / "summary.md").exists())
        self.assertFalse((self.repo / "build").exists())

    def test_no_summary_without_a_target(self):
        self.judge()
        self.assertEqual(self.analyse()[0], 0)
        self.assertEqual(sorted(os.listdir(self.repo)), ["muster.toml"])
        self.assertEqual(sorted(os.listdir(self.work)), [])

    def test_unwritable_summary(self):
        self.judge()
        (self.work / "taken").mkdir()
        code, _, err = self.analyse("--summary-file", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write the summary taken", err)

    def test_invalid_config(self):
        self.config.write_bytes(b"[coverage]\nformat = \"cobertura\"\n")
        code, out, err = self.analyse("--json-out", "out.json", "--strict")
        self.assertEqual(code, 2)
        message = 'muster.toml: coverage.format: must be one of jacoco, got "cobertura"'
        self.assertEqual(out, "::error title=muster coverage::" + message + "\n")
        self.assertIn("muster coverage: error: " + message, err)
        env = self.read_json(self.work / "out.json")
        self.assertEqual((env["error"], env["sections"]), ({"kind": "config", "message": message}, []))

    def test_missing_config(self):
        self.config.unlink()
        code, out, _ = self.analyse("--format", "json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], {"kind": "config", "message": "muster.toml: not found"})

    def test_name_that_is_not_utf8_reaches_the_summary(self):
        self.judge(md=("## Coverage", "- `src/bad" + chr(0xDCFF) + ".py`", "lone " + chr(0xD800)))
        code, _, _ = self.analyse("--summary-file", "summary.md")
        self.assertEqual(code, 0)
        text = (self.work / "summary.md").read_bytes().decode("utf-8")
        self.assertEqual(text, "## Coverage\n- `src/bad\N{REPLACEMENT CHARACTER}.py`\nlone ?\n\n")

    def test_config_error_writes_a_summary_line(self):
        self.config.write_bytes(b"bogus = 1\n")
        self.analyse("--summary-file", "summary.md")
        line = b"muster coverage: error: muster.toml: unknown key bogus\n\n"
        self.assertEqual((self.work / "summary.md").read_bytes(), line)


class BaseTest(CliCase):
    """The stand-in takes --base; the command line computes the merge base once, for the section
    (args.base_commit) and for the envelope."""

    def setUp(self):
        super().setUp()
        self.seen = []
        stand_in = cli.Analysis(
            lambda args, cfg, rep: self.seen.append(getattr(args, "base_commit", "not set")),
            "stand-in section",
            lambda parser: parser.add_argument("--base"),
        )
        self.enterContext(mock.patch.dict(cli.ANALYSES, {"coverage": stand_in}))

    def analyse(self, *options: str) -> tuple[int, dict]:
        code, out, _ = self.run_main("--repo", str(self.repo), "coverage", "--format", "json", *options)
        return code, json.loads(out)

    def commit(self) -> str:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        commit = ["git", *IDENTITY, "commit", "-q", "--allow-empty", "-m", "first"]
        subprocess.run(commit, cwd=self.repo, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, check=True).stdout
        return head.decode("ascii").strip()

    def test_null_without_base(self):
        code, env = self.analyse()
        self.assertEqual((code, env["base"], self.seen), (0, None, ["not set"]))

    def test_ref_and_merge_base(self):
        head = self.commit()
        code, env = self.analyse("--base", "main")
        self.assertEqual((code, env["base"]), (0, {"ref": "main", "commit": head}))
        self.assertEqual(self.seen, [head])

    def test_ref_without_merge_base(self):
        self.commit()
        code, env = self.analyse("--base", "no-such-ref")
        self.assertEqual((code, env["base"], self.seen), (0, {"ref": "no-such-ref", "commit": None}, [None]))

    def test_outside_a_repository(self):
        code, env = self.analyse("--base", "main")
        self.assertEqual((code, env["base"]), (0, {"ref": "main", "commit": None}))

    def test_kept_on_a_config_error(self):
        head = self.commit()
        self.config.write_bytes(b"bogus = 1\n")
        code, env = self.analyse("--base", "main")
        self.assertEqual((code, env["error"]["kind"], env["base"]), (2, "config", {"ref": "main", "commit": head}))

    def test_none_when_the_arguments_fail(self):
        code, env = self.analyse("--base", "main", "--no-such-option")
        self.assertEqual((code, env["base"]), (2, None))

    def test_envelope_carries_the_given_base(self):
        base = {"ref": "main", "commit": None}
        self.assertEqual(envelope("coverage", exit_code=0, base=base)["base"], base)
        self.assertIsNone(envelope("coverage", exit_code=0)["base"])


if __name__ == "__main__":
    unittest.main()
