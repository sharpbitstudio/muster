"""Tests of the policy command, end to end through cli.main on a synthetic repository, and of its
snapshot and zone-list formats.

Every run happens in temporary folders, with git's global and system configuration switched off.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from muster import __version__, cli
from muster.config import Config, ConfigError, load
from muster.policy import (
    PolicyError,
    check_policy,
    outputs,
    policy_hash,
    policy_sha256,
    protected,
    read_snapshot,
    report_invalid,
    sources_sha256,
)
from muster.report import FatalError, Report, ReportError

BASE = "src/main/java/org/example/app"
TESTS = "src/test/java/org/example/app"
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
"""
MUTATION = """
[mutation]
targets = "policy/mutation.txt"
own_test = "{stem}Test{ext}"
own_test_forbidden = ["*IntegrationTest*"]
"""
CRITICAL = b"# Zone 1: money.\nbilling/**\n# Zone 2: access.\nauth/**\n"
STANDARD = b"codec/*\nqueue/*\n"
EXEMPTIONS = b"# path | reason | valid-until\nauth/Token.java | rename only | 2026-05-30\n"
SOURCES = [
    f"{BASE}/auth/Token.java",
    f"{BASE}/billing/internal/ledger/Engine.java",
    f"{BASE}/codec/KeyParser.java",
    f"{BASE}/queue/Dispatcher.java",
]
ZONES = (
    f"{BASE}/auth/Token.java\tZ2\t2026-05-30\n"
    f"{BASE}/billing/internal/ledger/Engine.java\tZ1\t\n"
    f"{BASE}/codec/KeyParser.java\tstandard\t\n"
    f"{BASE}/queue/Dispatcher.java\tstandard\t\n"
)
VERDICT = "policy: PASS (4 source files: 2 critical in 2 zones, 2 standard; 1 exemption)\n"
UNCLASSIFIED = "unclassified package 'report' - add it to policy/critical.txt or policy/standard.txt"
TABLES = {
    "source": {
        "roots": ["src/main/java"],
        "tests": ["src/test/java"],
        "include": ["**/*.java"],
        "exclude": ["**/package-info.java"],
    },
    "zones": {
        "base": BASE,
        "critical": "policy/critical.txt",
        "standard": "policy/standard.txt",
        "exemptions": "policy/exemptions.txt",
    },
}


class PolicyCase(unittest.TestCase):
    """A repository with a valid policy, a separate working directory and a fixed today, 2026-05-19."""

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        self.enterContext(mock.patch("muster.policy.utc_today", return_value=date(2026, 5, 19)))
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
        self.write("muster.toml", PROFILE)
        self.write("policy/critical.txt", CRITICAL)
        self.write("policy/standard.txt", STANDARD)
        self.write("policy/exemptions.txt", EXEMPTIONS)
        for path in SOURCES:
            self.write(path, "class X {}\n")
        self.write(f"{BASE}/package-info.java", "package org.example.app;\n")
        self.write(f"{TESTS}/codec/KeyParserTest.java", "class X {}\n")
        self.write(f"{TESTS}/support/BaseIT.java", "class X {}\n")
        self.snapshot = self.repo / "build" / "muster" / "snapshot.json"

    def write(self, path: str, content) -> Path:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return file

    def policy(self, *options: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["--repo", str(self.repo), "policy", *options]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def envelope(self, *options: str) -> tuple[int, dict]:
        code, out, _ = self.policy("--format", "json", *options)
        return code, json.loads(out)

    def read_json(self, path: Path) -> dict:
        raw = path.read_bytes()
        self.assertNotIn(b"\r", raw)
        return json.loads(raw.decode("ascii"))

    def unclassify(self) -> None:
        self.write(f"{BASE}/report/Summary.java", "class X {}\n")

    def link(self, target: Path, link: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symbolic link here")


class ValidPolicyTest(PolicyCase):
    def test_passes(self):
        self.assertEqual(self.policy(), (0, VERDICT, ""))

    def test_envelope_section(self):
        code, env = self.envelope()
        self.assertEqual((code, env["command"], env["verdict"], env["error"]), (0, "policy", "pass", None))
        section = env["sections"][0]
        self.assertEqual((section["name"], section["verdict"], section["violations"]), ("policy", "pass", []))
        self.assertEqual(
            section["data"],
            {
                "sources": 4,
                "critical": 2,
                "standard": 2,
                "zones": {"Z1": 1, "Z2": 1},
                "exemptions": [{"path": f"{BASE}/auth/Token.java", "until": "2026-05-30"}],
                "mutation_targets": None,
            },
        )
        self.assertEqual(section["verdict_text"], VERDICT.strip())

    def test_markdown_table(self):
        self.assertEqual(self.policy("--summary-file", "summary.md")[0], 0)
        summary = (self.work / "summary.md").read_text(encoding="utf-8")
        self.assertTrue(summary.startswith("## Policy\n"), summary)
        for row in ("| Z1 | 1 |", "| Z2 | 1 |", "| standard | 2 |", f"| `{BASE}/auth/Token.java` | 2026-05-30 |"):
            self.assertIn(row, summary)

    def test_without_exemptions(self):
        self.write("muster.toml", PROFILE.replace('exemptions = "policy/exemptions.txt"\n', ""))
        (self.repo / "policy" / "exemptions.txt").unlink()
        code, out, _ = self.policy()
        self.assertEqual((code, out), (0, VERDICT.replace("1 exemption", "0 exemptions")))

    def test_today_cannot_be_given(self):
        self.write("policy/exemptions.txt", b"auth/Token.java | rename only | 2026-05-18\n")
        code, _, err = self.policy("--today", "2026-05-17")
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --today 2026-05-17", err)

    def test_root_folder_in_another_case_does_not_exist(self):
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/Java"]'))
        code, _, err = self.policy()
        self.assertEqual(code, 2)
        self.assertIn("muster.toml: source.roots[0]: folder src/main/Java does not exist", err)


class InvalidPolicyTest(PolicyCase):
    def test_unclassified_folder_is_named(self):
        self.unclassify()
        code, out, err = self.policy()
        self.assertEqual(code, 2)
        self.assertEqual(
            out,
            f"::error title=Invalid policy::{UNCLASSIFIED}\n"
            "policy: INVALID (1 violation)\n"
            "::error title=muster policy::the policy is invalid: 1 violation\n",
        )
        self.assertIn("muster policy: error: the policy is invalid: 1 violation", err)

    def test_line_with_more_than_1024_alternatives(self):
        line = "{ab,cd}" * 30 + "/**"
        self.write("policy/critical.txt", CRITICAL + line.encode("ascii") + b"\n")
        code, env = self.envelope()
        self.assertEqual((code, env["error"]["kind"]), (2, "policy"))
        too_many = f"policy/critical.txt: more than 1024 alternatives in '{line}'"
        self.assertEqual(env["sections"][0]["violations"], [too_many])

    def test_exit_two_with_and_without_strict(self):
        self.unclassify()
        for options in ([], ["--strict"], ["--strict-only", "coverage"], ["--fail-on-error"]):
            with self.subTest(options=options):
                self.assertEqual(self.policy(*options)[0], 2)

    def test_every_violation_is_reported(self):
        self.unclassify()
        self.write("policy/critical.txt", CRITICAL + b"gone/**\n")
        dead = "policy/critical.txt: 'gone/**' matches no source file"
        code, out, _ = self.policy("--json-out", "out.json", "--summary-file", "summary.md")
        self.assertEqual(code, 2)
        self.assertIn(f"::error title=Invalid policy::{dead}\n::error title=Invalid policy::{UNCLASSIFIED}\n", out)
        env = self.read_json(self.work / "out.json")
        self.assertEqual(env["error"], {"kind": "policy", "message": "the policy is invalid: 2 violations"})
        self.assertEqual((env["verdict"], env["exit_code"]), ("error", 2))
        section = env["sections"][0]
        self.assertEqual((section["verdict"], section["violations"]), ("error", [dead, UNCLASSIFIED]))
        summary = (self.work / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"- `{dead}`", summary)
        self.assertIn(f"- `{UNCLASSIFIED}`", summary)

    def test_envelope_replaces_an_existing_file(self):
        self.unclassify()
        (self.work / "out.json").write_bytes(b"stale\n")
        self.assertEqual(self.policy("--json-out", "out.json")[0], 2)
        env = self.read_json(self.work / "out.json")
        self.assertEqual((env["exit_code"], env["error"]["kind"]), (2, "policy"))

    def test_expired_exemption(self):
        self.write("policy/exemptions.txt", b"auth/Token.java | rename only | 2026-05-18\n")
        code, out, _ = self.policy()
        self.assertEqual(code, 2)
        self.assertIn("policy/exemptions.txt: 'auth/Token.java' expired on 2026-05-18", out)

    def test_missing_policy_file(self):
        (self.repo / "policy" / "standard.txt").unlink()
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"], {"kind": "policy", "message": "policy/standard.txt: not found (zones.standard)"})

    def test_policy_file_in_another_case(self):
        self.write("muster.toml", PROFILE.replace("policy/standard.txt", "policy/Standard.txt"))
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"]["message"], "policy/Standard.txt: not found (zones.standard)")

    def test_policy_file_not_utf8(self):
        self.write("policy/critical.txt", b"# Zone 1: money.\nbilling/\xff**\n")
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"], {"kind": "policy", "message": "policy/critical.txt: not valid UTF-8 (byte 25)"})

    def test_crlf_policy_files(self):
        for name, content in (("critical", CRITICAL), ("standard", STANDARD), ("exemptions", EXEMPTIONS)):
            self.write(f"policy/{name}.txt", content.replace(b"\n", b"\r\n"))
        self.assertEqual(self.policy(), (0, VERDICT, ""))


class ProfileTest(PolicyCase):
    def test_required_keys(self):
        self.write("muster.toml", PROFILE.replace('critical = "policy/critical.txt"\n', ""))
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"], {"kind": "config", "message": "muster.toml: missing key zones.critical"})

    def test_base_option_does_not_exist(self):
        code, _, err = self.policy("--base", "origin/main")
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --base origin/main", err)

    def test_root_folder_that_does_not_exist(self):
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "lib/src"]'))
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"]["message"], "muster.toml: source.roots[1]: folder lib/src does not exist")

    def test_empty_source_set(self):
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]', 'include = ["**/*.py"]'))
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(
            env["error"],
            {"kind": "config", "message": "muster.toml: no file under source.roots matches source.include"},
        )

    def test_bad_glob(self):
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]', 'include = ["**.java"]'))
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertTrue(env["error"]["message"].startswith('muster.toml: source.include[0]: bad glob "**.java"'))

    def test_glob_with_more_than_1024_alternatives(self):
        glob = "{ab,cd}" * 30 + "/*.java"
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]', f'include = ["{glob}"]'))
        code, env = self.envelope()
        self.assertEqual((code, env["error"]["kind"]), (2, "config"))
        message = f'muster.toml: source.include[0]: bad glob "{glob}": more than 1024 alternatives'
        self.assertEqual(env["error"]["message"], message)

    def suite(self, file: str) -> None:
        entry = f'\n[[coverage.full_run_suites]]\nname = "org.example.app.FullRunSuite"\nfile = "{file}"\n'
        self.write("muster.toml", PROFILE + entry)

    def test_full_run_suite_file_exists(self):
        self.write(f"{TESTS}/FullRunSuite.java", "class X {}\n")
        self.suite(f"{TESTS}/FullRunSuite.java")
        self.assertEqual(self.policy()[0], 0)

    def test_full_run_suite_file_missing(self):
        self.suite(f"{TESTS}/FullRunSuite.java")
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(
            env["error"]["message"],
            f"muster.toml: coverage.full_run_suites[0].file: {TESTS}/FullRunSuite.java does not exist"
            " (suite renamed or deleted)",
        )

    def test_full_run_suite_file_in_another_case(self):
        self.write(f"{TESTS}/FullRunSuite.java", "class X {}\n")
        self.suite(f"{TESTS}/FullrunSuite.java")
        self.assertEqual(self.policy()[0], 2)

    def test_integer_that_toml_cannot_hold(self):
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)  # the default, which the message in hex relies on
        huge = "0x" + "f" * 5000
        cases = [
            (f"exemption_max_days = {huge}", "zones.exemption_max_days: must be a 64-bit integer"),
            (f"[coverage.floor]\nline = {huge}", "coverage.floor.line: must be 1 or less, got 0x"),
        ]
        for table, message in cases:
            with self.subTest(message=message):
                self.write("muster.toml", PROFILE.replace("[policy]", f"{table}\n\n[policy]"))
                code, env = self.envelope("--snapshot")
                self.assertEqual((code, env["error"]["kind"]), (2, "config"))
                self.assertEqual(env["error"]["message"][: len(message) + 13], f"muster.toml: {message}")


class ZoneListOutputTest(PolicyCase):
    def test_rows(self):
        self.assertEqual(self.policy("--zones-out", "zones.tsv")[0], 0)
        self.assertEqual((self.work / "zones.tsv").read_bytes(), ZONES.encode("utf-8"))

    def test_parent_folders_created(self):
        self.assertEqual(self.policy("--zones-out", "out/zones/list.tsv")[0], 0)
        self.assertTrue((self.work / "out" / "zones" / "list.tsv").is_file())

    def test_absent_after_an_invalid_policy(self):
        self.unclassify()
        self.assertEqual(self.policy("--zones-out", "zones.tsv")[0], 2)
        self.assertFalse((self.work / "zones.tsv").exists())

    def test_removed_when_it_existed_before(self):
        (self.work / "zones.tsv").write_bytes(b"old\n")
        self.unclassify()
        self.assertEqual(self.policy("--zones-out", "zones.tsv")[0], 2)
        self.assertFalse((self.work / "zones.tsv").exists())

    def test_removed_on_a_config_error_after_the_profile_is_read(self):
        (self.work / "zones.tsv").write_bytes(b"old\n")
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]', 'include = ["**/*.py"]'))
        self.assertEqual(self.policy("--zones-out", "zones.tsv")[0], 2)
        self.assertFalse((self.work / "zones.tsv").exists())

    def test_unwritable(self):
        (self.work / "taken").mkdir()
        code, _, err = self.policy("--zones-out", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write --zones-out taken", err)
        self.assertTrue((self.work / "taken").is_dir())

    def test_removed_when_stdout_cannot_take_the_envelope(self):
        class ClosedPipe(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        argv = ["--repo", str(self.repo), "policy", "--format", "json", "--json-out", "out.json"]
        with contextlib.redirect_stdout(ClosedPipe()), contextlib.redirect_stderr(err):
            code = cli.main([*argv, "--zones-out", "zones.tsv", "--snapshot"])
        self.assertEqual(code, 3)
        document = self.read_json(self.work / "out.json")
        self.assertEqual((document["exit_code"], document["error"]["kind"]), (3, "internal"))
        self.assertEqual(document["error"]["message"], "cannot write stdout: Broken pipe")
        self.assertIn("muster policy: error: cannot write stdout: Broken pipe", err.getvalue())
        self.assertFalse((self.work / "zones.tsv").exists())
        self.assertFalse(self.snapshot.exists())

    def test_removed_when_a_later_output_fails(self):
        (self.work / "taken").mkdir()
        code, _, err = self.policy("--zones-out", "zones.tsv", "--summary-file", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write the summary taken", err)
        self.assertFalse((self.work / "zones.tsv").exists())

    def test_empty_name_is_refused_before_anything_is_removed(self):
        snapshot = self.work / "snapshot.json"
        snapshot.write_bytes(b"old\n")
        for options in (["--zones-out", ""], ["--zones-out=", "--snapshot", "snapshot.json"]):
            with self.subTest(options=options):
                code, _, err = self.policy(*options)
                self.assertEqual(code, 2)
                self.assertIn("muster policy: error: --zones-out: the file name is empty", err)
                self.assertEqual(snapshot.read_bytes(), b"old\n")


class SnapshotTest(PolicyCase):
    def set_created(self, value: int) -> bytes:
        document = self.read_json(self.snapshot)
        document["created_epoch_ms"] = value
        self.snapshot.write_bytes((json.dumps(document, indent=2) + "\n").encode("ascii"))
        return self.snapshot.read_bytes()

    def test_written(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        document = self.read_json(self.snapshot)
        self.assertEqual(
            list(document), ["schema", "tool_version", "created_epoch_ms", "sources", "sources_sha256", "policy_sha256"]
        )
        self.assertEqual((document["schema"], document["tool_version"], document["sources"]), (1, __version__, SOURCES))
        expected_sources = hashlib.sha256("".join(path + "\n" for path in SOURCES).encode("utf-8")).hexdigest()
        self.assertEqual(document["sources_sha256"], expected_sources)
        tables = {
            "source": {
                "roots": ["src/main/java"],
                "tests": ["src/test/java"],
                "include": ["**/*.java"],
                "exclude": ["**/package-info.java"],
            },
            "zones": {
                "base": BASE,
                "critical": "policy/critical.txt",
                "standard": "policy/standard.txt",
                "exemptions": "policy/exemptions.txt",
            },
        }
        canonical = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("ascii")
        digests = b"".join(hashlib.sha256(content).digest() for content in (CRITICAL, STANDARD, EXEMPTIONS))
        self.assertEqual(document["policy_sha256"], hashlib.sha256(digests + canonical).hexdigest())
        self.assertIsInstance(document["created_epoch_ms"], int)

    def test_file_option_is_relative_to_the_working_directory(self):
        self.assertEqual(self.policy("--snapshot", "snap/s.json")[0], 0)
        self.assertTrue((self.work / "snap" / "s.json").is_file())
        self.assertFalse(self.snapshot.exists())

    def test_second_run_without_change_leaves_the_file(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = self.set_created(1)
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_new_source_file_changes_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.set_created(1)
        self.write(f"{BASE}/codec/KeyWriter.java", "class X {}\n")
        self.assertEqual(self.policy("--snapshot")[0], 0)
        document = self.read_json(self.snapshot)
        self.assertNotEqual(document["created_epoch_ms"], 1)
        self.assertIn(f"{BASE}/codec/KeyWriter.java", document["sources"])

    def test_change_of_only_a_policy_file_changes_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = self.read_json(self.snapshot)
        self.set_created(1)
        self.write("policy/standard.txt", STANDARD + b"# a new comment\n")
        self.assertEqual(self.policy("--snapshot")[0], 0)
        after = self.read_json(self.snapshot)
        self.assertNotEqual(after["created_epoch_ms"], 1)
        self.assertEqual(after["sources_sha256"], before["sources_sha256"])
        self.assertNotEqual(after["policy_sha256"], before["policy_sha256"])

    def test_without_exemptions_the_hash_takes_empty_bytes(self):
        self.write("muster.toml", PROFILE.replace('exemptions = "policy/exemptions.txt"\n', ""))
        (self.repo / "policy" / "exemptions.txt").unlink()
        self.assertEqual(self.policy("--snapshot")[0], 0)
        tables = {"source": TABLES["source"], "zones": {**TABLES["zones"]}}
        del tables["zones"]["exemptions"]
        expected = policy_sha256(CRITICAL, STANDARD, b"", tables)
        self.assertEqual(self.read_json(self.snapshot)["policy_sha256"], expected)

    def test_prior_snapshot_with_equal_hashes_but_a_broken_shape_is_written_anew(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = self.read_json(self.snapshot)
        self.snapshot.write_bytes(json.dumps({**before, "created_epoch_ms": "1"}).encode("ascii"))
        self.assertEqual(self.policy("--snapshot")[0], 0)
        after = self.read_json(self.snapshot)
        self.assertIsInstance(after["created_epoch_ms"], int)
        self.assertEqual(
            (after["sources_sha256"], after["policy_sha256"]), (before["sources_sha256"], before["policy_sha256"])
        )

    def test_prior_snapshot_with_an_unknown_key_is_left(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        document = {**self.read_json(self.snapshot), "created_epoch_ms": 1, "later": {"added": True}}
        self.snapshot.write_bytes((json.dumps(document, indent=2) + "\n").encode("ascii"))
        before = self.snapshot.read_bytes()
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_prior_snapshot_nested_too_deeply_is_written_anew(self):
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_bytes(b"[" * 100_000 + b"]" * 100_000)
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertEqual(self.read_json(self.snapshot)["sources"], SOURCES)

    def test_change_of_the_zones_table_changes_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = self.read_json(self.snapshot)
        self.write("muster.toml", PROFILE.replace("[policy]", "exemption_max_days = 20\n\n[policy]"))
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertNotEqual(self.read_json(self.snapshot)["policy_sha256"], before["policy_sha256"])

    def test_invalid_policy_removes_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.unclassify()
        self.assertEqual(self.policy("--snapshot")[0], 2)
        self.assertFalse(self.snapshot.exists())

    def test_missing_policy_file_removes_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        (self.repo / "policy" / "exemptions.txt").unlink()
        self.assertEqual(self.policy("--snapshot")[0], 2)
        self.assertFalse(self.snapshot.exists())

    def test_invalid_policy_without_the_option_leaves_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.unclassify()
        self.assertEqual(self.policy()[0], 2)
        self.assertTrue(self.snapshot.exists())

    def test_unreadable_profile_leaves_it(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.write("muster.toml", "[source\n")
        code, env = self.envelope("--snapshot")
        self.assertEqual((code, env["error"]["kind"]), (2, "config"))
        self.assertTrue(self.snapshot.exists())

    def test_option_without_file_needs_the_profile_key(self):
        self.write("muster.toml", PROFILE.replace('[policy]\nsnapshot = "build/muster/snapshot.json"\n', ""))
        code, env = self.envelope("--snapshot")
        self.assertEqual(code, 2)
        self.assertEqual(
            env["error"],
            {"kind": "config", "message": "muster.toml: --snapshot without FILE needs the key policy.snapshot"},
        )

    def test_unwritable(self):
        self.snapshot.mkdir(parents=True)
        code, _, err = self.policy("--snapshot")
        self.assertEqual(code, 2)
        self.assertIn("cannot write the snapshot", err)

    def written(self) -> None:
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertTrue(self.snapshot.exists())

    def test_empty_source_set_removes_it(self):
        self.written()
        self.write("muster.toml", PROFILE.replace('include = ["**/*.java"]', 'include = ["**/*.py"]'))
        self.assertEqual(self.policy("--snapshot")[0], 2)
        self.assertFalse(self.snapshot.exists())

    def test_missing_source_root_removes_it(self):
        self.written()
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/java", "lib/src"]'))
        code, env = self.envelope("--snapshot")
        self.assertEqual(env["error"]["message"], "muster.toml: source.roots[1]: folder lib/src does not exist")
        self.assertFalse(self.snapshot.exists())

    def test_output_that_cannot_be_cleared_removes_it(self):
        self.written()
        (self.work / "taken").mkdir()
        code, _, err = self.policy("--snapshot", "--zones-out", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write --zones-out taken", err)
        self.assertFalse(self.snapshot.exists())

    def test_summary_that_cannot_be_written_removes_it(self):
        self.written()
        (self.work / "taken").mkdir()
        code, _, err = self.policy("--snapshot", "--summary-file", "taken")
        self.assertEqual(code, 2)
        self.assertIn("cannot write the summary taken", err)
        self.assertFalse(self.snapshot.exists())

    def test_json_out_that_cannot_be_written_at_the_end_removes_it(self):
        self.written()
        with mock.patch.object(cli, "write_json", side_effect=PermissionError(13, "Permission denied")):
            code, _, err = self.policy("--snapshot", "--json-out", "out.json")
        self.assertEqual(code, 2)
        self.assertIn("cannot write --json-out out.json", err)
        self.assertFalse(self.snapshot.exists())

    def test_bug_removes_it(self):
        self.written()
        with mock.patch("muster.policy.classify", side_effect=RuntimeError("boom")):
            self.assertEqual(self.policy("--snapshot")[0], 3)
        self.assertFalse(self.snapshot.exists())

    def test_profile_key_without_the_option_is_not_touched_on_failure(self):
        self.written()
        (self.work / "taken").mkdir()
        self.assertEqual(self.policy("--zones-out", "taken")[0], 2)
        self.assertTrue(self.snapshot.exists())

    def test_file_named_like_the_profile_default_is_a_file(self):
        cfg = Config(self.repo, self.repo / "muster.toml", {"policy": {"snapshot": "build/muster/snapshot.json"}})
        args = argparse.Namespace(zones_out=None, snapshot="<policy.snapshot>")
        expected = [("the snapshot <policy.snapshot>", Path("<policy.snapshot>"), False, True)]
        self.assertEqual(outputs(args, cfg), (expected, None))
        args = cli.build_parser().parse_args(["policy", "--snapshot"])
        self.assertEqual(outputs(args, cfg)[0][0][1], self.repo / "build" / "muster" / "snapshot.json")


class OutputClashTest(PolicyCase):
    def assertUntouched(self, files: dict[Path, bytes]) -> None:
        for path, content in files.items():
            self.assertEqual(path.read_bytes(), content, path)

    def test_zones_out_is_the_profile(self):
        stale = self.work / "out.json"
        stale.write_bytes(b"stale\n")
        profile = self.repo / "muster.toml"
        before = {profile: profile.read_bytes(), stale: b"stale\n"}
        code, out, err = self.policy("--zones-out", str(profile), "--json-out", "out.json")
        self.assertEqual(code, 2)
        self.assertIn(f"--zones-out {profile} is the config file", err)
        self.assertTrue(out.startswith("::error title=muster policy::"), out)
        self.assertUntouched(before)

    def test_zones_out_is_the_snapshot(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = {self.snapshot: self.snapshot.read_bytes()}
        code, _, err = self.policy("--snapshot", "--zones-out", str(self.snapshot))
        self.assertEqual(code, 2)
        self.assertIn("is the same file as --zones-out", err)
        self.assertUntouched(before)

    def test_json_out_is_a_policy_file(self):
        critical = self.repo / "policy" / "critical.txt"
        code, _, err = self.policy("--json-out", str(critical))
        self.assertEqual(code, 2)
        self.assertIn(f"--json-out {critical} is the file of zones.critical", err)
        self.assertUntouched({critical: CRITICAL})

    def test_json_out_is_the_zones_out(self):
        (self.work / "same.txt").write_bytes(b"keep\n")
        code, _, err = self.policy("--json-out", "same.txt", "--zones-out", "same.txt")
        self.assertEqual(code, 2)
        self.assertIn("--zones-out same.txt is the same file as --json-out same.txt", err)
        self.assertUntouched({self.work / "same.txt": b"keep\n"})

    def test_snapshot_is_the_exemptions_file(self):
        exemptions = self.repo / "policy" / "exemptions.txt"
        self.assertEqual(self.policy("--snapshot", str(exemptions))[0], 2)
        self.assertUntouched({exemptions: EXEMPTIONS})

    def test_json_out_is_a_policy_file_when_a_required_key_is_missing(self):
        self.write("muster.toml", PROFILE.replace('standard = "policy/standard.txt"\n', ""))
        critical = self.repo / "policy" / "critical.txt"
        code, _, err = self.policy("--json-out", str(critical))
        self.assertEqual(code, 2)
        self.assertIn(f"--json-out {critical} is the file of zones.critical", err)
        self.assertUntouched({critical: CRITICAL})

    def test_json_out_is_a_policy_file_when_the_snapshot_cannot_be_named(self):
        critical = self.repo / "policy" / "critical.txt"
        self.write("muster.toml", PROFILE.replace('[policy]\nsnapshot = "build/muster/snapshot.json"\n', ""))
        for options in (["--snapshot"], ["--snapshot", ""]):
            with self.subTest(options=options):
                code, _, err = self.policy(*options, "--json-out", str(critical))
                self.assertEqual(code, 2)
                self.assertIn("is the file of zones.critical", err)
                self.assertUntouched({critical: CRITICAL})

    def test_json_out_is_a_policy_file_of_an_invalid_profile(self):
        self.write("muster.toml", PROFILE.replace("schema = 1", "schema = 2"))
        critical = self.repo / "policy" / "critical.txt"
        code, _, err = self.policy("--json-out", str(critical))
        self.assertEqual(code, 2)
        self.assertIn(f"--json-out {critical} is the file of zones.critical", err)
        self.assertUntouched({critical: CRITICAL})

    def test_json_out_is_a_policy_file_when_the_tree_cannot_be_walked(self):
        critical = self.repo / "policy" / "critical.txt"
        with mock.patch("muster.policy.source_sets", side_effect=RuntimeError("boom")):
            code, _, err = self.policy("--json-out", str(critical))
        self.assertEqual(code, 2)
        self.assertIn(f"--json-out {critical} is the file of zones.critical", err)
        self.assertUntouched({critical: CRITICAL})

    def test_protected_files_never_raise_on_a_denied_folder(self):
        denied, scandir = str(self.repo / BASE / "codec"), os.scandir

        def listing(path="."):
            if os.fspath(path) == denied:
                raise PermissionError(13, "Permission denied", path)
            return scandir(path)

        args = argparse.Namespace()
        with mock.patch("os.scandir", side_effect=listing):
            files = protected(args, load(self.repo))
        self.assertEqual(str(args.source_sets.error), f"source.roots: cannot read {denied}: Permission denied")
        self.assertIn((f"the source file {SOURCES[3]}", self.repo / SOURCES[3]), files)
        self.assertNotIn(f"the source file {SOURCES[2]}", [label for label, _ in files])

    def test_outputs_are_checked_when_the_snapshot_cannot_be_named(self):
        self.write("muster.toml", PROFILE.replace('[policy]\nsnapshot = "build/muster/snapshot.json"\n', ""))
        same = self.work / "same.txt"
        same.write_bytes(b"keep\n")
        code, _, err = self.policy("--zones-out", "same.txt", "--json-out", "same.txt", "--snapshot")
        self.assertEqual(code, 2)
        self.assertIn("--zones-out same.txt is the same file as --json-out same.txt", err)
        self.assertUntouched({same: b"keep\n"})

    def test_output_is_the_file_a_full_run_suite_links_to(self):
        target = self.write("outside/FullRunSuite.java", "class Suite {}\n")
        self.link(target, self.repo / "suites" / "Alias.java")
        entry = '\n[[coverage.full_run_suites]]\nname = "FullRunSuite"\nfile = "suites/Alias.java"\n'
        self.write("muster.toml", PROFILE + entry)
        code, _, err = self.policy("--zones-out", str(target))
        self.assertEqual(code, 2)
        self.assertIn("is the file of coverage.full_run_suites[0].file", err)
        self.assertUntouched({target: b"class Suite {}\n"})

    def test_output_is_a_source_file_that_is_a_link(self):
        target = self.write("outside/Linked.java", "class X {}\n")
        link = self.repo / BASE / "codec" / "Linked.java"
        self.link(target, link)
        code, _, err = self.policy("--zones-out", str(link))
        self.assertEqual(code, 2)
        self.assertIn(f"is the source file {BASE}/codec/Linked.java", err)
        self.assertTrue(link.is_symlink())
        self.assertUntouched({target: b"class X {}\n"})

    def test_output_is_the_file_a_source_link_leads_to(self):
        target = self.write("outside/Linked.java", "class X {}\n")
        self.link(target, self.repo / BASE / "codec" / "Linked.java")
        alias = self.work / "alias.tsv"
        self.link(target, alias)
        for output in (target, alias):
            with self.subTest(output=output.name):
                code, _, err = self.policy("--zones-out", str(output))
                self.assertEqual(code, 2)
                self.assertIn(f"is the source file {BASE}/codec/Linked.java", err)
                self.assertTrue(alias.is_symlink())
                self.assertUntouched({target: b"class X {}\n"})

    def test_output_is_the_file_a_test_link_leads_to(self):
        target = self.write("outside/LinkedTest.java", "class X {}\n")
        self.link(target, self.repo / TESTS / "codec" / "LinkedTest.java")
        code, _, err = self.policy("--zones-out", str(target))
        self.assertEqual(code, 2)
        self.assertIn(f"is the test file {TESTS}/codec/LinkedTest.java", err)
        self.assertUntouched({target: b"class X {}\n"})

    def test_summary_file_is_a_policy_file(self):
        critical = self.repo / "policy" / "critical.txt"
        code, _, err = self.policy("--summary-file", str(critical))
        self.assertEqual(code, 2)
        self.assertIn(f"--summary-file {critical} is the file of zones.critical", err)
        self.assertUntouched({critical: CRITICAL})

    def test_github_summary_is_the_profile(self):
        profile = self.repo / "muster.toml"
        before = profile.read_bytes()
        os.environ["GITHUB_STEP_SUMMARY"] = str(profile)
        code, _, err = self.policy("--github")
        self.assertEqual(code, 2)
        self.assertIn("is the config file", err)
        self.assertUntouched({profile: before})

    def test_nothing_is_written_after_a_clash(self):
        critical = self.repo / "policy" / "critical.txt"
        code, out, _ = self.policy("--json-out", str(critical), "--summary-file", "summary.md")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("::error title=muster policy::"), out)
        self.assertFalse((self.work / "summary.md").exists())
        self.assertUntouched({critical: CRITICAL})

    def test_summary_file_is_the_json_out(self):
        code, _, err = self.policy("--json-out", "same.md", "--summary-file", "same.md")
        self.assertEqual(code, 2)
        self.assertIn("--summary-file same.md is the same file as --json-out same.md", err)
        self.assertFalse((self.work / "same.md").exists())

    def test_output_dir_summary_is_an_output(self):
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        target = self.repo / "build" / "muster" / "policy.md"
        code, _, err = self.policy("--zones-out", str(target))
        self.assertEqual(code, 2)
        self.assertIn(f"--zones-out {target} is the same file as the summary {target}", err)
        self.assertFalse(target.exists())

    def test_output_dir_summary_is_a_policy_file(self):
        profile = PROFILE.replace("policy/standard.txt", "policy/policy.md")
        self.write("muster.toml", profile + '\n[output]\ndir = "policy"\n')
        self.write("policy/policy.md", STANDARD)
        code, _, err = self.policy()
        self.assertEqual(code, 2)
        self.assertIn("the summary", err)
        self.assertIn("is the file of zones.standard", err)
        self.assertUntouched({self.repo / "policy" / "policy.md": STANDARD})

    def test_output_is_a_source_file(self):
        source = self.repo / SOURCES[2]
        for option in ("--zones-out", "--json-out", "--summary-file", "--snapshot"):
            with self.subTest(option=option):
                code, _, err = self.policy(option, str(source))
                self.assertEqual(code, 2)
                label = "the snapshot" if option == "--snapshot" else option
                self.assertIn(f"{label} {source} is the source file {SOURCES[2]}", err)
                self.assertUntouched({source: b"class X {}\n"})

    def test_output_is_a_test_file_or_a_full_run_suite(self):
        test_file = self.repo / TESTS / "codec" / "KeyParserTest.java"
        suite = self.write("suites/FullRunSuite.java", "class Suite {}\n")  # outside the test roots
        entry = '\n[[coverage.full_run_suites]]\nname = "FullRunSuite"\nfile = "suites/FullRunSuite.java"\n'
        self.write("muster.toml", PROFILE + entry)
        code, _, err = self.policy("--zones-out", str(test_file))
        self.assertEqual(code, 2)
        self.assertIn(f"is the test file {TESTS}/codec/KeyParserTest.java", err)
        code, _, err = self.policy("--snapshot", str(suite))
        self.assertEqual(code, 2)
        self.assertIn("is the file of coverage.full_run_suites[0].file", err)
        self.assertUntouched({test_file: b"class X {}\n", suite: b"class Suite {}\n"})

    def test_source_file_is_protected_when_another_root_is_missing(self):
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]', 'roots = ["lib/src", "src/main/java"]'))
        source = self.repo / SOURCES[2]
        code, _, err = self.policy("--json-out", str(source))
        self.assertEqual(code, 2)
        self.assertIn(f"is the source file {SOURCES[2]}", err)
        self.assertUntouched({source: b"class X {}\n"})

    @unittest.skipUnless(os.path.normcase("A") == "a", "case-insensitive paths only on Windows")
    def test_other_case_is_the_same_file_on_windows(self):
        profile = self.repo / "MUSTER.TOML"
        self.assertEqual(self.policy("--zones-out", str(profile))[0], 2)
        self.assertTrue((self.repo / "muster.toml").read_bytes().startswith(b"schema = 1"))


class OutputDirSummaryTest(PolicyCase):
    """<[output].dir>/policy.md holds only what this run wrote."""

    def setUp(self):
        super().setUp()
        self.write("muster.toml", PROFILE + '\n[output]\ndir = "build/muster"\n')
        self.target = self.write("build/muster/policy.md", "stale report\n")

    def test_failure_line_replaces_an_old_report(self):
        profile = PROFILE.replace('include = ["**/*.java"]', 'include = ["**/*.py"]')
        self.write("muster.toml", profile + '\n[output]\ndir = "build/muster"\n')
        self.assertEqual(self.policy()[0], 2)
        line = "muster policy: error: muster.toml: no file under source.roots matches source.include\n\n"
        self.assertEqual(self.target.read_text(encoding="utf-8"), line)

    def test_failure_line_when_the_command_cannot_name_its_outputs(self):
        self.assertEqual(self.policy("--snapshot", "")[0], 2)
        self.assertEqual(self.target.read_bytes(), b"muster policy: error: --snapshot: the file name is empty\n\n")

    def test_invalid_policy_keeps_its_violations_and_the_failure_line(self):
        self.unclassify()
        self.assertEqual(self.policy()[0], 2)
        text = self.target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## Policy\n"), text)
        self.assertIn(f"- `{UNCLASSIFIED}`", text)
        self.assertTrue(text.endswith("muster policy: error: the policy is invalid: 1 violation\n\n"), text)
        self.assertNotIn("stale", text)

    def test_unreadable_profile_leaves_it(self):
        self.write("muster.toml", "[output\n")
        self.assertEqual(self.policy()[0], 2)
        self.assertEqual(self.target.read_bytes(), b"stale report\n")


@unittest.skipIf(sys.platform == "win32", "Windows file names are always Unicode")
class UndecodableNameTest(PolicyCase):
    def test_folder_name_that_is_not_utf8_reaches_the_summary(self):
        folder = self.repo / BASE / os.fsdecode(b"bad\xff")
        try:
            folder.mkdir()
            (folder / "Odd.java").write_bytes(b"class X {}\n")
        except (OSError, UnicodeError):
            self.skipTest("this file system takes only valid UTF-8 names")
        code, out, _ = self.policy("--summary-file", "summary.md", "--json-out", "out.json")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.read_json(self.work / "out.json")["error"]["kind"], "policy")
        summary = (self.work / "summary.md").read_bytes().decode("utf-8")
        self.assertIn("unclassified package 'bad\N{REPLACEMENT CHARACTER}'", summary)

    def test_classified_name_that_is_not_utf8_stops_the_outputs(self):
        try:
            self.write(f"{BASE}/codec/" + os.fsdecode(b"Bad\xff.java"), "class X {}\n")
        except (OSError, UnicodeError):
            self.skipTest("this file system takes only valid UTF-8 names")
        self.assertEqual(self.policy()[0], 0)
        code, _, err = self.policy("--zones-out", "zones.tsv", "--snapshot")
        self.assertEqual(code, 2)
        self.assertIn(f"muster policy: error: {BASE}/codec/Bad\\udcff.java: the file name is not valid UTF-8", err)
        self.assertFalse((self.work / "zones.tsv").exists())
        self.assertFalse(self.snapshot.exists())


class MutationTargetTest(PolicyCase):
    def setUp(self):
        super().setUp()
        self.write("muster.toml", PROFILE + MUTATION)

    def test_valid_allowlist(self):
        self.write("policy/mutation.txt", "# pure logic\ncodec/KeyParser.java\n")
        code, env = self.envelope()
        self.assertEqual(code, 0)
        self.assertEqual(env["sections"][0]["data"]["mutation_targets"], 1)

    def test_file_without_its_own_test(self):
        self.write("policy/mutation.txt", "queue/Dispatcher.java\n")
        code, out, _ = self.policy()
        self.assertEqual(code, 2)
        self.assertIn(
            "policy/mutation.txt: 'queue/Dispatcher.java' has no own unit test"
            " 'org/example/app/queue/DispatcherTest.java' in src/test/java",
            out,
        )

    def test_own_test_matching_a_forbidden_glob(self):
        self.write(f"{BASE}/codec/CodecIntegration.java", "class X {}\n")
        self.write(f"{TESTS}/codec/CodecIntegrationTest.java", "class X {}\n")
        self.write("policy/mutation.txt", "codec/CodecIntegration.java\n")
        code, out, _ = self.policy()
        self.assertEqual(code, 2)
        self.assertIn(
            "policy/mutation.txt: 'codec/CodecIntegration.java' has its own test named 'CodecIntegrationTest.java',"
            " which matches own_test_forbidden '*IntegrationTest*'",
            out,
        )

    def test_not_a_source_file(self):
        self.write("policy/mutation.txt", "codec/keyparser.java\n")
        code, out, _ = self.policy()
        self.assertEqual(code, 2)
        self.assertIn("policy/mutation.txt: 'codec/keyparser.java' is not a source file", out)

    def test_allowlist_without_entries(self):
        self.write("policy/mutation.txt", "# nothing yet\n")
        code, out, _ = self.policy()
        self.assertEqual(code, 2)
        self.assertIn("policy/mutation.txt: lists no file - it must list at least one file", out)

    def test_targets_need_the_own_test_key(self):
        self.write("muster.toml", PROFILE + MUTATION.replace('own_test = "{stem}Test{ext}"\n', ""))
        self.write("policy/mutation.txt", "codec/KeyParser.java\n")
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertEqual(env["error"], {"kind": "config", "message": "muster.toml: missing key mutation.own_test"})

    def test_bad_forbidden_glob(self):
        self.write("muster.toml", PROFILE + MUTATION.replace('"*IntegrationTest*"', '"{Integration"'))
        self.write("policy/mutation.txt", "codec/KeyParser.java\n")
        code, env = self.envelope()
        self.assertEqual(code, 2)
        self.assertTrue(env["error"]["message"].startswith("muster.toml: mutation.own_test_forbidden[0]: bad glob"))

    def test_change_of_only_the_allowlist_leaves_the_policy_hash(self):
        self.write("policy/mutation.txt", "codec/KeyParser.java\n")
        self.assertEqual(self.policy("--snapshot")[0], 0)
        before = self.read_json(self.snapshot)["policy_sha256"]
        self.write("policy/mutation.txt", "# pure logic\ncodec/KeyParser.java\n")
        self.assertEqual(self.policy("--snapshot")[0], 0)
        self.assertEqual(self.read_json(self.snapshot)["policy_sha256"], before)

    def test_missing_allowlist(self):
        code, env = self.envelope()
        self.assertEqual(code, 2)
        message = "policy/mutation.txt: not found (mutation.targets)"
        self.assertEqual(env["error"], {"kind": "policy", "message": message})


class CheckPolicyTest(PolicyCase):
    def check(self, today: date = date(2026, 5, 19)):
        return check_policy(argparse.Namespace(), load(self.repo), today)

    def test_valid_policy(self):
        checked = self.check()
        self.assertEqual((checked.sets.sources, checked.violations, checked.targets), (SOURCES, [], None))
        self.assertEqual(checked.classification.zones[f"{BASE}/auth/Token.java"], "Z2")
        expected = {"zones.critical": CRITICAL, "zones.standard": STANDARD, "zones.exemptions": EXEMPTIONS}
        self.assertEqual(checked.raw, expected)

    def test_violations_are_returned_not_reported(self):
        self.unclassify()
        self.assertEqual(self.check().violations, [UNCLASSIFIED])

    def test_today_is_given_by_the_caller(self):
        self.assertEqual(self.check(date(2026, 5, 30)).violations, [])
        expired = self.check(date(2026, 5, 31)).violations
        self.assertEqual(len(expired), 1)
        self.assertIn("'auth/Token.java' expired on 2026-05-30", expired[0])

    def test_profile_and_policy_file_errors_are_raised(self):
        (self.repo / "policy" / "standard.txt").unlink()
        with self.assertRaises(PolicyError):
            self.check()
        self.write("muster.toml", PROFILE.replace('roots = ["src/main/java"]', 'roots = ["src/main/gone"]'))
        with self.assertRaises(ConfigError):
            self.check()

    def test_policy_hash_is_the_one_in_the_snapshot(self):
        self.assertEqual(self.policy("--snapshot")[0], 0)
        cfg = load(self.repo)
        checked = check_policy(argparse.Namespace(), cfg, date(2026, 5, 19))
        self.assertEqual(policy_hash(cfg, checked), self.read_json(self.snapshot)["policy_sha256"])
        self.assertEqual(policy_hash(cfg, checked), policy_sha256(CRITICAL, STANDARD, EXEMPTIONS, TABLES))

    def test_invalid_policy_with_the_texts_of_the_caller(self):
        rep = Report("coverage")
        error = report_invalid(
            rep,
            ["first", "second"],
            heading="## Coverage",
            verdict="coverage: NOT EVALUATED (invalid policy: {count})",
            message="cannot evaluate: {count}",
            summary="**NOT EVALUATED**: invalid policy, {count}.",
        )
        self.assertEqual((str(error), error.kind), ("cannot evaluate: 2 violations", "policy"))
        self.assertEqual(rep.verdict, "coverage: NOT EVALUATED (invalid policy: 2 violations)")
        summary = "**NOT EVALUATED**: invalid policy, 2 violations."
        self.assertEqual(rep.md, ["## Coverage", "", summary, "", "- `first`", "- `second`"])
        self.assertEqual((rep.violation, rep.violations), (True, ["first", "second"]))
        self.assertEqual(
            rep.annotations, ["::error title=Invalid policy::first", "::error title=Invalid policy::second"]
        )


SNAPSHOT = {
    "schema": 1,
    "tool_version": "0.1.0",
    "created_epoch_ms": 1,
    "sources": [f"{BASE}/auth/Token.java"],
    "sources_sha256": "0123456789abcdef" * 4,
    "policy_sha256": "fedcba9876543210" * 4,
}


class ReadSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.file = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory()))) / "snapshot.json"

    def read(self, raw: bytes) -> dict:
        self.file.write_bytes(raw)
        return read_snapshot(self.file)

    def test_valid_snapshot_reads_back(self):
        self.assertEqual(self.read(json.dumps(SNAPSHOT).encode("ascii")), SNAPSHOT)
        later = {**SNAPSHOT, "later": [1, 2], "sources": []}
        self.assertEqual(self.read(json.dumps(later).encode("ascii")), later)

    def test_broken_snapshot_is_a_report_error(self):
        def text(**changes) -> bytes:
            return json.dumps({**SNAPSHOT, **changes}).encode("ascii")

        cases = [
            ("not JSON", b'{"schema": 1,', None),
            ("not UTF-8", text(tool_version="0.1.0").replace(b"0.1.0", b"0.1." + bytes([0xFF])), None),
            ("NaN", text(created_epoch_ms=float("nan")), None),
            ("Infinity", text(created_epoch_ms=float("inf")), None),
            ("-Infinity", text(created_epoch_ms=float("-inf")), None),
            ("top level a list", b"[]", None),
            ("top level a string", b'"snapshot"', None),
            ("nested too deeply", b"[" * 100_000 + b"]" * 100_000, None),
        ]
        for key in SNAPSHOT:
            missing = {name: value for name, value in SNAPSHOT.items() if name != key}
            cases.append((f"{key} missing", json.dumps(missing).encode("ascii"), key))
        wrong = [
            ("schema", "1"),
            ("schema", True),
            ("schema", 1.0),
            ("schema", 2),
            ("tool_version", 1),
            ("tool_version", None),
            ("created_epoch_ms", "1"),
            ("created_epoch_ms", True),
            ("created_epoch_ms", 1.5),
            ("created_epoch_ms", -1),
            ("sources", f"{BASE}/auth/Token.java"),
            ("sources", [f"{BASE}/auth/Token.java", 1]),
            ("sources", None),
            ("sources_sha256", "0123456789ABCDEF" * 4),
            ("sources_sha256", "0" * 63),
            ("sources_sha256", "0" * 65),
            ("sources_sha256", "g" * 64),
            ("policy_sha256", int("1" * 64)),
            ("policy_sha256", "0" * 64 + "\n"),
        ]
        for key, value in wrong:
            cases.append((f"{key} = {value!r}", text(**{key: value}), key))
        for name, raw, key in cases:
            with self.subTest(case=name):
                with self.assertRaises(ReportError) as caught:
                    self.read(raw)
                self.assertIn(str(self.file), str(caught.exception))
                if key is not None:
                    self.assertIn(key, str(caught.exception))

    def test_missing_file_is_left_to_the_caller(self):
        with self.assertRaises(FileNotFoundError):
            read_snapshot(self.file)

    def test_file_that_cannot_be_read_is_a_report_error(self):
        self.file.mkdir()
        with self.assertRaises(ReportError) as caught:
            read_snapshot(self.file)
        self.assertIn(str(self.file), str(caught.exception))


class HashTest(unittest.TestCase):
    def test_policy_hash_covers_the_digests_of_the_three_files_in_order(self):
        canonical = b'{"source":{"roots":["src"]},"zones":{"base":""}}'
        tables = {"zones": {"base": ""}, "source": {"roots": ["src"]}}
        digests = b"".join(hashlib.sha256(content).digest() for content in (b"a", b"b", b"c"))
        self.assertEqual(policy_sha256(b"a", b"b", b"c", tables), hashlib.sha256(digests + canonical).hexdigest())
        no_exemptions = b"".join(hashlib.sha256(content).digest() for content in (b"a", b"b", b""))
        self.assertEqual(policy_sha256(b"a", b"b", b"", tables), hashlib.sha256(no_exemptions + canonical).hexdigest())
        self.assertNotEqual(policy_sha256(b"a\r\n", b"", b"", tables), policy_sha256(b"a\n", b"", b"", tables))
        self.assertNotEqual(policy_sha256(b"a", b"b", b"", tables), policy_sha256(b"b", b"a", b"", tables))

    def test_line_moved_from_the_critical_to_the_standard_file_changes_it(self):
        tables = {"source": {"roots": ["src"]}, "zones": {"base": ""}}
        before = policy_sha256(b"# Zone 1: money.\nbilling/**\nauth/**\n", b"codec/*\n", b"", tables)
        after = policy_sha256(b"# Zone 1: money.\nbilling/**\n", b"auth/**\ncodec/*\n", b"", tables)
        self.assertNotEqual(before, after)

    def test_policy_hash_covers_the_tables(self):
        tables = {"source": {"roots": ["src"]}, "zones": {"base": ""}}
        other = {"source": {"roots": ["src"]}, "zones": {"base": "app"}}
        self.assertNotEqual(policy_sha256(b"", b"", b"", tables), policy_sha256(b"", b"", b"", other))

    def test_sources_hash(self):
        self.assertEqual(sources_sha256(["a.py", "src/b.py"]), hashlib.sha256(b"a.py\nsrc/b.py\n").hexdigest())
        self.assertEqual(sources_sha256([]), hashlib.sha256(b"").hexdigest())

    def test_name_that_utf8_cannot_encode(self):
        for surrogate in (0xDCFF, 0xD800):
            with self.subTest(surrogate=hex(surrogate)):
                with self.assertRaises(FatalError) as caught:
                    sources_sha256(["src/ok.py", "src/bad" + chr(surrogate) + ".py"])
                message = str(caught.exception)
                self.assertTrue(message.startswith(f"src/bad\\u{surrogate:04x}.py: the file name is not valid UTF-8"))
                message.encode("utf-8")  # no lone surrogate left


if __name__ == "__main__":
    unittest.main()
