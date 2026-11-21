"""Tests of muster.zones: the zone grammar, the classification, the exemptions and the allowlist of
mutation targets.

The world is synthetic: sources under src/main/java/org/example/app, tests under src/test/java.
Characters outside ASCII are written as escapes.
"""

import unittest
from datetime import date

from muster.gitio import records
from muster.policy import utc_today
from muster.zones import Names, Targets, classify, expand, glob_error, parse_date, target_violations, trim

BASE = "src/main/java/org/example/app"
CRITICAL = "policy/critical.txt"
STANDARD = "policy/standard.txt"
EXEMPTIONS = "policy/exemptions.txt"
TARGETS = "policy/mutation.txt"
NAMES = Names(CRITICAL, STANDARD, EXEMPTIONS)
TODAY = date(2026, 5, 19)
ZONE_1 = "# Zone 1: test zone."


def sources(*paths: str) -> list[str]:
    return [f"{BASE}/{path}" for path in paths]


def zoned(*lines: str) -> list[str]:
    """Critical-file lines under a zone header."""
    return [ZONE_1, *lines]


def run(critical, standard, exemptions, files, *, today=TODAY, max_days=30, base=BASE):
    return classify(critical, standard, exemptions, files, base=base, names=NAMES, today=today, max_days=max_days)


def check(critical, standard, files) -> list[str]:
    """The violations of the glob and zone rules alone."""
    return run(critical, standard, [], files).violations


def exempting(*lines: str, **options):
    """Critical billing/Engine.java and billing/Ledger.java, standard auth/Token.java, with these exemptions."""
    files = sources("billing/Engine.java", "billing/Ledger.java", "auth/Token.java")
    return run(zoned("billing/**"), ["auth/*"], list(lines), files, **options)


def unclassified(package: str) -> str:
    return f"unclassified package '{package}' - add it to {CRITICAL} or {STANDARD}"


def standard_form(glob: str) -> str:
    return (
        f"{STANDARD}: '{glob}' must name packages without subpackages ('<package>/*', or '*' for the root package)"
    )


def malformed_header(line: str) -> str:
    return (
        f"{CRITICAL}: malformed zone header '{line}' - expected '# Zone <n>: <title>' with n from 1 to 999,"
        " without leading zeros"
    )


def repeated_header(line: str) -> str:
    return f"{CRITICAL}: zone header '{line}' repeats a zone number - each zone has one header"


def malformed_exemption(line: str) -> str:
    return f"{EXEMPTIONS}: malformed entry '{line}' - expected 'path | reason | valid-until'"


def malformed_target(line: str) -> str:
    return (
        f"{TARGETS}: '{line}' must be one source file '<folder>/<file>' - no glob, no file directly in the base"
        " folder, no comment after the path"
    )


def targets_check(lines, files, tests, *, forbidden=("*IntegrationTest*",), own_test="{stem}Test{ext}", **where):
    """The allowlist checked against these sources and tests (relative to the base package)."""
    targets = Targets(
        name=TARGETS,
        sources=frozenset(sources(*files)),
        tests=frozenset(f"src/test/java/org/example/app/{test}" for test in tests),
        roots=where.get("roots", ["src/main/java"]),
        test_roots=where.get("test_roots", ["src/test/java"]),
        own_test=own_test,
        forbidden=list(forbidden),
    )
    return target_violations(lines, BASE, targets)


class ClassificationTest(unittest.TestCase):
    def test_unclassified_package_is_named(self):
        files = sources("billing/Engine.java", "auth/Token.java", "codec/parse/KeyParser.java")
        self.assertEqual(check(zoned("billing/**"), ["auth/*"], files), [unclassified("codec/parse")])

    def test_unclassified_file_in_the_base_folder(self):
        files = sources("billing/Engine.java", "App.java")
        self.assertEqual(check(zoned("billing/**"), [], files), [unclassified("<root>")])

    def test_unclassified_packages_are_sorted_and_named_once(self):
        files = sources("queue/Dispatcher.java", "queue/Worker.java", "codec/KeyParser.java", "auth/Token.java")
        self.assertEqual(
            check(zoned("auth/**"), [], files), [unclassified("codec"), unclassified("queue")]
        )

    def test_dead_glob_and_dead_alternative(self):
        files = sources("billing/Engine.java", "auth/session/Token.java")
        self.assertEqual(
            check(zoned("billing/**", "gone/**"), ["auth/{session,gone}/*"], files),
            [
                f"{CRITICAL}: 'gone/**' matches no source file",
                f"{STANDARD}: 'auth/gone/*' (alternative of 'auth/" + "{session,gone}/*') matches no source file",
            ],
        )

    def test_standard_glob_matching_only_critical_files(self):
        self.assertEqual(
            check(zoned("billing/Engine.java"), ["billing/*"], sources("billing/Engine.java")),
            [f"{STANDARD}: 'billing/*' matches only critical files (critical wins) - drop it"],
        )

    def test_critical_wins_over_standard(self):
        files = sources("billing/Engine.java", "billing/Fee.java")
        result = run(zoned("billing/Engine.java"), ["billing/*"], [], files)
        self.assertEqual(result.violations, [])
        self.assertEqual(result.zones, {f"{BASE}/billing/Engine.java": "Z1", f"{BASE}/billing/Fee.java": "standard"})

    def test_standard_glob_with_subpackages_or_a_file_name(self):
        files = sources("billing/Engine.java", "auth/Token.java", "codec/KeyParser.java")
        violations = check([], ["billing/**", "auth/Token.java", "codec/*Parser*"], files)
        for glob in ("billing/**", "auth/Token.java", "codec/*Parser*"):
            self.assertIn(standard_form(glob), violations)

    def test_duplicate_glob_within_and_across_alternatives(self):
        files = sources("billing/Engine.java", "auth/Token.java", "codec/KeyParser.java")
        self.assertEqual(
            check(zoned("billing/**", "billing/**"), ["auth/*", "{auth,codec}/*"], files),
            [f"duplicate glob 'billing/**' in {CRITICAL}", f"duplicate glob 'auth/*' in {STANDARD}"],
        )

    def test_duplicate_glob_across_the_two_files(self):
        files = sources("billing/Engine.java", "auth/Token.java")
        self.assertEqual(
            check(zoned("billing/**", "auth/*"), ["auth/*"], files), [f"duplicate glob 'auth/*' in {STANDARD}"]
        )

    def test_malformed_globs_and_a_file_outside_the_base(self):
        self.assertEqual(
            check(
                zoned("billing/{ledger,{core}}/**", "billing/auth/**/codec", "billing//auth/**"),
                [],
                ["src/main/java/org/other/Foo.java"],
            ),
            [
                f"source file outside {BASE}/: src/main/java/org/other/Foo.java",
                f"{CRITICAL}: unbalanced or nested braces in 'billing/" + "{ledger,{core}}/**'",
                f"{CRITICAL}: '**' must be a whole last segment in 'billing/auth/**/codec'",
                f"{CRITICAL}: empty path segment in 'billing//auth/**'",
            ],
        )

    def test_glob_grammar_as_documented(self):
        with self.subTest("'**' as the last segment matches the package's files and its subpackages"):
            files = sources("billing/Engine.java", "billing/ledger/Ledger.java")
            self.assertEqual(check(zoned("billing/**"), [], files), [])
        with self.subTest("'*' does not cross '/'"):
            files = sources("billing/Engine.java", "billing/ledger/Ledger.java")
            self.assertEqual(check([], ["billing/*"], files), [unclassified("billing/ledger")])
        with self.subTest("'*Security*' matches SecurityConfig.java"):
            files = sources("auth/config/SecurityConfig.java")
            self.assertEqual(check(zoned("auth/config/*Security*"), [], files), [])
        with self.subTest("comments and blank lines are skipped, lines are trimmed; '*' is the base folder"):
            files = sources("billing/Engine.java", "Root.java")
            self.assertEqual(check(zoned("# comment", "", "  billing/** # trailing\r"), ["*"], files), [])

    def test_critical_glob_before_the_first_zone_header(self):
        self.assertEqual(
            check(
                ["# Zones of the test:", "billing/**", "# Zone 1: auth.", "auth/**"],
                [],
                sources("billing/Engine.java", "auth/Token.java"),
            ),
            [f"{CRITICAL}: 'billing/**' comes before the first '# Zone <n>: ' header"],
        )

    def test_file_in_two_zones(self):
        self.assertEqual(
            check(
                ["# Zone 1: billing.", "billing/**", "# Zone 2: shared files.", "billing/*Shared*", "auth/**"],
                [],
                sources("billing/Engine.java", "billing/SharedLedger.java", "auth/Token.java"),
            ),
            ["'billing/SharedLedger.java' is matched by globs of zones Z1 and Z2 - a file belongs to one zone"],
        )

    def test_zones_of_a_file_are_named_in_sorted_order(self):
        critical = ["# Zone 2: two.", "billing/**", "# Zone 10: ten.", "billing/Engine.java"]
        self.assertEqual(
            check(critical, [], sources("billing/Engine.java", "billing/Fee.java")),
            ["'billing/Engine.java' is matched by globs of zones Z10 and Z2 - a file belongs to one zone"],
        )

    def test_zone_headers_name_the_zones(self):
        result = run(
            [
                "# Zone 1: billing.",
                "billing/**",
                "# Zone 3: auth and codec.",
                "# a comment, not a header: # Zone 4: x",
                "  # Zone 5 has no colon",
                "auth/**",
                "codec/KeyParser.java # trailing comment",
            ],
            ["codec/*"],
            [],
            sources(
                "billing/Engine.java",
                "billing/ledger/Ledger.java",
                "auth/Token.java",
                "codec/KeyParser.java",
                "codec/KeyWriter.java",
            ),
        )
        self.assertEqual(result.violations, [malformed_header("# Zone 5 has no colon")])
        self.assertEqual(
            list(result.zones.items()),
            [
                (f"{BASE}/auth/Token.java", "Z3"),
                (f"{BASE}/billing/Engine.java", "Z1"),
                (f"{BASE}/billing/ledger/Ledger.java", "Z1"),
                (f"{BASE}/codec/KeyParser.java", "Z3"),
                (f"{BASE}/codec/KeyWriter.java", "standard"),
            ],
        )

    def test_malformed_zone_headers(self):
        self.assertEqual(
            check(
                zoned(
                    "billing/**",
                    "# Zone 7:",
                    "# zone 7: lower case.",
                    "#Zone 7: no space.",
                    "  # Zone 7 \u2014 no colon",
                    "# Zone 0: zero.",
                    "# Zone 1000: too large.",
                    "# Zone 12345678901: overflows an int.",
                    "# Zone 007: leading zeros.",
                    "# Zone 00: zeros only.",
                    "# Zones are numbered (a comment).",
                    "# Zone 999: the largest.",
                    "auth/**",
                ),
                [],
                sources("billing/Engine.java", "auth/Token.java"),
            ),
            [
                malformed_header("# Zone 7:"),
                malformed_header("# zone 7: lower case."),
                malformed_header("#Zone 7: no space."),
                malformed_header("# Zone 7 \u2014 no colon"),
                malformed_header("# Zone 0: zero."),
                malformed_header("# Zone 1000: too large."),
                malformed_header("# Zone 12345678901: overflows an int."),
                malformed_header("# Zone 007: leading zeros."),
                malformed_header("# Zone 00: zeros only."),
            ],
        )

    def test_repeated_zone_number(self):
        result = run(
            zoned(
                "billing/**", "# Zone 5: auth.", "auth/**", "# Zone 5: auth again.", "codec/**",
                "# Zone 05: auth padded.", "queue/**",
            ),
            [],
            [],
            sources("billing/Engine.java", "auth/Token.java", "codec/KeyParser.java", "queue/Dispatcher.java"),
        )
        self.assertEqual(
            result.violations, [repeated_header("# Zone 5: auth again."), malformed_header("# Zone 05: auth padded.")]
        )
        self.assertEqual(result.zones[f"{BASE}/codec/KeyParser.java"], "Z5")
        self.assertEqual(result.zones[f"{BASE}/queue/Dispatcher.java"], "Z5")

    def test_header_in_the_standard_file_is_a_comment(self):
        files = sources("billing/Engine.java", "auth/Token.java")
        self.assertEqual(check(zoned("billing/**"), ["# Zone 7 has no colon", "auth/*"], files), [])

    def test_empty_base_is_the_repository_root(self):
        files = ["core/src/billing/Engine.py", "web/src/auth/views.py", "setup.py"]
        result = run(["# Zone 1: money.", "core/src/billing/**"], ["web/src/auth/*", "*"], [], files, base="")
        self.assertEqual(result.violations, [])
        self.assertEqual(result.zones["setup.py"], "standard")

    def test_sources_of_several_roots_under_one_base(self):
        files = ["services/core/src/billing/Engine.py", "services/web/src/auth/views.py", "tools/build.py"]
        result = run(["# Zone 1: money.", "core/src/billing/**"], ["web/src/auth/*"], [], files, base="services")
        self.assertEqual(result.violations, ["source file outside services/: tools/build.py"])
        self.assertEqual(
            result.zones,
            {"services/core/src/billing/Engine.py": "Z1", "services/web/src/auth/views.py": "standard"},
        )


class ExemptionTest(unittest.TestCase):
    def test_valid_exemptions_from_today_to_the_maximum_ahead(self):
        result = exempting(
            "# comment",
            "",
            " billing/Engine.java | rename only | 2026-05-19 \r",
            "billing/Ledger.java|re-wrap, see issue #12|2026-06-18",
        )
        self.assertEqual(result.violations, [])
        self.assertEqual(
            list(result.exempt_until.items()),
            [(f"{BASE}/billing/Engine.java", date(2026, 5, 19)), (f"{BASE}/billing/Ledger.java", date(2026, 6, 18))],
        )

    def test_today_is_the_date_in_utc(self):
        late_evening = 1779233400.0  # 23:30 UTC on 2026-05-19, already the next day east of UTC
        today = utc_today(lambda: late_evening)
        self.assertEqual(today, date(2026, 5, 19))
        self.assertEqual(exempting("billing/Engine.java | rename only | 2026-05-19", today=today).violations, [])

    def test_expired_or_too_distant(self):
        self.assertEqual(
            exempting(
                "billing/Engine.java | rename only | 2026-05-18", "billing/Ledger.java | rename only | 2026-06-19"
            ).violations,
            [
                f"{EXEMPTIONS}: 'billing/Engine.java' expired on 2026-05-18",
                f"{EXEMPTIONS}: 'billing/Ledger.java' is valid until 2026-06-19, more than 30 days ahead",
            ],
        )

    def test_maximum_days_ahead_is_configured(self):
        self.assertEqual(
            exempting("billing/Engine.java | rename only | 2026-05-27", max_days=7).violations,
            [f"{EXEMPTIONS}: 'billing/Engine.java' is valid until 2026-05-27, more than 7 days ahead"],
        )
        self.assertEqual(exempting("billing/Engine.java | rename only | 2026-05-19", max_days=0).violations, [])
        self.assertEqual(exempting("billing/Engine.java | rename only | 9999-12-31", max_days=10**9).violations, [])

    def test_missing_or_non_critical_file(self):
        self.assertEqual(
            exempting(
                "billing/Gone.java | renamed | 2026-05-20", "auth/Token.java | rename only | 2026-05-20"
            ).violations,
            [
                f"{EXEMPTIONS}: 'billing/Gone.java' is not a source file",
                f"{EXEMPTIONS}: 'auth/Token.java' is not in a critical zone",
            ],
        )

    def test_duplicate_exemption(self):
        self.assertEqual(
            exempting("billing/Engine.java | rename only | 2026-05-20", "billing/Engine.java | re-wrap | 2026-05-21")
            .violations,
            [f"{EXEMPTIONS}: duplicate entry for 'billing/Engine.java'"],
        )

    def test_malformed_exemptions(self):
        self.assertEqual(
            exempting(
                "billing/Engine.java | rename only",
                "billing/Engine.java | rename | only | 2026-05-20",
                " | no path | 2026-05-20",
                "billing/Engine.java |  | 2026-05-20",
                "billing/Engine.java | rename only | 2026-5-20",
                "billing/Ledger.java | rename only | 2026-02-30",
                "billing/*.java | rename only | 2026-05-20",
            ).violations,
            [
                malformed_exemption("billing/Engine.java | rename only"),
                malformed_exemption("billing/Engine.java | rename | only | 2026-05-20"),
                malformed_exemption("| no path | 2026-05-20"),
                malformed_exemption("billing/Engine.java |  | 2026-05-20"),
                f"{EXEMPTIONS}: 'billing/Engine.java' has an invalid date '2026-5-20' (YYYY-MM-DD)",
                f"{EXEMPTIONS}: 'billing/Ledger.java' has an invalid date '2026-02-30' (YYYY-MM-DD)",
                f"{EXEMPTIONS}: 'billing/*.java' must name one source file, without globs",
            ],
        )

    def test_year_zero_is_a_past_date(self):
        self.assertEqual(
            exempting("billing/Engine.java | rename only | 0000-02-29", "billing/Ledger.java | x | 0001-02-29")
            .violations,
            [
                f"{EXEMPTIONS}: 'billing/Engine.java' expired on 0000-02-29",
                f"{EXEMPTIONS}: 'billing/Ledger.java' has an invalid date '0001-02-29' (YYYY-MM-DD)",
            ],
        )

    def test_glob_character_after_a_line_separator_is_not_seen(self):
        self.assertEqual(
            exempting("billing/Engine.java\u2028* | rename only | 2026-05-20").violations,
            [f"{EXEMPTIONS}: 'billing/Engine.java\u2028*' is not a source file"],
        )


class HazardTest(unittest.TestCase):
    """Lines come from records(), trimming drops only characters up to U+0020, and the regular
    expressions are ASCII-only."""

    def lines(self, text: str) -> list[str]:
        return records(text)

    def test_unicode_digit_in_a_zone_header_is_no_header(self):
        self.assertEqual(
            check(["# Zone \u0661: billing.", "billing/**"], [], sources("billing/Engine.java")),
            [
                malformed_header("# Zone \u0661: billing."),
                f"{CRITICAL}: 'billing/**' comes before the first '# Zone <n>: ' header",
            ],
        )

    def test_no_break_space_is_not_trimmed(self):
        self.assertEqual(
            check(zoned("billing/**\xa0"), [], sources("billing/Engine.java")),
            [f"{CRITICAL}: '**' must be a whole last segment in 'billing/**\xa0'", unclassified("billing")],
        )
        self.assertEqual(trim("\xa0\t x \r\x00"), "\xa0\t x")

    def test_form_feed_and_line_separator_do_not_split_a_line(self):
        standard = self.lines("auth/*\x0ccodec/*\u2028queue/*\n")
        files = sources("billing/Engine.java", "auth/Token.java", "codec/KeyParser.java", "queue/Dispatcher.java")
        self.assertEqual(
            check(zoned("billing/**"), standard, files),
            [
                standard_form("auth/*\x0ccodec/*\u2028queue/*"),
                unclassified("auth"),
                unclassified("codec"),
                unclassified("queue"),
            ],
        )

    def test_lone_carriage_return_stays_in_the_line_and_fails_as_a_dead_glob(self):
        critical = self.lines("# Zone 1: billing.\nbilling/Engine.java\rauth/Token.java\n")
        self.assertEqual(
            check(critical, [], sources("billing/Engine.java", "auth/Token.java")),
            [
                f"{CRITICAL}: 'billing/Engine.java\rauth/Token.java' matches no source file",
                unclassified("auth"),
                unclassified("billing"),
            ],
        )

    def test_double_star_does_not_span_a_line_terminator(self):
        files = sources("billing/Engine.java", "billing/Odd\u2028Name.java")
        self.assertEqual(check(zoned("billing/**"), [], files), [unclassified("billing")])
        self.assertEqual(check([], ["billing/*"], files), [])

    def test_crlf_policy_files(self):
        result = run(
            self.lines("# Zone 1: billing.\r\nbilling/**\r\n"),
            self.lines("auth/*\r\n"),
            self.lines("billing/Engine.java | rename only | 2026-05-20\r\n"),
            sources("billing/Engine.java", "auth/Token.java"),
        )
        self.assertEqual(result.violations, [])
        self.assertEqual(result.zones, {f"{BASE}/auth/Token.java": "standard", f"{BASE}/billing/Engine.java": "Z1"})
        self.assertEqual(result.exempt_until, {f"{BASE}/billing/Engine.java": date(2026, 5, 20)})


class GrammarHelperTest(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(expand("billing/**"), ["billing/**"])
        self.assertEqual(expand("{auth,codec}/{x,y}/*"), ["auth/x/*", "auth/y/*", "codec/x/*", "codec/y/*"])
        self.assertEqual(expand("auth/{,core/}*"), ["auth/*", "auth/core/*"])
        for bad in ("auth/{x", "auth/x}", "auth/}x{", "auth/{x,{y}}", "{x}/{y"):
            with self.subTest(bad=bad):
                self.assertEqual(expand(bad), [])

    def test_many_groups_on_one_line(self):
        line = "billing/" + "{x}" * 1000 + "/**"
        self.assertEqual(expand(line), ["billing/" + "x" * 1000 + "/**"])
        dead = f"{CRITICAL}: '{expand(line)[0]}' (alternative of '{line}') matches no source file"
        self.assertEqual(check(zoned(line), ["auth/*"], sources("auth/Token.java")), [dead])

    def test_line_with_more_than_1024_alternatives(self):
        line = "{ab,cd}" * 30 + "/**"
        too_many = f"{CRITICAL}: more than 1024 alternatives in '{line}'"
        self.assertEqual(check(zoned(line, "auth/**"), [], sources("auth/Token.java")), [too_many])
        self.assertEqual(expand("{ab,cd}" * 30 + "/{x"), [])  # bad braces are still found
        self.assertEqual(len(expand("{ab,cd}" * 10 + "/**")), 1024)
        violations = check(zoned("{ab,cd}" * 10 + "/**", "auth/**"), [], sources("auth/Token.java"))
        self.assertEqual(len(violations), 1024)
        self.assertFalse(any("more than 1024" in violation for violation in violations))

    def test_glob_error(self):
        self.assertIsNone(glob_error("billing/**"))
        self.assertIsNone(glob_error("**"))
        self.assertEqual(glob_error("billing//x"), "empty path segment in 'billing//x'")
        self.assertEqual(glob_error("/billing"), "empty path segment in '/billing'")
        self.assertEqual(glob_error("**/billing"), "'**' must be a whole last segment in '**/billing'")
        self.assertEqual(glob_error("billing/x**"), "'**' must be a whole last segment in 'billing/x**'")

    def test_parse_date(self):
        self.assertEqual(parse_date("2028-02-29"), date(2028, 2, 29))
        for bad in ("2026-02-29", "2026-13-01", "26-01-01", "2026-01-01 ", "\u0662026-01-01", "20260101"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_date(bad))


class TargetTest(unittest.TestCase):
    def test_listed_files_with_their_own_unit_tests(self):
        self.assertEqual(
            targets_check(
                ["# comment", "", "  billing/Engine.java \r", "billing/ledger/Ledger_2.java"],
                ["billing/Engine.java", "billing/ledger/Ledger_2.java"],
                ["billing/EngineTest.java", "billing/ledger/Ledger_2Test.java"],
            ),
            [],
        )

    def test_malformed_line(self):
        lines = [
            "billing/*.java",
            "billing/Engine?.java",
            "billing/{Engine,Ledger}.java",
            "billing/[Ee]ngine.java",
            "billing/**",
            "Root.java",
            "billing/Engine.java # trailing comment",
            "billing\\Engine.java",
            "/billing/Engine.java",
            "billing/Engine.java/",
            "billing//Engine.java",
            "billing/./Engine.java",
            "billing/../Engine.java",
            "billing/Engine\xa0Fee.java",
        ]
        self.assertEqual(
            targets_check(lines, ["billing/Engine.java", "Root.java"], ["billing/EngineTest.java", "RootTest.java"]),
            [malformed_target(line) for line in lines],
        )

    def test_any_file_name_in_a_folder_is_well_formed(self):
        self.assertEqual(
            targets_check(["billing/package-info.java", "billing/Engine", "billing/engine.py"], [], []),
            [
                f"{TARGETS}: 'billing/package-info.java' is not a source file",
                f"{TARGETS}: 'billing/Engine' is not a source file",
                f"{TARGETS}: 'billing/engine.py' is not a source file",
            ],
        )

    def test_file_that_is_not_a_source_file(self):
        self.assertEqual(
            targets_check(
                ["billing/Gone.java", "Billing/Engine.java", "billing/billing/Engine.java"],
                ["billing/Engine.java"],
                ["billing/GoneTest.java", "Billing/EngineTest.java", "billing/billing/EngineTest.java"],
            ),
            [
                f"{TARGETS}: 'billing/Gone.java' is not a source file",
                f"{TARGETS}: 'Billing/Engine.java' is not a source file",
                f"{TARGETS}: 'billing/billing/Engine.java' is not a source file",
            ],
        )

    def test_duplicate(self):
        self.assertEqual(
            targets_check(
                ["billing/Engine.java", " billing/Engine.java"], ["billing/Engine.java"], ["billing/EngineTest.java"]
            ),
            [f"{TARGETS}: duplicate entry 'billing/Engine.java'"],
        )

    def test_list_without_entries(self):
        self.assertEqual(
            targets_check(["# only a comment", "", "  "], ["billing/Engine.java"], ["billing/EngineTest.java"]),
            [f"{TARGETS}: lists no file - it must list at least one file"],
        )

    def test_file_without_its_own_unit_test(self):
        self.assertEqual(
            targets_check(
                ["billing/Engine.java", "billing/Ledger.java", "billing/Report.java"],
                ["billing/Engine.java", "billing/Ledger.java", "billing/Report.java"],
                [
                    "auth/EngineTest.java",
                    "billing/LedgerIntegrationTest.java",
                    "billing/ReportTests.java",
                    "billing/report/ReportTest.java",
                ],
            ),
            [
                f"{TARGETS}: 'billing/Engine.java' has no own unit test 'org/example/app/billing/EngineTest.java'"
                " in src/test/java",
                f"{TARGETS}: 'billing/Ledger.java' has no own unit test 'org/example/app/billing/LedgerTest.java'"
                " in src/test/java",
                f"{TARGETS}: 'billing/Report.java' has no own unit test 'org/example/app/billing/ReportTest.java'"
                " in src/test/java",
            ],
        )

    def test_own_test_matching_a_forbidden_glob(self):
        self.assertEqual(
            targets_check(
                ["billing/LedgerIntegration.java"],
                ["billing/LedgerIntegration.java"],
                ["billing/LedgerIntegrationTest.java"],
            ),
            [
                f"{TARGETS}: 'billing/LedgerIntegration.java' has its own test named 'LedgerIntegrationTest.java',"
                " which matches own_test_forbidden '*IntegrationTest*'"
            ],
        )

    def test_first_forbidden_glob_is_named(self):
        self.assertEqual(
            targets_check(
                ["queue/Dispatcher.java"],
                ["queue/Dispatcher.java"],
                ["queue/DispatcherTest.java"],
                forbidden=["*IT.java", "Dispatcher*", "*Test*"],
            ),
            [
                f"{TARGETS}: 'queue/Dispatcher.java' has its own test named 'DispatcherTest.java',"
                " which matches own_test_forbidden 'Dispatcher*'"
            ],
        )

    def test_own_test_template(self):
        self.assertEqual(
            targets_check(["codec/key_parser.py"], ["codec/key_parser.py"], ["codec/test_key_parser.py"],
                          own_test="test_{stem}{ext}"),
            [],
        )
        self.assertEqual(
            targets_check(["codec/Makefile"], ["codec/Makefile"], ["codec/MakefileTest"]),
            [],
        )

    def test_own_test_under_any_test_root(self):
        self.assertEqual(
            targets_check(
                ["codec/KeyParser.java"],
                ["codec/KeyParser.java"],
                ["codec/KeyParserTest.java"],
                test_roots=["src/fixtures/java", "src/test/java"],
            ),
            [],
        )
        self.assertEqual(
            targets_check(
                ["codec/KeyParser.java"],
                ["codec/KeyParser.java"],
                [],
                test_roots=["src/fixtures/java", "src/test/java"],
            ),
            [
                f"{TARGETS}: 'codec/KeyParser.java' has no own unit test 'org/example/app/codec/KeyParserTest.java'"
                " in src/fixtures/java or src/test/java"
            ],
        )

    def test_own_test_folder_is_relative_to_the_source_root(self):
        self.assertEqual(
            targets_check(
                ["codec/KeyParser.java"],
                ["codec/KeyParser.java"],
                [],
                roots=["src/main/java/org/example"],
            ),
            [f"{TARGETS}: 'codec/KeyParser.java' has no own unit test 'app/codec/KeyParserTest.java' in src/test/java"],
        )

    def test_no_test_roots(self):
        self.assertEqual(
            targets_check(["codec/KeyParser.java"], ["codec/KeyParser.java"], [], test_roots=[]),
            [
                f"{TARGETS}: 'codec/KeyParser.java' has no own unit test 'org/example/app/codec/KeyParserTest.java'"
                " in no folder ([source].tests is not set)"
            ],
        )


if __name__ == "__main__":
    unittest.main()
