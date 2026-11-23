"""The repository profile, muster.toml: reading it and checking it against schema v1.

The checker knows every key of schema v1: an unknown key at any level, a value of the wrong type or
a value out of range is a configuration error (exit 2). No key is required here; each command names
the keys it needs with require(). Every path and glob is relative to the repository root, in
canonical POSIX form; every glob is well-formed, and compiled here. There is no built-in default
path.
"""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from muster.globs import alternatives
from muster.lang import BUILT_IN, Language, languages
from muster.report import FatalError, has_control

SCHEMA_VERSION = 1
DEFAULT_CONFIG = "muster.toml"
SECTIONS = ("policy", "coverage", "duration", "loc", "tests", "mutation")
DEFAULT_EXEMPTION_MAX_DAYS = 30
# The coverage thresholds that apply where the profile sets none (POLICY.md, point 4).
DEFAULT_FLOOR_LINE, DEFAULT_FLOOR_BRANCH = 0.80, 0.65
DEFAULT_DELTA_LINE, DEFAULT_DELTA_BRANCH = 0.80, 0.80
# The duration thresholds in seconds that apply where the profile sets none (POLICY.md, point 5).
DEFAULT_WARN_S, DEFAULT_ERROR_S = 240, 300
# The mutation thresholds of test strength that apply where the profile sets none (POLICY.md, point 6).
DEFAULT_MUTATION_THRESHOLD, DEFAULT_MUTATION_TARGET = 0.60, 0.70


class ConfigError(FatalError):
    """muster.toml is missing, unreadable or invalid."""

    kind = "config"


Check = Callable[[str, Any], None]

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_DRIVE = re.compile(r"[A-Za-z]:")
SHA256 = re.compile(r"[0-9a-f]{64}")  # a SHA-256 digest in lower-case hex (the profile, the snapshot)
_MISSING = object()
# The range of a TOML integer: 64-bit signed.
INT_MIN, INT_MAX = -(2**63), 2**63 - 1


def _key(prefix: str, name: str) -> str:
    """The dotted key; a key that is not a bare TOML key is quoted (ASCII, escaped as in JSON)."""
    name = name if _BARE_KEY.fullmatch(name) else json.dumps(name)
    return f"{prefix}.{name}" if prefix else name


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return "a date or time"


def _shown(value: Any) -> str:
    """A number as a message shows it; an integer too long for a decimal string is shown in hex."""
    try:
        return str(value)
    except ValueError:  # more digits than int-to-str conversion allows
        return hex(value)


def _expect(key: str, value: Any, ok: bool, expected: str) -> None:
    if not ok:
        raise ConfigError(f"{key}: expected {expected}, got {_kind(value)}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)  # a TOML boolean is never a number


def _in_toml_range(key: str, value: Any) -> None:
    """An integer that TOML cannot hold (Python reads one of any length) is an error; checked after
    the range of the key, so that a key with a range keeps its own message."""
    if _is_int(value) and not INT_MIN <= value <= INT_MAX:
        raise ConfigError(f"{key}: must be a 64-bit integer, from {INT_MIN} to {INT_MAX}")


def integer(minimum: int | None = None) -> Check:
    def check(key: str, value: Any) -> None:
        _expect(key, value, _is_int(value), "an integer")
        if minimum is not None and value < minimum:
            raise ConfigError(f"{key}: must be {minimum} or more, got {_shown(value)}")
        _in_toml_range(key, value)

    return check


def number(low: float, high: float | None = None, *, above: bool = False) -> Check:
    """A float or an integer, finite, in [low, high], or above low when `above`; an integer also fits
    in 64 bits. An integer is compared exactly, however large: it is never converted to a float."""

    def check(key: str, value: Any) -> None:
        _expect(key, value, _is_int(value) or isinstance(value, float), "a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"{key}: must be a finite number, got {value}")
        if above and value <= low:
            raise ConfigError(f"{key}: must be greater than {low:g}, got {_shown(value)}")
        if not above and value < low:
            raise ConfigError(f"{key}: must be {low:g} or more, got {_shown(value)}")
        if high is not None and value > high:
            raise ConfigError(f"{key}: must be {high:g} or less, got {_shown(value)}")
        _in_toml_range(key, value)

    return check


def string(choices: tuple[str, ...] = (), pattern: re.Pattern[str] | None = None, shape: str = "") -> Check:
    def check(key: str, value: Any) -> None:
        _expect(key, value, isinstance(value, str), "a string")
        if choices and value not in choices:
            raise ConfigError(f"{key}: must be one of {', '.join(choices)}, got {json.dumps(value)}")
        if pattern is not None and not pattern.fullmatch(value):
            raise ConfigError(f"{key}: must be {shape}")

    return check


_NOT_RELATIVE = "must be a path relative to the repository root with / separators"
_LINE_SEPARATORS = ("\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}")


def _has_control(value: str) -> bool:
    """A control character (Unicode category Cc: C0, DEL and C1), or a line or paragraph separator,
    which the zone grammar takes as a line terminator."""
    return has_control(value) or any(ch in _LINE_SEPARATORS for ch in value)


def _path_problem(value: str, allow_empty: bool = False) -> str | None:
    """What keeps `value` from being a canonical repository-relative path, or None."""
    if value == "":
        return None if allow_empty else "is empty"
    segments = value.split("/")
    return (
        "has a control character" if _has_control(value)
        else "starts with /" if value.startswith("/")
        else "has a backslash" if "\\" in value
        else "starts with a drive" if _DRIVE.match(value)
        else "has a .. segment" if ".." in segments
        else "has a . segment" if "." in segments
        else "ends with /" if value.endswith("/")
        else "has an empty segment" if "" in segments
        else None
    )


def path(*, allow_empty: bool = False) -> Check:
    """A path relative to the repository root in canonical form: forward slashes, no leading or
    trailing slash, no drive, no empty, `.` or `..` segment, no control character."""

    def check(key: str, value: Any) -> None:
        _expect(key, value, isinstance(value, str), "a string")
        problem = _path_problem(value, allow_empty)
        if problem:
            raise ConfigError(f"{key}: {_NOT_RELATIVE}; it {problem}")

    return check


def _alternatives(key: str, value: str) -> list[str]:
    try:
        return alternatives(value)
    except ValueError as exc:
        raise ConfigError(f"{key}: bad glob {json.dumps(value)}: {exc}") from None


def glob(key: str, value: Any) -> None:
    """A glob of repository-relative paths: well-formed, and the glob and each of its alternatives
    pass the rules of path()."""
    _PATH(key, value)
    for alternative in _alternatives(key, value):
        problem = _path_problem(alternative)
        if problem:
            raise ConfigError(f"{key}: {_NOT_RELATIVE}; its alternative {json.dumps(alternative)} {problem}")


def name_glob(key: str, value: Any) -> None:
    """A well-formed glob of file names, without control characters, and without / or backslash in
    the glob or in any of its alternatives: it is matched against a file name only. No alternative
    is empty, `.` or `..`, or starts with a drive."""
    _expect(key, value, isinstance(value, str), "a string")
    if _has_control(value):
        raise ConfigError(f"{key}: has a control character")
    if "/" in value or "\\" in value:  # an alternative is made of parts of the glob
        raise ConfigError(f"{key}: must be a glob of file names, without / or \\")
    for alternative in _alternatives(key, value):
        problem = (
            "is empty" if alternative == ""
            else "is . or .." if alternative in (".", "..")
            else "starts with a drive" if _DRIVE.match(alternative)
            else None
        )
        if problem:
            shown = json.dumps(alternative)
            raise ConfigError(f"{key}: must be a glob of file names; its alternative {shown} {problem}")


def file_name_template(key: str, value: Any) -> None:
    """The name of a file, with {stem} (and maybe {ext}) in it: no folder, so no / and no backslash."""
    _expect(key, value, isinstance(value, str), "a string")
    if "{stem}" not in value:
        raise ConfigError(f"{key}: must contain {{stem}}")
    if "/" in value or "\\" in value or _has_control(value):
        raise ConfigError(f"{key}: must be a file name, without / or \\ or a control character")


def regex(key: str, value: Any) -> None:
    _expect(key, value, isinstance(value, str), "a string")
    try:
        re.compile(value)
    except (re.error, OverflowError, RecursionError) as exc:  # also a huge repeat count, a deep nesting
        raise ConfigError(f"{key}: not a valid regular expression: {exc}") from None


def array(item: Check, *, non_empty: bool = False, length: int | None = None, what: str = "an array") -> Check:
    def check(key: str, value: Any) -> None:
        _expect(key, value, isinstance(value, list), what)
        if non_empty and not value:
            raise ConfigError(f"{key}: must not be empty")
        if length is not None and len(value) != length:
            raise ConfigError(f"{key}: must have {length} items, got {len(value)}")
        for i, entry in enumerate(value):
            item(f"{key}[{i}]", entry)

    return check


def table(schema: dict[str, Check], required: tuple[str, ...] = ()) -> Check:
    def check(key: str, value: Any) -> None:
        _expect(key, value, isinstance(value, dict), "a table")
        _check_table(key, value, schema, required)

    return check


def tables(schema: dict[str, Check], required: tuple[str, ...] = ()) -> Check:
    """An array of tables; each entry must carry the `required` keys."""
    return array(table(schema, required), what="an array of tables")


def _check_table(prefix: str, value: dict, schema: dict[str, Check], required: tuple[str, ...] = ()) -> None:
    for name, item in value.items():
        key = _key(prefix, name)
        if name not in schema:
            raise ConfigError(f"unknown key {key}")
        schema[name](key, item)
    for name in required:
        if name not in value:
            raise ConfigError(f"{prefix}: missing key {name}")


def _schema_version(key: str, value: Any) -> None:
    _expect(key, value, _is_int(value), "an integer")
    if value != SCHEMA_VERSION:
        raise ConfigError(f"{key}: unsupported schema {_shown(value)}; this muster reads schema {SCHEMA_VERSION}")


_PATH = path()
_PATHS = array(path())
_GLOBS = array(glob)
_FRACTION = number(0, 1)
_POSITIVE = number(0, above=True)
_COUNT = integer(0)
_STRINGS = array(string())

SCHEMA: dict[str, Check] = {
    "schema": _schema_version,
    "tool": table({"version": string(), "sha256": string(pattern=SHA256, shape="64 lower-case hex digits")}),
    "source": table(
        {
            "roots": array(path(), non_empty=True),
            "tests": _PATHS,
            "include": array(glob, non_empty=True),
            "exclude": _GLOBS,
            "generated": _GLOBS,
        }
    ),
    "zones": table(
        {
            "base": path(allow_empty=True),  # "" is the repository root
            "critical": _PATH,
            "standard": _PATH,
            "exemptions": _PATH,
            "exemption_max_days": _COUNT,
        }
    ),
    "policy": table({"snapshot": _PATH}),
    "coverage": table(
        {
            "format": string(choices=("jacoco",)),
            "report": _PATH,
            "floor": table({"line": _FRACTION, "branch": _FRACTION}),
            "delta": table({"line": _FRACTION, "branch": _FRACTION}),
            "full_run_suites": tables({"name": string(), "file": _PATH}, required=("name", "file")),
        }
    ),
    "tests": table({"format": string(choices=("junit",)), "results": glob}),
    "duration": table(
        {
            "source": string(choices=("task-json", "junit-span")),
            "task_json": _PATH,
            "task": string(),
            "warn_s": _POSITIVE,
            "error_s": _POSITIVE,
        }
    ),
    "loc": table(
        {
            "exclude": _GLOBS,
            "test_support": _GLOBS,
            "budget_ratio": _POSITIVE,
            "budget_grace_lines": _COUNT,
            "budget_trailer": string(),
        }
    ),
    "mutation": table(
        {
            "format": string(choices=("pit",)),
            "report": _PATH,
            "targets": _PATH,
            "threshold": _FRACTION,
            "target": _FRACTION,
            "own_test": file_name_template,
            "own_test_forbidden": array(name_glob),
            "tool_versions": table({"file": _PATH, "keys": _STRINGS}),
            "known_variation": tables(
                {"class": string(), "method": string(), "line": _COUNT, "mutator": string()},
                required=("class", "method", "line", "mutator"),
            ),
        }
    ),
    "language": tables(
        {
            "name": string(),
            "extensions": _STRINGS,
            "line_comment": string(),
            "block_comment": array(string(), length=2),
            "skip_patterns": array(regex),
        },
        required=("name",),
    ),
    "output": table({"dir": _PATH}),
    "check": table({"sections": array(string(choices=SECTIONS))}),
}


def _check_duration(data: dict) -> None:
    """The effective error threshold of the duration, a default included, is not below the warning
    threshold; the two may be equal."""
    duration = data.get("duration", {})
    warn_s = duration.get("warn_s", DEFAULT_WARN_S)
    error_s = duration.get("error_s", DEFAULT_ERROR_S)
    if error_s < warn_s:

        def shown(key: str, value: Any) -> str:
            return _shown(value) + ("" if key in duration else " (the default)")

        raise ConfigError(
            f"duration.error_s: {shown('error_s', error_s)} is below duration.warn_s {shown('warn_s', warn_s)}"
        )


def _check_mutation(data: dict) -> None:
    """The effective mutation target, a default included, is not below the threshold (the two may be
    equal), and the keys of mutation.tool_versions are usable: each one not empty, without a control
    character and listed once, and read from a file that the table names."""
    mutation = data.get("mutation", {})
    threshold = mutation.get("threshold", DEFAULT_MUTATION_THRESHOLD)
    target = mutation.get("target", DEFAULT_MUTATION_TARGET)
    if target < threshold:

        def shown(key: str, value: Any) -> str:
            return _shown(value) + ("" if key in mutation else " (the default)")

        raise ConfigError(
            f"mutation.target: {shown('target', target)} is below mutation.threshold {shown('threshold', threshold)}"
        )
    versions = mutation.get("tool_versions", {})
    seen: dict[str, int] = {}
    for i, key in enumerate(versions.get("keys", [])):
        item, shown_key = f"mutation.tool_versions.keys[{i}]", json.dumps(key)
        if key == "":
            raise ConfigError(f"{item}: must not be empty")
        if _has_control(key):
            raise ConfigError(f"{item}: has a control character, got {shown_key}")
        if key in seen:
            raise ConfigError(f"{item}: {shown_key} is already mutation.tool_versions.keys[{seen[key]}]")
        seen[key] = i
    if seen and "file" not in versions:
        raise ConfigError("mutation.tool_versions.file: missing; the versions of the keys are read from it")


def _extension_problem(value: str) -> str | None:
    return (
        "must start with ." if not value.startswith(".")
        else "must not contain / or \\" if "/" in value or "\\" in value
        else "must have a character after the ." if value == "."
        else "has a control character" if _has_control(value)
        else None
    )  # fmt: skip


def _marker_problem(value: str) -> str | None:
    """What makes a comment marker unusable: the counter scans one line at a time, so a control
    character (a line feed, say) would keep the marker from ever matching."""
    return (
        "must not be empty" if value == ""
        else "has a control character" if _has_control(value)
        else "must not be only white space" if not value.strip()
        else None
    )  # fmt: skip


def _check_languages(data: dict) -> None:
    """Every [[language]] entry is unambiguous: a name that is not empty and not taken by an earlier
    entry; a new language with at least one extension; every extension a file-name suffix that no
    other language has, the built-in ones included, and listed once in its entry; comment markers
    that are neither empty nor only white space, and on a built-in language only its own ones. No
    extension or marker has a control character."""
    owners = {ext: f"the built-in language {name}" for name, lang in BUILT_IN.items() for ext in lang.extensions}
    seen: dict[str, int] = {}
    for i, entry in enumerate(data.get("language", [])):
        key, name = f"language[{i}]", entry["name"]
        if name == "":
            raise ConfigError(f"{key}.name: must not be empty")
        if name in seen:
            raise ConfigError(f"{key}.name: {json.dumps(name)} is already the name of language[{seen[name]}]")
        seen[name] = i
        built_in = BUILT_IN.get(name)
        extensions = entry.get("extensions", _MISSING)
        if built_in is None and extensions is _MISSING:
            raise ConfigError(f"{key}: missing key extensions; a new language needs at least one")
        if built_in is None and not extensions:
            raise ConfigError(f"{key}.extensions: must not be empty for a new language")
        listed: dict[str, int] = {}
        for j, ext in enumerate([] if extensions is _MISSING else extensions):
            ext_key, shown = f"{key}.extensions[{j}]", json.dumps(ext)
            problem = _extension_problem(ext)
            if problem:
                raise ConfigError(f"{ext_key}: {problem}, got {shown}")
            if ext in listed:
                raise ConfigError(f"{ext_key}: {shown} is already {key}.extensions[{listed[ext]}]")
            listed[ext] = j
            owner = owners.setdefault(ext, key)
            if owner != key and not (built_in is not None and ext in built_in.extensions):
                raise ConfigError(f"{ext_key}: {shown} is already an extension of {owner}")
        markers = [("line_comment", entry["line_comment"])] if "line_comment" in entry else []
        markers += [(f"block_comment[{j}]", marker) for j, marker in enumerate(entry.get("block_comment", []))]
        for marker_key, marker in markers:
            problem = _marker_problem(marker)
            if problem == "must not be empty":
                raise ConfigError(f"{key}.{marker_key}: {problem}")
            if problem:
                raise ConfigError(f"{key}.{marker_key}: {problem}, got {json.dumps(marker)}")
        if built_in is not None:
            if "line_comment" in entry and entry["line_comment"] != built_in.line_comment:
                shown = json.dumps(built_in.line_comment)
                raise ConfigError(f"{key}.line_comment: must be {shown} on the built-in language {name}, or left out")
            if "block_comment" in entry and tuple(entry["block_comment"]) != built_in.block_comment:
                shown = json.dumps(list(built_in.block_comment or ()))
                raise ConfigError(f"{key}.block_comment: must be {shown} on the built-in language {name}, or left out")


def validate(data: dict) -> None:
    """Raises ConfigError naming the first problem of a parsed muster.toml."""
    _check_table("", data, SCHEMA)
    _check_duration(data)
    _check_mutation(data)
    _check_languages(data)


def config_path(root: Path, value: str) -> Path:
    """The config file named by --config: a relative value is relative to the repository root."""
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def _display(root: Path, file: Path) -> str:
    try:
        return file.relative_to(root).as_posix()
    except ValueError:
        return str(file)


@dataclass(frozen=True)
class Config:
    root: Path
    path: Path
    data: dict

    @property
    def name(self) -> str:
        """The config file as messages name it: relative to the repository root when inside it."""
        return _display(self.root, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        """The value of a dotted key such as `zones.base`, or `default` when it is not set."""
        value: Any = self.data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    @property
    def exemption_max_days(self) -> int:
        return self.get("zones.exemption_max_days", DEFAULT_EXEMPTION_MAX_DAYS)

    @property
    def coverage_floor(self) -> dict[str, float]:
        """The effective floor: each key the profile leaves out of coverage.floor has its default."""
        return {"line": DEFAULT_FLOOR_LINE, "branch": DEFAULT_FLOOR_BRANCH, **self.get("coverage.floor", {})}

    @property
    def coverage_delta(self) -> dict[str, float]:
        """The effective delta gate: each key the profile leaves out of coverage.delta has its default."""
        return {"line": DEFAULT_DELTA_LINE, "branch": DEFAULT_DELTA_BRANCH, **self.get("coverage.delta", {})}

    @property
    def duration_source(self) -> str:
        return self.get("duration.source", "task-json")

    @property
    def duration_task(self) -> str | None:
        """The task a timing record must name; None when the profile does not check it."""
        return self.get("duration.task")

    @property
    def duration_warn_s(self) -> float:
        return self.get("duration.warn_s", DEFAULT_WARN_S)

    @property
    def duration_error_s(self) -> float:
        return self.get("duration.error_s", DEFAULT_ERROR_S)

    @property
    def coverage_format(self) -> str:
        return self.get("coverage.format", "jacoco")

    @property
    def tests_format(self) -> str:
        return self.get("tests.format", "junit")

    @property
    def snapshot(self) -> str | None:
        return self.get("policy.snapshot")

    @property
    def output_dir(self) -> str | None:
        return self.get("output.dir")

    @property
    def mutation_format(self) -> str:
        return self.get("mutation.format", "pit")

    @property
    def mutation_threshold(self) -> float:
        return self.get("mutation.threshold", DEFAULT_MUTATION_THRESHOLD)

    @property
    def mutation_target(self) -> float:
        return self.get("mutation.target", DEFAULT_MUTATION_TARGET)

    @property
    def mutation_targets(self) -> str | None:
        return self.get("mutation.targets")

    @property
    def own_test(self) -> str | None:
        return self.get("mutation.own_test")

    @property
    def own_test_forbidden(self) -> list[str]:
        return self.get("mutation.own_test_forbidden", [])

    @property
    def loc_exclude(self) -> list[str]:
        return self.get("loc.exclude", [])

    @property
    def loc_test_support(self) -> list[str]:
        return self.get("loc.test_support", [])

    @property
    def languages(self) -> list[Language]:
        """The built-in languages and those of the [[language]] entries (muster.lang)."""
        return languages(self.get("language", []))


# Every key of the schema that names one file by a literal path; each entry of
# coverage.full_run_suites names one more. The folders and the globs are walked instead.
FILE_KEYS = (
    "zones.critical",
    "zones.standard",
    "zones.exemptions",
    "mutation.targets",
    "coverage.report",
    "policy.snapshot",
    "duration.task_json",
    "mutation.report",
    "mutation.tool_versions.file",
)


def named_files(cfg: Config, *, leave_out: tuple[str, ...] = ()) -> list[tuple[str, Path]]:
    """Every file the profile names by a literal path (label, path): the keys of FILE_KEYS but those
    in `leave_out`, then the file of each coverage.full_run_suites entry. It never raises, not even
    on a profile that failed its check, where it takes the string values it finds: the command line
    protects them before a command can report any error."""
    named = [(f"the file of {key}", cfg.get(key)) for key in FILE_KEYS if key not in leave_out]
    suites = cfg.get("coverage.full_run_suites")
    for i, suite in enumerate(suites if isinstance(suites, list) else []):
        if isinstance(suite, dict):
            named.append((f"the file of coverage.full_run_suites[{i}].file", suite.get("file")))
    usable = [(label, value) for label, value in named if isinstance(value, str) and value and "\0" not in value]
    return [(label, cfg.root / value) for label, value in usable]


def listed_folders(cfg: Config, key: str) -> list[str]:
    """The entries of the folder list `key` (source.roots, source.tests): on a checked profile all
    of them. It never raises, not even on a profile that failed its check, where it leaves out a
    value that is not a list and every entry that is not a canonical repository-relative path."""
    value = cfg.get(key)
    entries = value if isinstance(value, list) else []
    return [entry for entry in entries if isinstance(entry, str) and _path_problem(entry) is None]


def require(cfg: Config, *keys: str) -> None:
    """Raises ConfigError naming the first of the dotted keys that the profile does not set."""
    for key in keys:
        if cfg.get(key, _MISSING) is _MISSING:
            raise ConfigError(f"{cfg.name}: missing key {key}")


def _exact_case(root: Path, file: Path) -> bool:
    """Whether every part of `file` below `root` (only its name, outside the root) has exactly that
    name on disk: Windows would open the file under another case too."""
    try:
        current, parts = root, file.relative_to(root).parts
    except ValueError:
        current, parts = file.parent, (file.name,)
    for part in parts:
        if part not in ("", ".", ".."):
            try:
                if part not in os.listdir(current):
                    return False
            except OSError:
                return True  # reading the file says what is wrong
        current = current / part
    return True


def load(root: Path, value: str = DEFAULT_CONFIG) -> Config:
    """Read, parse and check the profile. Every failure is a ConfigError that names the file."""
    file = config_path(root, value)
    name = _display(root, file)
    try:
        if not _exact_case(root, file):
            raise FileNotFoundError(file)
        raw = file.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{name}: not found") from None
    except OSError as exc:
        raise ConfigError(f"{name}: cannot read: {exc.strerror or exc}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{name}: not valid UTF-8 (byte {exc.start})") from None
    try:
        data = tomllib.loads(text)
    except ValueError as exc:  # TOMLDecodeError, or an integer with more digits than Python converts
        raise ConfigError(f"{name}: invalid TOML: {exc}") from None
    except RecursionError:
        raise ConfigError(f"{name}: invalid TOML: nested too deeply") from None
    try:
        validate(data)
    except ConfigError as exc:
        raise ConfigError(f"{name}: {exc}") from None
    return Config(root, file, data)


def load_unchecked(root: Path, value: str = DEFAULT_CONFIG) -> Config | None:
    """The profile parsed but not checked against the schema, for a best-effort look into one that
    load() refused; None when it cannot be read or is not TOML. It never raises."""
    file = config_path(root, value)
    try:
        return Config(root, file, tomllib.loads(file.read_bytes().decode("utf-8")))
    except Exception:  # noqa: BLE001 - any failure means there is nothing to look into
        return None
