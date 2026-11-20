"""Tests of muster.gitio: running git, the repository root, the merge base and
reading git output.

The repositories are built in temporary folders, with git's global and system
configuration switched off and a synthetic identity given per command.
Non-ASCII characters are built at run time with chr().
"""

import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster.gitio import find_repo_root, git, merge_base, records, unquote_git_path
from muster.report import ReportError

IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


class GitRepoCase(unittest.TestCase):
    """A temporary folder, an environment without git variables or global and
    system configuration, and helpers to build a repository in it."""

    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ))
        for key in [key for key in os.environ if key.startswith("GIT_")]:
            del os.environ[key]
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.root = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        # git looks for a repository no higher than the temporary folder itself.
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.root)

    def run_git(self, repo: Path, *args: str) -> str:
        proc = subprocess.run(["git", *IDENTITY, *args], cwd=repo, capture_output=True, check=True)
        return proc.stdout.decode("utf-8").strip()

    def make_repo(self, name: str = "repo") -> Path:
        repo = self.root / name
        repo.mkdir()
        self.run_git(repo, "init", "-q", "-b", "main")
        return repo

    def commit(self, repo: Path, message: str) -> str:
        self.run_git(repo, "commit", "-q", "--allow-empty", "-m", message)
        return self.run_git(repo, "rev-parse", "HEAD")


class GitTest(GitRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.head = self.commit(self.repo, "first")

    def test_returns_stdout(self):
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head + "\n")

    def test_failure_names_command_and_last_stderr_line(self):
        args = ("log", "-1", "--format=%H", "no-such-ref")
        proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True)
        last = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1]
        with self.assertRaises(ReportError) as ctx:
            git(self.repo, *args)
        self.assertEqual(str(ctx.exception), "git log -1 --format=%H failed: " + last)

    def test_failure_without_stderr_names_exit_code(self):
        (self.repo / "left.txt").write_bytes(b"left\n")
        (self.repo / "right.txt").write_bytes(b"right\n")
        with self.assertRaises(ReportError) as ctx:
            git(self.repo, "diff", "--no-index", "--quiet", "left.txt", "right.txt")
        self.assertEqual(str(ctx.exception), "git diff --no-index --quiet failed: 1")

    def test_no_check_returns_stdout_of_failed_command(self):
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", "no-such-ref", check=False), "")


class MergeBaseTest(GitRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.fork = self.commit(self.repo, "first")

    def test_common_ancestor(self):
        self.run_git(self.repo, "checkout", "-q", "-b", "feature")
        self.commit(self.repo, "on feature")
        self.run_git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "on main")
        self.run_git(self.repo, "checkout", "-q", "feature")
        self.assertEqual(merge_base(self.repo, "main"), self.fork)

    def test_unrelated_histories(self):
        self.run_git(self.repo, "checkout", "-q", "--orphan", "other")
        self.commit(self.repo, "unrelated")
        self.assertIsNone(merge_base(self.repo, "main"))

    def test_missing_ref(self):
        self.assertIsNone(merge_base(self.repo, "no-such-ref"))


class FindRepoRootTest(GitRepoCase):
    def test_explicit_path_wins(self):
        repo = self.make_repo()
        with contextlib.chdir(repo):
            found = find_repo_root(str(self.root / "elsewhere"))
        self.assertTrue(same_path(found, self.root / "elsewhere"))

    def test_explicit_relative_path_resolved(self):
        with contextlib.chdir(self.root):
            found = find_repo_root(os.path.join("nested", "..", "other"))
        self.assertTrue(found.is_absolute())
        self.assertTrue(same_path(found, self.root / "other"))

    def test_top_level_of_enclosing_repo(self):
        repo = self.make_repo()
        inner = repo / "module" / "package"
        inner.mkdir(parents=True)
        with contextlib.chdir(inner):
            found = find_repo_root(None)
        self.assertTrue(same_path(found, repo))

    def test_outside_repo_falls_back_to_current_directory(self):
        outside = self.root / "plain"
        outside.mkdir()
        with contextlib.chdir(outside):
            found = find_repo_root(None)
        self.assertTrue(found.is_absolute())
        self.assertTrue(same_path(found, outside))


class UnquoteGitPathTest(unittest.TestCase):
    def test_unquoted_passes_through(self):
        self.assertEqual(unquote_git_path("org/example/app/Main.java"), "org/example/app/Main.java")

    def test_lone_quote_passes_through(self):
        self.assertEqual(unquote_git_path('"'), '"')

    def test_simple_escapes(self):
        self.assertEqual(unquote_git_path('"tab\\there\\nnew\\rret\\a\\b\\f\\v"'), "tab\there\nnew\rret\a\b\f\v")

    def test_octal_bytes_form_utf8(self):
        self.assertEqual(unquote_git_path('"docs/caf\\303\\251.md"'), "docs/caf" + chr(0xE9) + ".md")

    def test_octal_three_byte_character(self):
        self.assertEqual(unquote_git_path('"\\342\\202\\254.txt"'), chr(0x20AC) + ".txt")

    def test_escaped_quote_and_backslash(self):
        self.assertEqual(unquote_git_path('"say \\"hi\\" back\\\\slash"'), 'say "hi" back\\slash')

    def test_lone_trailing_backslash_kept(self):
        self.assertEqual(unquote_git_path('"ends\\"'), "ends\\")

    def test_unknown_escape_keeps_character(self):
        self.assertEqual(unquote_git_path('"\\q"'), "q")

    def test_invalid_utf8_replaced(self):
        self.assertEqual(unquote_git_path('"bad\\377"'), "bad" + chr(0xFFFD))


class RecordsTest(unittest.TestCase):
    def test_lf(self):
        self.assertEqual(records("one\ntwo\n"), ["one", "two"])

    def test_crlf(self):
        self.assertEqual(records("one\r\ntwo\r\n"), ["one", "two"])

    def test_only_one_trailing_cr_dropped(self):
        self.assertEqual(records("one\r\r\n"), ["one\r"])

    def test_lone_cr_inside_line_kept(self):
        self.assertEqual(records("one\rtwo\n"), ["one\rtwo"])

    def test_other_line_breaks_do_not_split(self):
        for code in (0x0C, 0x0B, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029):
            with self.subTest(code=hex(code)):
                text = "one" + chr(code) + "two"
                self.assertEqual(records(text + "\n"), [text])

    def test_empty_text_has_no_lines(self):
        self.assertEqual(records(""), [])

    def test_last_line_without_newline_counts(self):
        self.assertEqual(records("one\ntwo"), ["one", "two"])

    def test_trailing_empty_line(self):
        self.assertEqual(records("one\n\n"), ["one", ""])

    def test_single_newline_is_one_empty_line(self):
        self.assertEqual(records("\n"), [""])


if __name__ == "__main__":
    unittest.main()
