"""Tests of muster.config: reading muster.toml, the schema v1 checks and the per-command keys.

Every path is synthetic. A Windows drive path is built at run time.
"""

import copy
import math
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from muster import globs
from muster.config import Config, ConfigError, _exact_case, config_path, load, require, validate

DRIVE = "C" + ":"

EXAMPLE = r"""
schema = 1

[tool]
version = "0.1.0"
sha256  = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

[source]
roots   = ["app/src/main/java"]
tests   = ["app/src/test/java"]
include = ["**/*.java"]
exclude = ["**/package-info.java"]
generated = []

[zones]
base       = "app/src/main/java/org/example/app"
critical   = "policy/critical.txt"
standard   = "policy/standard.txt"
exemptions = "policy/exemptions.txt"
exemption_max_days = 30

[policy]
snapshot = "app/build/muster/snapshot.json"

[coverage]
format = "jacoco"
report = "app/build/reports/jacoco/test/coverage.xml"
floor  = { line = 0.80, branch = 0.65 }
delta  = { line = 0.80, branch = 0.80 }

[[coverage.full_run_suites]]
name = "org.example.app.FullRunSuite"
file = "app/src/test/java/org/example/app/FullRunSuite.java"

[tests]
format  = "junit"
results = "app/build/test-results/test/TEST-*.xml"

[duration]
source = "task-json"
task_json = "app/build/muster/task.json"
task   = ":app:test"
warn_s = 240
error_s = 300

[loc]
exclude = []
test_support = [
  "app/src/test/java/org/example/app/support/**",
  "**/*Fixture*.java",
]
budget_ratio = 1.0
budget_grace_lines = 50
budget_trailer = "Test-Budget"

[mutation]
format  = "pit"
report  = "app/build/reports/mutation/mutations.xml"
targets = "policy/mutation.txt"
threshold = 0.60
target    = 0.70
own_test  = "{stem}Test{ext}"
own_test_forbidden = ["*IntegrationTest*"]
tool_versions = { file = "build/versions.toml", keys = ["mutation-plugin", "mutation-tool"] }

[[mutation.known_variation]]
class = "OrderRules"
method = "total"
line = 42
mutator = "MathMutator"

[[language]]
name = "java"
extensions = [".java"]
line_comment = "//"
block_comment = ["/*", "*/"]
skip_patterns = ['@Disabled\b', '@Ignore\b']

[output]
dir = "app/build/muster"

[check]
sections = ["policy", "coverage", "duration", "loc", "tests"]
"""

# The keys an entry of each array of tables must carry, so that a test of one key builds a valid entry.
ENTRIES = {
    "full_run_suites": {"name": "AppSuite", "file": "app/src/test/AppSuite.java"},
    "known_variation": {"class": "OrderRules", "method": "total", "line": 1, "mutator": "MathMutator"},
    "language": {"name": "java"},
}

PATH_KEYS = [
    "zones.base",
    "zones.critical",
    "zones.standard",
    "zones.exemptions",
    "policy.snapshot",
    "coverage.report",
    "coverage.full_run_suites[].file",
    "tests.results",
    "duration.task_json",
    "mutation.report",
    "mutation.targets",
    "mutation.tool_versions.file",
    "output.dir",
]
PATH_LIST_KEYS = [
    "source.roots",
    "source.tests",
    "source.include",
    "source.exclude",
    "source.generated",
    "loc.exclude",
    "loc.test_support",
]
FRACTION_KEYS = [
    "coverage.floor.line",
    "coverage.floor.branch",
    "coverage.delta.line",
    "coverage.delta.branch",
    "mutation.threshold",
    "mutation.target",
]
COUNT_KEYS = ["zones.exemption_max_days", "loc.budget_grace_lines", "mutation.known_variation[].line"]
POSITIVE_KEYS = ["duration.warn_s", "duration.error_s", "loc.budget_ratio"]


def doc(key: str, value) -> dict:
    """A document that sets one dotted key; a part ending in [] is an array with one entry."""
    root: dict = {}
    current = root
    parts = key.split(".")
    for part in parts[:-1]:
        if part.endswith("[]"):
            entry = copy.deepcopy(ENTRIES[part[:-2]])
            current[part[:-2]] = [entry]
            current = entry
        else:
            current = current.setdefault(part, {})
    current[parts[-1]] = value
    return root


def shown(key: str) -> str:
    """The key as an error message names it: the one entry of an array is entry 0."""
    return key.replace("[]", "[0]")


class ValidateCase(unittest.TestCase):
    def error(self, data: dict) -> str:
        with self.assertRaises(ConfigError) as ctx:
            validate(data)
        self.assertEqual(ctx.exception.kind, "config")
        return str(ctx.exception)


class ExampleTest(ValidateCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_full_example_loads(self):
        (self.root / "muster.toml").write_bytes(EXAMPLE.encode("ascii"))
        cfg = load(self.root)
        self.assertEqual(cfg.get("coverage.floor.branch"), 0.65)
        self.assertEqual(cfg.get("coverage.full_run_suites")[0]["name"], "org.example.app.FullRunSuite")
        self.assertEqual(cfg.get("mutation.known_variation")[0]["line"], 42)
        self.assertEqual(cfg.get("language")[0]["skip_patterns"], ["@Disabled\\b", "@Ignore\\b"])
        self.assertEqual(cfg.get("mutation.tool_versions.file"), "build/versions.toml")

    def test_every_table_of_the_example_is_optional(self):
        full = tomllib.loads(EXAMPLE)
        for name in full:
            with self.subTest(name=name):
                validate({key: value for key, value in full.items() if key != name})

    def test_empty_document(self):
        validate({})


class UnknownKeyTest(ValidateCase):
    def test_top_level(self):
        self.assertEqual(self.error({"strict": True}), "unknown key strict")

    def test_in_a_table(self):
        self.assertEqual(self.error({"source": {"rots": ["app"]}}), "unknown key source.rots")

    def test_in_an_inline_table(self):
        data = tomllib.loads("[coverage]\nfloor = { line = 0.8, lines = 0.7 }\n")
        self.assertEqual(self.error(data), "unknown key coverage.floor.lines")

    def test_in_inline_tool_versions(self):
        data = doc("mutation.tool_versions.version", "1")
        self.assertEqual(self.error(data), "unknown key mutation.tool_versions.version")

    def test_in_an_array_of_tables(self):
        cases = [
            ("[[coverage.full_run_suites]]\nname = 'Suite'\nfile = 'app/Suite.java'\npath = 'x'\n",
             "unknown key coverage.full_run_suites[0].path"),
            ("[[mutation.known_variation]]\nclass = 'Rules'\nmethod = 'run'\nline = 1\nmutator = 'Math'\ncolumn = 2\n",
             "unknown key mutation.known_variation[0].column"),
            ("[[language]]\nname = 'java'\n[[language]]\nname = 'kotlin'\ncomment = '//'\n",
             "unknown key language[1].comment"),
        ]
        for text, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.error(tomllib.loads(text)), message)

    def test_strict_is_no_key(self):
        self.assertEqual(self.error({"coverage": {"strict": True}}), "unknown key coverage.strict")

    def test_quoted_key_is_shown_quoted(self):
        self.assertEqual(self.error({"source": {"two words": 1}}), 'unknown key source."two words"')


class TypeTest(ValidateCase):
    def test_wrong_types(self):
        cases = [
            ("zones.exemption_max_days", "30", "expected an integer, got a string"),
            ("zones.exemption_max_days", True, "expected an integer, got a boolean"),
            ("zones.exemption_max_days", 30.0, "expected an integer, got a float"),
            ("loc.budget_grace_lines", False, "expected an integer, got a boolean"),
            ("coverage.floor.line", True, "expected a number, got a boolean"),
            ("coverage.floor.line", "0.8", "expected a number, got a string"),
            ("duration.warn_s", True, "expected a number, got a boolean"),
            ("loc.budget_ratio", [1], "expected a number, got an array"),
            ("tool.version", 1, "expected a string, got an integer"),
            ("duration.task", {}, "expected a string, got a table"),
            ("coverage.report", ["app/report.xml"], "expected a string, got an array"),
            ("source.roots", "app/src", "expected an array, got a string"),
            ("coverage.floor", 0.8, "expected a table, got a float"),
        ]
        for key, value, message in cases:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.error(doc(key, value)), f"{key}: {message}")

    def test_table_expected_at_top_level(self):
        self.assertEqual(self.error({"source": "app"}), "source: expected a table, got a string")

    def test_array_of_tables_expected(self):
        data = {"coverage": {"full_run_suites": {"name": "Suite", "file": "app/Suite.java"}}}
        self.assertEqual(self.error(data), "coverage.full_run_suites: expected an array of tables, got a table")

    def test_entry_of_array_of_tables_must_be_a_table(self):
        self.assertEqual(self.error({"language": ["java"]}), "language[0]: expected a table, got a string")

    def test_wrong_item_type_in_a_list(self):
        message = self.error(doc("check.sections", ["policy", 1]))
        self.assertEqual(message, "check.sections[1]: expected a string, got an integer")
        self.assertEqual(self.error(doc("source.tests", [7])), "source.tests[0]: expected a string, got an integer")

    def test_date_is_not_an_integer(self):
        data = tomllib.loads("[zones]\nexemption_max_days = 2026-01-31\n")
        self.assertEqual(self.error(data), "zones.exemption_max_days: expected an integer, got a date or time")

    def test_integer_accepted_where_a_float_is_expected(self):
        for key in FRACTION_KEYS:
            with self.subTest(key=key):
                validate(doc(key, 1))
        validate(doc("duration.warn_s", 240))
        validate(doc("loc.budget_ratio", 2))


class EnumTest(ValidateCase):
    def test_accepted_values(self):
        for key, value in [
            ("coverage.format", "jacoco"),
            ("tests.format", "junit"),
            ("mutation.format", "pit"),
            ("duration.source", "task-json"),
            ("duration.source", "junit-span"),
            ("check.sections", ["policy", "coverage", "duration", "loc", "tests", "mutation"]),
        ]:
            with self.subTest(key=key, value=value):
                validate(doc(key, value))

    def test_rejected_values(self):
        cases = [
            ("coverage.format", "cobertura", "coverage.format: must be one of jacoco"),
            ("tests.format", "xunit", "tests.format: must be one of junit"),
            ("mutation.format", "other", "mutation.format: must be one of pit"),
            ("duration.source", "build-log", "duration.source: must be one of task-json, junit-span"),
        ]
        for key, value, prefix in cases:
            with self.subTest(key=key):
                self.assertEqual(self.error(doc(key, value)), f'{prefix}, got "{value}"')

    def test_unknown_section_name(self):
        message = self.error(doc("check.sections", ["policy", "lint"]))
        self.assertTrue(message.startswith("check.sections[1]: must be one of policy, coverage,"), message)


class RangeTest(ValidateCase):
    def test_fraction_bounds_accepted(self):
        for key in FRACTION_KEYS:
            for value in (0, 0.0, 0.5, 1, 1.0):
                with self.subTest(key=key, value=value):
                    validate(doc(key, value))

    def test_fraction_out_of_range(self):
        for key in FRACTION_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.error(doc(key, 1.5)), f"{shown(key)}: must be 1 or less, got 1.5")
                self.assertEqual(self.error(doc(key, -0.1)), f"{shown(key)}: must be 0 or more, got -0.1")

    def test_positive_values(self):
        for key in POSITIVE_KEYS:
            with self.subTest(key=key):
                validate(doc(key, 0.5))
                validate(doc(key, 1000))
                self.assertEqual(self.error(doc(key, 0)), f"{key}: must be greater than 0, got 0")
                self.assertEqual(self.error(doc(key, -1.5)), f"{key}: must be greater than 0, got -1.5")

    def test_non_finite_numbers(self):
        for key in FRACTION_KEYS[:1] + POSITIVE_KEYS:
            for value in (math.nan, math.inf):
                with self.subTest(key=key, value=value):
                    self.assertIn("must be a finite number", self.error(doc(key, value)))

    def test_counts(self):
        for key in COUNT_KEYS:
            with self.subTest(key=key):
                validate(doc(key, 0))
                self.assertEqual(self.error(doc(key, -1)), f"{shown(key)}: must be 0 or more, got -1")


class SchemaVersionTest(ValidateCase):
    def test_version_one(self):
        validate({"schema": 1})

    def test_other_version(self):
        self.assertEqual(self.error({"schema": 2}), "schema: unsupported schema 2; this muster reads schema 1")

    def test_wrong_type(self):
        self.assertEqual(self.error({"schema": "1"}), "schema: expected an integer, got a string")
        self.assertEqual(self.error({"schema": True}), "schema: expected an integer, got a boolean")


class StringShapeTest(ValidateCase):
    def test_sha256(self):
        validate(doc("tool.sha256", "0123456789abcdef" * 4))
        for value in ("0123456789ABCDEF" * 4, "0123456789abcdef" * 4 + "0", ("0123456789abcdef" * 4)[1:], "g" * 64):
            with self.subTest(value=value):
                self.assertEqual(self.error(doc("tool.sha256", value)), "tool.sha256: must be 64 lower-case hex digits")

    def test_skip_pattern_must_compile(self):
        message = self.error(doc("language[].skip_patterns", ["@Disabled\\b", "(unclosed"]))
        self.assertTrue(message.startswith("language[0].skip_patterns[1]: not a valid regular expression: "), message)

    def test_block_comment_is_two_strings(self):
        validate(doc("language[].block_comment", ["/*", "*/"]))
        self.assertEqual(
            self.error(doc("language[].block_comment", ["/*"])), "language[0].block_comment: must have 2 items, got 1"
        )
        self.assertEqual(
            self.error(doc("language[].block_comment", ["/*", "*/", "*"])),
            "language[0].block_comment: must have 2 items, got 3",
        )
        self.assertEqual(
            self.error(doc("language[].block_comment", ["/*", 2])),
            "language[0].block_comment[1]: expected a string, got an integer",
        )


class RequiredEntryKeyTest(ValidateCase):
    def test_each_entry_carries_its_keys(self):
        cases = [
            ({"coverage": {"full_run_suites": [{"name": "Suite"}]}}, "coverage.full_run_suites[0]: missing key file"),
            (
                {"coverage": {"full_run_suites": [{"file": "app/Suite.java"}]}},
                "coverage.full_run_suites[0]: missing key name",
            ),
            (
                {"mutation": {"known_variation": [{"class": "Rules", "method": "run", "line": 3}]}},
                "mutation.known_variation[0]: missing key mutator",
            ),
            ({"language": [{"extensions": [".py"]}]}, "language[0]: missing key name"),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                self.assertEqual(self.error(data), message)


class PathTest(ValidateCase):
    BAD = [
        ("/work/src", "starts with /"),
        ("app\\src", "has a backslash"),
        (DRIVE + "/work/src", "starts with a drive"),
        (DRIVE + "src", "starts with a drive"),
        ("app/../other", "has a .. segment"),
        ("..", "has a .. segment"),
        ("app/src/..", "has a .. segment"),
        ("./app", "has a . segment"),
        ("app/./src", "has a . segment"),
        (".", "has a . segment"),
        ("app/src/", "ends with /"),
        ("app//src", "has an empty segment"),
        ("app/" + chr(0) + "src", "has a control character"),
        ("app/src" + chr(0x1F), "has a control character"),
        ("app/" + chr(0x7F), "has a control character"),
        ("app/" + chr(0x85) + "src", "has a control character"),
        ("app/src" + chr(0x9F), "has a control character"),
        ("app/" + chr(0x2028) + "src", "has a control character"),
        ("app/" + chr(0x2029), "has a control character"),
    ]
    GOOD = ["app/src/main", "**/*.java", "app/release..notes/x", "app/.hidden/x", "TEST-*.xml"]

    def message(self, key: str, problem: str) -> str:
        return f"{shown(key)}: must be a path relative to the repository root with / separators; it {problem}"

    def test_single_paths(self):
        for key in PATH_KEYS:
            for value, problem in self.BAD:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.error(doc(key, value)), self.message(key, problem))
            for value in self.GOOD:
                with self.subTest(key=key, value=value):
                    validate(doc(key, value))

    def test_lists_of_paths(self):
        for key in PATH_LIST_KEYS:
            for value, problem in self.BAD:
                with self.subTest(key=key, value=value):
                    self.assertEqual(self.error(doc(key, ["app/ok", value])), self.message(key + "[1]", problem))
            with self.subTest(key=key, value="good"):
                validate(doc(key, self.GOOD))

    def test_empty_path_rejected(self):
        for key in PATH_KEYS[1:]:
            with self.subTest(key=key):
                self.assertEqual(self.error(doc(key, "")), self.message(key, "is empty"))
        for key in PATH_LIST_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.error(doc(key, ["app", ""])), self.message(key + "[1]", "is empty"))

    def test_empty_zones_base_is_the_repository_root(self):
        validate(doc("zones.base", ""))
        self.assertEqual(self.error(doc("zones.base", ".")), self.message("zones.base", "has a . segment"))

    def test_roots_and_include_not_empty(self):
        self.assertEqual(self.error(doc("source.roots", [])), "source.roots: must not be empty")
        self.assertEqual(self.error(doc("source.include", [])), "source.include: must not be empty")

    def test_other_path_lists_may_be_empty(self):
        for key in PATH_LIST_KEYS[1:]:
            if key != "source.include":
                with self.subTest(key=key):
                    validate(doc(key, []))


GLOB_KEYS = ["source.include", "source.exclude", "source.generated", "loc.exclude", "loc.test_support"]


class GlobTest(ValidateCase):
    def test_malformed_glob_is_rejected_when_the_profile_is_read(self):
        cases = [
            ("src/{", "a '{' without its '}'"),
            ("src/}x", "a '}' without its '{'"),
            ("src/{a,{b}}", "nested braces"),
            ("**.java", "'**' must be a whole path segment"),
        ]
        for key in GLOB_KEYS:
            for glob, reason in cases:
                with self.subTest(key=key, glob=glob):
                    self.assertEqual(self.error(doc(key, ["**/*.py", glob])), f'{key}[1]: bad glob "{glob}": {reason}')
        self.assertEqual(
            self.error(doc("tests.results", "build/**.xml")),
            "tests.results: bad glob \"build/**.xml\": '**' must be a whole path segment",
        )

    def test_every_alternative_is_a_relative_path(self):
        prefix = "must be a path relative to the repository root with / separators; its alternative"
        cases = [
            ("{../private.py,**/*.py}", '"../private.py" has a .. segment'),
            ("{src,/work}/*.py", '"/work/*.py" starts with /'),
            ("src/{main,}", '"src/" ends with /'),
            ("{,src/**}", '"" is empty'),
            ("{./src,lib}/*.py", '"./src/*.py" has a . segment'),
        ]
        for key in GLOB_KEYS:
            for glob, problem in cases:
                with self.subTest(key=key, glob=glob):
                    self.assertEqual(self.error(doc(key, [glob])), f"{key}[0]: {prefix} {problem}")
        validate(doc("source.include", ["{src,lib}/**/*.{py,pyi}", "src/{,app/}main.py"]))

    def test_no_alternative_of_the_test_results_leaves_the_repository(self):
        prefix = "must be a path relative to the repository root with / separators; its alternative"
        cases = [
            ("{../reports,build}/*.xml", '"../reports/*.xml" has a .. segment'),
            ("{build,/work}/*.xml", '"/work/*.xml" starts with /'),
            ("{build," + DRIVE + "/work}/*.xml", f'"{DRIVE}/work/*.xml" starts with a drive'),
        ]
        for glob, problem in cases:
            with self.subTest(glob=glob):
                self.assertEqual(self.error(doc("tests.results", glob)), f"tests.results: {prefix} {problem}")
        validate(doc("tests.results", "{app,lib}/build/test-results/**/TEST-*.xml"))

    def test_own_test_forbidden_is_a_glob_of_file_names(self):
        validate(doc("mutation.own_test_forbidden", ["*IntegrationTest*", "{Slow,Live}*"]))
        message = self.error(doc("mutation.own_test_forbidden", ["*Test*", "{Integration"]))
        self.assertEqual(message, "mutation.own_test_forbidden[1]: bad glob \"{Integration\": a '{' without its '}'")
        for value in ("*" + chr(0) + "*", "*" + chr(0x85) + "*"):
            with self.subTest(value=value):
                message = self.error(doc("mutation.own_test_forbidden", [value]))
                self.assertEqual(message, "mutation.own_test_forbidden[0]: has a control character")

    def test_glob_with_more_than_1024_alternatives(self):
        glob = "{ab,cd}" * 30 + "/*.py"
        message = f'source.include[0]: bad glob "{glob}": more than 1024 alternatives'
        self.assertEqual(self.error(doc("source.include", [glob])), message)
        validate(doc("source.include", ["{ab,cd}" * 10 + "/*.py"]))

    def test_every_glob_is_compiled_when_the_profile_is_checked(self):
        with mock.patch("muster.globs._compile", wraps=globs._compile) as compile_:
            validate({"tests": {"results": "reports/*.xml"}, "mutation": {"own_test_forbidden": ["*IT*"]}})
        compile_.assert_any_call("reports/*.xml")
        compile_.assert_any_call("*IT*")

    def test_own_test_forbidden_alternative_is_a_file_name(self):
        prefix = "mutation.own_test_forbidden[0]: must be a glob of file names; its alternative"
        cases = [
            ("", '"" is empty'),
            ("{.,*Test*}", '"." is . or ..'),
            ("{*Test*,..}", '".." is . or ..'),
            (DRIVE + "*Test*", f'"{DRIVE}*Test*" starts with a drive'),
            ("{*Test*," + DRIVE + "*}", f'"{DRIVE}*" starts with a drive'),
        ]
        for value, problem in cases:
            with self.subTest(value=value):
                self.assertEqual(self.error(doc("mutation.own_test_forbidden", [value])), f"{prefix} {problem}")
        validate(doc("mutation.own_test_forbidden", ["*IntegrationTest*", "..*Test*", "{Slow,Live}*"]))

    def test_own_test_forbidden_has_no_folder(self):
        message = "mutation.own_test_forbidden[0]: must be a glob of file names, without / or \\"
        for value in ("dir/../*Test*", "{Slow,support/Live}*", "support\\*Test*"):
            with self.subTest(value=value):
                self.assertEqual(self.error(doc("mutation.own_test_forbidden", [value])), message)


class OwnTestTemplateTest(ValidateCase):
    def test_a_file_name_with_the_stem(self):
        for value in ("{stem}Test{ext}", "test_{stem}{ext}", "{stem}.spec.ts"):
            with self.subTest(value=value):
                validate(doc("mutation.own_test", value))

    def test_stem_is_required(self):
        self.assertEqual(self.error(doc("mutation.own_test", "Test{ext}")), "mutation.own_test: must contain {stem}")

    def test_no_folder(self):
        message = "mutation.own_test: must be a file name, without / or \\ or a control character"
        for value in ("support/{stem}Test{ext}", "support\\{stem}Test{ext}", "../{stem}Test{ext}"):
            with self.subTest(value=value):
                self.assertEqual(self.error(doc("mutation.own_test", value)), message)


class NeverInternalErrorTest(ValidateCase):
    """A value Python cannot handle the plain way is a configuration error, never a crash."""

    def setUp(self):
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)  # the default, which the messages in hex rely on

    def test_huge_integer_where_a_number_is_expected(self):
        huge = 16**5000 - 1  # more digits than int-to-str conversion allows, more than a float holds
        self.assertEqual(
            self.error(doc("coverage.floor.line", huge)), f"coverage.floor.line: must be 1 or less, got {hex(huge)}"
        )
        self.assertEqual(
            self.error(doc("duration.warn_s", -(10**400))), f"duration.warn_s: must be greater than 0, got {-(10**400)}"
        )
        self.assertEqual(self.error(doc("duration.warn_s", 10**400)), f"duration.warn_s: {self.BEYOND_64_BITS}")
        self.assertIn("unsupported schema 0xfff", self.error({"schema": huge}))

    BEYOND_64_BITS = "must be a 64-bit integer, from -9223372036854775808 to 9223372036854775807"

    def test_integer_beyond_64_bits(self):
        for key in COUNT_KEYS + POSITIVE_KEYS:
            with self.subTest(key=key):
                validate(doc(key, 2**63 - 1))
                self.assertEqual(self.error(doc(key, 2**63)), f"{shown(key)}: {self.BEYOND_64_BITS}")
                self.assertEqual(self.error(doc(key, 16**5000)), f"{shown(key)}: {self.BEYOND_64_BITS}")
        huge = -(16**5000)
        message = f"zones.exemption_max_days: must be 0 or more, got {hex(huge)}"
        self.assertEqual(self.error(doc("zones.exemption_max_days", huge)), message)

    def test_regular_expression_that_overflows(self):
        for pattern in ("x{4294967296}", "(" * 2000 + ")" * 2000):
            with self.subTest(pattern=pattern[:20]):
                message = self.error(doc("language[].skip_patterns", [pattern]))
                self.assertTrue(
                    message.startswith("language[0].skip_patterns[0]: not a valid regular expression: "), message
                )


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def error(self, value: str = "muster.toml") -> ConfigError:
        with self.assertRaises(ConfigError) as ctx:
            load(self.root, value)
        self.assertEqual(ctx.exception.kind, "config")
        return ctx.exception

    def test_missing_file(self):
        self.assertEqual(str(self.error()), "muster.toml: not found")

    def test_folder_instead_of_file(self):
        (self.root / "muster.toml").mkdir()
        self.assertTrue(str(self.error()).startswith("muster.toml: cannot read: "))

    def test_not_utf8(self):
        self.write("muster.toml", b"schema = 1\n# caf\xe9\n")
        self.assertEqual(str(self.error()), "muster.toml: not valid UTF-8 (byte 16)")

    def test_toml_syntax_error_carries_the_parser_message(self):
        text = "[source]\nroots = [\"app\"\n"
        self.write("muster.toml", text.encode("ascii"))
        with self.assertRaises(tomllib.TOMLDecodeError) as ctx:
            tomllib.loads(text)
        self.assertEqual(str(self.error()), f"muster.toml: invalid TOML: {ctx.exception}")

    def test_toml_that_python_cannot_hold_is_invalid_toml(self):
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)  # the default, which the parser and the message rely on
        cases = [
            (b"schema = " + b"1" * 5000 + b"\n", "muster.toml: invalid TOML: "),
            (b"x = " + b"[" * 5000 + b"]" * 5000 + b"\n", "muster.toml: invalid TOML: "),
            (
                b"[coverage.floor]\nline = 0x" + b"f" * 5000 + b"\n",
                "muster.toml: coverage.floor.line: must be 1 or less, got 0xfff",
            ),
        ]
        for text, prefix in cases:
            with self.subTest(text=text[:20]):
                self.write("muster.toml", text)
                message = str(self.error())
                self.assertTrue(message.startswith(prefix), message[:200])

    def test_escaped_control_character_in_a_path(self):
        self.write("muster.toml", b'[policy]\nsnapshot = "build/snap\\u0000shot.json"\n')
        self.assertEqual(
            str(self.error()),
            "muster.toml: policy.snapshot: must be a path relative to the repository root with / separators;"
            " it has a control character",
        )

    def test_root_of_the_file_system_is_not_reported_missing(self):
        root = Path(os.path.abspath(os.sep))
        self.assertTrue(_exact_case(self.root, root))
        if sys.platform != "win32":  # Windows itself reports a drive root as not found
            self.assertTrue(str(self.error(str(root))).startswith(f"{root}: cannot read: "))

    def test_file_name_in_another_case_is_not_found(self):
        self.write("muster.toml", b"schema = 1\n")
        self.write("profile/muster.toml", b"schema = 1\n")
        self.assertEqual(str(self.error("MUSTER.TOML")), "MUSTER.TOML: not found")
        self.assertEqual(str(self.error("Profile/muster.toml")), "Profile/muster.toml: not found")
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "elsewhere.toml"
        outside.write_bytes(b"schema = 1\n")
        other_case = outside.with_name("ELSEWHERE.toml")
        self.assertEqual(str(self.error(str(other_case))), f"{other_case}: not found")
        self.assertEqual(load(self.root, "profile/../muster.toml").name, "profile/../muster.toml")

    def test_invalid_value_names_the_file(self):
        self.write("muster.toml", b"[zones]\nexemption_max_days = -1\n")
        self.assertEqual(str(self.error()), "muster.toml: zones.exemption_max_days: must be 0 or more, got -1")

    def test_relative_value_is_relative_to_the_root(self):
        self.write("profile/muster.toml", b"[output]\ndir = \"build/muster\"\n")
        cfg = load(self.root, "profile/muster.toml")
        self.assertEqual(cfg.path, self.root / "profile" / "muster.toml")
        self.assertEqual(cfg.root, self.root)
        self.assertEqual(cfg.name, "profile/muster.toml")
        self.assertEqual(cfg.output_dir, "build/muster")

    def test_relative_value_error_names_posix_path(self):
        self.write("profile/muster.toml", b"bogus = 1\n")
        self.assertEqual(str(self.error("profile/muster.toml")), "profile/muster.toml: unknown key bogus")

    def test_absolute_value_outside_the_root(self):
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "elsewhere.toml"
        outside.write_bytes(b"schema = 1\n")
        cfg = load(self.root, str(outside))
        self.assertEqual(cfg.path, outside)
        self.assertEqual(cfg.name, str(outside))

    def test_crlf_line_ends(self):
        self.write("muster.toml", b"[zones]\r\nbase = \"\"\r\nexemption_max_days = 14\r\n")
        cfg = load(self.root)
        self.assertEqual(cfg.get("zones.base"), "")
        self.assertEqual(cfg.exemption_max_days, 14)

    def test_config_path(self):
        self.assertEqual(config_path(self.root, "muster.toml"), self.root / "muster.toml")
        self.assertEqual(config_path(self.root, str(self.root / "other.toml")), self.root / "other.toml")


class ConfigAccessTest(unittest.TestCase):
    def config(self, data: dict) -> Config:
        validate(data)
        return Config(Path("/work/repo"), Path("/work/repo/muster.toml"), data)

    def test_defaults(self):
        cfg = self.config({})
        self.assertEqual(cfg.exemption_max_days, 30)
        self.assertIsNone(cfg.snapshot)
        self.assertIsNone(cfg.output_dir)
        self.assertIsNone(cfg.mutation_targets)
        self.assertIsNone(cfg.own_test)
        self.assertEqual(cfg.own_test_forbidden, [])

    def test_set_values(self):
        cfg = self.config(tomllib.loads(EXAMPLE))
        self.assertEqual(cfg.exemption_max_days, 30)
        self.assertEqual(cfg.snapshot, "app/build/muster/snapshot.json")
        self.assertEqual(cfg.output_dir, "app/build/muster")
        self.assertEqual(cfg.mutation_targets, "policy/mutation.txt")
        self.assertEqual(cfg.own_test, "{stem}Test{ext}")
        self.assertEqual(cfg.own_test_forbidden, ["*IntegrationTest*"])

    def test_get(self):
        cfg = self.config({"zones": {"base": ""}})
        self.assertEqual(cfg.get("zones.base"), "")
        self.assertIsNone(cfg.get("zones.critical"))
        self.assertEqual(cfg.get("source.roots", []), [])
        self.assertEqual(cfg.get("zones.base.deeper", "none"), "none")


class RequireTest(unittest.TestCase):
    KEYS = ("source.roots", "source.include", "zones.base", "zones.critical", "zones.standard")

    def config(self, data: dict) -> Config:
        return Config(Path("/work/repo"), Path("/work/repo/muster.toml"), data)

    def full(self) -> dict:
        return {
            "source": {"roots": ["app/src"], "include": ["**/*.py"]},
            "zones": {"base": "", "critical": "policy/critical.txt", "standard": "policy/standard.txt"},
        }

    def test_all_present(self):
        require(self.config(self.full()), *self.KEYS)

    def test_empty_base_counts_as_set(self):
        require(self.config({"zones": {"base": ""}}), "zones.base")

    def test_first_missing_key_named(self):
        data = self.full()
        del data["zones"]["critical"]
        del data["zones"]["standard"]
        with self.assertRaises(ConfigError) as ctx:
            require(self.config(data), *self.KEYS)
        self.assertEqual(str(ctx.exception), "muster.toml: missing key zones.critical")
        self.assertEqual(ctx.exception.kind, "config")

    def test_missing_table(self):
        with self.assertRaises(ConfigError) as ctx:
            require(self.config({}), *self.KEYS)
        self.assertEqual(str(ctx.exception), "muster.toml: missing key source.roots")

    def test_no_keys(self):
        require(self.config({}))


if __name__ == "__main__":
    unittest.main()
