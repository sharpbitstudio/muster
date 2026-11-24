"""Tests of muster.paths: the walked sets, the exact-case existence check and the source roots.

Every tree is built in a temporary folder.
"""

import argparse
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster.config import Config, ConfigError
from muster.paths import (
    _is_junction,
    exists_exact,
    file_argument,
    group_of,
    in_loc_set,
    is_link,
    loc_set,
    normalize,
    present_exact,
    resolve_glob,
    root_of,
    source_sets,
    walk,
)


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))

    def make(self, *paths: str) -> None:
        for path in paths:
            file = self.root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"")

    def config(self, **source) -> Config:
        return Config(self.root, self.root / "muster.toml", {"source": source})

    def denied(self, folder: str):
        """os.scandir() fails for this folder, as for one whose listing is denied."""
        path, scandir = str(self.root / folder), os.scandir

        def listing(top="."):
            if os.fspath(top) == path:
                raise PermissionError(13, "Permission denied", top)
            return scandir(top)

        return mock.patch("os.scandir", side_effect=listing)

    def junction(self, target: str, link: str) -> None:
        _winapi = __import__("_winapi")  # Windows only; a junction needs no privilege
        _winapi.CreateJunction(str(self.root / target), str(self.root / link))


class WalkTest(TreeCase):
    def test_posix_keys_sorted_and_without_duplicates(self):
        self.make("src/app/main.py", "src/app/core/rules.py", "src/tool.py")
        found = walk(self.root, "source.roots", ["src", "src/app", "./src/"])
        self.assertEqual(found, ["src/app/core/rules.py", "src/app/main.py", "src/tool.py"])

    def test_root_folder_itself(self):
        self.make("tool.py", "src/main.py")
        self.assertEqual(walk(self.root, "source.roots", ["."]), ["src/main.py", "tool.py"])

    def test_missing_folder(self):
        self.make("src/main.py")
        with self.assertRaises(ConfigError) as caught:
            walk(self.root, "source.roots", ["src", "lib"])
        self.assertEqual(str(caught.exception), "source.roots[1]: folder lib does not exist")

    def test_folder_in_another_case_does_not_exist(self):
        self.make("src/main.py")
        with self.assertRaises(ConfigError):
            walk(self.root, "source.roots", ["Src"])

    def test_a_file_is_no_folder(self):
        self.make("src/main.py")
        with self.assertRaises(ConfigError):
            walk(self.root, "source.tests", ["src/main.py"])

    def test_empty_folder(self):
        (self.root / "src").mkdir()
        self.assertEqual(walk(self.root, "source.roots", ["src"]), [])

    def test_folder_that_cannot_be_listed(self):
        self.make("src/app/main.py", "src/locked/secret.py", "src/tool.py", "lib/util.py")
        message = "source.roots: cannot read src/locked: Permission denied"  # repository-relative
        with self.denied("src/locked"), self.assertRaises(ConfigError) as caught:
            walk(self.root, "source.roots", ["src", "lib"])
        self.assertEqual(str(caught.exception), message)
        errors = []
        with self.denied("src/locked"):
            found = walk(self.root, "source.roots", ["src", "lib"], errors)
        self.assertEqual(found, ["lib/util.py", "src/app/main.py", "src/tool.py"])
        self.assertEqual([str(error) for error in errors], [message])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links")
    def test_linked_folder_is_not_followed(self):
        self.make("src/main.py", "elsewhere/other.py")
        try:
            os.symlink(self.root / "elsewhere", self.root / "src" / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("cannot create a symbolic link here")
        self.assertEqual(walk(self.root, "source.roots", ["src"]), ["src/main.py"])

    @unittest.skipUnless(sys.platform == "win32", "junctions exist only on Windows")
    def test_junction_below_a_root_is_not_followed(self):
        self.make("src/main.py", "elsewhere/other.py", "elsewhere/deeper/more.py")
        self.junction("elsewhere", "src/linked")
        self.assertTrue(is_link(str(self.root / "src" / "linked")))
        self.assertEqual(walk(self.root, "source.roots", ["src"]), ["src/main.py"])

    @unittest.skipUnless(sys.platform == "win32", "junctions exist only on Windows")
    def test_root_that_is_a_junction_is_walked(self):
        self.make("elsewhere/other.py")
        self.junction("elsewhere", "linked")
        self.assertEqual(walk(self.root, "source.roots", ["linked"]), ["linked/other.py"])

    @unittest.skipIf(sys.platform == "win32", "POSIX symbolic links")
    def test_root_that_is_a_symbolic_link_is_walked(self):
        self.make("elsewhere/other.py")
        os.symlink(self.root / "elsewhere", self.root / "linked", target_is_directory=True)
        self.assertEqual(walk(self.root, "source.roots", ["linked"]), ["linked/other.py"])

    def test_only_a_mount_point_is_a_junction_on_python_3_11(self):
        mount_point, cloud_files = 0xA0000003, 0x9000001A  # Windows reparse tags; only Windows has them in stat
        self.enterContext(mock.patch.object(stat, "IO_REPARSE_TAG_MOUNT_POINT", mount_point, create=True))
        for tag, junction in ((mount_point, True), (cloud_files, False), (0, False)):
            with self.subTest(tag=hex(tag)):
                found = mock.Mock(st_reparse_tag=tag, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                with mock.patch("os.lstat", return_value=found):
                    self.assertIs(_is_junction("/work/src/folder"), junction)

    def test_plain_folder_and_file_are_no_links(self):
        self.make("src/main.py")
        self.assertFalse(is_link(str(self.root / "src")))
        self.assertFalse(is_link(str(self.root / "src" / "main.py")))
        self.assertFalse(is_link(str(self.root / "gone")))


RESULTS = "app/build/test-results"
TOKEN = f"{RESULTS}/test/TEST-org.example.app.auth.TokenTest.xml"
PARSER = f"{RESULTS}/test/nested/deeper/TEST-org.example.app.codec.KeyParserTest.xml"
TOP = f"{RESULTS}/TEST-org.example.app.TopTest.xml"
NOTES = f"{RESULTS}/test/notes.txt"
DISPATCHER = "lib/build/test-results/test/TEST-org.example.app.queue.DispatcherTest.xml"


class ResolveGlobTest(TreeCase):
    def setUp(self):
        super().setUp()
        self.make(TOKEN, PARSER, TOP, NOTES, DISPATCHER, "TEST-org.example.app.RootTest.xml")

    def test_double_star_at_several_depths(self):
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/**/TEST-*.xml"), [TOP, TOKEN, PARSER])
        self.assertEqual(resolve_glob(self.root, "**/test/TEST-*.xml"), [TOKEN, DISPATCHER])
        self.assertEqual(resolve_glob(self.root, "app/**"), [TOP, TOKEN, PARSER, NOTES])
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/test/**/deeper/*.xml"), [PARSER])

    def test_star_within_a_segment(self):
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/test/TEST-*Token*.xml"), [TOKEN])
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/*.xml"), [TOP])  # a star stays in its folder
        self.assertEqual(resolve_glob(self.root, "*/build/test-results/test/*.xml"), [TOKEN, DISPATCHER])
        self.assertEqual(resolve_glob(self.root, "TEST-*.xml"), ["TEST-org.example.app.RootTest.xml"])

    def test_alternatives_sorted_and_without_duplicates(self):
        self.assertEqual(resolve_glob(self.root, "{lib,app}/build/test-results/test/*.xml"), [TOKEN, DISPATCHER])
        overlap = resolve_glob(self.root, f"{{{RESULTS}/**/*.xml,**/test/TEST-*.xml,{TOKEN}}}")
        self.assertEqual(overlap, [TOP, TOKEN, PARSER, DISPATCHER])
        self.assertEqual(overlap, sorted(set(overlap)))

    def test_literal_file(self):
        self.assertEqual(resolve_glob(self.root, TOKEN), [TOKEN])
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/test/TEST-Gone.xml"), [])
        # A folder is listed, so that the reader refuses it rather than take it as no results.
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/test"), [f"{RESULTS}/test"])

    def test_git_folders_are_not_walked(self):
        self.make(".git/TEST-org.example.app.GitTest.xml", "app/.git/refs/TEST-org.example.app.GitTest.xml")
        found = resolve_glob(self.root, "**/TEST-*.xml")
        self.assertEqual(found, sorted([TOP, TOKEN, PARSER, DISPATCHER, "TEST-org.example.app.RootTest.xml"]))
        self.assertEqual(resolve_glob(self.root, "app/**/TEST-*.xml"), [TOP, TOKEN, PARSER])

    def test_missing_start_folder_gives_no_files(self):
        self.assertEqual(resolve_glob(self.root, "gone/**/*.xml"), [])
        self.assertEqual(resolve_glob(self.root, "app/build/missing/*.xml"), [])
        self.assertEqual(resolve_glob(self.root, f"{TOP}/*.xml"), [])  # a file is no folder

    def test_start_folder_in_another_case_gives_no_files(self):
        self.assertEqual(resolve_glob(self.root, "App/build/test-results/**/*.xml"), [])
        self.assertEqual(resolve_glob(self.root, "app/Build/test-results/test/*.xml"), [])
        self.assertEqual(resolve_glob(self.root, TOKEN.replace("app/build", "app/BUILD")), [])

    def test_file_name_in_another_case_is_not_matched(self):
        self.assertEqual(resolve_glob(self.root, "**/test-*.xml"), [])
        self.assertEqual(resolve_glob(self.root, "**/TEST-*.XML"), [])
        self.assertEqual(resolve_glob(self.root, TOKEN.replace("TokenTest", "tokentest")), [])

    def test_folder_that_cannot_be_read_is_skipped(self):
        with self.denied(f"{RESULTS}/test"):
            self.assertEqual(resolve_glob(self.root, "app/**/*.xml"), [TOP])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links")
    def test_linked_folder_below_the_start_is_not_followed(self):
        self.make("elsewhere/TEST-org.example.app.OtherTest.xml")
        try:
            os.symlink(self.root / "elsewhere", self.root / RESULTS / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("cannot create a symbolic link here")
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/**/*.xml"), [TOP, TOKEN, PARSER])
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/*/TEST-*.xml"), [TOKEN])

    @unittest.skipUnless(sys.platform == "win32", "junctions exist only on Windows")
    def test_junction_below_the_start_is_not_followed(self):
        self.make("elsewhere/TEST-org.example.app.OtherTest.xml")
        self.junction("elsewhere", f"{RESULTS}/linked")
        self.assertEqual(resolve_glob(self.root, f"{RESULTS}/**/*.xml"), [TOP, TOKEN, PARSER])

    @unittest.skipUnless(sys.platform == "win32", "junctions exist only on Windows")
    def test_start_folder_that_is_a_junction_is_walked(self):
        self.make("elsewhere/TEST-org.example.app.OtherTest.xml")
        self.junction("elsewhere", "linked")
        self.assertEqual(resolve_glob(self.root, "linked/*.xml"), ["linked/TEST-org.example.app.OtherTest.xml"])


class SourceSetsTest(TreeCase):
    def test_include_exclude_generated(self):
        self.make(
            "src/main/java/org/example/app/auth/Token.java",
            "src/main/java/org/example/app/auth/package-info.java",
            "src/main/java/org/example/app/codec/KeyParser.java",
            "src/main/java/org/example/app/gen/Schema.java",
            "src/main/java/org/example/app/notes.txt",
            "src/test/java/org/example/app/auth/TokenTest.java",
            "src/test/java/org/example/app/auth/package-info.java",
            "src/test/java/org/example/app/gen/SchemaTest.java",
        )
        sets = source_sets(
            self.config(
                roots=["src/main/java"],
                tests=["src/test/java"],
                include=["**/*.java"],
                exclude=["**/package-info.java"],
                generated=["**/gen/**"],
            )
        )
        self.assertEqual(
            sets.sources,
            ["src/main/java/org/example/app/auth/Token.java", "src/main/java/org/example/app/codec/KeyParser.java"],
        )
        expected_tests = {
            "src/test/java/org/example/app/auth/TokenTest.java",
            "src/test/java/org/example/app/auth/package-info.java",  # exclude is not applied to tests
        }
        self.assertEqual(sets.tests, frozenset(expected_tests))

    def test_several_roots(self):
        self.make("core/src/ledger.py", "web/src/views.py", "web/src/static/app.js")
        sets = source_sets(self.config(roots=["core/src", "web/src"], include=["**/*.py"]))
        self.assertEqual(sets.sources, ["core/src/ledger.py", "web/src/views.py"])
        self.assertEqual(sets.tests, frozenset())

    def test_globs_are_case_sensitive(self):
        self.make("src/Tool.PY", "src/main.py")
        self.assertEqual(source_sets(self.config(roots=["src"], include=["**/*.py"])).sources, ["src/main.py"])

    def test_missing_folder_deferred(self):
        self.make("src/main.py", "tests/test_main.py")
        sets = source_sets(self.config(roots=["gone", "src"], tests=["tests"], include=["**/*.py"]), defer=True)
        self.assertEqual((sets.sources, sets.tests), (["src/main.py"], frozenset({"tests/test_main.py"})))
        self.assertEqual(str(sets.error), "source.roots[0]: folder gone does not exist")
        with self.assertRaises(ConfigError):
            source_sets(self.config(roots=["gone", "src"], include=["**/*.py"]))
        self.assertIsNone(source_sets(self.config(roots=["src"], include=["**/*.py"]), defer=True).error)


class WalkedFilesTest(TreeCase):
    def test_every_walked_file_before_the_globs(self):
        self.make("src/main.py", "src/notes.txt", "src/gen/schema.py", "tests/test_main.py", "tests/data.json")
        sets = source_sets(self.config(roots=["src"], tests=["tests"], include=["**/*.py"], generated=["src/gen/**"]))
        self.assertEqual(sets.sources, ["src/main.py"])
        self.assertEqual(sets.walked_sources, ["src/gen/schema.py", "src/main.py", "src/notes.txt"])
        self.assertEqual(sets.walked_tests, ["tests/data.json", "tests/test_main.py"])


MAIN = "src/main/java/org/example/app"
TESTS = "src/test/java/org/example/app"


class LocSetTest(TreeCase):
    def loc_config(self, loc: dict | None = None, **source) -> Config:
        data = {"source": source} if loc is None else {"source": source, "loc": loc}
        return Config(self.root, self.root / "muster.toml", data)

    def setUp(self):
        super().setUp()
        self.make(
            f"{MAIN}/auth/Token.java",
            f"{MAIN}/auth/package-info.java",
            f"{MAIN}/gen/Schema.java",
            f"{MAIN}/legacy/OldToken.java",
            f"{MAIN}/notes.txt",
            f"{TESTS}/auth/TokenTest.java",
            f"{TESTS}/gen/SchemaTest.java",
            f"{TESTS}/legacy/OldTokenTest.java",
            f"{TESTS}/fixtures/data.json",
            "build/generated/Other.java",
        )
        self.cfg = self.loc_config(
            {"exclude": ["**/legacy/**"]},
            roots=["src/main/java"],
            tests=["src/test/java"],
            include=["**/*.java"],
            exclude=["**/package-info.java"],
            generated=["**/gen/**"],
        )

    def test_roots_and_tests_are_walked(self):
        found = loc_set(self.cfg)
        expected = [f"{MAIN}/auth/Token.java", f"{MAIN}/auth/package-info.java", f"{TESTS}/auth/TokenTest.java"]
        self.assertEqual(found.files, expected)  # source.exclude does not apply; generated and loc.exclude do
        walked = [
            f"{MAIN}/auth/Token.java",
            f"{MAIN}/auth/package-info.java",
            f"{MAIN}/gen/Schema.java",
            f"{MAIN}/legacy/OldToken.java",
            f"{MAIN}/notes.txt",
            f"{TESTS}/auth/TokenTest.java",
            f"{TESTS}/fixtures/data.json",
            f"{TESTS}/gen/SchemaTest.java",
            f"{TESTS}/legacy/OldTokenTest.java",
        ]
        self.assertEqual(found.walked, walked)
        self.assertIsNone(found.error)

    def test_tests_folder_nested_under_a_root_is_listed_once(self):
        cfg = self.loc_config(roots=["src"], tests=["src/test/java"], include=["**/*.java"])
        found = loc_set(cfg)
        self.assertEqual(len(found.files), len(set(found.files)))
        self.assertIn(f"{TESTS}/auth/TokenTest.java", found.files)
        self.assertEqual(found.walked, sorted(set(found.walked)))

    def test_globs_are_case_sensitive(self):
        self.make(f"{MAIN}/auth/Upper.JAVA")
        self.assertNotIn(f"{MAIN}/auth/Upper.JAVA", loc_set(self.cfg).files)

    def test_missing_folder(self):
        cfg = self.loc_config(roots=["src/main/java"], tests=["src/test/kotlin"], include=["**/*.java"])
        with self.assertRaises(ConfigError) as caught:
            loc_set(cfg)
        self.assertEqual(str(caught.exception), "source.tests[0]: folder src/test/kotlin does not exist")
        deferred = loc_set(cfg, defer=True)
        self.assertEqual(str(deferred.error), "source.tests[0]: folder src/test/kotlin does not exist")
        self.assertEqual(deferred.files[0], f"{MAIN}/auth/Token.java")

    def test_predicate_on_a_path_alone(self):
        cases = {
            f"{MAIN}/auth/Gone.java": True,
            f"{MAIN}/auth/package-info.java": True,
            f"{TESTS}/auth/GoneTest.java": True,
            f"{MAIN}/gen/Gone.java": False,
            f"{TESTS}/legacy/GoneTest.java": False,
            f"{MAIN}/auth/gone.txt": False,
            "build/generated/Other.java": False,
            "src/main/javax/Gone.java": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(in_loc_set(self.cfg, path), expected)
        self.assertFalse((self.root / MAIN / "auth" / "Gone.java").exists())
        for path in loc_set(self.cfg).files:
            self.assertTrue(in_loc_set(self.cfg, path))


class GroupOfTest(TreeCase):
    def group_config(self, roots: list[str], tests: list[str], support: list[str] | None = None) -> Config:
        data = {"source": {"roots": roots, "tests": tests, "include": ["**/*.java"]}}
        if support is not None:
            data["loc"] = {"test_support": support}
        return Config(Path("/work/repo"), Path("/work/repo/muster.toml"), data)

    def test_main_test_and_test_support(self):
        cfg = self.group_config(["src/main/java"], ["src/test/java"], [f"{TESTS}/support/**", "**/*Fixture*.java"])
        cases = {
            f"{MAIN}/auth/Token.java": "main",
            f"{TESTS}/auth/TokenTest.java": "test",
            f"{TESTS}/support/Clock.java": "test-support",
            f"{TESTS}/auth/TokenFixture.java": "test-support",
            "build/generated/Other.java": None,
            "src/main/javax/Token.java": None,
        }
        for path, group in cases.items():
            with self.subTest(path=path):
                self.assertEqual(group_of(cfg, path), group)

    def test_tests_folder_nested_under_a_root(self):
        cfg = self.group_config(["src"], ["src/test/java"], ["**/support/**"])
        self.assertEqual(group_of(cfg, "src/main/java/org/example/app/Token.java"), "main")
        self.assertEqual(group_of(cfg, "src/test/kotlin/org/example/app/TokenTest.java"), "main")
        self.assertEqual(group_of(cfg, f"{TESTS}/TokenTest.java"), "test")
        self.assertEqual(group_of(cfg, f"{TESTS}/support/Clock.java"), "test-support")

    def test_test_support_glob_under_a_root_is_main(self):
        cfg = self.group_config(["src/main/java"], ["src/test/java"], ["**/*Fixture*.java", "src/main/java/**"])
        self.assertEqual(group_of(cfg, f"{MAIN}/auth/TokenFixture.java"), "main")

    def test_root_nested_under_a_tests_folder(self):
        cfg = self.group_config(["tests/fixtures/app"], ["tests"], ["tests/**"])
        self.assertEqual(group_of(cfg, "tests/fixtures/app/Token.java"), "main")
        self.assertEqual(group_of(cfg, "tests/fixtures/Clock.java"), "test-support")

    def test_folder_in_both_lists_is_a_root(self):
        cfg = self.group_config(["src/java"], ["src/java"], ["**"])
        self.assertEqual(group_of(cfg, "src/java/org/example/app/Token.java"), "main")


class ExistsExactTest(TreeCase):
    def test_file_and_folder(self):
        self.make("src/test/java/FullRunSuite.java")
        self.assertTrue(exists_exact(self.root, "src/test/java/FullRunSuite.java"))
        self.assertTrue(exists_exact(self.root, "./src/test/java/FullRunSuite.java"))
        self.assertTrue(exists_exact(self.root, "src/test", folder=True))
        self.assertFalse(exists_exact(self.root, "src/test"))
        self.assertFalse(exists_exact(self.root, "src/test/java/FullRunSuite.java", folder=True))

    def test_other_case_or_missing(self):
        self.make("src/test/java/FullRunSuite.java")
        self.assertFalse(exists_exact(self.root, "src/test/java/FullrunSuite.java"))
        self.assertFalse(exists_exact(self.root, "Src/test/java/FullRunSuite.java"))
        self.assertFalse(exists_exact(self.root, "src/test/java/Gone.java"))
        self.assertFalse(exists_exact(self.root, "src/test/java/FullRunSuite.java/inner"))

    def test_present_is_a_file_a_folder_or_another_entry(self):
        self.make("src/test/java/FullRunSuite.java")
        self.assertTrue(present_exact(self.root, "src/test/java/FullRunSuite.java"))
        self.assertTrue(present_exact(self.root, "src/test/java"))
        self.assertFalse(present_exact(self.root, "src/test/Java"))
        self.assertFalse(present_exact(self.root, "src/test/java/Gone.java"))


class HelperTest(unittest.TestCase):
    def test_file_argument_refuses_an_empty_name_or_a_nul(self):
        self.assertEqual(file_argument("build/reports/report.xml"), "build/reports/report.xml")
        for text, message in (("", "the file name is empty"), (f"build{chr(0)}.xml", "the file name has a NUL")):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError) as caught:
                    file_argument(text)
                self.assertEqual(str(caught.exception), message)

    def test_normalize(self):
        self.assertEqual([normalize(p) for p in ("", ".", "./src/", "src//app")], ["", "", "src", "src/app"])

    def test_root_of_takes_the_deepest_root(self):
        roots = ["src", "src/main/java", "lib"]
        self.assertEqual(root_of("src/main/java/org/Engine.java", roots), "src/main/java")
        self.assertEqual(root_of("src/tool.py", roots), "src")
        self.assertIsNone(root_of("srcx/tool.py", roots))
        self.assertEqual(root_of("tool.py", ["."]), "")


if __name__ == "__main__":
    unittest.main()
