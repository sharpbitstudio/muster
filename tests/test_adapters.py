"""Tests of muster.adapters: the source resolver and the JaCoCo, PIT, JUnit and timing adapters.

Every tree and every report is built in a temporary folder. Non-ASCII and control characters are
built at run time with chr().
"""

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster import config, policy
from muster.adapters import SourceResolver, exists, jacoco, junit, pit, strict_json, timing
from muster.config import ConfigError
from muster.report import ReportError

MAIN = "src/main/java"
EXTRA = "gen/main/java"
ENGINE = "org/example/app/billing/internal/ledger/Engine.java"
TOKEN = "org/example/app/auth/Token.java"
PARSER = "org/example/app/codec/KeyParser.java"
DISPATCHER = "org/example/app/queue/Dispatcher.java"
UTC = dt.timezone.utc


def jacoco_xml(body: str, sessions: str = '<sessioninfo id="localhost-1a2b" start="1000" dump="5000"/>') -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<report name="app">{sessions}{body}</report>\n'


def sourcefile(name: str, inner: str = "") -> str:
    return f'<sourcefile name="{name}">{inner}</sourcefile>'


def package(name: str, *files: str) -> str:
    return f'<package name="{name}">{"".join(files)}</package>'


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))

    def make(self, *paths: str) -> None:
        for path in paths:
            self.write(path, "")

    def write(self, path: str, content: str | bytes) -> Path:
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            file.write_bytes(content)
        else:
            file.write_text(content, encoding="utf-8", newline="\n")
        return file

    def resolver(self, roots: list[str], classified: list[str] | None = None) -> SourceResolver:
        return SourceResolver(self.root, roots, frozenset(classified or []))

    def assertNamedOnly(self, message: str, name: str) -> None:
        """The message names the file by the given name, not by its absolute path."""
        self.assertIn(name, message)
        self.assertNotIn(self.root.as_posix(), message)
        self.assertNotIn(str(self.root), message)


class SourceResolverTest(AdapterCase):
    def test_one_root(self):
        self.make(f"{MAIN}/{TOKEN}", f"{MAIN}/{ENGINE}")
        resolver = self.resolver([MAIN])
        self.assertEqual(resolver.resolve(TOKEN), [f"{MAIN}/{TOKEN}"])
        self.assertEqual(resolver.resolve(ENGINE), [f"{MAIN}/{ENGINE}"])

    def test_two_roots(self):
        self.make(f"{MAIN}/{TOKEN}", f"{EXTRA}/{PARSER}")
        resolver = self.resolver([MAIN, EXTRA])
        self.assertEqual(resolver.resolve(TOKEN), [f"{MAIN}/{TOKEN}"])
        self.assertEqual(resolver.resolve(PARSER), [f"{EXTRA}/{PARSER}"])

    def test_name_under_two_roots(self):
        self.make(f"{MAIN}/{TOKEN}", f"{EXTRA}/{TOKEN}")
        resolver = self.resolver([MAIN, EXTRA])
        self.assertEqual(resolver.resolve(TOKEN), [f"{EXTRA}/{TOKEN}", f"{MAIN}/{TOKEN}"])

    def test_the_same_root_twice_is_one_path(self):
        self.make(f"{MAIN}/{TOKEN}")
        self.assertEqual(self.resolver([MAIN, f"./{MAIN}/"]).resolve(TOKEN), [f"{MAIN}/{TOKEN}"])

    def test_name_under_no_root(self):
        self.make(f"{MAIN}/{TOKEN}")
        self.assertEqual(self.resolver([MAIN]).resolve(DISPATCHER), [])

    def test_exact_case(self):
        self.make(f"{MAIN}/{TOKEN}")
        resolver = self.resolver([MAIN])
        self.assertEqual(resolver.resolve(TOKEN.replace("Token", "token")), [])
        self.assertEqual(resolver.resolve(TOKEN.replace("auth", "Auth")), [])

    def test_given_files_are_not_walked_again(self):
        # Nothing is on disk: the walk the policy check made is used as it is.
        resolver = SourceResolver(self.root, [MAIN], frozenset(), [f"{MAIN}/{TOKEN}"])
        self.assertEqual(resolver.resolve(TOKEN), [f"{MAIN}/{TOKEN}"])
        self.assertEqual(resolver.resolve(ENGINE), [])

    def test_missing_root_is_a_config_error(self):
        self.make(f"{MAIN}/{TOKEN}")
        with self.assertRaises(ConfigError) as caught:
            self.resolver([MAIN, EXTRA])
        self.assertIn(EXTRA, str(caught.exception))


class JacocoLoadTest(AdapterCase):
    def setUp(self):
        super().setUp()
        self.make(f"{MAIN}/{ENGINE}", f"{MAIN}/{TOKEN}", f"{MAIN}/Launcher.java", f"{MAIN}/{PARSER}")
        self.classified = [f"{MAIN}/{ENGINE}", f"{MAIN}/{TOKEN}", f"{MAIN}/Launcher.java"]

    def load(self, xml: str, roots: list[str] | None = None) -> jacoco.CoverageReport:
        path = self.write("build/reports/jacoco.xml", xml)
        return jacoco.load(path, self.resolver(roots or [MAIN], self.classified))

    def test_valid_report(self):
        engine = sourcefile(
            "Engine.java",
            '<line nr="3" mi="0" ci="4" mb="0" cb="0"/>'
            '<line nr="7" mi="2" ci="1" mb="1" cb="1"/>'
            '<counter type="INSTRUCTION" missed="2" covered="5"/>'
            '<counter type="LINE" missed="1" covered="1"/>'
            '<counter type="BRANCH" missed="1" covered="1"/>',
        )
        token = sourcefile("Token.java", '<line nr="1" mi="1" ci="0" mb="0" cb="0"/>')
        launcher = sourcefile("Launcher.java", '<counter type="LINE" missed="0" covered="2"/>')
        sessions = (
            '<sessioninfo id="localhost-1a2b" start="1000" dump="5000"/>'
            '<sessioninfo id="localhost-3c4d" start="6000" dump="9000"/>'
        )
        body = (
            '<group name="app">'
            + package("org/example/app/billing/internal/ledger", engine)
            + "</group>"
            + package("org/example/app/auth", token)
            + package("", launcher)
            + package("org/example/app/codec", sourcefile("KeyParser.java"))
            + package("org/example/app/queue", sourcefile("Dispatcher.java"))
        )
        report = self.load(jacoco_xml(body, sessions))
        self.assertEqual(report.sessions, [(1000, 5000), (6000, 9000)])
        self.assertEqual(report.sourcefiles, 5)
        self.assertEqual(report.unresolved, [DISPATCHER])
        self.assertEqual(report.excluded, [f"{MAIN}/{PARSER}"])
        self.assertEqual(sorted(report.files), [f"{MAIN}/Launcher.java", f"{MAIN}/{TOKEN}", f"{MAIN}/{ENGINE}"])
        self.assertEqual(
            report.files[f"{MAIN}/{ENGINE}"],
            jacoco.FileCoverage({3: (0, 4, 0, 0), 7: (2, 1, 1, 1)}, (1, 1), (1, 1)),
        )
        self.assertEqual(report.files[f"{MAIN}/{TOKEN}"], jacoco.FileCoverage({1: (1, 0, 0, 0)}, (0, 0), (0, 0)))
        self.assertEqual(report.files[f"{MAIN}/Launcher.java"], jacoco.FileCoverage({}, (0, 2), (0, 0)))

    def test_report_with_the_doctype_jacoco_writes(self):
        doctype = '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
        token = sourcefile("Token.java", '<line nr="1" mi="0" ci="2" mb="0" cb="0"/>')
        xml = jacoco_xml(package("org/example/app/auth", token)).replace("\n<report", "\n" + doctype + "\n<report", 1)
        self.assertIn(doctype, xml)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("the DTD is fetched")):
            report = self.load(xml)  # report.dtd is not there and is not looked for
        self.assertEqual(report.files[f"{MAIN}/{TOKEN}"].lines, {1: (0, 2, 0, 0)})

    def test_counters_are_kept_as_given(self):
        # The LINE counter says 5 missed, 9 covered; the single <line> says otherwise. Both are kept.
        token = sourcefile(
            "Token.java",
            '<line nr="4" mi="3" ci="0" mb="2" cb="0"/>'
            '<counter type="LINE" missed="5" covered="9"/>'
            '<counter type="BRANCH" missed="0" covered="6"/>',
        )
        coverage = self.load(jacoco_xml(package("org/example/app/auth", token))).files[f"{MAIN}/{TOKEN}"]
        self.assertEqual(coverage.lines, {4: (3, 0, 2, 0)})
        self.assertEqual(coverage.line, (5, 9))
        self.assertEqual(coverage.branch, (0, 6))

    def test_report_without_sourcefiles(self):
        report = self.load(jacoco_xml(package("org/example/app/auth")))
        self.assertEqual(report.sourcefiles, 0)
        self.assertEqual(report.files, {})
        self.assertEqual((report.unresolved, report.excluded), ([], []))
        self.assertEqual(report.sessions, [(1000, 5000)])

    def test_name_under_two_roots_is_a_config_error(self):
        self.make(f"{EXTRA}/{TOKEN}")
        with self.assertRaises(ConfigError) as caught:
            self.load(jacoco_xml(package("org/example/app/auth", sourcefile("Token.java"))), [MAIN, EXTRA])
        message = str(caught.exception)
        for part in (TOKEN, f"{MAIN}/{TOKEN}", f"{EXTRA}/{TOKEN}"):
            self.assertIn(part, message)

    def test_two_names_found_as_one_file_is_a_config_error(self):
        nested = f"{MAIN}/org"
        outer = package("org/example/app/auth", sourcefile("Token.java"))
        inner = package("example/app/auth", sourcefile("Token.java"))
        for body, names in ((outer + inner, (TOKEN, "example/app/auth/Token.java")), (inner + outer, None)):
            with self.subTest(first=body[:40]):
                with self.assertRaises(ConfigError) as caught:
                    self.load(jacoco_xml(body), [MAIN, nested])
                message = str(caught.exception)
                for part in (TOKEN, "example/app/auth/Token.java", f"{MAIN}/{TOKEN}"):
                    self.assertIn(part, message)
                if names is not None:
                    self.assertIn(f"{names[0]} and {names[1]} are both {MAIN}/{TOKEN}", message)

    def test_name_in_another_case_is_unresolved(self):
        report = self.load(jacoco_xml(package("org/example/app/auth", sourcefile("token.java"))))
        self.assertEqual(report.unresolved, ["org/example/app/auth/token.java"])
        self.assertEqual(report.files, {})


class JacocoBrokenTest(AdapterCase):
    def test_broken_inputs(self):
        self.make(f"{MAIN}/{TOKEN}")
        resolver = self.resolver([MAIN], [f"{MAIN}/{TOKEN}"])

        def token(inner: str) -> str:
            return jacoco_xml(package("org/example/app/auth", sourcefile("Token.java", inner)))

        def named(folder: str) -> str:
            return jacoco_xml(package(folder, sourcefile("Token.java")))

        cases = {
            "not XML": "<report><package",
            "empty file": "",
            "not a report": '<testsuite name="app"/>',
            "unknown encoding": '<?xml version="1.0" encoding="no-such-codec"?><report/>',
            "counter missed -1": token('<counter type="LINE" missed="-1" covered="0"/>'),
            "counter covered 1.5": token('<counter type="LINE" missed="0" covered="1.5"/>'),
            "counter covered +3": token('<counter type="BRANCH" missed="0" covered="+3"/>'),
            "counter covered with spaces": token('<counter type="LINE" missed="0" covered=" 3"/>'),
            "counter covered missing": token('<counter type="LINE" missed="0"/>'),
            "counter twice": token('<counter type="LINE" missed="0" covered="1"/>' * 2),
            "line nr missing": token('<line mi="0" ci="1" mb="0" cb="0"/>'),
            "line ci non-ASCII digit": token(f'<line nr="1" mi="0" ci="{chr(0x0663)}" mb="0" cb="0"/>'),
            "line twice": token('<line nr="1" mi="0" ci="1" mb="0" cb="0"/>' * 2),
            "counter covered past a long": token(f'<counter type="LINE" missed="0" covered="{2**63}"/>'),
            "counter of 400 digits": token('<counter type="LINE" missed="0" covered="1' + "0" * 400 + '"/>'),
            "line cb of 4301 digits": token('<line nr="1" mi="0" ci="1" mb="0" cb="' + "9" * 4301 + '"/>'),
            "line nr of 5000 digits": token('<line nr="' + "7" * 5000 + '" mi="0" ci="1" mb="0" cb="0"/>'),
            "counter without type": token('<counter missed="0" covered="1"/>'),
            "INSTRUCTION counter twice": token('<counter type="INSTRUCTION" missed="0" covered="1"/>' * 2),
            "METHOD counter missed -1": token('<counter type="METHOD" missed="-1" covered="1"/>'),
            "report counter missed -1": jacoco_xml(
                package("org/example/app/auth", sourcefile("Token.java"))
                + '<counter type="LINE" missed="-1" covered="1"/>'
            ),
            "group counter without type": jacoco_xml(
                '<group name="more">'
                + package("org/example/app/auth", sourcefile("Token.java"))
                + '<counter missed="0" covered="1"/></group>'
            ),
            "package counter twice": jacoco_xml(
                package(
                    "org/example/app/auth",
                    sourcefile("Token.java"),
                    '<counter type="LINE" missed="0" covered="1"/>' * 2,
                )
            ),
            "class counter covered x": jacoco_xml(
                package(
                    "org/example/app/auth",
                    '<class name="org/example/app/auth/Token"><counter type="LINE" missed="0" covered="x"/></class>',
                )
            ),
            "method counter of 400 digits": jacoco_xml(
                package(
                    "org/example/app/auth",
                    '<class name="org/example/app/auth/Token"><method name="run" desc="()V">'
                    '<counter type="METHOD" missed="0" covered="1' + "0" * 400 + '"/></method></class>',
                )
            ),
            "counter of a sourcefile outside a package": jacoco_xml(
                sourcefile("Token.java", '<counter type="LINE" missed="-1" covered="0"/>')
            ),
            "session start missing": jacoco_xml("", '<sessioninfo id="localhost-1a2b" dump="5000"/>'),
            "session dump x": jacoco_xml("", '<sessioninfo id="localhost-1a2b" start="1000" dump="x"/>'),
            "session start 1_0": jacoco_xml("", '<sessioninfo id="localhost-1a2b" start="1_0" dump="20"/>'),
            "session start below a long": jacoco_xml("", f'<sessioninfo start="{-(2**63) - 1}" dump="0"/>'),
            "session dump past a long": jacoco_xml("", f'<sessioninfo start="0" dump="{2**63}"/>'),
            "session dump of 4301 digits": jacoco_xml("", '<sessioninfo start="0" dump="' + "9" * 4301 + '"/>'),
            "session dump before start": jacoco_xml("", '<sessioninfo start="7000" dump="-1000"/>'),
            "package name missing": jacoco_xml(f"<package>{sourcefile('Token.java')}</package>"),
            "package name missing without source files": jacoco_xml(
                package("org/example/app/auth", sourcefile("Token.java")) + "<package/>"
            ),
            "package name missing with only classes": jacoco_xml(
                '<package><class name="org/example/app/auth/Token"/></package>'
            ),
            "counter with an empty type": token('<counter type="" missed="0" covered="1"/>'),
            "report counter with an empty type": jacoco_xml(
                package("org/example/app/auth", sourcefile("Token.java")) + '<counter type="" missed="0" covered="1"/>'
            ),
            "package counter with an empty type": jacoco_xml(
                package("org/example/app/auth", sourcefile("Token.java"), '<counter type="" missed="0" covered="1"/>')
            ),
            "package empty segment": named("org//example/app/auth"),
            "package dot segment": named("org/./example/app/auth"),
            "package dotdot segment": named("org/../example/app/auth"),
            "package trailing slash": named("org/example/app/auth/"),
            "package backslash": named("org\\example\\app\\auth"),
            "package leading slash": named("/org/example/app/auth"),
            "package NUL reference": named("org/example&#0;/app/auth"),
            "package raw NUL": named(f"org/example{chr(0)}/app/auth"),
            "sourcefile name missing": jacoco_xml(package("org/example/app/auth", "<sourcefile/>")),
            "sourcefile empty": jacoco_xml(package("org/example/app/auth", sourcefile(""))),
            "sourcefile with slash": jacoco_xml(package("org/example/app", sourcefile("auth/Token.java"))),
            "sourcefile dotdot": jacoco_xml(package("org/example/app/auth", sourcefile(".."))),
            "sourcefile dot": jacoco_xml(package("org/example/app/auth", sourcefile("."))),
            "sourcefile backslash": jacoco_xml(package("org/example/app", sourcefile("auth\\Token.java"))),
            "the same name twice": jacoco_xml(
                package("org/example/app/auth", sourcefile("Token.java"))
                + '<group name="more">'
                + package("org/example/app/auth", sourcefile("Token.java"))
                + "</group>"
            ),
        }
        for case, content in cases.items():
            with self.subTest(case):
                path = self.write("build/reports/jacoco.xml", content)
                with self.assertRaises(ReportError) as caught:
                    jacoco.load(path, resolver)
                self.assertIn(path.as_posix(), str(caught.exception))

    def test_values_at_the_bounds_of_a_long(self):
        self.make(f"{MAIN}/{TOKEN}")
        most = 2**63 - 1
        inner = (
            f'<line nr="{most}" mi="0" ci="{most}" mb="0" cb="0"/>'
            f'<counter type="LINE" missed="0" covered="{most}"/>'
            '<counter type="BRANCH" missed="' + "0" * 5000 + '1" covered="0"/>'
        )
        sessions = f'<sessioninfo start="{-(2**63)}" dump="{most}"/>'
        body = package("org/example/app/auth", sourcefile("Token.java", inner))
        path = self.write("jacoco.xml", jacoco_xml(body, sessions))
        report = jacoco.load(path, self.resolver([MAIN], [f"{MAIN}/{TOKEN}"]))
        self.assertEqual(report.sessions, [(-(2**63), most)])
        coverage = report.files[f"{MAIN}/{TOKEN}"]
        self.assertEqual((coverage.lines, coverage.line, coverage.branch), ({most: (0, most, 0, 0)}, (0, most), (1, 0)))

    def test_a_folder_instead_of_a_file(self):
        folder = self.root / "build" / "jacoco.xml"
        folder.mkdir(parents=True)
        (self.root / MAIN).mkdir(parents=True)
        with self.assertRaises(ReportError) as caught:
            jacoco.load(folder, self.resolver([MAIN]))
        self.assertIn(folder.as_posix(), str(caught.exception))

    def test_messages_name_the_attribute(self):
        (self.root / MAIN).mkdir(parents=True)
        token = sourcefile("Token.java", '<line nr="1" mi="0" ci="1" mb="0"/>')
        path = self.write("jacoco.xml", jacoco_xml(package("org/example/app/auth", token)))
        with self.assertRaises(ReportError) as caught:
            jacoco.load(path, self.resolver([MAIN]))
        self.assertIn("cb", str(caught.exception))

    def test_given_name_replaces_the_path(self):
        (self.root / MAIN).mkdir(parents=True)
        path = self.write("build/reports/jacoco.xml", jacoco_xml(package("org/example/app/auth", sourcefile(".."))))
        with self.assertRaises(ReportError) as caught:
            jacoco.load(path, self.resolver([MAIN]), name="build/reports/jacoco.xml")
        self.assertNamedOnly(str(caught.exception), "build/reports/jacoco.xml")


class JacocoSessionsTest(AdapterCase):
    def test_valid(self):
        sessions = (
            '<sessioninfo id="localhost-1a2b" start="1700000000000" dump="1700000060000"/>'
            '<sessioninfo id="localhost-3c4d" start="-5" dump="0"/>'
        )
        path = self.write("jacoco.xml", jacoco_xml(package("org/example/app/auth", sourcefile("Token.java")), sessions))
        self.assertEqual(jacoco.read_sessions(path), [(1700000000000, 1700000060000), (-5, 0)])

    def test_no_sessions(self):
        self.assertEqual(jacoco.read_sessions(self.write("jacoco.xml", jacoco_xml("", ""))), [])

    def test_sources_are_not_resolved(self):
        path = self.write("jacoco.xml", jacoco_xml(package("org/example/app/auth", sourcefile("Token.java"))))
        self.assertEqual(jacoco.read_sessions(path), [(1000, 5000)])  # no source root exists

    def test_whole_report_is_checked(self):
        for case, inner in (
            ("counter missed -1", '<counter type="LINE" missed="-1" covered="0"/>'),
            ("counter without type", '<counter missed="0" covered="1"/>'),
            ("line twice", '<line nr="1" mi="0" ci="1" mb="0" cb="0"/>' * 2),
        ):
            with self.subTest(case):
                body = package("org/example/app/auth", sourcefile("Token.java", inner))
                path = self.write("jacoco.xml", jacoco_xml(body))
                with self.assertRaises(ReportError) as caught:
                    jacoco.read_sessions(path)
                self.assertIn(path.as_posix(), str(caught.exception))
        path = self.write("jacoco.xml", jacoco_xml(package("org/example/app", sourcefile("auth/Token.java"))))
        with self.assertRaises(ReportError):
            jacoco.read_sessions(path)
        counter = '<counter type="LINE" missed="-1" covered="1"/>'  # of a package, outside every source file
        path = self.write("jacoco.xml", jacoco_xml(package("org/example/app/auth", sourcefile("Token.java"), counter)))
        with self.assertRaises(ReportError) as caught:
            jacoco.read_sessions(path)
        self.assertIn("<counter> attribute missed", str(caught.exception))

    def test_inverted_session(self):
        path = self.write("jacoco.xml", jacoco_xml("", '<sessioninfo id="localhost-1a2b" start="7000" dump="-1000"/>'))
        with self.assertRaises(ReportError) as caught:
            jacoco.read_sessions(path)
        self.assertIn("dump -1000 is before start 7000", str(caught.exception))

    def test_missing_attribute(self):
        path = self.write("jacoco.xml", jacoco_xml("", '<sessioninfo id="localhost-1a2b" start="1000"/>'))
        with self.assertRaises(ReportError) as caught:
            jacoco.read_sessions(path)
        self.assertIn(path.as_posix(), str(caught.exception))
        self.assertIn("dump", str(caught.exception))

    def test_not_xml(self):
        path = self.write("jacoco.xml", "<report")
        with self.assertRaises(ReportError) as caught:
            jacoco.read_sessions(path)
        self.assertIn(path.as_posix(), str(caught.exception))

    def test_given_name_replaces_the_path(self):
        path = self.write("build/reports/jacoco.xml", jacoco_xml("", '<sessioninfo id="localhost-1a2b" dump="1"/>'))
        with self.assertRaises(ReportError) as caught:
            jacoco.read_sessions(path, name="build/reports/jacoco.xml")
        self.assertNamedOnly(str(caught.exception), "build/reports/jacoco.xml")


MATH = "org.pitest.mutationtest.engine.gregor.mutators.MathMutator"
NEGATE = "org.pitest.mutationtest.engine.gregor.mutators.NegateConditionalsMutator"
LEDGER = "org.example.app.billing.internal.ledger"
# The elements of a killed mutant in Token.java, in the order PIT writes them; a test overrides one
# by its name, and None leaves it out.
TOKEN_MUTATION = {
    "sourceFile": "Token.java",
    "mutatedClass": "org.example.app.auth.Token",
    "mutatedMethod": "expires",
    "methodDescription": "(J)Z",
    "lineNumber": "12",
    "mutator": MATH,
    "indexes": "<index>4</index>",
    "blocks": "<block>1</block>",
    "killingTest": "org.example.app.auth.TokenTest.[method:expiresAfterTheDeadline()]",
    "description": "Replaced long subtraction with addition",
}


def mutation(status: str | None = "KILLED", detected: str | None = "true", more: str = "", **elements) -> str:
    attributes = ' numberOfTestsRun="3"'
    if detected is not None:
        attributes = f' detected="{detected}"' + attributes
    if status is not None:
        attributes += f' status="{status}"'
    fields = dict(TOKEN_MUTATION, **elements)
    body = "".join(f"<{tag}>{text}</{tag}>" for tag, text in fields.items() if text is not None)
    return f"<mutation{attributes}>{body}{more}</mutation>"


def mutations_xml(*mutations: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<mutations partial="false">{"".join(mutations)}</mutations>\n'


def in_ledger(cls: str, source: str = "Engine.java", **fields) -> str:
    return mutation(mutatedClass=f"{LEDGER}.{cls}", sourceFile=source, **fields)


class PitLoadTest(AdapterCase):
    def setUp(self):
        super().setUp()
        self.make(f"{MAIN}/{ENGINE}", f"{MAIN}/{TOKEN}", f"{MAIN}/Launcher.java", f"{MAIN}/{PARSER}")
        self.classified = [f"{MAIN}/{ENGINE}", f"{MAIN}/{TOKEN}", f"{MAIN}/Launcher.java"]

    def load(self, *mutations: str, roots: list[str] | None = None) -> pit.MutationReport:
        path = self.write("build/reports/pit/mutations.xml", mutations_xml(*mutations))
        return pit.load(path, self.resolver(roots or [MAIN], self.classified))

    def test_killed_mutant_has_every_field(self):
        [mutant] = self.load(mutation()).mutants
        self.assertEqual(
            mutant,
            pit.Mutant(
                id=("org.example.app.auth.Token", "expires", "(J)Z", 12, MATH, (4,)),
                file=f"{MAIN}/{TOKEN}",
                line=12,
                mutator="MathMutator",
                status_raw="KILLED",
                status="killed",
                detected=True,
                killing_tests=["org.example.app.auth.TokenTest.[method:expiresAfterTheDeadline()]"],
                description="Replaced long subtraction with addition",
                meta={
                    "class": "org.example.app.auth.Token",
                    "method": "expires",
                    "descriptor": "(J)Z",
                    "indexes": (4,),
                    "mutator_full": MATH,
                    "report_name": TOKEN,
                },
            ),
        )

    def test_descriptor_indexes_and_killing_test_land_in_id_meta_and_killing_tests(self):
        given = mutation(
            methodDescription="(Ljava/lang/String;I)J",
            indexes="<index>7</index><index>0</index><index>19</index>",
            killingTest="org.example.app.auth.TokenTest.[method:renews()]",
        )
        empty = mutation(status="SURVIVED", detected="false", killingTest="", indexes="")
        self_closed = mutation(status="SURVIVED", detected="false", indexes=None).replace(
            "<killingTest>org.example.app.auth.TokenTest.[method:expiresAfterTheDeadline()]</killingTest>",
            "<killingTest/>",
        )
        absent = mutation(
            status="NO_COVERAGE",
            detected="false",
            mutatedMethod=None,
            methodDescription=None,
            indexes=None,
            killingTest=None,
            description=None,
        )
        self.assertIn("<killingTest/>", self_closed)
        report = self.load(given, empty, self_closed, absent)
        first, second, third, fourth = report.mutants
        descriptor = "(Ljava/lang/String;I)J"
        self.assertEqual(first.id, ("org.example.app.auth.Token", "expires", descriptor, 12, MATH, (7, 0, 19)))
        self.assertEqual((first.meta["descriptor"], first.meta["indexes"]), (descriptor, (7, 0, 19)))
        self.assertEqual(first.killing_tests, ["org.example.app.auth.TokenTest.[method:renews()]"])
        for mutant in (second, third):
            self.assertEqual((mutant.killing_tests, mutant.meta["indexes"], mutant.id[5]), ([], (), ()))
        self.assertEqual(fourth.id, ("org.example.app.auth.Token", "", "", 12, MATH, ()))
        self.assertEqual((fourth.meta["method"], fourth.meta["descriptor"], fourth.meta["indexes"]), ("", "", ()))
        self.assertEqual((fourth.killing_tests, fourth.description), ([], ""))

    def test_statuses_are_normalized_and_detected_is_read_as_given(self):
        cases = (
            ("KILLED", "true", "killed"),
            ("SURVIVED", "false", "survived"),
            ("NO_COVERAGE", "false", "no-coverage"),
            ("TIMED_OUT", "true", "other"),
            ("MEMORY_ERROR", "true", "other"),
            ("RUN_ERROR", "false", "other"),
            ("NON_VIABLE", "false", "other"),
            ("killed", "false", "other"),
            ("", "true", "other"),
            (None, "false", "other"),
        )
        report = self.load(*(mutation(status=status, detected=detected) for status, detected, _ in cases))
        for (status, detected, normalized), mutant in zip(cases, report.mutants, strict=True):
            with self.subTest(status=status):
                self.assertEqual(mutant.status_raw, status or "?")
                self.assertEqual(mutant.status, normalized)
                self.assertIs(mutant.detected, detected == "true")

    def test_numbers_without_white_space_at_their_ends_and_at_the_bounds_of_a_long(self):
        most = 2**63 - 1
        report = self.load(
            mutation(lineNumber=f" \t{most}\n", indexes="<index>\n  " + "0" * 5000 + "3 </index><index>0</index>")
        )
        [mutant] = report.mutants
        self.assertEqual((mutant.line, mutant.meta["indexes"]), (most, (3, 0)))

    def test_numbers_share_the_rule_of_the_jacoco_adapter(self):
        self.assertIs(pit.long_number, jacoco.long_number)

    def test_description_and_killing_test_are_kept_as_given(self):
        tab, delete, accented = chr(9), chr(0x7F), chr(0xE9)
        description = f"Replaced{tab}long subtraction{delete} with addition {accented}"
        killing = f"org.example.app.auth.TokenTest.[method:{accented}{tab}renews()]"
        [mutant] = self.load(mutation(description=description, killingTest=killing)).mutants
        self.assertEqual((mutant.description, mutant.killing_tests), (description, [killing]))

    def test_unread_attributes_and_elements_are_ignored(self):
        more = "<blocks><block>x</block></blocks><succeedingTests/><succeedingTests/><index>x</index>"
        broken = mutation(more=more).replace('numberOfTestsRun="3"', 'numberOfTestsRun="many"')
        xml = mutations_xml(broken).replace('partial="false"', 'partial="maybe"')
        path = self.write("build/reports/pit/mutations.xml", xml)
        [mutant] = pit.load(path, self.resolver([MAIN], self.classified)).mutants
        self.assertEqual(mutant.meta["indexes"], (4,))

    def test_the_same_mutant_twice_is_two_mutants(self):
        report = self.load(mutation(), mutation())
        self.assertEqual(len(report.mutants), 2)
        self.assertEqual(report.mutants[0], report.mutants[1])

    def test_name_under_no_root_is_unresolved(self):
        report = self.load(
            mutation(mutatedClass="org.example.app.queue.Dispatcher", sourceFile="Dispatcher.java"),
            mutation(),
            mutation(mutatedClass="org.example.app.queue.Dispatcher", sourceFile="Dispatcher.java", lineNumber="40"),
            mutation(mutatedClass="org.example.app.auth.Session", sourceFile="Session.java"),
        )
        self.assertEqual(report.unresolved, ["org/example/app/auth/Session.java", DISPATCHER])
        self.assertEqual([mutant.file for mutant in report.mutants], [None, f"{MAIN}/{TOKEN}", None, None])
        self.assertEqual(report.mutants[0].meta["report_name"], DISPATCHER)

    def test_one_hit_outside_the_classified_set_is_the_file(self):
        report = self.load(mutation(mutatedClass="org.example.app.codec.KeyParser", sourceFile="KeyParser.java"))
        self.assertNotIn(f"{MAIN}/{PARSER}", self.classified)
        self.assertEqual(report.mutants[0].file, f"{MAIN}/{PARSER}")
        self.assertEqual(report.unresolved, [])

    def test_name_in_another_case_is_unresolved(self):
        report = self.load(mutation(sourceFile="token.java"))
        self.assertEqual((report.mutants[0].file, report.unresolved), (None, ["org/example/app/auth/token.java"]))

    def test_name_under_two_roots_is_a_config_error(self):
        self.make(f"{EXTRA}/{TOKEN}")
        with self.assertRaises(ConfigError) as caught:
            self.load(mutation(), roots=[MAIN, EXTRA])
        message = str(caught.exception)
        for part in (TOKEN, f"{MAIN}/{TOKEN}", f"{EXTRA}/{TOKEN}"):
            self.assertIn(part, message)

    def test_two_names_found_as_one_file_is_a_config_error(self):
        nested = f"{MAIN}/org"
        inner = mutation(mutatedClass="example.app.auth.Token")
        orders = ((mutation(), inner, (TOKEN, "example/app/auth/Token.java")), (inner, mutation(), None))
        for first, second, names in orders:
            with self.subTest(first=first[:80]):
                with self.assertRaises(ConfigError) as caught:
                    self.load(first, second, roots=[MAIN, nested])
                message = str(caught.exception)
                for part in (TOKEN, "example/app/auth/Token.java", f"{MAIN}/{TOKEN}"):
                    self.assertIn(part, message)
                if names is not None:
                    self.assertIn(f"{names[0]} and {names[1]} are both {MAIN}/{TOKEN}", message)

    def test_nested_class_and_secondary_type_belong_to_the_file_of_the_source(self):
        report = self.load(
            in_ledger("Engine"),
            in_ledger("Engine$Cursor", lineNumber="31"),
            in_ledger("Engine$Cursor$1", lineNumber="33"),
            in_ledger("Journal", lineNumber="88"),
        )
        self.assertEqual([mutant.file for mutant in report.mutants], [f"{MAIN}/{ENGINE}"] * 4)
        self.assertEqual({mutant.meta["report_name"] for mutant in report.mutants}, {ENGINE})
        self.assertEqual(
            [mutant.meta["class"] for mutant in report.mutants],
            [f"{LEDGER}.Engine", f"{LEDGER}.Engine$Cursor", f"{LEDGER}.Engine$Cursor$1", f"{LEDGER}.Journal"],
        )
        self.assertEqual(report.unresolved, [])

    def test_default_package(self):
        report = self.load(
            mutation(mutatedClass="Launcher", sourceFile="Launcher.java"),
            mutation(mutatedClass="Launcher$Options", sourceFile="Launcher.java"),
            mutation(mutatedClass="Banner", sourceFile="Banner.java"),
        )
        names = [mutant.meta["report_name"] for mutant in report.mutants]
        self.assertEqual(names, ["Launcher.java"] * 2 + ["Banner.java"])
        self.assertEqual([mutant.file for mutant in report.mutants], [f"{MAIN}/Launcher.java"] * 2 + [None])
        self.assertEqual(report.unresolved, ["Banner.java"])

    def test_unresolved_name_with_the_text_of_a_resolved_path(self):
        # The second name is the path the first resolves to; under the root it is no file.
        report = self.load(mutation(), mutation(mutatedClass="src.main.java.org.example.app.auth.Token"))
        self.assertEqual(report.mutants[1].meta["report_name"], f"{MAIN}/{TOKEN}")
        self.assertEqual([mutant.file for mutant in report.mutants], [f"{MAIN}/{TOKEN}", None])
        self.assertEqual(report.unresolved, [f"{MAIN}/{TOKEN}"])


class PitBrokenTest(AdapterCase):
    def test_broken_inputs(self):
        self.make(f"{MAIN}/{TOKEN}")
        resolver = self.resolver([MAIN], [f"{MAIN}/{TOKEN}"])
        delete, c1, tab = chr(0x7F), chr(0x85), chr(9)
        cases = {
            "not XML": ("<mutations><mutation", "not well-formed XML"),
            "empty file": ("", "not well-formed XML"),
            "unknown encoding": ('<?xml version="1.0" encoding="no-such-codec"?><mutations/>', "not well-formed XML"),
            "empty root": ("<mutations/>", "has no mutations"),
            "empty root with an attribute": ('<mutations partial="true">\n</mutations>', "has no mutations"),
            "root without mutation elements": ("<mutations><mutant/></mutations>", "has no mutations"),
            "another root": ('<report name="app"/>', "not a PIT report, the root element is <report>"),
            "another root with mutation elements": (
                f"<report>{mutation()}</report>",
                "not a PIT report, the root element is <report>",
            ),
            "lineNumber x": (mutations_xml(mutation(lineNumber="x")), "<lineNumber> must be a non-negative integer"),
            "lineNumber 1.5": (mutations_xml(mutation(lineNumber="1.5")), "<lineNumber>"),
            "lineNumber +3": (mutations_xml(mutation(lineNumber="+3")), "<lineNumber>"),
            "lineNumber 1_0": (mutations_xml(mutation(lineNumber="1_0")), "<lineNumber>"),
            "lineNumber non-ASCII digit": (mutations_xml(mutation(lineNumber=chr(0x0663))), "<lineNumber>"),
            "lineNumber -1": (mutations_xml(mutation(lineNumber="-1")), "<lineNumber> must be a non-negative integer"),
            "lineNumber past a long": (mutations_xml(mutation(lineNumber=str(2**63))), "must be from 0 to"),
            "lineNumber of 5000 digits": (mutations_xml(mutation(lineNumber="7" * 5000)), "must be from 0 to"),
            "lineNumber of spaces": (mutations_xml(mutation(lineNumber="  ")), "<lineNumber>"),
            "index x": (mutations_xml(mutation(indexes="<index>x</index>")), "<index>"),
            "index -3": (mutations_xml(mutation(indexes="<index>1</index><index>-3</index>")), "<index>"),
            "index past a long": (mutations_xml(mutation(indexes=f"<index>{2**63}</index>")), "must be from 0 to"),
            "index of 4301 digits": (mutations_xml(mutation(indexes="<index>" + "9" * 4301 + "</index>")), "<index>"),
            "index empty": (mutations_xml(mutation(indexes="<index/>")), "<index>"),
            "index of spaces": (mutations_xml(mutation(indexes="<index> </index>")), "<index>"),
            "detected yes": (mutations_xml(mutation(detected="yes")), "detected"),
            "detected True": (mutations_xml(mutation(detected="True")), "detected"),
            "detected with a space": (mutations_xml(mutation(detected="true ")), "detected"),
            "detected empty": (mutations_xml(mutation(detected="")), "detected"),
            "detected missing": (mutations_xml(mutation(detected=None)), "no attribute detected"),
            "KILLED not detected": (mutations_xml(mutation(detected="false")), "contradicts"),
            "SURVIVED detected": (mutations_xml(mutation(status="SURVIVED", detected="true")), "contradicts"),
            "NO_COVERAGE detected": (mutations_xml(mutation(status="NO_COVERAGE", detected="true")), "contradicts"),
            "status with a control character": (mutations_xml(mutation(status=f"KILLED{delete}")), "status"),
            "status with a tab": (mutations_xml(mutation(status="KILLED&#9;")), "status"),
            "mutator with a control character": (mutations_xml(mutation(mutator=MATH + c1)), "<mutator>"),
            "mutatedMethod with a tab": (mutations_xml(mutation(mutatedMethod=f"exp{tab}ires")), "<mutatedMethod>"),
            "methodDescription with a control character": (
                mutations_xml(mutation(methodDescription=f"(J){delete}Z")),
                "<methodDescription>",
            ),
            "mutator without a simple name": (mutations_xml(mutation(mutator="org.pitest.")), "no simple name"),
            "mutator of dots": (mutations_xml(mutation(mutator="org.pitest..")), "no simple name"),
            "mutatedClass empty segment": (
                mutations_xml(mutation(mutatedClass="org.example..app.auth.Token")),
                "empty segment",
            ),
            "mutatedClass leading dot": (
                mutations_xml(mutation(mutatedClass=".org.example.app.auth.Token")),
                "segment",
            ),
            "mutatedClass trailing dot": (mutations_xml(mutation(mutatedClass="org.example.app.auth.")), "segment"),
            "mutatedClass with a slash": (
                mutations_xml(mutation(mutatedClass="org.example/app.auth.Token")),
                "segment",
            ),
            "mutatedClass with a backslash": (
                mutations_xml(mutation(mutatedClass="org.example\\app.auth.Token")),
                "segment",
            ),
            "mutatedClass with a colon": (
                mutations_xml(mutation(mutatedClass="org.example:app.auth.Token")),
                "segment",
            ),
            "mutatedClass with a control character": (
                mutations_xml(mutation(mutatedClass=f"org.example{c1}.app.auth.Token")),
                "segment",
            ),
            "sourceFile with a slash": (mutations_xml(mutation(sourceFile="auth/Token.java")), "<sourceFile>"),
            "sourceFile with a backslash": (mutations_xml(mutation(sourceFile="auth\\Token.java")), "<sourceFile>"),
            "sourceFile with a colon": (mutations_xml(mutation(sourceFile="Token.java:stream")), "<sourceFile>"),
            "sourceFile dot": (mutations_xml(mutation(sourceFile=".")), "<sourceFile>"),
            "sourceFile dotdot": (mutations_xml(mutation(sourceFile="..")), "<sourceFile>"),
            "sourceFile with a control character": (
                mutations_xml(mutation(sourceFile=f"Token{delete}.java")),
                "<sourceFile>",
            ),
            "sourceFile NUL reference": (mutations_xml(mutation(sourceFile="Token&#0;.java")), "not well-formed XML"),
        }
        for tag in ("sourceFile", "mutatedClass", "mutator", "lineNumber"):
            cases[f"{tag} missing"] = (mutations_xml(mutation(**{tag: None})), f"has no <{tag}>")
            cases[f"{tag} empty"] = (mutations_xml(mutation(**{tag: ""})), f"has an empty <{tag}>")
            cases[f"{tag} self-closed"] = (mutations_xml(mutation(**{tag: None}, more=f"<{tag}/>")), f"<{tag}>")
        for tag, text in TOKEN_MUTATION.items():
            if tag != "blocks":
                more = f"<{tag}>{text}</{tag}>"
                cases[f"{tag} twice"] = (mutations_xml(mutation(more=more)), f"has <{tag}> twice")
        cases["second mutation broken"] = (mutations_xml(mutation(), mutation(lineNumber="x")), "mutation 2: ")
        for case, (content, part) in cases.items():
            with self.subTest(case):
                path = self.write("build/reports/pit/mutations.xml", content)
                with self.assertRaises(ReportError) as caught:
                    pit.load(path, resolver)
                self.assertIn(path.as_posix(), str(caught.exception))
                self.assertIn(part, str(caught.exception))

    def test_messages_have_the_texts_of_the_other_adapters(self):
        (self.root / MAIN).mkdir(parents=True)
        resolver = self.resolver([MAIN])
        cases = (
            ("<mutations/>", "build/mutations.xml has no mutations"),
            (
                mutations_xml(mutation(lineNumber="-1")),
                "build/mutations.xml: mutation 1: <lineNumber> must be a non-negative integer, got '-1'",
            ),
            (
                mutations_xml(mutation(indexes=f"<index>{2**63}</index>")),
                f"build/mutations.xml: mutation 1: <index> must be from 0 to {2**63 - 1}",
            ),
            (
                mutations_xml(mutation(status="SURVIVED")),
                "build/mutations.xml: mutation 1: status SURVIVED contradicts detected=true",
            ),
        )
        for content, message in cases:
            with self.subTest(message):
                path = self.write("build/mutations.xml", content)
                with self.assertRaises(ReportError) as caught:
                    pit.load(path, resolver, name="build/mutations.xml")
                self.assertEqual(str(caught.exception), message)
        path = self.write("build/mutations.xml", "<mutations")
        with self.assertRaises(ReportError) as caught:
            pit.load(path, resolver, name="build/mutations.xml")
        self.assertTrue(str(caught.exception).startswith("build/mutations.xml: not well-formed XML: "))

    def test_a_folder_instead_of_a_file(self):
        folder = self.root / "build" / "mutations.xml"
        folder.mkdir(parents=True)
        (self.root / MAIN).mkdir(parents=True)
        with self.assertRaises(ReportError) as caught:
            pit.load(folder, self.resolver([MAIN]))
        self.assertIn(f"{folder.as_posix()}: cannot read: ", str(caught.exception))

    def test_given_name_replaces_the_path(self):
        (self.root / MAIN).mkdir(parents=True)
        path = self.write("build/reports/pit/mutations.xml", mutations_xml(mutation(sourceFile="..")))
        with self.assertRaises(ReportError) as caught:
            pit.load(path, self.resolver([MAIN]), name="build/reports/pit/mutations.xml")
        self.assertNamedOnly(str(caught.exception), "build/reports/pit/mutations.xml")


def suite_xml(name: str, timestamp: str | None, time: str | None = "1.5") -> str:
    attributes = f'name="{name}" tests="1" hostname="localhost"'
    if timestamp is not None:
        attributes += f' timestamp="{timestamp}"'
    if time is not None:
        attributes += f' time="{time}"'
    return f'<testsuite {attributes}><testcase name="works" time="0.1"/></testsuite>'


class TimestampTest(unittest.TestCase):
    def test_naive_is_utc(self):
        self.assertEqual(junit.parse_timestamp("2026-11-10T08:15:30"), dt.datetime(2026, 11, 10, 8, 15, 30, tzinfo=UTC))

    def test_fraction_with_z(self):
        self.assertEqual(
            junit.parse_timestamp("2026-11-10T08:15:30.250Z"),
            dt.datetime(2026, 11, 10, 8, 15, 30, 250000, tzinfo=UTC),
        )

    def test_offset_is_the_same_instant(self):
        value = junit.parse_timestamp("2026-11-10T10:15:30+02:00")
        self.assertEqual(value, dt.datetime(2026, 11, 10, 8, 15, 30, tzinfo=UTC))
        self.assertEqual(value.astimezone(UTC).hour, 8)

    def test_other_formats_raise(self):
        for text in ("10.11.2026", "2026-11-10", "2026-11-10 08:15:30", ""):
            with self.subTest(text), self.assertRaises(ValueError):
                junit.parse_timestamp(text)


class JunitSuitesTest(AdapterCase):
    PLAIN = "build/test-results/test/TEST-org.example.app.auth.TokenTest.xml"
    WRAPPED = "build/test-results/test/TEST-all.xml"

    def setUp(self):
        super().setUp()
        self.write(self.PLAIN, suite_xml("org.example.app.auth.TokenTest", "2026-11-10T08:00:00", "2.5"))
        self.write(
            self.WRAPPED,
            "<testsuites>"
            + suite_xml("org.example.app.codec.KeyParserTest", "2026-11-10T08:00:01.500Z", "10")
            + suite_xml("org.example.app.queue.DispatcherTest", None)
            + suite_xml("org.example.app.billing.EngineTest", "2026-11-10T09:59:59+02:00", None)
            + "</testsuites>",
        )

    def test_read_suites(self):
        suites = junit.read_suites(self.root, [self.PLAIN, self.WRAPPED])
        self.assertEqual(
            [(s.name, s.file, s.start, s.seconds) for s in suites],
            [
                ("org.example.app.auth.TokenTest", self.PLAIN, dt.datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC), 2.5),
                (
                    "org.example.app.codec.KeyParserTest",
                    self.WRAPPED,
                    dt.datetime(2026, 11, 10, 8, 0, 1, 500000, tzinfo=UTC),
                    10.0,
                ),
                (
                    "org.example.app.billing.EngineTest",
                    self.WRAPPED,
                    dt.datetime(2026, 11, 10, 7, 59, 59, tzinfo=UTC),
                    0.0,
                ),
            ],
        )
        self.assertEqual(suites[1].end, dt.datetime(2026, 11, 10, 8, 0, 11, 500000, tzinfo=UTC))

    def test_span(self):
        suites = junit.read_suites(self.root, [self.PLAIN, self.WRAPPED])
        self.assertEqual(
            junit.span(suites),
            (
                dt.datetime(2026, 11, 10, 7, 59, 59, tzinfo=UTC),
                dt.datetime(2026, 11, 10, 8, 0, 11, 500000, tzinfo=UTC),
                3,
            ),
        )

    def test_span_of_no_suites(self):
        self.assertIsNone(junit.span([]))

    def test_suite_without_name(self):
        self.write("results.xml", '<testsuite timestamp="2026-11-10T08:00:00"/>')
        (suite,) = junit.read_suites(self.root, ["results.xml"])
        self.assertEqual((suite.name, suite.seconds), ("", 0.0))

    def test_time_forms(self):
        for text, seconds in (("0", 0.0), ("1.5", 1.5), (".5", 0.5), ("2e-3", 0.002), ("3.", 3.0), ("1E2", 100.0)):
            with self.subTest(text):
                self.write("results.xml", suite_xml("org.example.app.auth.TokenTest", "2026-11-10T08:00:00", text))
                (suite,) = junit.read_suites(self.root, ["results.xml"])
                self.assertEqual(suite.seconds, seconds)

    def test_broken_inputs(self):
        def timed(text: str) -> str:
            return suite_xml("org.example.app.auth.TokenTest", "2026-11-10T08:00:00", text)

        cases = {
            "not XML": "<testsuite",
            "bad timestamp": suite_xml("org.example.app.auth.TokenTest", "10.11.2026"),
            "timestamp out of range": suite_xml("org.example.app.auth.TokenTest", "2026-13-10T08:00:00"),
            "offset past the last date": suite_xml("org.example.app.auth.TokenTest", "9999-12-31T23:59:59-02:00", "0"),
            "offset before the first date": suite_xml(
                "org.example.app.auth.TokenTest", "0001-01-01T00:00:00+02:00", "0"
            ),
            "end past the last date in UTC": suite_xml(
                "org.example.app.auth.TokenTest", "9999-12-31T21:00:00-02:00", "7200"
            ),
            "time nan": timed("nan"),
            "time inf": timed("inf"),
            "time -1": timed("-1"),
            "time 1_0": timed("1_0"),
            "time abc": timed("abc"),
            "time 1,5": timed("1,5"),
            "time with a space": timed(" 1"),
            "time empty": timed(""),
            "time +1": timed("+1"),
            "time overflows": timed("1e999"),
            "time past the last date": timed("1e300"),
            "time non-ASCII digit": timed(chr(0x0663)),
            "another root": f'<report name="app">{timed("1.5")}</report>',
            "another root without suites": '<report name="app"/>',
        }
        for case, content in cases.items():
            with self.subTest(case):
                self.write("results.xml", content)
                with self.assertRaises(ReportError) as caught:
                    junit.read_suites(self.root, [self.PLAIN, "results.xml"])
                self.assertIn("results.xml", str(caught.exception))


class JunitErrorsTest(AdapterCase):
    def test_every_broken_file_is_collected(self):
        self.write("results/TEST-first.xml", "<testsuite")
        self.write("results/TEST-good.xml", suite_xml("org.example.app.auth.TokenTest", "2026-11-10T08:00:00"))
        self.write("results/TEST-second.xml", "<report/>")
        files = ["results/TEST-first.xml", "results/TEST-good.xml", "results/TEST-second.xml"]
        errors: list[str] = []
        suites = junit.read_suites(self.root, files, errors)
        self.assertEqual([suite.file for suite in suites], ["results/TEST-good.xml"])
        self.assertEqual([error.split(":")[0] for error in errors], [files[0], files[2]])
        self.assertIn("the root element is <report>", errors[1])
        with self.assertRaises(ReportError) as caught:
            junit.read_suites(self.root, files)
        self.assertIn("results/TEST-first.xml", str(caught.exception))

    def test_a_folder_is_not_a_file(self):
        (self.root / "results" / "TEST-folder.xml").mkdir(parents=True)
        errors: list[str] = []
        self.assertEqual(junit.read_suites(self.root, ["results/TEST-folder.xml"], errors), [])
        self.assertEqual(errors, ["results/TEST-folder.xml: not a file"])


class WithinTest(unittest.TestCase):
    """A JUnit window against a window in epoch ms, give or take a second, in exact integers."""

    def test_bounds_to_the_microsecond_far_from_the_epoch(self):
        start = dt.datetime(9999, 1, 1, tzinfo=UTC)
        last = int((start - dt.datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) * 1000
        tolerance = dt.timedelta(milliseconds=junit.TOLERANCE_MS)
        self.assertTrue(junit.within(start, start + tolerance, last, last))
        self.assertFalse(junit.within(start, start + tolerance + dt.timedelta(microseconds=10), last, last))
        self.assertTrue(junit.within(start - tolerance, start, last, last))
        self.assertFalse(junit.within(start - tolerance - dt.timedelta(microseconds=1), start, last, last))

    def test_micros(self):
        moment = dt.datetime(1970, 1, 1, 0, 0, 1, 7, tzinfo=UTC)
        self.assertEqual(junit.micros(moment), 1_000_007)


class ExistsTest(AdapterCase):
    def test_file_folder_and_missing(self):
        self.make("build/report.xml")
        (self.root / "build/folder.xml").mkdir()
        self.assertTrue(exists(self.root, "build/report.xml"))
        self.assertFalse(exists(self.root, "build/gone.xml"))
        self.assertFalse(exists(self.root, "build/Report.xml"))
        with self.assertRaises(ReportError) as caught:
            exists(self.root, "build/folder.xml")
        self.assertEqual(str(caught.exception), "build/folder.xml: not a file")

    def test_folder_on_the_way_that_cannot_be_read(self):
        self.make("build/muster/report.xml")
        denied, listdir = str(self.root / "build" / "muster"), os.listdir

        def listing(path="."):
            if os.fspath(path) == denied:
                raise PermissionError(13, "Permission denied", path)
            return listdir(path)

        with mock.patch("os.listdir", side_effect=listing), self.assertRaises(ReportError) as caught:
            exists(self.root, "build/muster/report.xml")
        message = "build/muster/report.xml: cannot read a folder on its path: Permission denied"
        self.assertEqual(str(caught.exception), message)
        with mock.patch("os.listdir", side_effect=listing):
            self.assertFalse(exists(self.root, "build/gone/report.xml"))  # a missing folder is no error


class StrictJsonTest(unittest.TestCase):
    def test_every_number_is_finite(self):
        self.assertEqual(strict_json('{"a": 1.5, "b": [2, -0.25e3]}'), {"a": 1.5, "b": [2, -250.0]})
        for text in ('{"a": NaN}', "[Infinity]", "[-Infinity]", '{"x": {"y": 1e400}}', "[-1e400]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                strict_json(text)

    def test_one_copy_of_each_shared_helper(self):
        self.assertIs(timing.strict_json, strict_json)
        self.assertIs(policy.strict_json, strict_json)
        self.assertIs(policy.SHA256, config.SHA256)


class TimingTest(AdapterCase):
    RECORD = {"task": ":app:test", "start_ms": 1000, "end_ms": 9000, "outcome": "success", "failure": None}

    def record(self, content: dict | str | bytes) -> Path:
        if isinstance(content, dict):
            content = json.dumps(content)
        return self.write("build/timing.json", content)

    def test_valid_record(self):
        record = timing.load(self.record(dict(self.RECORD, extra=[1, 2])), ":app:test")
        self.assertEqual(record, timing.TimingRecord(":app:test", 1000, 9000, "success", None))

    def test_data_has_exactly_the_five_fields(self):
        record = timing.load(self.record(dict(self.RECORD, extra={"key": "value"})))
        self.assertEqual(record.data(), self.RECORD)

    def test_times_at_the_bounds(self):
        record = timing.load(self.record({"start_ms": 0, "end_ms": timing.EPOCH_MS_MAX, "extra": 1e300}))
        self.assertEqual((record.start_ms, record.end_ms), (0, timing.EPOCH_MS_MAX))

    def test_optional_fields(self):
        record = timing.load(self.record({"start_ms": 5, "end_ms": 5, "task": None}))
        self.assertEqual(record, timing.TimingRecord(None, 5, 5, None, None))

    def test_failure_text(self):
        record = timing.load(self.record(dict(self.RECORD, outcome="failed", failure="2 tests failed")))
        self.assertEqual((record.outcome, record.failure), ("failed", "2 tests failed"))

    def test_broken_inputs(self):
        def changed(**fields) -> dict:
            return {key: value for key, value in dict(self.RECORD, **fields).items() if value is not ...}

        cases = {
            "not JSON": ("{", None, None),
            "not UTF-8": (b'{"start_ms": 1, "end_ms": 2, "task": "\xff"}', None, None),
            "NaN": ('{"start_ms": NaN, "end_ms": 2}', None, None),
            "Infinity": ('{"start_ms": 1, "end_ms": Infinity}', None, None),
            "-Infinity": ('{"start_ms": -Infinity, "end_ms": 2}', None, None),
            "nested too deeply": ("[" * 100000 + "]" * 100000, None, None),
            "top level a list": ("[]", None, None),
            "start_ms missing": (changed(start_ms=...), None, "start_ms"),
            "start_ms a bool": (changed(start_ms=True), None, "start_ms"),
            "start_ms a float": (changed(start_ms=1000.0), None, "start_ms"),
            "start_ms null": (changed(start_ms=None), None, "start_ms"),
            "end_ms a string": (changed(end_ms="9000"), None, "end_ms"),
            "end_ms before start_ms": (changed(end_ms=999), None, "end_ms"),
            "start_ms before the epoch": (changed(start_ms=-1), None, "start_ms"),
            "end_ms past the year 9999": (changed(end_ms=timing.EPOCH_MS_MAX + 1), None, "end_ms"),
            "end_ms of 400 digits, failed": (changed(end_ms=10**400, outcome="failed"), None, "end_ms"),
            "an extra field 1e9999": ('{"start_ms": 1, "end_ms": 2, "extra": 1e9999}', None, "1e9999"),
            "an extra field -1E400 in a list": ('{"start_ms": 1, "end_ms": 2, "extra": [-1E400]}', None, None),
            "outcome a number": (changed(outcome=1), None, "outcome"),
            "failure a list": (changed(failure=["boom"]), None, "failure"),
            "task a bool": (changed(task=False), None, "task"),
            "another task": (changed(task=":lib:test"), ":app:test", ":lib:test"),
            "task missing": (changed(task=...), ":app:test", "no task"),
        }
        for case, (content, task, part) in cases.items():
            with self.subTest(case):
                path = self.record(content)
                with self.assertRaises(ReportError) as caught:
                    timing.load(path, task)
                self.assertIn(path.as_posix(), str(caught.exception))
                if part is not None:
                    self.assertIn(part, str(caught.exception))

    def test_a_record_of_another_task_names_both(self):
        path = self.record(dict(self.RECORD, task=":lib:test"))
        with self.assertRaises(ReportError) as caught:
            timing.load(path, ":app:test")
        self.assertIn(":lib:test", str(caught.exception))
        self.assertIn(":app:test", str(caught.exception))

    def test_given_name_replaces_the_path(self):
        path = self.record(dict(self.RECORD, end_ms=1))
        with self.assertRaises(ReportError) as caught:
            timing.load(path, name="build/timing.json")
        self.assertNamedOnly(str(caught.exception), "build/timing.json")


if __name__ == "__main__":
    unittest.main()
