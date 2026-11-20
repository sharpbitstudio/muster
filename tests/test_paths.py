"""Tests of muster.paths: the walked sets, the exact-case existence check and the source roots.

Every tree is built in a temporary folder.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster.config import Config, ConfigError
from muster.paths import _is_junction, exists_exact, is_link, normalize, root_of, source_sets, walk


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

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links")
    def test_linked_folder_is_not_followed(self):
        self.make("src/main.py", "elsewhere/other.py")
        try:
            os.symlink(self.root / "elsewhere", self.root / "src" / "linked", target_is_directory=True)
        except OSError:
            self.skipTest("cannot create a symbolic link here")
        self.assertEqual(walk(self.root, "source.roots", ["src"]), ["src/main.py"])

    def junction(self, target: str, link: str) -> None:
        _winapi = __import__("_winapi")  # Windows only; a junction needs no privilege
        _winapi.CreateJunction(str(self.root / target), str(self.root / link))

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


class HelperTest(unittest.TestCase):
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
