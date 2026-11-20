"""Tests of muster.cli and of the entry point: the commands, exit codes, the JSON envelope on every
exit and the output of an analysis command.

The analysis command here is a stand-in registered for the test only; its state is set by each test.
Every run happens in temporary folders, with git's global and system configuration switched off.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster import __version__, cli
from muster.report import envelope, json_text

REPO_ROOT = Path(__file__).resolve().parents[1]
IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)


def version_envelope(code: int = 0, error=None, command="version") -> dict:
    return envelope(command, exit_code=code, error=error)


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


class UsageErrorTest(CliCase):
    def test_unknown_command(self):
        code, out, err = self.run_main("policy")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error title=muster::argument command: invalid choice: 'policy'"), out)
        self.assertIn("muster: error: argument command: invalid choice: 'policy'", err)
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
        code, _, _ = self.run_main("policy", "--json-out", "out.json")
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
        self.assertEqual(self.run_main("policy", "--json-out=eq.json")[0], 2)
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

    def test_config_error_writes_a_summary_line(self):
        self.config.write_bytes(b"bogus = 1\n")
        self.analyse("--summary-file", "summary.md")
        line = b"muster coverage: error: muster.toml: unknown key bogus\n\n"
        self.assertEqual((self.work / "summary.md").read_bytes(), line)


if __name__ == "__main__":
    unittest.main()
