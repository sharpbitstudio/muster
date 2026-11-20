"""Tests of muster.globs: the globs of muster.toml."""

import unittest

from muster.config import ConfigError
from muster.globs import check_globs, compile_glob, matches, matches_any


class MatchTest(unittest.TestCase):
    def assertMatches(self, glob, *paths):
        for path in paths:
            with self.subTest(glob=glob, path=path):
                self.assertTrue(matches(glob, path))

    def assertNoMatch(self, glob, *paths):
        for path in paths:
            with self.subTest(glob=glob, path=path):
                self.assertFalse(matches(glob, path))

    def test_double_star_first_matches_zero_or_more_folders(self):
        self.assertMatches("**/*.py", "tool.py", "src/tool.py", "src/muster/tool.py")
        self.assertNoMatch("**/*.py", "tool.pyc", "src/tool.py/data")

    def test_double_star_in_the_middle_matches_zero_or_more_folders(self):
        self.assertMatches("src/**/main.py", "src/main.py", "src/app/main.py", "src/app/core/main.py")
        self.assertNoMatch("src/**/main.py", "srcmain.py", "lib/src/main.py", "src/app/main.pyc")

    def test_double_star_last_matches_everything_below(self):
        self.assertMatches("src/**", "src/tool.py", "src/app/core/main.py")
        self.assertNoMatch("src/**", "src", "lib/src/tool.py", "srcx/tool.py")

    def test_double_star_alone_matches_every_path(self):
        self.assertMatches("**", "tool.py", "src/app/main.py")

    def test_star_stays_inside_a_segment(self):
        self.assertMatches("src/*.py", "src/tool.py", "src/.py")
        self.assertNoMatch("src/*.py", "src/app/tool.py", "lib/tool.py")
        self.assertMatches("**/*Fixture*.java", "src/test/java/OrderFixtures.java")

    def test_alternation(self):
        self.assertMatches("{src,lib}/**/*.{py,pyi}", "src/tool.py", "lib/app/tool.pyi")
        self.assertNoMatch("{src,lib}/**/*.{py,pyi}", "docs/tool.py", "src/tool.pyc")
        self.assertMatches("src/{,app/}main.py", "src/main.py", "src/app/main.py")

    def test_other_characters_are_literal(self):
        self.assertMatches("src/a+b(1)[x]?.py", "src/a+b(1)[x]?.py")
        self.assertNoMatch("src/a+b(1)[x]?.py", "src/aab(1)x1.py")
        self.assertNoMatch("src/tool.py", "src/toolxpy")

    def test_case_sensitive(self):
        self.assertNoMatch("src/Tool.py", "src/tool.py", "SRC/Tool.py")

    def test_whole_path(self):
        self.assertNoMatch("tool.py", "src/tool.py", "tool.py.bak")

    def test_line_breaks_in_a_name_are_matched_like_any_character(self):
        self.assertMatches("src/**", "src/odd\nname.py")
        self.assertMatches("src/*.py", "src/odd\nname.py")

    def test_a_file_name(self):
        self.assertTrue(matches("*IntegrationTest*", "LedgerIntegrationTest.java"))
        self.assertFalse(matches("*IntegrationTest*", "LedgerTest.java"))

    def test_matches_any(self):
        self.assertTrue(matches_any(["**/*.md", "src/**"], "src/tool.py"))
        self.assertFalse(matches_any([], "src/tool.py"))


class MalformedGlobTest(unittest.TestCase):
    def assertRejected(self, glob, reason):
        with self.assertRaises(ConfigError) as caught:
            compile_glob("source.include[0]", glob)
        self.assertEqual(str(caught.exception), f'source.include[0]: bad glob "{glob}": {reason}')

    def test_double_star_inside_a_segment(self):
        self.assertRejected("src/**.py", "'**' must be a whole path segment")
        self.assertRejected("src/x**/tool.py", "'**' must be a whole path segment")
        self.assertRejected("src/{**x,lib}/tool.py", "'**' must be a whole path segment")

    def test_bad_braces(self):
        self.assertRejected("src/{app,lib/tool.py", "a '{' without its '}'")
        self.assertRejected("src/app}/tool.py", "a '}' without its '{'")
        self.assertRejected("src/}app{/tool.py", "a '}' without its '{'")
        self.assertRejected("src/{app,{lib}}/tool.py", "nested braces")

    def test_check_globs_names_the_item(self):
        check_globs("source.exclude", ["**/*.md", "docs/**"])
        with self.assertRaises(ConfigError) as caught:
            check_globs("source.exclude", ["**/*.md", "docs/**.md"])
        self.assertTrue(str(caught.exception).startswith('source.exclude[1]: bad glob "docs/**.md"'))


if __name__ == "__main__":
    unittest.main()
