"""The globs of muster.toml, matched against a repository-relative POSIX path (or a file name).

`**` as a whole segment matches zero or more segments, `*` matches within a segment, `{a,b}` is an
alternation (not nested), and every other character is literal. Matching is case-sensitive and
covers the whole path. The zone files have their own, stricter grammar (see muster.zones).
"""

from __future__ import annotations

import functools
import json
import re

from muster.config import ConfigError


def _alternatives(glob: str) -> list[str]:
    """Every alternative of `glob` with each `{a,b}` group expanded, left to right."""
    out = [""]
    rest = glob
    while rest:
        open_, close = rest.find("{"), rest.find("}")
        if open_ < 0 and close < 0:
            return [head + rest for head in out]
        if open_ >= 0 and close < 0:
            raise ValueError("a '{' without its '}'")
        if open_ < 0 or close < open_:
            raise ValueError("a '}' without its '{'")
        options = rest[open_ + 1 : close]
        if "{" in options:
            raise ValueError("nested braces")
        out = [head + rest[:open_] + option for head in out for option in options.split(",")]
        rest = rest[close + 1 :]
    return out


def _regex(alternative: str) -> str:
    segments = alternative.split("/")
    parts = []
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:.*/)?")
        elif "**" in segment:
            raise ValueError("'**' must be a whole path segment")
        else:
            parts.append("[^/]*".join(re.escape(literal) for literal in segment.split("*")) + ("" if last else "/"))
    return "".join(parts)


@functools.lru_cache(maxsize=None)
def _compile(glob: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{_regex(alt)})" for alt in _alternatives(glob)), re.DOTALL)


def compile_glob(key: str, glob: str) -> re.Pattern[str]:
    """The compiled glob; a malformed one is a ConfigError naming the key and the glob."""
    try:
        return _compile(glob)
    except ValueError as exc:
        raise ConfigError(f"{key}: bad glob {json.dumps(glob)}: {exc}") from None


def check_globs(key: str, globs: list[str]) -> None:
    """Raises ConfigError for the first malformed glob of a list."""
    for i, glob in enumerate(globs):
        compile_glob(f"{key}[{i}]", glob)


def matches(glob: str, path: str) -> bool:
    """Whether the (well-formed) glob matches the whole path."""
    return _compile(glob).fullmatch(path) is not None


def matches_any(globs: list[str], path: str) -> bool:
    return any(matches(glob, path) for glob in globs)
