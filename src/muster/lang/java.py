"""The Java language: its line counter.

A line of Java code has a character other than white space outside comments; the contents of a
string, a character literal and a text block are code, so a `//` inside one is no comment.
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
