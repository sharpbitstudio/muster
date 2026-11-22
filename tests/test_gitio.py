"""Tests of muster.gitio: running git, the repository root, the merge base,
reading git output, and the changes since a base.

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

from muster.gitio import (
    binary_counts_since,
    count_file_lines,
    diff_since,
    find_repo_root,
    git,
    merge_base,
    numstat_since,
    parse_diff,
    records,
    ref_problem,
    unquote_git_path,
    untracked_under,
)
from muster.report import ReportError

IDENTITY = (
    "-c", "user.name=Test",
    "-c", "user.email=test@users.noreply.github.com",
    "-c", "commit.gpgsign=false",
)

ENGINE = "src/main/java/org/example/app/billing/internal/ledger/Engine.java"
TOKEN = "src/main/java/org/example/app/auth/Token.java"
KEY_PARSER = "src/main/java/org/example/app/codec/KeyParser.java"
DISPATCHER = "src/main/java/org/example/app/queue/Dispatcher.java"
TOKEN_CHECK = "src/test/java/org/example/app/auth/TokenCheck.java"
# Line breaks of str.splitlines() that are no line end for git: form feed,
# U+2028, NEL, file separator, vertical tab, U+2029.
ODD_BREAKS = [chr(code) for code in (0x0C, 0x2028, 0x85, 0x1C, 0x0B, 0x2029)]
# A folder whose name is a glob, a sibling folder "ac" and a sibling file "bc".
# git matches a glob pathspec against the whole path, so as a pattern the name
# would take in the file "bc" (the files in "ac" have a longer path).
GLOB_FOLDER = "src/main/java/org/example/app/[ab]c"
GLOB_FOLDER_FILE = GLOB_FOLDER + "/Token.java"
GLOB_SIBLINGS = ("src/main/java/org/example/app/ac/Token.java", "src/main/java/org/example/app/bc")


def same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))


def diff_text(*lines: str, end: str = "\n") -> str:
    return "".join(line + end for line in lines)


def file_header(path: str) -> tuple[str, ...]:
    return (
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
    )


def c_quote(path: str) -> str:
    """`path` as git quotes it: `"` and `\\` escaped, bytes above 0x7F in octal."""
    out = []
    for byte in path.encode("utf-8"):
        if byte >= 0x80:
            out.append(f"\\{byte:03o}")
        elif chr(byte) in '"\\':
            out.append("\\" + chr(byte))
        else:
            out.append(chr(byte))
    return '"' + "".join(out) + '"'


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

    def test_valid_ref_resolves(self):
        self.assertEqual(merge_base(self.repo, "main"), self.fork)

    def test_option_like_or_empty_ref_never_reaches_git(self):
        for ref in ("-x", "--all", ""):
            with self.subTest(ref=ref), mock.patch("muster.gitio.subprocess.run") as run:
                with self.assertRaises(ValueError) as ctx:
                    merge_base(self.repo, ref)
                run.assert_not_called()
                self.assertIn(repr(ref), str(ctx.exception))


class RefProblemTest(unittest.TestCase):
    def test_problems(self):
        cases = {"": "is empty", "-x": "starts with -", "--all": "starts with -", "main\0x": "has a NUL"}
        for ref, problem in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(ref_problem(ref), problem)

    def test_ordinary_refs_pass(self):
        for ref in ("main", "origin/main", "HEAD~2", "feature/retry-queue", "a" * 40):
            with self.subTest(ref=ref):
                self.assertIsNone(ref_problem(ref))


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


class ParseDiffTest(unittest.TestCase):
    def test_hunk_without_counts_adds_one_line(self):
        text = diff_text(*file_header(ENGINE), "@@ -4 +5 @@", "-old", "+new")
        self.assertEqual(parse_diff(text, ""), {ENGINE: {5}})

    def test_pure_deletion_adds_nothing(self):
        text = diff_text(*file_header(ENGINE), "@@ -7,2 +6,0 @@", "-one", "-two")
        self.assertEqual(parse_diff(text, ""), {})

    def test_insertion_adds_every_new_line(self):
        text = diff_text(*file_header(ENGINE), "@@ -29,0 +30,3 @@", "+one", "+two", "+three")
        self.assertEqual(parse_diff(text, ""), {ENGINE: {30, 31, 32}})

    def test_deleted_file_adds_nothing(self):
        text = diff_text(
            f"diff --git a/{ENGINE} b/{ENGINE}",
            "deleted file mode 100644",
            "index 1111111..0000000",
            f"--- a/{ENGINE}",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-one",
            "-two",
            *file_header(TOKEN),
            "@@ -1 +1 @@",
            "-old",
            "+new",
        )
        self.assertEqual(parse_diff(text, ""), {TOKEN: {1}})

    def test_quoted_path_is_unquoted(self):
        name = 'src/main/java/org/example/app/codec/Key"Parser' + chr(0x65E5) + chr(0x672C) + ".java"
        old, new = c_quote("a/" + name), c_quote("b/" + name)
        self.assertIn('\\"Parser\\346\\227\\245\\346\\234\\254.java"', new)
        text = diff_text(f"diff --git {old} {new}", f"--- {old}", f"+++ {new}", "@@ -2 +2 @@", "-old", "+new")
        self.assertEqual(parse_diff(text, ""), {name: {2}})

    def test_no_newline_marker_changes_no_count(self):
        text = diff_text(
            *file_header(ENGINE),
            "@@ -9 +9,2 @@",
            "-last",
            "\\ No newline at end of file",
            "+last",
            "+added",
            "\\ No newline at end of file",
            *file_header(TOKEN),
            "@@ -3 +3 @@",
            "-old",
            "+new",
        )
        self.assertEqual(parse_diff(text, ""), {ENGINE: {9, 10}, TOKEN: {3}})

    def test_prefix_is_stripped_and_files_outside_it_left_out(self):
        text = diff_text(
            *file_header(ENGINE),
            "@@ -1 +1 @@",
            "-old",
            "+new",
            *file_header(TOKEN_CHECK),
            "@@ -2 +2 @@",
            "-old",
            "+new",
            *file_header("build.gradle"),
            "@@ -3 +3 @@",
            "-old",
            "+new",
        )
        expected = {"org/example/app/billing/internal/ledger/Engine.java": {1}}
        self.assertEqual(parse_diff(text, "src/main/java/"), expected)

    def test_context_lines_of_merged_hunk_are_not_changes(self):
        text = diff_text(
            *file_header(ENGINE),
            "@@ -3,6 +3,6 @@",
            "-old three",
            "+new three",
            " four",
            " five",
            " six",
            " seven",
            "-old eight",
            "+new eight",
        )
        self.assertEqual(parse_diff(text, ""), {ENGINE: {3, 8}})

    def test_hunk_lines_that_look_like_file_headers(self):
        text = diff_text(
            *file_header(ENGINE),
            "@@ -2 +2 @@",
            "--- x",
            "+++ x",
            "@@ -10,0 +11 @@",
            "+later",
        )
        self.assertEqual(parse_diff(text, ""), {ENGINE: {2, 11}})

    def test_only_line_feed_ends_a_record(self):
        breaks = [*ODD_BREAKS, "\r"]
        lines = [
            *file_header(ENGINE),
            f"@@ -1,{len(breaks) + 1} +1,{len(breaks) + 1} @@",
            *(f" context{brk} more" for brk in breaks),
            "-old",
            "+new",
            f"@@ -20,0 +21,{len(breaks)} @@",
            *(f"+added{brk}+more" for brk in breaks),
        ]
        expected = {ENGINE: {len(breaks) + 1, *range(21, 21 + len(breaks))}}
        for end in ("\n", "\r\n"):
            with self.subTest(end=repr(end)):
                self.assertEqual(parse_diff(diff_text(*lines, end=end), ""), expected)

    def test_short_hunk_ends_at_next_file(self):
        text = diff_text(
            *file_header(ENGINE),
            "@@ -1,5 +1,5 @@",
            "-old",
            "+new",
            *file_header(TOKEN),
            "@@ -3 +3 @@",
            "-old",
            "+new",
        )
        self.assertEqual(parse_diff(text, ""), {ENGINE: {1}, TOKEN: {3}})


class CountFileLinesTest(unittest.TestCase):
    def test_counts_records(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        odd = ("one" + chr(0x0C) + chr(0x2028) + chr(0x85) + "\r" + "two").encode("utf-8")
        cases = [(b"", 0), (b"x", 1), (b"x\n", 1), (b"a\n\nb", 3), (b"a\r\nb\r\n", 2), (odd, 1)]
        for number, (data, expected) in enumerate(cases):
            with self.subTest(data=data):
                path = folder / f"sample{number}.txt"
                path.write_bytes(data)
                self.assertEqual(count_file_lines(path), expected)


class ChangesRepoCase(GitRepoCase):
    """A repository whose files are written and committed by path."""

    def write(self, root: Path, relative: str, data: str):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8"))

    def commit_all(self, repo: Path, message: str) -> str:
        self.run_git(repo, "add", "-A")
        return self.commit(repo, message)


class DiffSinceTest(ChangesRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def test_lines_with_other_line_breaks(self):
        # The text after each break starts with a space: split there, it would read as a context line.
        lines = [f"line{number}{brk} tail" for number, brk in enumerate(ODD_BREAKS, 1)]
        self.write(self.repo, ENGINE, "\n".join(lines) + "\n")
        base = self.commit_all(self.repo, "base")
        lines[1] = "edited" + ODD_BREAKS[1] + " tail"
        lines[4] = "edited" + ODD_BREAKS[4] + " tail"
        self.write(self.repo, ENGINE, "\n".join(lines) + "\n")
        self.assertEqual(parse_diff(diff_since(self.repo, base, ["src/main/java"]), ""), {ENGINE: {2, 5}})

    def test_inter_hunk_context_of_repo_config_adds_no_lines(self):
        self.run_git(self.repo, "config", "--local", "diff.interHunkContext", "10")
        lines = [f"line {number}" for number in range(1, 21)]
        self.write(self.repo, ENGINE, "\n".join(lines) + "\n")
        base = self.commit_all(self.repo, "base")
        lines[4] = "edited 5"
        lines[9] = "edited 10"
        self.write(self.repo, ENGINE, "\n".join(lines) + "\n")
        self.assertEqual(parse_diff(diff_since(self.repo, base, ["src/main/java"]), ""), {ENGINE: {5, 10}})

    def test_paths_relative_to_profile_root_in_subfolder(self):
        profile_root = self.repo / "app"
        self.write(profile_root, ENGINE, "one\n")
        self.write(self.repo, DISPATCHER, "one\n")
        base = self.commit_all(self.repo, "base")
        self.write(profile_root, ENGINE, "edited\n")
        self.write(self.repo, DISPATCHER, "edited\n")
        changed = parse_diff(diff_since(profile_root, base, ["src/main/java"]), "")
        self.assertEqual(changed, {ENGINE: {1}})

    def test_glob_characters_in_folder_match_literally(self):
        for path in (GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "one\n")
        base = self.commit_all(self.repo, "base")
        for path in (GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "edited\n")
        changed = parse_diff(diff_since(self.repo, base, [GLOB_FOLDER]), "")
        self.assertEqual(changed, {GLOB_FOLDER_FILE: {1}})


class UntrackedUnderTest(ChangesRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.profile_root = self.repo / "app"
        self.write(self.profile_root, ".gitignore", "*.log\n")
        self.write(self.profile_root, ENGINE, "one\n")
        self.commit_all(self.repo, "base")

    def test_lists_untracked_files_under_folders(self):
        self.write(self.profile_root, TOKEN_CHECK, "one\n")
        self.write(self.profile_root, DISPATCHER, "one\n")
        self.write(self.profile_root, "src/main/java/org/example/app/queue/trace.log", "one\n")
        self.write(self.profile_root, "docs/notes.txt", "one\n")
        found = untracked_under(self.profile_root, ["src/test/java", "src/main/java"])
        self.assertEqual(found, [DISPATCHER, TOKEN_CHECK])

    def test_glob_characters_in_folder_match_literally(self):
        for path in (GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.profile_root, path, "one\n")
        self.assertEqual(untracked_under(self.profile_root, [GLOB_FOLDER]), [GLOB_FOLDER_FILE])


class PathspecVariableTest(ChangesRepoCase):
    """A pathspec variable in the environment (a git hook started with --literal-pathspecs passes
    one on) changes neither helper: the folders still match literally."""

    VARIABLES = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        for path in (ENGINE, GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "one\n")
        self.base = self.commit_all(self.repo, "base")
        for path in (ENGINE, GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "edited\n")
        self.write(self.repo, TOKEN, "one\n")
        self.write(self.repo, GLOB_FOLDER + "/Session.java", "one\n")
        self.write(self.repo, "src/main/java/org/example/app/ac/Session.java", "one\n")

    def test_change_under_a_root_is_seen(self):
        for variable in self.VARIABLES:
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: "1"}):
                changed = parse_diff(diff_since(self.repo, self.base, ["src/main/java"]), "")
                self.assertIn(ENGINE, changed)
                found = untracked_under(self.repo, ["src/main/java"])
                self.assertIn(TOKEN, found)

    def test_glob_characters_in_folder_still_match_literally(self):
        for variable in self.VARIABLES:
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: "1"}):
                changed = parse_diff(diff_since(self.repo, self.base, [GLOB_FOLDER]), "")
                self.assertEqual(changed, {GLOB_FOLDER_FILE: {1}})
                found = untracked_under(self.repo, [GLOB_FOLDER])
                self.assertEqual(found, [GLOB_FOLDER + "/Session.java"])


MOVED_CHECK = "src/main/java/org/example/app/auth/TokenCheck.java"


class NumstatSinceTest(ChangesRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def test_added_modified_and_deleted_files(self):
        self.write(self.repo, ENGINE, "one\ntwo\nthree\n")
        self.write(self.repo, TOKEN, "one\ntwo\n")
        base = self.commit_all(self.repo, "base")
        self.write(self.repo, ENGINE, "one\nedited\nthree\nfour\nfive\n")
        (self.repo / TOKEN).unlink()
        self.write(self.repo, KEY_PARSER, "one\ntwo\nthree\n")
        self.run_git(self.repo, "add", "-N", KEY_PARSER)
        found = sorted(numstat_since(self.repo, base, ["src/main/java"]), key=lambda change: change[2])
        self.assertEqual(found, [(0, 2, TOKEN), (3, 1, ENGINE), (3, 0, KEY_PARSER)])

    def test_rename_across_folders_is_a_deletion_and_an_addition(self):
        self.write(self.repo, TOKEN_CHECK, "one\ntwo\nthree\nfour\n")
        base = self.commit_all(self.repo, "base")
        (self.repo / MOVED_CHECK).parent.mkdir(parents=True)
        self.run_git(self.repo, "mv", TOKEN_CHECK, MOVED_CHECK)
        self.run_git(self.repo, "config", "--local", "diff.renames", "true")
        found = numstat_since(self.repo, base, ["src/main/java", "src/test/java"])
        self.assertEqual(sorted(found, key=lambda change: change[2]), [(4, 0, MOVED_CHECK), (0, 4, TOKEN_CHECK)])

    def test_binary_file_has_no_counts(self):
        self.write(self.repo, DISPATCHER, "one\n")
        base = self.commit_all(self.repo, "base")
        (self.repo / DISPATCHER).write_bytes(b"one\n\0two\n")
        self.assertEqual(numstat_since(self.repo, base, ["src/main/java"]), [(None, None, DISPATCHER)])

    def test_paths_relative_to_profile_root_in_subfolder(self):
        profile_root = self.repo / "app"
        self.write(profile_root, ENGINE, "one\n")
        self.write(self.repo, DISPATCHER, "one\n")
        base = self.commit_all(self.repo, "base")
        self.write(profile_root, ENGINE, "edited\n")
        self.write(self.repo, DISPATCHER, "edited\n")
        self.assertEqual(numstat_since(profile_root, base, ["src/main/java"]), [(1, 1, ENGINE)])

    def test_glob_characters_in_folder_match_literally(self):
        for path in (GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "one\n")
        base = self.commit_all(self.repo, "base")
        for path in (GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "edited\nmore\n")
        self.assertEqual(numstat_since(self.repo, base, [GLOB_FOLDER]), [(2, 1, GLOB_FOLDER_FILE)])

    def test_path_with_non_ascii_characters_or_a_tab(self):
        name = "src/main/java/org/example/app/codec/Key" + chr(0x00E9) + chr(0x4E2D) + "Parser.java"
        self.write(self.repo, ENGINE, "one\n")
        base = self.commit_all(self.repo, "base")
        self.write(self.repo, name, "one\n")
        self.run_git(self.repo, "add", "-N", "--", name)
        self.assertEqual(numstat_since(self.repo, base, ["src/main/java"]), [(1, 0, name)])
        tabbed = "src/main/java/org/example/app/codec/Key" + chr(9) + "Parser.java"
        try:
            self.write(self.repo, tabbed, "one\ntwo\n")
        except OSError:
            self.skipTest("the file system takes no tab in a file name")
        self.run_git(self.repo, "add", "-N", "--", tabbed)
        self.assertEqual(sorted(numstat_since(self.repo, base, ["src/main/java"])), [(1, 0, name), (2, 0, tabbed)])

    def test_pathspec_variables_change_nothing(self):
        for path in (ENGINE, GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "one\n")
        base = self.commit_all(self.repo, "base")
        for path in (ENGINE, GLOB_FOLDER_FILE, *GLOB_SIBLINGS):
            self.write(self.repo, path, "edited\n")
        for variable in PathspecVariableTest.VARIABLES:
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {variable: "1"}):
                self.assertEqual(numstat_since(self.repo, base, [GLOB_FOLDER]), [(1, 1, GLOB_FOLDER_FILE)])
                self.assertIn((1, 1, ENGINE), numstat_since(self.repo, base, ["src/main/java"]))


class BinaryCountsSinceTest(ChangesRepoCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def test_file_marked_as_no_diff(self):
        self.write(self.repo, "src/main/java/.gitattributes", "*.java -diff\n")
        self.write(self.repo, ENGINE, "one\ntwo\nthree\nfour\n")
        base = self.commit_all(self.repo, "base")
        self.write(self.repo, ENGINE, "one\nedited\nthree\nfour\nfive\nsix\n")
        self.assertEqual(numstat_since(self.repo, base, ["src/main/java"]), [(None, None, ENGINE)])
        self.assertEqual(binary_counts_since(self.repo, base, ENGINE), (3, 1))

    def test_file_with_a_nul_byte(self):
        self.write(self.repo, TOKEN, "one\ntwo\nthree\n")
        base = self.commit_all(self.repo, "base")
        (self.repo / TOKEN).write_bytes(b"one\nt\0wo\n")
        self.assertEqual(binary_counts_since(self.repo, base, TOKEN), (1, 2))

    def test_deleted_file(self):
        self.write(self.repo, TOKEN, "one\0\ntwo\nthree\n")
        base = self.commit_all(self.repo, "base")
        (self.repo / TOKEN).unlink()
        self.assertEqual(binary_counts_since(self.repo, base, TOKEN), (0, 3))

    def test_path_is_literal_and_relative_to_profile_root(self):
        profile_root = self.repo / "app"
        self.write(profile_root, GLOB_FOLDER_FILE, "one\0\n")
        self.write(profile_root, "src/main/java/org/example/app/ac/Token.java", "one\0\n")
        base = self.commit_all(self.repo, "base")
        self.write(profile_root, GLOB_FOLDER_FILE, "one\0\ntwo\n")
        self.write(profile_root, "src/main/java/org/example/app/ac/Token.java", "edited\0\nmore\nlines\n")
        self.assertEqual(binary_counts_since(profile_root, base, GLOB_FOLDER_FILE), (1, 0))
        with mock.patch.dict(os.environ, {"GIT_GLOB_PATHSPECS": "1"}):
            self.assertEqual(binary_counts_since(profile_root, base, GLOB_FOLDER_FILE), (1, 0))


class GitNotRunnableTest(GitRepoCase):
    def test_os_error_becomes_report_error(self):
        calls = {
            "diff_since": lambda: diff_since(self.root, "HEAD", ["src/main/java"]),
            "untracked_under": lambda: untracked_under(self.root, ["src/main/java"]),
        }
        for name, call in calls.items():
            with self.subTest(helper=name), mock.patch("muster.gitio.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(ReportError):
                    call()

    def test_message_gives_the_reason_of_the_os_error(self):
        calls = {
            "git diff": lambda: diff_since(self.root, "HEAD", ["src/main/java"]),
            "git ls-files": lambda: untracked_under(self.root, ["src/main/java"]),
        }
        error = FileNotFoundError(2, "synthetic reason")
        for name, call in calls.items():
            with self.subTest(helper=name), mock.patch("muster.gitio.subprocess.run", side_effect=error):
                with self.assertRaises(ReportError) as caught:
                    call()
                self.assertEqual(str(caught.exception), f"{name} could not run: synthetic reason")

    def test_line_count_helpers_give_the_reason_of_the_os_error(self):
        calls = {
            "numstat_since": lambda: numstat_since(self.root, "HEAD", ["src/main/java"]),
            "binary_counts_since": lambda: binary_counts_since(self.root, "HEAD", ENGINE),
        }
        error = FileNotFoundError(2, "synthetic reason")
        for name, call in calls.items():
            with self.subTest(helper=name), mock.patch("muster.gitio.subprocess.run", side_effect=error):
                with self.assertRaises(ReportError) as caught:
                    call()
                self.assertEqual(str(caught.exception), "git diff could not run: synthetic reason")

    def test_missing_folder_becomes_report_error(self):
        gone = self.root / "gone"
        with self.assertRaises(ReportError):
            diff_since(gone, "HEAD", ["src/main/java"])
        with self.assertRaises(ReportError):
            untracked_under(gone, ["src/main/java"])


if __name__ == "__main__":
    unittest.main()
