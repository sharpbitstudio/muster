"""Tests of muster.report: annotations, the verdict state, the Markdown summary and the JSON
envelope.

Non-ASCII and control characters are written as escapes.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muster import __version__, report
from muster.config import ConfigError, validate
from muster.report import (
    Report,
    cap_markdown,
    cell_span,
    code_span,
    envelope,
    escape_data,
    escape_property,
    has_control,
    json_text,
    md_path,
    one_line,
    percent_digits,
    shown_names,
    write_json,
    write_markdown,
)

NOTICE = "\n\n_Report truncated: the step summary is capped at ~900 KiB._\n"
NOTICE_BYTES = len(NOTICE.encode("utf-8"))
E_ACUTE = "\u00e9"  # two bytes in UTF-8
LONG_LINE = "z" * 100 + "\n"  # longer than the notice, so a small budget truncates


class EscapeDataTest(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(escape_data("all tests passed: 3, skipped 0"), "all tests passed: 3, skipped 0")

    def test_percent_and_line_breaks(self):
        self.assertEqual(escape_data("100%\r\nnext\n"), "100%25%0D%0Anext%0A")

    def test_percent_escaped_first(self):
        self.assertEqual(escape_data("%0A"), "%250A")


class EscapePropertyTest(unittest.TestCase):
    def test_colon_and_comma(self):
        self.assertEqual(escape_property("coverage: line, branch"), "coverage%3A line%2C branch")

    def test_data_escapes_included(self):
        self.assertEqual(escape_property("50%\n:"), "50%25%0A%3A")


class OneLineTest(unittest.TestCase):
    def test_whitespace_runs_collapse(self):
        self.assertEqual(one_line("  first \t second\n\nthird\r\n"), "first second third")

    def test_empty(self):
        self.assertEqual(one_line(" \n "), "")


class MdPathTest(unittest.TestCase):
    def test_wraps_in_code_span(self):
        self.assertEqual(md_path("org/example/app/Main.java"), "`org/example/app/Main.java`")

    def test_backtick_becomes_quote(self):
        self.assertEqual(md_path("a`b``c"), "`a'b''c`")


class CodeSpanTest(unittest.TestCase):
    def test_blank_text_is_a_span_of_one_space(self):
        for text in ("", " ", "\t\r\n "):
            with self.subTest(text=text):
                self.assertEqual(code_span(text), "` `")

    def test_backtick_and_line_break_as_md_path_and_one_line(self):
        text = " run `all`\n  tests\r\n"
        self.assertEqual(code_span(text), "`run 'all' tests`")
        self.assertEqual(code_span(text), md_path(one_line(text)))


class CellSpanTest(unittest.TestCase):
    def test_pipe_is_escaped(self):
        self.assertEqual(cell_span("left|right\n|"), "`left\\|right \\|`")
        self.assertEqual(cell_span(" "), "` `")


class ShownNamesTest(unittest.TestCase):
    NAMES = [f"name{i}" for i in range(11)]

    def test_more_only_past_the_cap(self):
        self.assertEqual(shown_names(self.NAMES[:10]), ", ".join(self.NAMES[:10]))
        self.assertEqual(shown_names(self.NAMES), ", ".join(self.NAMES[:10]) + " (+1 more)")
        self.assertEqual(shown_names([]), "")

    def test_custom_cap_and_separator(self):
        self.assertEqual(shown_names(self.NAMES[:3], cap=3, sep="; "), "name0; name1; name2")
        self.assertEqual(shown_names(self.NAMES[:5], cap=2, sep="; "), "name0; name1 (+3 more)")


class HasControlTest(unittest.TestCase):
    def test_c0_del_and_c1(self):
        for code in (0x00, 0x09, 0x0A, 0x1F, 0x7F, 0x80, 0x85, 0x9F):
            with self.subTest(code=hex(code)):
                self.assertTrue(has_control("text" + chr(code) + "more"))

    def test_other_characters(self):
        for text in ("", "plain text", chr(0xA0), chr(0xE9), chr(0x200B)):
            with self.subTest(text=text):
                self.assertFalse(has_control(text))

    def test_line_separator_is_a_control_only_for_the_profile(self):
        self.assertFalse(has_control(chr(0x2028)))
        with self.assertRaises(ConfigError) as ctx:
            validate({"zones": {"base": "app/" + chr(0x2028)}})
        self.assertIn("has a control character", str(ctx.exception))


class PercentDigitsTest(unittest.TestCase):
    def test_number_as_the_profile_gives_it(self):
        cases = ((0.8, "80"), (0.07, "7"), (0.825, "82.5"), (1, "100"), (1.0, "100"), (0, "0"), (-0.0, "0"))
        for fraction, text in cases:
            with self.subTest(fraction=fraction):
                self.assertEqual(percent_digits(fraction), text)

    def test_never_rounded(self):
        self.assertEqual(percent_digits(0.800000000001), "80.0000000001")
        self.assertEqual(percent_digits(0.123456789012345), "12.3456789012345")


class CapMarkdownTest(unittest.TestCase):
    def test_under_budget_unchanged(self):
        self.assertEqual(cap_markdown("one\ntwo\n", 100), "one\ntwo\n")

    def test_exactly_at_budget_unchanged(self):
        text = "x" * 10 + "\n"
        self.assertEqual(cap_markdown(text, len(text)), text)

    def test_over_budget_cut_at_line_end(self):
        text = "first\nsecond\n" + LONG_LINE
        budget = NOTICE_BYTES + len("first\nsec")
        out = cap_markdown(text, budget)
        self.assertEqual(out, "first\n" + NOTICE)
        self.assertLessEqual(len(out.encode("utf-8")), budget)

    def test_no_line_end_in_kept_part(self):
        self.assertEqual(cap_markdown("x" * 200, NOTICE_BYTES + 5), NOTICE)

    def test_budget_below_notice(self):
        self.assertEqual(cap_markdown("x" * 200, 3), NOTICE)

    def test_cut_inside_multi_byte_character(self):
        text = "ab\n" + E_ACUTE * 4 + "\n" + "z" * 100
        keep = len("ab\n".encode("utf-8")) + 1  # one byte into the first two-byte character
        out = cap_markdown(text, NOTICE_BYTES + keep)
        self.assertEqual(out, "ab\n" + NOTICE)

    def test_multi_byte_line_kept_whole(self):
        line = E_ACUTE * 4 + "\n"
        text = line + "z" * 100
        out = cap_markdown(text, NOTICE_BYTES + len(line.encode("utf-8")))
        self.assertEqual(out, line + NOTICE)

    def test_budget_counts_bytes_not_characters(self):
        text = E_ACUTE * 10 + "\n"
        self.assertEqual(cap_markdown(text, 21), text)  # 11 characters, 21 bytes
        self.assertEqual(cap_markdown(text, 20), NOTICE)


class WriteMarkdownTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_creates_parent_folders(self):
        path = self.root / "nested" / "deeper" / "summary.md"
        write_markdown(path, "# Title\n")
        self.assertEqual(path.read_bytes(), b"# Title\n")

    def test_appends_by_default(self):
        path = self.root / "summary.md"
        write_markdown(path, "one\n")
        write_markdown(path, "two\n")
        self.assertEqual(path.read_bytes(), b"one\ntwo\n")

    def test_overwrite(self):
        path = self.root / "summary.md"
        path.write_bytes(b"old content\n")
        write_markdown(path, "new\n", append=False)
        self.assertEqual(path.read_bytes(), b"new\n")

    def test_writes_lf_line_ends(self):
        path = self.root / "summary.md"
        write_markdown(path, "one\ntwo\n")
        self.assertNotIn(b"\r", path.read_bytes())

    def test_cap_counts_bytes_already_in_file(self):
        path = self.root / "summary.md"
        existing = b"y" * 50 + b"\n"
        path.write_bytes(existing)
        text = "first\nsecond\n" + LONG_LINE
        cap = len(existing) + NOTICE_BYTES + len("first\nsec")
        with mock.patch.object(report, "SUMMARY_CAP_BYTES", cap):
            write_markdown(path, text)
        self.assertEqual(path.read_bytes(), existing + ("first\n" + NOTICE).encode("utf-8"))

    def test_overwrite_ignores_existing_size(self):
        path = self.root / "summary.md"
        path.write_bytes(b"y" * 500)
        text = "first\nsecond\n"
        with mock.patch.object(report, "SUMMARY_CAP_BYTES", NOTICE_BYTES + len(text)):
            write_markdown(path, text, append=False)
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))


class ReportAnnotateTest(unittest.TestCase):
    def test_format_and_escapes(self):
        rep = Report("coverage")
        rep.annotate("notice", "zone: core, delta", "50%\nmore")
        self.assertEqual(rep.annotations, ["::notice title=zone%3A core%2C delta::50%25%0Amore"])

    def test_keeps_order(self):
        rep = Report("coverage")
        rep.annotate("notice", "first", "one")
        rep.annotate("warning", "second", "two")
        self.assertEqual(rep.annotations, ["::notice title=first::one", "::warning title=second::two"])


class ReportViolatedTest(unittest.TestCase):
    def test_warning_without_strict(self):
        rep = Report("coverage")
        rep.violated("Floor", "below")
        self.assertTrue(rep.violation)
        self.assertFalse(rep.error)
        self.assertEqual(rep.annotations, ["::warning title=Floor::below"])

    def test_error_under_strict(self):
        rep = Report("coverage", strict=True)
        rep.violated("Floor", "below")
        self.assertTrue(rep.violation)
        self.assertEqual(rep.annotations, ["::error title=Floor::below"])

    def test_fail_on_error_does_not_raise_level(self):
        rep = Report("coverage", fail_on_error=True)
        rep.violated("Floor", "below")
        self.assertEqual(rep.annotations, ["::warning title=Floor::below"])


class ReportErroredTest(unittest.TestCase):
    def test_warning_and_no_violation_without_strict(self):
        rep = Report("coverage")
        rep.errored("Input", "missing")
        self.assertTrue(rep.error)
        self.assertFalse(rep.violation)
        self.assertEqual(rep.annotations, ["::warning title=Input::missing"])

    def test_error_and_violation_under_strict(self):
        rep = Report("coverage", strict=True)
        rep.errored("Input", "missing")
        self.assertTrue(rep.error)
        self.assertTrue(rep.violation)
        self.assertEqual(rep.annotations, ["::error title=Input::missing"])

    def test_fail_on_error_gives_error_without_violation(self):
        rep = Report("coverage", fail_on_error=True)
        rep.errored("Input", "missing")
        self.assertTrue(rep.error)
        self.assertFalse(rep.violation)
        self.assertEqual(rep.annotations, ["::error title=Input::missing"])

    def test_keeps_earlier_violation(self):
        rep = Report("coverage")
        rep.violated("Floor", "below")
        rep.errored("Input", "missing")
        self.assertTrue(rep.violation)


class ReportListsTest(unittest.TestCase):
    def test_violated_keeps_messages(self):
        rep = Report("coverage")
        rep.violated("Floor", "line below")
        rep.violated("Delta", "branch below")
        self.assertEqual(rep.violations, ["line below", "branch below"])
        self.assertEqual(rep.errors, [])

    def test_errored_keeps_messages(self):
        rep = Report("coverage", strict=True)
        rep.errored("Input", "missing report")
        self.assertEqual(rep.errors, ["missing report"])
        self.assertEqual(rep.violations, [])

    def test_warned_only_by_warning_or_error_annotations(self):
        rep = Report("coverage")
        rep.annotate("notice", "Info", "fine")
        self.assertFalse(rep.warned)
        rep.annotate("warning", "Slow", "close to the budget")
        self.assertTrue(rep.warned)

    def test_evaluated_by_default(self):
        self.assertTrue(Report("coverage").evaluated)


class SectionVerdictTest(unittest.TestCase):
    def test_errors_give_error(self):
        rep = Report("coverage")
        rep.errored("Input", "missing")
        self.assertEqual(rep.section_verdict(), "error")

    def test_exit_two_or_three_gives_error(self):
        for code in (2, 3):
            with self.subTest(code=code):
                self.assertEqual(Report("coverage").section_verdict(code), "error")

    def test_errors_win_over_not_evaluated(self):
        rep = Report("coverage", evaluated=False)
        rep.errored("Input", "missing")
        self.assertEqual(rep.section_verdict(), "error")

    def test_not_evaluated(self):
        self.assertEqual(Report("coverage", evaluated=False).section_verdict(), "not-evaluated")

    def test_not_evaluated_wins_over_a_strict_violation(self):
        rep = Report("coverage", strict=True, evaluated=False)
        rep.violated("Delta", "no merge base")
        self.assertEqual(rep.section_verdict(), "not-evaluated")
        self.assertTrue(rep.violation)

    def test_strict_violation_fails(self):
        rep = Report("coverage", strict=True)
        rep.violated("Floor", "below")
        self.assertEqual(rep.section_verdict(), "fail")

    def test_violation_without_strict_warns(self):
        rep = Report("coverage")
        rep.violated("Floor", "below")
        self.assertEqual(rep.section_verdict(), "warn")

    def test_warning_annotation_warns(self):
        rep = Report("duration", strict=True)
        rep.annotate("warning", "Duration", "above the warning threshold")
        self.assertFalse(rep.violation)
        self.assertEqual(rep.section_verdict(), "warn")

    def test_error_annotation_warns(self):
        rep = Report("duration")
        rep.annotate("error", "Duration", "above the error threshold")
        self.assertEqual(rep.section_verdict(), "warn")

    def test_notice_annotation_passes(self):
        rep = Report("loc")
        rep.annotate("notice", "Budget", "within budget")
        self.assertEqual(rep.section_verdict(), "pass")

    def test_pass(self):
        self.assertEqual(Report("coverage").section_verdict(0), "pass")
        self.assertEqual(Report("coverage").section_verdict(1), "pass")


class SectionTest(unittest.TestCase):
    def test_shape(self):
        rep = Report("coverage", strict=True, verdict="coverage: FAIL", data={"line": 0.5})
        rep.violated("Floor", "line 50% below 80%")
        self.assertEqual(
            rep.section(),
            {
                "name": "coverage",
                "strict": True,
                "violation": True,
                "verdict": "fail",
                "verdict_text": "coverage: FAIL",
                "violations": ["line 50% below 80%"],
                "errors": [],
                "data": {"line": 0.5},
            },
        )

    def test_exit_code_reaches_the_verdict(self):
        self.assertEqual(Report("coverage").section(3)["verdict"], "error")


def section(name: str, verdict: str) -> Report:
    """A Report whose state gives the wanted verdict."""
    rep = Report(name, strict=verdict == "fail", evaluated=verdict != "not-evaluated")
    if verdict in ("fail", "warn"):
        rep.violated("Gate", "below")
    elif verdict == "error":
        rep.errored("Input", "missing")
    return rep


class EnvelopeTest(unittest.TestCase):
    def test_shape_without_sections(self):
        self.assertEqual(
            envelope("version", exit_code=0),
            {
                "schema_version": 1,
                "tool": {"name": "muster", "version": __version__},
                "command": "version",
                "commit": None,
                "base": None,
                "verdict": "pass",
                "exit_code": 0,
                "error": None,
                "sections": [],
                "flags": [],
            },
        )

    def test_commit_sections_and_error(self):
        rep = section("coverage", "warn")
        env = envelope("coverage", exit_code=2, commit="ab" * 20, sections=[rep], error=("config", "bad key"))
        self.assertEqual(env["commit"], "ab" * 20)
        self.assertEqual(env["error"], {"kind": "config", "message": "bad key"})
        self.assertEqual(env["verdict"], "error")
        self.assertEqual(env["sections"], [rep.section(2)])
        self.assertEqual(env["sections"][0]["verdict"], "error")

    def test_single_section_verdict(self):
        for verdict in ("pass", "warn", "fail", "not-evaluated", "error"):
            with self.subTest(verdict=verdict):
                env = envelope("coverage", exit_code=0, sections=[section("coverage", verdict)])
                self.assertEqual(env["verdict"], verdict)

    def test_worst_of_several_sections(self):
        order = ["pass", "not-evaluated", "warn", "fail", "error"]
        for i, worse in enumerate(order):
            for better in order[:i]:
                with self.subTest(worse=worse, better=better):
                    sections = [section("loc", better), section("coverage", worse), section("tests", better)]
                    self.assertEqual(envelope("check", exit_code=0, sections=sections)["verdict"], worse)

    def test_exit_two_or_three_is_error(self):
        for code in (2, 3):
            with self.subTest(code=code):
                self.assertEqual(envelope("version", exit_code=code)["verdict"], "error")

    def test_error_set_is_error(self):
        env = envelope("coverage", exit_code=0, sections=[section("coverage", "pass")], error=("usage", "x"))
        self.assertEqual(env["verdict"], "error")


class JsonTextTest(unittest.TestCase):
    def test_indent_ascii_and_final_newline(self):
        text = json_text({"name": "caf" + E_ACUTE, "list": [1]})
        self.assertEqual(text, '{\n  "name": "caf\\u00e9",\n  "list": [\n    1\n  ]\n}\n')
        self.assertEqual(json.loads(text), {"name": "caf" + E_ACUTE, "list": [1]})


class WriteJsonTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_replaces_file_with_lf_text(self):
        path = self.root / "out.json"
        path.write_bytes(b"old content that is longer\r\n")
        write_json(path, '{\n  "a": 1\n}\n')
        self.assertEqual(path.read_bytes(), b'{\n  "a": 1\n}\n')

    def test_creates_parent_folders(self):
        path = self.root / "nested" / "out.json"
        write_json(path, "{}\n")
        self.assertEqual(path.read_bytes(), b"{}\n")


if __name__ == "__main__":
    unittest.main()
