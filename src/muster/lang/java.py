"""The Java language: its line counter, and the reading of a JUnit Platform unique id and of a Java
test source.

A line of Java code has a character other than white space outside comments; the contents of a
string, a character literal and a text block are code, so a `//` inside one is no comment.

A JUnit Platform unique id (parse_killing_test) names a test class and its member; source_declares
tells from the text of a Java test source whether that class still declares the member as a test.
The source is scanned, not parsed, and the scan has known limits:

- it knows no `\\uXXXX` escapes, no local or anonymous classes and no `@interface`;
- it reads Java sources only;
- overloads are not told apart: only the method name is compared, so a name with one annotated
  overload is declared;
- JAVA_METHOD_HEADER can take polynomial time on a very long member header without parentheses
  (the test sources come from the repository under check).
"""

from __future__ import annotations

import re

_JAVA_TOKEN = re.compile(
    r'"""(?:\\.|[^\\])*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*.*?\*/', re.S
)


def java_loc(text: str) -> int:
    """Lines with code: neither blank nor only comment (string and text-block contents are code)."""

    def keep(match: re.Match) -> str:
        token = match.group(0)
        return "\n" * token.count("\n") if token.startswith("/") else token

    return sum(1 for line in _JAVA_TOKEN.sub(keep, text).split("\n") if line.strip())


# A `[kind:value]` segment of a unique id; a value may hold `]` (`f(java.lang.String[])`).
UNIQUE_ID_SEGMENT = re.compile(r"\[([a-z-]+):(.*?)\](?=/\[|$)")


# A JUnit XML test name that is a method signature, not a display name.
PLAIN_TEST_NAME = re.compile(r"([\w$]+)\(.*\)")


def parse_killing_test(entry: str) -> tuple[str, str, str] | None:
    """(test class as Outer$Inner, member name, member kind) of one PIT killingTest entry
    (`<class>.[engine:...]/[class:...]/[nested-class:...]/[method:name(...)]`; the kind is `method`,
    `test-template` or `test-factory`), None when it is not a JUnit Platform unique id."""
    start = entry.find("[engine:")
    if start < 0:
        return None
    cls = member = kind = None
    for seg_kind, value in UNIQUE_ID_SEGMENT.findall(entry[start:]):
        if seg_kind == "class":
            cls = value
        elif seg_kind == "nested-class" and cls:
            cls += "$" + value
        elif seg_kind in ("method", "test-template", "test-factory") and member is None:
            member, kind = value.partition("(")[0], seg_kind
    return (cls, member, kind) if cls and member else None


# The annotations that make a method a JUnit test, by simple or fully qualified name.
TEST_ANNOTATIONS = frozenset(
    {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
    | {f"org.junit.jupiter.api.{n}" for n in ("Test", "RepeatedTest", "TestFactory", "TestTemplate")}
    | {"org.junit.jupiter.params.ParameterizedTest"}
)


JAVA_MODIFIER = re.compile(r"(?:public|protected|private|static|final|abstract|synchronized|native|strictfp|default)\b")


JAVA_ANNOTATION = re.compile(r"@\s*([\w$]+(?:\s*\.\s*[\w$]+)*)\s*")


JAVA_METHOD_HEADER = re.compile(
    r"(?:<[^;{}]*?>\s*)?(?P<type>[\w$][\w$.<>\[\],?&\s]*?)\s+(?P<name>[\w$]+)\s*\(.*\)\s*(?:throws\s[\w$.,<>\s]+)?",
    re.DOTALL,
)


JAVA_KEYWORDS = frozenset(
    "return new throw else yield case class interface enum record assert do if for while switch try catch".split()
)


def strip_java(text: str) -> str | None:
    """`text` with comments and string, text block and char literals blanked to spaces (newlines kept,
    so positions stay), None when one of them is unterminated."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return None
            j += 2
        elif text.startswith('"""', i):
            j = i + 3
            while not text.startswith('"""', j):
                if j >= n:
                    return None
                j += 2 if text[j] == "\\" else 1
            j += 3
        elif text[i] in "\"'":
            j = i + 1
            while j < n and text[j] != text[i]:
                if text[j] == "\n":
                    return None
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return None
            j += 1
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def matching_brace(text: str, start: int) -> int:
    """Index of the `}` that closes the `{` at `start` in stripped text, -1 when unbalanced."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def java_members(text: str, start: int, end: int) -> list[tuple[str, str, int]] | None:
    """(header, `;` or `{`, its index) of each member-level chunk of the class body text[start:end]
    (stripped): a block's content is skipped, a brace inside parentheses (`@ValueSource(strings = {...})`)
    ends nothing. None on unbalanced parentheses or braces."""
    out, paren, chunk, i = [], 0, start, start
    while i < end:
        c = text[i]
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
            if paren < 0:
                return None
        elif paren == 0 and c in ";{":
            out.append((text[chunk:i], c, i))
            if c == "{":
                i = matching_brace(text, i)
                if i < 0 or i >= end:
                    return None
            chunk = i + 1
        elif paren == 0 and c == "}":
            return None
        i += 1
    return None if paren else out


def is_class_header(header: str, name: str) -> bool:
    pattern = r"(?<![\w$])(?:class|interface|enum|record)\s+" + re.escape(name) + r"(?![\w$])"
    return re.search(pattern, header) is not None


def is_test_declaration(header: str, member: str) -> bool:
    """Whether a member-level header declares the method `member` with a JUnit test annotation among
    its annotations (other annotations and modifiers may sit in between)."""
    i, annotations = 0, set()
    while True:
        while i < len(header) and header[i].isspace():
            i += 1
        m = JAVA_ANNOTATION.match(header, i)
        if m:
            annotations.add(re.sub(r"\s+", "", m.group(1)))
            i = m.end()
            if header.startswith("(", i):
                depth = 0
                for j in range(i, len(header)):
                    depth += {"(": 1, ")": -1}.get(header[j], 0)
                    if depth == 0:
                        break
                else:
                    return False
                i = j + 1
            continue
        m = JAVA_MODIFIER.match(header, i)
        if not m:
            break
        i = m.end()
    m = JAVA_METHOD_HEADER.fullmatch(header, i, len(header.rstrip()))
    return (
        m is not None
        and m.group("name") == member
        and re.match(r"[\w$]+", m.group("type")).group() not in JAVA_KEYWORDS
        and not annotations.isdisjoint(TEST_ANNOTATIONS)
    )


def source_declares(text: str, cls: str, member: str) -> str:
    """Whether the Java source `text` declares `member` as a test of the class `cls` (`pkg.Outer$Inner`,
    as a unique id names it): `present`, `deleted` or `unresolved`.

    The class chain is followed from the top-level class through each nested name; a step that finds
    no class header or more than one, an unterminated comment or literal, or unbalanced parentheses or
    braces is `unresolved`. Only a member directly in the body of the class itself counts, so a test of
    an enclosing or a nested class does not; a method of that name without a test annotation, or one
    that is only called, is `deleted`.
    """
    stripped = strip_java(text)
    if stripped is None:
        return "unresolved"
    top, *nested = cls.split("$")
    start, end = 0, len(stripped)
    for name in [top.rpartition(".")[2], *nested]:
        members = java_members(stripped, start, end) or []
        opens = [i for header, mark, i in members if mark == "{" and is_class_header(header, name)]
        if len(opens) != 1:
            return "unresolved"
        start, end = opens[0] + 1, matching_brace(stripped, opens[0])
    members = java_members(stripped, start, end)
    if members is None:
        return "unresolved"
    return "present" if any(is_test_declaration(header, member) for header, _, _ in members) else "deleted"
