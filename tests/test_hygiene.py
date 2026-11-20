"""Tests of tools/hygiene.py.

Every string that hygiene should report is assembled at run time, so the
check over the tree finds nothing in this file.
"""

import importlib.util
import re
import unittest
from pathlib import Path

HYGIENE_PATH = Path(__file__).resolve().parents[1] / "tools" / "hygiene.py"
_spec = importlib.util.spec_from_file_location("hygiene_under_test", HYGIENE_PATH)
hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hygiene)

CATEGORY_RE = re.compile(r": \[([\w-]+)\] ")

# Synthetic values that must never appear in the report.
HOST = "build-box-7"
USER = "someone"


def attr(name: str, value: str, quote: str = '"', escape: str = "") -> str:
    """An attribute in XML syntax, its quotes escaped as in JSON when escape
    is a backslash."""
    return name + "=" + escape + quote + value + escape + quote


def check(text: str, *, message: bool = False) -> hygiene.Report:
    rep = hygiene.Report()
    rep.text(text, "x.txt", message=message)
    return rep


def categories(text: str, *, message: bool = False) -> list[str]:
    return [CATEGORY_RE.search(line).group(1) for line in check(text, message=message).lines]


class HostnameTest(unittest.TestCase):
    def test_other_host_in_double_quotes(self):
        self.assertEqual(categories("<testsuite " + attr("hostname", HOST) + ' tests="1">'), ["hostname"])

    def test_other_host_in_single_quotes(self):
        self.assertEqual(categories("<testsuite " + attr("hostname", HOST, "'") + ">"), ["hostname"])

    def test_other_host_in_escaped_quotes(self):
        self.assertEqual(categories('"<testsuite ' + attr("hostname", HOST, '"', "\\") + '>"'), ["hostname"])

    def test_upper_case_attribute_name(self):
        self.assertEqual(categories("<testsuite " + attr("HostName", HOST) + ">"), ["hostname"])

    def test_spaces_around_equals_sign(self):
        self.assertEqual(categories("<testsuite " + "hostname" + " = " + '"' + HOST + '">'), ["hostname"])

    def test_localhost_with_suffix(self):
        self.assertEqual(categories("<testsuite " + attr("hostname", "localhost.localdomain") + ">"), ["hostname"])

    def test_placeholder_value(self):
        self.assertEqual(categories("<testsuite " + attr("hostname", "{host}") + ">"), ["hostname"])

    def test_localhost_followed_by_more(self):
        self.assertEqual(categories("<testsuite " + attr("hostname", "localhost x") + ">"), ["hostname"])

    def test_closing_quote_escaped_differently(self):
        line = "<testsuite " + "hostname" + "=" + '\\"localhost"' + ">"
        self.assertEqual(categories(line), ["hostname"])

    def test_two_on_one_line_give_one_finding(self):
        line = "<a " + attr("hostname", HOST) + "/><b " + attr("hostname", HOST) + "/>"
        rep = check(line)
        self.assertEqual(len(rep.lines), 1)
        self.assertIn("(+1 more)", rep.lines[0])

    def test_empty_is_fine(self):
        self.assertEqual(categories('<testsuite hostname="" tests="1">'), [])

    def test_localhost_is_fine(self):
        self.assertEqual(categories('<testsuite hostname="localhost" tests="1">'), [])

    def test_localhost_in_other_quotes_and_case_is_fine(self):
        self.assertEqual(categories("<testsuite hostname='LocalHost'>"), [])
        self.assertEqual(categories(r'"<testsuite hostname=\"localhost\">"'), [])

    def test_assignment_without_quote_is_fine(self):
        self.assertEqual(categories("hostname = socket.gethostname()"), [])

    def test_prose_is_fine(self):
        self.assertEqual(categories("the hostname attribute is localhost or empty"), [])

    def test_longer_attribute_name_is_fine(self):
        self.assertEqual(categories('<x build.hostname="a" my-hostname="b"/>'), [])


class SessioninfoTest(unittest.TestCase):
    def test_id_with_host_prefix(self):
        line = "<" + "sessioninfo " + attr("id", HOST + "-1a2b3c") + ' start="1" dump="2"/>'
        self.assertEqual(categories(line), ["sessioninfo"])

    def test_id_with_escaped_quotes(self):
        line = '"<' + "sessioninfo " + attr("id", HOST + "-1a2b3c", '"', "\\") + '/>"'
        self.assertEqual(categories(line), ["sessioninfo"])

    def test_id_with_non_hex_digits(self):
        line = "<" + "sessioninfo " + attr("id", "localhost-1a2bz") + "/>"
        self.assertEqual(categories(line), ["sessioninfo"])

    def test_open_tag_without_id_on_the_line(self):
        self.assertEqual(categories("<" + "sessioninfo" + ' start="1"'), ["sessioninfo"])
        self.assertEqual(categories("<" + "SessionInfo"), ["sessioninfo"])

    def test_closed_tag_without_id_is_fine(self):
        self.assertEqual(categories('<sessioninfo start="1" dump="2"/>'), [])
        self.assertEqual(categories("the report has a `<sessioninfo>` element"), [])

    def test_one_bad_id_of_two(self):
        line = "<" + "sessioninfo " + attr("id", "localhost-1a") + " " + attr("id", HOST) + "/>"
        self.assertEqual(categories(line), ["sessioninfo"])

    def test_localhost_hex_id_is_fine(self):
        self.assertEqual(categories('<sessioninfo id="localhost-1a2b3c" start="1" dump="2"/>'), [])
        self.assertEqual(categories(r'"<sessioninfo id=\"localhost-1A2B\"/>"'), [])

    def test_other_tag_name_is_fine(self):
        self.assertEqual(categories('<sessioninfos id="x"/>'), [])

    def test_prose_is_fine(self):
        self.assertEqual(categories("a sessioninfo id is localhost and hex digits"), [])


class UserNameTest(unittest.TestCase):
    def test_name_first(self):
        line = "<property " + attr("name", "user.name") + ' value="' + USER + '"/>'
        self.assertEqual(categories(line), ["user-name"])

    def test_value_first(self):
        line = '<property value="' + USER + '" ' + attr("name", "user.name") + "/>"
        self.assertEqual(categories(line), ["user-name"])

    def test_upper_case_and_single_quotes(self):
        line = "<property " + attr("NAME", "User.Name", "'") + " value='" + USER + "'/>"
        self.assertEqual(categories(line), ["user-name"])

    def test_builder_keyword_argument(self):
        line = 'SubElement(p, "property", ' + attr("name", "user.name") + ', value="' + USER + '")'
        self.assertEqual(categories(line), ["user-name"])

    def test_other_property_is_fine(self):
        self.assertEqual(categories('<property name="java.version" value="17"/>'), [])

    def test_longer_property_name_is_fine(self):
        self.assertEqual(categories('<property name="user.name.x" value="y"/>'), [])

    def test_prose_is_fine(self):
        self.assertEqual(categories("no fixture has a user.name property"), [])


class ReportNameOutputTest(unittest.TestCase):
    def test_value_is_never_printed(self):
        lines = [
            "<testsuite " + attr("hostname", HOST) + ">",
            "<" + "sessioninfo " + attr("id", HOST + "-1a2b3c") + "/>",
            "<property " + attr("name", "user.name") + ' value="' + USER + '"/>',
        ]
        rep = check("\n".join(lines))
        self.assertEqual(len(rep.lines), 3)
        for out in rep.lines:
            self.assertNotIn(HOST, out)
            self.assertNotIn(USER, out)

    def test_commit_message(self):
        text = "Add a report\n\nIt has <testsuite " + attr("hostname", HOST) + ">\n"
        self.assertEqual(categories(text, message=True), ["hostname"])


class RootHomeTest(unittest.TestCase):
    def test_closing_tag_is_fine(self):
        self.assertEqual(categories("<root>x</root>"), [])

    def test_escaped_closing_tag_is_fine(self):
        self.assertEqual(categories(r'"<root>x<\/root>"'), [])

    def test_xml_in_code_is_fine(self):
        self.assertEqual(categories("xml = '<root>x</root>'"), [])

    def test_redirect_from_root_home(self):
        self.assertEqual(categories("cat <" + "/" + "root/x"), ["abs-path"])

    def test_root_home(self):
        self.assertEqual(categories("/" + "root/x"), ["abs-path"])
        self.assertEqual(categories("ls " + "/" + "root"), ["abs-path"])
        self.assertEqual(categories("/" + "root"), ["abs-path"])


class GitBashTest(unittest.TestCase):
    def test_glob_is_fine(self):
        self.assertEqual(categories("**/a/b/*.py"), [])

    def test_glob_below_folder_is_fine(self):
        self.assertEqual(categories("src/**/x/*.py"), [])

    def test_brace_glob_with_one_letter_folders(self):
        self.assertEqual(categories("{a,b}" + "/" + "c/d/x.py"), ["abs-path"])

    def test_git_bash_path(self):
        self.assertEqual(categories("/" + "c/work/x"), ["abs-path"])


class SelfTest(unittest.TestCase):
    def test_hygiene_source_has_no_finding(self):
        rep = hygiene.Report()
        rep.blob("tools/hygiene.py", HYGIENE_PATH.read_bytes(), "tools/hygiene.py", pin=False)
        self.assertEqual(rep.lines, [])


if __name__ == "__main__":
    unittest.main()
