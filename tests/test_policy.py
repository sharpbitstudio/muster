"""Tests of the policy command, end to end through cli.main on a synthetic repository, and of its
snapshot and zone-list formats.

Every run happens in temporary folders, with git's global and system configuration switched off.
"""

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster import __version__, cli
from muster.policy import policy_sha256, sources_sha256

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


class PolicyCase(unittest.TestCase):
    """A repository with a valid policy, a separate working directory and a fixed today, 2026-05-19."""

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
        argv = ["--repo", str(self.repo), "policy", "--today", "2026-05-19", *options]
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

    def test_today_must_be_a_date(self):
        code, _, err = self.policy("--today", "2026-02-30")
        self.assertEqual(code, 2)
        self.assertIn("expected a date YYYY-MM-DD", err)

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
        self.assertEqual(
            document["policy_sha256"], hashlib.sha256(CRITICAL + STANDARD + EXEMPTIONS + canonical).hexdigest()
        )
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

    @unittest.skipUnless(os.path.normcase("A") == "a", "case-insensitive paths only on Windows")
    def test_other_case_is_the_same_file_on_windows(self):
        profile = self.repo / "MUSTER.TOML"
        self.assertEqual(self.policy("--zones-out", str(profile))[0], 2)
        self.assertTrue((self.repo / "muster.toml").read_bytes().startswith(b"schema = 1"))


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

    def test_missing_allowlist(self):
        code, env = self.envelope()
        self.assertEqual(code, 2)
        message = "policy/mutation.txt: not found (mutation.targets)"
        self.assertEqual(env["error"], {"kind": "policy", "message": message})


class HashTest(unittest.TestCase):
    def test_policy_hash_covers_the_raw_bytes_of_the_three_files_in_order(self):
        canonical = b'{"source":{"roots":["src"]},"zones":{"base":""}}'
        tables = {"zones": {"base": ""}, "source": {"roots": ["src"]}}
        self.assertEqual(policy_sha256(b"a", b"b", b"c", tables), hashlib.sha256(b"abc" + canonical).hexdigest())
        self.assertNotEqual(policy_sha256(b"a\r\n", b"", b"", tables), policy_sha256(b"a\n", b"", b"", tables))
        self.assertNotEqual(policy_sha256(b"a", b"b", b"", tables), policy_sha256(b"b", b"a", b"", tables))

    def test_policy_hash_covers_the_tables(self):
        tables = {"source": {"roots": ["src"]}, "zones": {"base": ""}}
        other = {"source": {"roots": ["src"]}, "zones": {"base": "app"}}
        self.assertNotEqual(policy_sha256(b"", b"", b"", tables), policy_sha256(b"", b"", b"", other))

    def test_sources_hash(self):
        self.assertEqual(sources_sha256(["a.py", "src/b.py"]), hashlib.sha256(b"a.py\nsrc/b.py\n").hexdigest())
        self.assertEqual(sources_sha256([]), hashlib.sha256(b"").hexdigest())


if __name__ == "__main__":
    unittest.main()
