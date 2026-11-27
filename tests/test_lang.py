"""Tests of muster.lang: the generic line counter, the Java counter, the reading of a JUnit Platform
unique id and of a Java test source, the language of a file, the languages of a profile and the
count of a file read from disk.

Every sample is synthetic. Non-ASCII and control characters are built at run time with chr().
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster.lang import BUILT_IN, JAVA, Language, count_file, generic_loc, language_of, languages
from muster.lang.java import is_test_declaration, java_loc, parse_killing_test, source_declares

C_LIKE = ("//", ("/*", "*/"))
PACKAGE = "org.example.app.billing.internal.ledger"
POSTING_TEST = PACKAGE + ".PostingTest"


def c_like(*lines: str) -> int:
    return generic_loc(list(lines), *C_LIKE)


class GenericCounterTest(unittest.TestCase):
    def test_first_marker_wins(self):
        self.assertEqual(c_like("total = 1; // a note /* not a block"), 1)
        self.assertEqual(c_like("// a note /* not a block", "total = 1;"), 1)
        self.assertEqual(c_like("/* a // note */ total = 1;"), 1)
        self.assertEqual(c_like("/* a block */ // then a note"), 0)

    def test_block_over_several_lines(self):
        lines = ["total = 1; /* opens", "  inside // still inside", "", "closes */", "x = 2;", "/*", "*/"]
        self.assertEqual(c_like(*lines), 2)

    def test_code_after_the_end_of_a_block_counts(self):
        self.assertEqual(c_like("/* opens", "closes */ x = 2;"), 1)
        self.assertEqual(c_like("/* opens", "closes */   "), 0)

    def test_blank_and_white_space_lines(self):
        self.assertEqual(c_like("", "   ", "\t", "x = 1;", " \t "), 1)

    def test_line_with_only_block_comments(self):
        self.assertEqual(c_like("/* one */", "   /* two */ /* three */  "), 0)
        self.assertEqual(c_like("/* one */ x /* two */"), 1)

    def test_markers_that_look_nested(self):
        # A block does not nest: its first end closes it, and the rest of the line is code.
        self.assertEqual(c_like("/* outer /* inner */ tail */"), 1)
        self.assertEqual(c_like("/* outer /* inner */"), 0)
        self.assertEqual(c_like("/*/ still open", "x = 1;", "*/"), 0)

    def test_marker_inside_a_string_reads_as_a_comment(self):
        # The known limitation: the generic counter does not know strings.
        self.assertEqual(c_like('"// quoted"'), 1)
        self.assertEqual(c_like('// quoted"'), 0)
        self.assertEqual(c_like('url = "http://example.org"'), 1)
        self.assertEqual(c_like('"/*"', "x = 1;", '"*/"'), 2)  # the line in between is lost

    def test_language_without_markers_counts_lines_that_are_not_blank(self):
        self.assertEqual(generic_loc(["# not a comment here", "", "  ", "x = 1"], None, None), 2)

    def test_only_a_line_comment(self):
        self.assertEqual(generic_loc(["# note", "x = 1  # note", '"""', "   # indented"], "#", None), 2)

    def test_only_a_block_comment(self):
        self.assertEqual(generic_loc(["(* opens", "x", "*) y", "// z"], None, ("(*", "*)")), 2)

    def test_longer_marker_wins_at_the_same_place(self):
        lines = ["--[[ opens a block", "inside", "]] x = 1", "-- a note", "y = 2 -- a note"]
        self.assertEqual(generic_loc(lines, "--", ("--[[", "]]")), 2)

    def test_longer_line_comment_wins_over_a_block_start_at_the_same_place(self):
        # The line comment ends its line, so no block is open on the next one.
        self.assertEqual(generic_loc(["--[[ a note", "x = 1"], "--[[", ("--", "]]")), 1)
        lines = ["-- opens a block", "inside", "]] y = 2", "--[[ a note -- ]]", "z = 3"]
        self.assertEqual(generic_loc(lines, "--[[", ("--", "]]")), 2)

    def test_long_line_with_many_blocks(self):
        self.assertEqual(c_like("/* a */ x = 1; " * 5000), 1)
        self.assertEqual(c_like("/* a */ " * 5000, "/* b */ " * 5000 + "y"), 1)
        self.assertEqual(c_like("/* a */ " * 5000 + "/* open", "inside", "close */"), 0)
        self.assertEqual(c_like("/* a */ " * 5000 + "// note */ y", "x = 1;"), 1)

    def test_long_line_with_many_blocks_before_a_far_line_comment(self):
        self.assertEqual(c_like("/* a */ x; " * 50000 + "// tail"), 1)
        self.assertEqual(c_like("/* a */ " * 50000 + "// tail", "/* b */ " * 50000 + "y // tail"), 1)
        self.assertEqual(c_like("// head /* a */ " * 50000, "x /* " + "// inside " * 50000 + "*/"), 1)

    def test_block_with_the_same_start_and_end(self):
        lines = ['"""', "a docstring", '"""', 'x = 1  """ inline """', '""" once """ y']
        self.assertEqual(generic_loc(lines, "#", ('"""', '"""')), 2)

    def test_line_comment_equal_to_the_block_start_wins(self):
        # A block would hide the second line and the closing marker; the line comment hides nothing more.
        self.assertEqual(generic_loc(["-- open", "x = 1", "]] y"], "--", ("--", "]]")), 2)


class JavaCounterTest(unittest.TestCase):
    SAMPLE = "\n".join(
        [
            "package org.example.app.auth;",  # 1
            "",
            "// a line comment",
            "/**",
            " * A block comment over lines.",
            " */",
            "public final class Token {",  # 2
            "    private static final String URL = \"https://example.org//path\";",  # 3
            "    /* inline */ private int size; /* again */",  # 4
            "    private final String note = \"\"\"",  # 5
            "        // text block content",  # 6
            "        \"\"\";",  # 7
            "    \t  ",
            "    char slash = '/';  // trailing note",  # 8
            "}",  # 9
            "",
        ]
    )

    def test_sample(self):
        self.assertEqual(java_loc(self.SAMPLE), 9)

    def test_crlf_and_odd_breaks(self):
        self.assertEqual(java_loc(self.SAMPLE.replace("\n", "\r\n")), 9)
        joined = "int a;" + chr(0x2028) + "int b;" + chr(0x0C) + "int c;\n// done\n"
        self.assertEqual(java_loc(joined), 1)

    def test_crlf_and_lf_give_the_same_count(self):
        java = BUILT_IN[JAVA]
        # A string whose backslash runs into the line end: the escape takes the CR, not the LF.
        unclosed = 'String s = "abc\\\n// inside literal";\nint a;\n'
        self.assertEqual(java.count(unclosed), 3)
        for text in (self.SAMPLE, unclosed, "int a;\r\r\n// note\r\n"):
            with self.subTest(text=text[:20]):
                self.assertEqual(java.count(text.replace("\n", "\r\n")), java.count(text))

    def test_built_in_java_uses_the_java_counter(self):
        java = BUILT_IN[JAVA]
        self.assertIs(java.counter, java_loc)
        # Java knows strings, the generic counter with the same markers does not.
        text = 'String open = "/*";\nint size;\nString close = "*/";\n'
        self.assertEqual(java.count(text), 3)
        self.assertEqual(Language("other", (".other",), "//", ("/*", "*/")).count(text), 2)


def unique_id(cls: str, *segments: str) -> str:
    """A killing test entry as PIT writes it: the class and a dot, then the JUnit Platform unique id
    of a test of that class, whose segments after the class segment are `segments`."""
    return cls + ".[engine:junit-jupiter]/[class:" + cls + "]/" + "/".join(segments)


def java_source(body: str) -> str:
    """The source of the top-level class POSTING_TEST with the class body `body`."""
    return "package " + PACKAGE + ";\n\nclass PostingTest {\n" + body + "}\n"


class UniqueIdTest(unittest.TestCase):
    def test_class_member_and_kind(self):
        cases = [
            (unique_id(POSTING_TEST, "[method:settles(java.lang.String[], int)]"), (POSTING_TEST, "settles", "method")),
            (
                unique_id(POSTING_TEST, "[nested-class:Daily]", "[nested-class:Closing]", "[method:reconciles()]"),
                (POSTING_TEST + "$Daily$Closing", "reconciles", "method"),
            ),
            (
                unique_id(POSTING_TEST, "[test-template:converts(int)]", "[test-template-invocation:#2]"),
                (POSTING_TEST, "converts", "test-template"),
            ),
            (
                unique_id(POSTING_TEST, "[test-factory:cases()]", "[dynamic-test:#1]"),
                (POSTING_TEST, "cases", "test-factory"),
            ),
            (POSTING_TEST + ".settles(" + POSTING_TEST + ")", None),  # a JUnit 4 entry
            ("", None),
        ]
        for entry, expected in cases:
            with self.subTest(entry=entry):
                self.assertEqual(parse_killing_test(entry), expected)


class SourceDeclaresTest(unittest.TestCase):
    SOURCE = java_source(
        "\n".join(
            [
                "    private static final char QUOTE = '\"';",
                '    private static final String NOTE = "@Test void quoted() {}";',
                '    private static final String BLOCK = """',
                "        @Test",
                "        void blocked() {}",
                '        """;',
                "    private final int[] limits = {7, 30};",
                "",
                "    // The field and the constructor carry @Test, so that only their shape keeps them out.",
                "    @Test",
                "    private int count;",
                "",
                "    @Test",
                "    PostingTest() {}",
                "",
                "    // @Test",
                "    // void lineHidden() {}",
                "",
                "    /*",
                "     * @Test",
                "     * void blockHidden() {}",
                "     */",
                "",
                "    @Test",
                "    void settles() {",
                "        audited(true, 2);",
                "    }",
                "",
                "    int audited(boolean open, int days) {",
                "        if (open) {",
                "            count++;",
                "        } else reopened();",
                "        if (days > 0) {",
                "            return refunded();",
                "        }",
                "        return switch (days) {",
                "            default -> {",
                "                yield yielded();",
                "            }",
                "        };",
                "    }",
                "",
                '    @DisplayName("Carries (and rounds) the balance")',
                "    @Timeout(value = 2, unit = TimeUnit.SECONDS)",
                "    public final @Test void carried() throws IOException, InterruptedException {}",
                "",
                "    @org.junit.jupiter.params.ParameterizedTest",
                "    @ValueSource(ints = {7, 30})",
                "    protected <T extends Number> void parsed(int days) {}",
                "",
                "    @Nested",
                "    class Daily {",
                "        @Test",
                "        void closes() {}",
                "",
                "        @Nested",
                "        class Closing {",
                '            @org.junit.jupiter.api.RepeatedTest(value = 3, name = "run {currentRepetition}")',
                "            void reconciles() {}",
                "        }",
                "    }",
                "",
            ]
        )
    )

    def test_only_a_test_of_the_killers_own_class_counts(self):
        cases = [
            ("", "settles", "present"),
            ("$Daily", "closes", "present"),
            ("$Daily$Closing", "reconciles", "present"),
            ("", "closes", "deleted"),  # a nested test for a top-level killer
            ("$Daily", "settles", "deleted"),  # a top-level test for a nested killer
            ("", "audited", "deleted"),  # no test annotation
            ("", "reopened", "deleted"),  # only called, after else, return or yield
            ("", "refunded", "deleted"),
            ("", "yielded", "deleted"),
            ("", "lineHidden", "deleted"),
            ("", "blockHidden", "deleted"),
            ("", "quoted", "deleted"),  # in a string
            ("", "blocked", "deleted"),  # in a text block
            ("", "carried", "present"),  # annotations and modifiers mixed
            ("", "parsed", "present"),
            ("", "PostingTest", "deleted"),  # the constructor
            ("", "count", "deleted"),  # the field
        ]
        for suffix, member, expected in cases:
            with self.subTest(cls=suffix, member=member):
                self.assertEqual(source_declares(self.SOURCE, POSTING_TEST + suffix, member), expected)

    def test_package_is_everything_before_the_last_dot(self):
        for cls, member in (("org.example.app$qa.PostingTest", "settles"), ("app$qa.PostingTest$Daily", "closes")):
            with self.subTest(cls=cls):
                self.assertEqual(source_declares(self.SOURCE, cls, member), "present")

    def test_keyword_before_a_call_is_no_return_type(self):
        for keyword in ("return", "else", "yield"):
            with self.subTest(keyword=keyword):
                self.assertFalse(is_test_declaration("\n    @Test\n    " + keyword + " refunded()", "refunded"))
        self.assertTrue(is_test_declaration("\n    @Test\n    Object refunded()", "refunded"))

    def test_method_with_annotations_the_scan_does_not_know(self):
        # One of them may be an annotation of its own that carries @Test: not ruled out.
        cases = [
            ('    @FastTest\n    @DisplayName("Settles a refund")\n    void settles() {}\n', "unresolved"),
            ("    public @FastTest void settles() {}\n", "unresolved"),
            ("    @Override\n    public void settles() {}\n", "unresolved"),
            ("    void settles() {}\n", "deleted"),
            ("    @FastTest\n    void refunds() {}\n", "deleted"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(source_declares(java_source(body), POSTING_TEST, "settles"), expected)

    def test_bare_carriage_return_ends_a_line_comment(self):
        cr = chr(13)
        for end in (cr, cr + "\n"):
            with self.subTest(end=end):
                text = java_source(f"    // settles a refund{end}    @Test{end}    void settles() {{}}\n")
                self.assertEqual(source_declares(text, POSTING_TEST, "settles"), "present")

    def test_overloads_are_one_declaration(self):
        # Only the method name is compared: the killer names the overload without a test annotation.
        text = java_source("    @Test\n    void split(int parts) {}\n\n    void split(String label) {}\n")
        cls, member, _ = parse_killing_test(unique_id(POSTING_TEST, "[method:split(java.lang.String)]"))
        self.assertEqual(source_declares(text, cls, member), "present")

    def test_source_that_cannot_be_followed_is_unresolved(self):
        body = "    @Test\n    void settles() {{\n        {}\n    }}\n"
        cases = [
            ("unterminated block comment", "", java_source("    /* open\n    @Test\n    void settles() {}\n")),
            ("unterminated string", "", java_source('    String label = "open;\n    @Test\n    void settles() {}\n')),
            ("unbalanced braces", "", java_source("    @Test\n    void settles() {\n")),
            ("unbalanced parentheses in a body", "", java_source(body.format("consume(;"))),
            ("crossed brackets in a body", "", java_source(body.format("total = ([)];"))),
            ("class header twice", "$Daily", java_source("    class Daily {}\n    @Nested\n    class Daily {}\n")),
            ("class header not found", "$Monthly", self.SOURCE),
        ]
        for label, suffix, text in cases:
            with self.subTest(label):
                self.assertEqual(source_declares(text, POSTING_TEST + suffix, "settles"), "unresolved")


class LanguageOfTest(unittest.TestCase):
    def setUp(self):
        self.langs = languages(
            [
                {"name": "typescript", "extensions": [".ts"], "line_comment": "//"},
                {"name": "declarations", "extensions": [".d.ts"]},
                {"name": "python", "extensions": [".py"], "line_comment": "#"},
            ]
        )

    def name(self, path: str) -> str | None:
        found = language_of(path, self.langs)
        return None if found is None else found.name

    def test_longest_suffix_wins(self):
        self.assertEqual(self.name("web/src/types/api.d.ts"), "declarations")
        self.assertEqual(self.name("web/src/types/api.ts"), "typescript")
        reordered = list(reversed(self.langs))
        self.assertEqual(language_of("web/src/types/api.d.ts", reordered).name, "declarations")
        # A name that is the longest extension has no language, not that of a shorter one.
        for langs in (self.langs, reordered):
            self.assertIsNone(language_of("web/src/types/.d.ts", langs))
        self.assertIsNone(self.name("web/src/types/.ts"))
        self.assertEqual(self.name("web/src/types/x.d.ts"), "declarations")

    def test_case_sensitive(self):
        self.assertEqual(self.name("src/main/java/org/example/app/Token.java"), "java")
        self.assertIsNone(self.name("src/main/java/org/example/app/Token.JAVA"))
        self.assertIsNone(self.name("tools/build.PY"))

    def test_no_extension(self):
        self.assertIsNone(self.name("tools/Makefile"))
        self.assertIsNone(self.name("tools/notes.txt"))
        self.assertIsNone(self.name("tools/build.py.orig"))

    def test_only_the_file_name_is_matched(self):
        self.assertIsNone(self.name("tools/lib.py/README"))

    def test_dot_file(self):
        self.assertIsNone(self.name("tools/.py"))
        self.assertIsNone(self.name("src/main/java/.java"))
        self.assertEqual(self.name("tools/.hidden.py"), "python")


class LanguagesTest(unittest.TestCase):
    def test_built_in_java(self):
        (java,) = languages([])
        self.assertEqual((java.name, java.extensions), ("java", (".java",)))
        self.assertEqual((java.line_comment, java.block_comment), ("//", ("/*", "*/")))

    def test_new_language(self):
        java, python = languages(
            [{"name": "python", "extensions": [".py", ".pyi"], "line_comment": "#", "skip_patterns": ["@skip"]}]
        )
        self.assertEqual(java, BUILT_IN[JAVA])
        self.assertEqual(python, Language("python", (".py", ".pyi"), "#", None, ("@skip",)))
        markup = languages([{"name": "markup", "extensions": [".page"], "block_comment": ["<!--", "-->"]}])[1]
        self.assertEqual((markup.line_comment, markup.block_comment), (None, ("<!--", "-->")))

    def test_java_entry_adds_extensions_and_keeps_its_counter(self):
        entry = {"name": "java", "extensions": [".java", ".jav", ".jav"], "skip_patterns": ["@Disabled"]}
        (java,) = languages([entry])
        self.assertEqual(java.extensions, (".java", ".jav"))
        self.assertEqual(java.skip_patterns, ("@Disabled",))
        self.assertIs(java.counter, java_loc)
        self.assertIs(language_of("src/main/java/org/example/app/Old.jav", [java]), java)

    def test_java_entry_without_extensions(self):
        (java,) = languages([{"name": "java", "line_comment": "//"}])
        self.assertEqual(java.extensions, (".java",))


class CountFileTest(unittest.TestCase):
    PYTHON = Language("python", (".py",), "#", None)

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def count(self, data: bytes, lang: Language) -> int:
        path = self.folder / "sample.bin"
        path.write_bytes(data)
        return count_file(path, lang)

    def test_invalid_utf8_is_replaced(self):
        self.assertEqual(self.count(b"x = '\xff\xfe'\n# \xc3\n\n", self.PYTHON), 1)
        self.assertEqual(self.count(b"int a = '\xff';\n// \xc3\n", BUILT_IN[JAVA]), 1)
        # A line that is only a bad byte keeps its replacement character, so it is code.
        self.assertEqual(self.count(b"\xff\n# \xfe\n\xc3\n", self.PYTHON), 2)
        self.assertEqual(self.count(b"\xff\n// \xfe\n", BUILT_IN[JAVA]), 1)

    def test_leading_byte_order_mark_is_dropped(self):
        bom = chr(0xFEFF).encode("utf-8")
        self.assertEqual(self.count(bom + b"# a note\nx = 1\n", self.PYTHON), 1)
        self.assertEqual(self.count(bom + b"// a note\nint a;\n", BUILT_IN[JAVA]), 1)
        self.assertEqual(self.count(bom + b"\nint a;\n", BUILT_IN[JAVA]), 1)
        # Only one, and only at the start: a second one is a character of the line.
        self.assertEqual(self.count(bom + bom + b"# a note\n", self.PYTHON), 1)
        self.assertEqual(self.count(b"x = 1\n" + bom + b"\n", self.PYTHON), 2)

    def test_other_line_breaks_never_split_a_line(self):
        for code in (0x0C, 0x2028, 0x0D):
            with self.subTest(code=hex(code)):
                text = "# a note" + chr(code) + "x = 1\n" + "x = 2" + chr(code) + "y = 3\n"
                self.assertEqual(self.count(text.encode("utf-8"), self.PYTHON), 1)
                java = "// a note" + chr(code) + "int a;\n" + "int b;" + chr(code) + "int c;\n"
                self.assertEqual(self.count(java.encode("utf-8"), BUILT_IN[JAVA]), 1)

    def test_crlf(self):
        self.assertEqual(self.count(b"x = 1\r\n# note\r\n\r\ny = 2\r\n", self.PYTHON), 2)
        self.assertEqual(self.count(b"int a;\r\n/* a\r\n b */\r\nint b;", BUILT_IN[JAVA]), 2)

    def test_empty_file(self):
        self.assertEqual(self.count(b"", self.PYTHON), 0)

    def test_file_that_cannot_be_read(self):
        with self.assertRaises(FileNotFoundError):
            count_file(self.folder / "gone.py", self.PYTHON)
        error = PermissionError(13, "synthetic reason")
        with mock.patch("builtins.open", side_effect=error), self.assertRaises(OSError) as caught:
            count_file(self.folder / "sample.py", self.PYTHON)
        self.assertEqual(caught.exception.strerror, "synthetic reason")

    @unittest.skipIf(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0, "needs POSIX modes")
    def test_file_without_read_permission(self):
        path = self.folder / "locked.py"
        path.write_bytes(b"x = 1\n")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        try:
            path.open("rb").close()
        except PermissionError as exc:
            reason = exc.strerror
        else:
            self.skipTest("chmod does not make a file unreadable here")
        with self.assertRaises(PermissionError) as caught:
            count_file(path, self.PYTHON)
        self.assertEqual(caught.exception.strerror, reason)


if __name__ == "__main__":
    unittest.main()
