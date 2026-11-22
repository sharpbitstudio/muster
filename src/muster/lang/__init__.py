"""The languages whose lines of code `loc` counts, and their line counters.

Built in is only Java, counted by muster.lang.java. Each [[language]] entry of the profile with a
new name adds a language, counted by the generic counter; an entry named after a built-in language
adds extensions to it and keeps its counter. The profile check (muster.config) refuses an entry
that would make the languages ambiguous, so the entries here are already checked.

A file has the language of the longest extension that ends its name, compared case-sensitively; a
name with no such extension, or one that is nothing but the extension (a dot-file), has none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from muster.gitio import records
from muster.lang.java import java_loc

JAVA = "java"
JAVA_LINE_COMMENT = "//"
JAVA_BLOCK_COMMENT = ("/*", "*/")


@dataclass(frozen=True)
class Language:
    name: str
    extensions: tuple[str, ...]
    line_comment: str | None = None
    block_comment: tuple[str, str] | None = None
    skip_patterns: tuple[str, ...] = ()  # carried for the commands that skip tests; not used here
    counter: Callable[[str], int] | None = None  # takes the whole text; None is the generic counter

    def count(self, text: str) -> int:
        """The lines of code in `text`."""
        if self.counter is not None:
            return self.counter(text)
        return generic_loc(records(text), self.line_comment, self.block_comment)


BUILT_IN: dict[str, Language] = {
    JAVA: Language(JAVA, (".java",), JAVA_LINE_COMMENT, JAVA_BLOCK_COMMENT, counter=java_loc),
}


def generic_loc(lines: list[str], line_comment: str | None, block_comment: tuple[str, str] | None) -> int:
    """The lines that keep a character other than white space outside comments.

    Each line is scanned once, left to right. Outside a block comment the marker that occurs first
    wins (the longer one when two start at the same place): a line comment ends the line, a block
    start opens a block. Inside a block only the block end is looked for, and a block goes on over
    the following lines until its end. The counter does not know strings: a marker inside a string
    reads as a comment. A language without markers counts the lines that are not blank.
    """
    start, end = block_comment if block_comment else (None, None)
    count = 0
    in_block = False
    for line in lines:
        code = False
        i = 0
        while True:  # each pass ends the line or moves past a marker, which is never empty
            if in_block:
                close = line.find(end, i)
                if close < 0:
                    break
                in_block = False
                i = close + len(end)
                continue
            # (place, -length, marker) of each marker found: the first, then the longer one wins
            found = [(line.find(marker, i), -len(marker), marker) for marker in (line_comment, start) if marker]
            first = min((hit for hit in found if hit[0] >= 0), default=None)
            stop = len(line) if first is None else first[0]
            code = code or bool(line[i:stop].strip())
            if first is None or first[2] == line_comment:
                break
            in_block = True
            i = stop + len(start)
        count += code
    return count


def languages(entries: Iterable[dict[str, Any]]) -> list[Language]:
    """The languages of a profile: the built-in ones, then each new language of the [[language]]
    entries in their order. An entry named after a built-in language adds its extensions to it (one
    listed again is kept once) and its skip_patterns."""
    found = dict(BUILT_IN)
    for entry in entries:
        name = entry["name"]
        skip = tuple(entry.get("skip_patterns", ()))
        built_in = BUILT_IN.get(name)
        if built_in is not None:
            extra = tuple(ext for ext in dict.fromkeys(entry.get("extensions", ())) if ext not in built_in.extensions)
            found[name] = replace(built_in, extensions=built_in.extensions + extra, skip_patterns=skip)
            continue
        block = entry.get("block_comment")
        found[name] = Language(
            name, tuple(entry["extensions"]), entry.get("line_comment"), tuple(block) if block else None, skip
        )
    return list(found.values())


def language_of(path: str, langs: Iterable[Language]) -> Language | None:
    """The language of the longest extension that ends the file name of `path`, or None. The name
    must be longer than the extension: a dot-file such as `.java` has no language."""
    name = path.rsplit("/", 1)[-1]
    best: tuple[int, Language] | None = None
    for lang in langs:
        for ext in lang.extensions:
            if len(name) > len(ext) and name.endswith(ext) and (best is None or len(ext) > best[0]):
                best = (len(ext), lang)
    return best[1] if best else None


def count_file(path: Path, lang: Language) -> int:
    """The lines of code of the file at `path`, read as bytes and decoded as UTF-8 with every bad
    byte replaced. A file that cannot be read raises its OSError: the caller reports it."""
    with open(path, "rb") as fh:
        data = fh.read()
    return lang.count(data.decode("utf-8", "replace"))
