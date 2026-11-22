"""The zone grammar of the policy files and the classification of the source files.

The critical file lists globs under zone headers `# Zone <n>: <title>` (n from 1 to 999, without
leading zeros); the standard file lists packages, `<package>/*` or `*` for the base folder; the
exemption file lists `path | reason | valid-until` lines. Globs and paths are relative to
[zones].base. A glob is a `/`-separated path where `*` stays inside a segment, `**` is only a whole
last segment, and `{a,b}` groups expand into alternatives (not nested, at most MAX_ALTERNATIVES
per line); `#` starts a comment.

Every source file must be in exactly one zone or in a standard package; every glob and every
alternative must match a file; a standard glob must match a file that is not critical (critical
wins); a valid exemption names one critical source file and ends neither in the past nor more than
exemption_max_days ahead (UTC).

The lines come from gitio.records(): only a line feed ends a line. Lines are trimmed of the
characters up to U+0020 at both ends, and the regular expressions are ASCII-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from muster.globs import MAX_ALTERNATIVES, matches
from muster.paths import normalize, root_of

MAX_ZONE = 999
STANDARD_ZONE = "standard"
ROOT_PACKAGE = "<root>"

HEADER = re.compile(r"# Zone (\d+): ", re.ASCII)
# A comment that looks like a zone header; unless it is a valid one, it is a violation, not a comment.
HEADER_LOOKALIKE = re.compile(r"#\s*zone\b", re.ASCII | re.IGNORECASE)
# A standard alternative: `*` (the base folder) or `<segment>/.../<segment>/*`, no wildcard before.
STANDARD_FORM = re.compile(r"\*|[^*/]+(/[^*/]+)*/\*", re.ASCII)
EXEMPTION_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
# Any character but a line terminator (LF, CR, NEL, LS, PS): what a `**` spans, and what may stand
# around a glob character in an exemption path.
ANY_IN_LINE = "[^\n\r\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]"
GLOB_CHARACTER = re.compile(ANY_IN_LINE + r"*[*?{},]" + ANY_IN_LINE + "*", re.ASCII)
TARGET_FORBIDDEN = frozenset("*?{}[]#\\")


def trim(text: str) -> str:
    """Drop the characters up to U+0020 from both ends (str.strip() would drop other ones too)."""
    start, end = 0, len(text)
    while start < end and text[start] <= " ":
        start += 1
    while end > start and text[end - 1] <= " ":
        end -= 1
    return text[start:end]


@dataclass(frozen=True)
class Names:
    """The policy files as messages name them: their configured paths."""

    critical: str
    standard: str
    exemptions: str = ""


@dataclass(frozen=True)
class Glob:
    """One alternative of a glob line, with its compiled pattern and its zone (None when standard
    or when no zone header came before it)."""

    file: str
    line: str
    alternative: str
    regex: re.Pattern[str]
    zone: str | None

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None

    def describe(self) -> str:
        origin = "" if self.line == self.alternative else f" (alternative of '{self.line}')"
        return f"{self.file}: '{self.alternative}'{origin}"


@dataclass(frozen=True)
class Classification:
    """violations in the order the rules find them, empty when the policy files are valid; zones maps
    a source file to `Z<n>` or `standard` and exempt_until a critical file to the last day of its
    valid exemption, both by repository-relative path; zones is complete only without violations."""

    violations: list[str]
    zones: dict[str, str] = field(default_factory=dict)
    exempt_until: dict[str, date] = field(default_factory=dict)


def unclassified(names: Names, package: str) -> str:
    return f"unclassified package '{package}' - add it to {names.critical} or {names.standard}"


def standard_form(names: Names, glob: str) -> str:
    return (
        f"{names.standard}: '{glob}' must name packages without subpackages ('<package>/*', or '*' for"
        " the root package)"
    )


def malformed_header(names: Names, line: str) -> str:
    return (
        f"{names.critical}: malformed zone header '{line}' - expected '# Zone <n>: <title>' with n from"
        f" 1 to {MAX_ZONE}, without leading zeros"
    )


def repeated_header(names: Names, line: str) -> str:
    return f"{names.critical}: zone header '{line}' repeats a zone number - each zone has one header"


def malformed_exemption(names: Names, line: str) -> str:
    return f"{names.exemptions}: malformed entry '{line}' - expected 'path | reason | valid-until'"


def malformed_target(name: str, line: str) -> str:
    return (
        f"{name}: '{line}' must be one source file '<folder>/<file>' - no glob, no file directly in the"
        " base folder, no comment after the path"
    )


def zone_number(digits: str) -> int | None:
    """The zone number of a header, when it is from 1 to MAX_ZONE and has no leading zero."""
    if digits.startswith("0") or len(digits) > len(str(MAX_ZONE)):
        return None
    number = int(digits)
    return number if 1 <= number <= MAX_ZONE else None


def expand(glob: str) -> list[str]:
    """All alternatives of `glob` with each `{a,b}` group expanded, left to right; empty on bad
    braces. One group at a time, without recursion, so that the number of groups has no limit.
    Past MAX_ALTERNATIVES the groups are only counted, not expanded: with good braces, such a glob
    is a ValueError."""
    alternatives = [""]
    count = 1
    rest = glob
    while True:
        open_, close = rest.find("{"), rest.find("}")
        if open_ < 0 and close < 0:
            if count > MAX_ALTERNATIVES:
                raise ValueError(f"more than {MAX_ALTERNATIVES} alternatives")
            return [head + rest for head in alternatives]
        if open_ < 0 or close < open_ or "{" in rest[open_ + 1 : close]:
            return []
        options = rest[open_ + 1 : close].split(",")
        count = min(count * len(options), MAX_ALTERNATIVES + 1)
        if count <= MAX_ALTERNATIVES:
            alternatives = [head + rest[:open_] + option for head in alternatives for option in options]
        rest = rest[close + 1 :]


def glob_error(glob: str) -> str | None:
    """Why `glob` breaks the grammar: an empty segment, or `**` other than a whole last segment."""
    segments = glob.split("/")
    if any(segment == "" for segment in segments):
        return f"empty path segment in '{glob}'"
    for i, segment in enumerate(segments):
        if "**" in segment and not (segment == "**" and i == len(segments) - 1):
            return f"'**' must be a whole last segment in '{glob}'"
    return None


def to_regex(glob: str) -> re.Pattern[str]:
    """The glob as a regular expression over a `/`-separated path: `*` stays in a segment, a last
    `**` spans one or more characters of a line."""
    parts = []
    for segment in glob.split("/"):
        if segment == "**":
            parts.append(ANY_IN_LINE + "+")
        else:
            parts.append("[^/]*".join(re.escape(literal) for literal in segment.split("*")))
    return re.compile("/".join(parts), re.ASCII)


def package_of(file: str) -> str:
    slash = file.rfind("/")
    return ROOT_PACKAGE if slash < 0 else file[:slash]


def parse_date(text: str) -> date | None:
    """The date of `YYYY-MM-DD` when it is a real calendar day. Year 0000 is a real (leap) year of
    the proleptic calendar; it is earlier than any date Python has, so it stands as date.min."""
    if not EXEMPTION_DATE.fullmatch(text):
        return None
    year, month, day = int(text[:4]), int(text[5:7]), int(text[8:])
    try:
        if year == 0:
            date(2000, month, day)  # the day exists in a leap year
            return date.min
        return date(year, month, day)
    except ValueError:
        return None


def _globs(names: Names, critical: bool, lines: list[str], seen: set[str], violations: list[str]) -> list[Glob]:
    """The well-formed glob alternatives of `lines`; format violations go to `violations`."""
    file = names.critical if critical else names.standard
    globs: list[Glob] = []
    opened: set[str] = set()
    zone = None
    for raw in lines:
        if critical:
            trimmed = trim(raw)
            header = HEADER.match(trimmed)
            number = zone_number(header.group(1)) if header else None
            if number is not None:
                zone = f"Z{number}"
                if zone in opened:
                    # A second header would silently merge its globs into the zone of the first.
                    violations.append(repeated_header(names, trimmed))
                opened.add(zone)
                continue
            if HEADER_LOOKALIKE.match(trimmed):
                # A near-miss would silently be a comment, and the globs below it would join the
                # previous zone.
                violations.append(malformed_header(names, trimmed))
                continue
        hash_ = raw.find("#")
        line = trim(raw if hash_ < 0 else raw[:hash_])
        if not line:
            continue
        if critical and zone is None:
            violations.append(f"{file}: '{line}' comes before the first '# Zone <n>: ' header")
        try:
            alternatives = expand(line)
        except ValueError:
            violations.append(f"{file}: more than {MAX_ALTERNATIVES} alternatives in '{line}'")
            continue
        if not alternatives:
            violations.append(f"{file}: unbalanced or nested braces in '{line}'")
            continue
        for alternative in alternatives:
            if alternative in seen:
                violations.append(f"duplicate glob '{alternative}' in {file}")
                continue
            seen.add(alternative)
            error = glob_error(alternative)
            if error is not None:
                violations.append(f"{file}: {error}")
            elif not critical and not STANDARD_FORM.fullmatch(alternative):
                violations.append(standard_form(names, alternative))
            else:
                globs.append(Glob(file, line, alternative, to_regex(alternative), zone))
    return globs


def _exemptions(
    names: Names,
    lines: list[str],
    files: set[str],
    critical_files: set[str],
    today: date,
    max_days: int,
    violations: list[str],
) -> dict[str, date]:
    """The valid exemptions of `lines` (path relative to the base -> last valid day); every broken
    rule goes to `violations`."""
    try:
        latest = today + timedelta(days=max_days)
    except OverflowError:
        latest = date.max
    valid: dict[str, date] = {}
    seen: set[str] = set()
    for raw in lines:
        line = trim(raw)
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        if len(fields) != 3 or any(not trim(item) for item in fields):
            violations.append(malformed_exemption(names, line))
            continue
        path = trim(fields[0])
        text = trim(fields[2])
        until = parse_date(text)
        if path in seen:
            violations.append(f"{names.exemptions}: duplicate entry for '{path}'")
        elif GLOB_CHARACTER.fullmatch(path):
            violations.append(f"{names.exemptions}: '{path}' must name one source file, without globs")
        elif until is None:
            violations.append(f"{names.exemptions}: '{path}' has an invalid date '{text}' (YYYY-MM-DD)")
        elif path not in files:
            violations.append(f"{names.exemptions}: '{path}' is not a source file")
        elif path not in critical_files:
            violations.append(f"{names.exemptions}: '{path}' is not in a critical zone")
        elif until < today:
            violations.append(f"{names.exemptions}: '{path}' expired on {text}")
        elif until > latest:
            violations.append(
                f"{names.exemptions}: '{path}' is valid until {text}, more than {max_days} days ahead"
            )
        else:
            valid[path] = until
        seen.add(path)
    return valid


def base_prefix(base: str) -> str:
    return f"{base}/" if base else ""


def classify(
    critical_lines: list[str],
    standard_lines: list[str],
    exemption_lines: list[str],
    sources: list[str],
    *,
    base: str,
    names: Names,
    today: date,
    max_days: int,
) -> Classification:
    """The classification of `sources` (repository-relative) by the lines of the policy files."""
    prefix = base_prefix(base)
    violations: list[str] = []
    files: list[str] = []
    for path in sources:
        if path.startswith(prefix):
            files.append(path[len(prefix) :])
        else:
            violations.append(f"source file outside {prefix}: {path}")

    seen: set[str] = set()
    critical = _globs(names, True, critical_lines, seen, violations)
    standard = _globs(names, False, standard_lines, seen, violations)

    critical_files = {file for file in files if any(glob.matches(file) for glob in critical)}
    for glob in critical:
        if not any(glob.matches(file) for file in files):
            violations.append(f"{glob.describe()} matches no source file")
    for glob in standard:
        matched = [file for file in files if glob.matches(file)]
        if not matched:
            violations.append(f"{glob.describe()} matches no source file")
        elif all(file in critical_files for file in matched):
            violations.append(f"{glob.describe()} matches only critical files (critical wins) - drop it")

    zones: dict[str, str] = {}
    for file in files:
        file_zones = sorted({glob.zone for glob in critical if glob.zone is not None and glob.matches(file)})
        if len(file_zones) > 1:
            violations.append(
                f"'{file}' is matched by globs of zones {' and '.join(file_zones)} - a file belongs to one zone"
            )
        elif len(file_zones) == 1:
            zones[file] = file_zones[0]
        elif file not in critical_files and any(glob.matches(file) for glob in standard):
            zones[file] = STANDARD_ZONE

    unmatched = {
        package_of(file)
        for file in files
        if file not in critical_files and not any(glob.matches(file) for glob in standard)
    }
    violations.extend(unclassified(names, package) for package in sorted(unmatched))

    exempt = _exemptions(names, exemption_lines, set(files), critical_files, today, max_days, violations)
    return Classification(
        violations,
        {prefix + file: zone for file, zone in sorted(zones.items())},
        {prefix + file: until for file, until in exempt.items()},
    )


def own_test_name(template: str, file_name: str) -> str:
    """The own test's file name: `{stem}` is the name before its last dot, `{ext}` the rest."""
    dot = file_name.rfind(".")
    stem, ext = (file_name, "") if dot < 0 else (file_name[:dot], file_name[dot:])
    return template.replace("{stem}", stem).replace("{ext}", ext)


def _target_shape(line: str) -> bool:
    """One or more folders, then a file name; no empty, `.` or `..` segment, no glob character, no
    whitespace, no `#` and no backslash."""
    segments = line.split("/")
    if len(segments) < 2 or any(segment in ("", ".", "..") for segment in segments):
        return False
    return not any(ch in TARGET_FORBIDDEN or ch <= " " or ch == "\x7f" or ch.isspace() for ch in line)


@dataclass(frozen=True)
class Targets:
    """What the allowlist of mutation targets is checked against."""

    name: str  # the allowlist as messages name it
    sources: frozenset[str]  # the classification set
    tests: frozenset[str]  # the test set
    roots: list[str]  # [source].roots
    test_roots: list[str]  # [source].tests
    own_test: str  # the own test's file name, e.g. "{stem}Test{ext}"
    forbidden: list[str] = field(default_factory=list)  # globs an own test's file name must not match


def _target_entries(lines: list[str], base: str) -> list[tuple[str, str]]:
    """The entries of the allowlist of mutation targets in file order, each as the trimmed line and
    its repository-relative path; blank lines and lines that start with `#` are skipped, and no
    entry is checked."""
    prefix = base_prefix(base)
    entries: list[tuple[str, str]] = []
    for raw in lines:
        line = trim(raw)
        if line and not line.startswith("#"):
            entries.append((line, prefix + line))
    return entries


def target_files(lines: list[str], base: str) -> list[str]:
    """The repository-relative paths of the entries of the allowlist of mutation targets, in file
    order; they are the checked list only when target_violations() finds nothing."""
    return [path for _, path in _target_entries(lines, base)]


def target_violations(lines: list[str], base: str, targets: Targets) -> list[str]:
    """The broken rules of the allowlist of mutation targets, in line order; empty when it is valid.
    Each entry is one source file, relative to the base, with its own test in the same folder
    (relative to its source root) under one of the test roots."""
    name = targets.name
    entries = _target_entries(lines, base)
    violations: list[str] = []
    seen: set[str] = set()
    for line, path in entries:
        if not _target_shape(line):
            violations.append(malformed_target(name, line))
            continue
        if line in seen:
            violations.append(f"{name}: duplicate entry '{line}'")
            continue
        seen.add(line)
        if path not in targets.sources:
            # The walked set, not an existence check: a case-insensitive file system would accept a
            # path that a case-sensitive one does not have.
            violations.append(f"{name}: '{line}' is not a source file")
            continue
        root = root_of(path, targets.roots) or ""
        folder, _, file_name = path[len(root) + 1 if root else 0 :].rpartition("/")
        test_name = own_test_name(targets.own_test, file_name)
        forbidden = [glob for glob in targets.forbidden if matches(glob, test_name)]
        test_path = f"{folder}/{test_name}" if folder else test_name
        if forbidden:
            violations.append(
                f"{name}: '{line}' has its own test named '{test_name}', which matches own_test_forbidden"
                f" '{forbidden[0]}'"
            )
        elif not any(
            (f"{normalize(test_root)}/{test_path}" if normalize(test_root) else test_path) in targets.tests
            for test_root in targets.test_roots
        ):
            folders = " or ".join(targets.test_roots) or "no folder ([source].tests is not set)"
            violations.append(f"{name}: '{line}' has no own unit test '{test_path}' in {folders}")
    if not entries:
        # An empty target list must never reach a mutation tool: it could fall back to a project-wide default.
        violations.append(f"{name}: lists no file - it must list at least one file")
    return violations
