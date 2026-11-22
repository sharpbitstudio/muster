"""The policy command: classify every source file by the policy files and check the policy itself.

An invalid policy (a broken rule of the zone grammar, an unclassified package, a bad exemption or a
bad mutation target) exits 2 with every violation reported, with or without --strict. A valid one
can write the zone of every source file (--zones-out) and a snapshot of the classification set and
the policy (--snapshot), which later commands compare with the state they measure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from muster import __version__
from muster.adapters import strict_json
from muster.config import SHA256, Config, ConfigError, named_files, require
from muster.gitio import records
from muster.paths import SourceSets, exists_exact, resolve_glob, source_sets
from muster.report import FatalError, Report, ReportError, json_text, md_path, write_json
from muster.zones import Classification, Names, Targets, classify, target_files, target_violations

HELP = "classify the source files by the policy files and check the policy"
SNAPSHOT_SCHEMA = 1
# The freshness reason of a command when the snapshot is not there.
NO_SNAPSHOT = 'no snapshot: run "muster policy --snapshot" before the tests'
REQUIRED = ("source.roots", "source.include", "zones.base", "zones.critical", "zones.standard")
POLICY_FILES = ("zones.critical", "zones.standard", "zones.exemptions", "mutation.targets")
_FROM_CONFIG = object()  # --snapshot without FILE; no file name given on the command line is it


class PolicyError(FatalError):
    """The policy files are missing, unreadable or break a rule."""

    kind = "policy"


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const=_FROM_CONFIG,
        metavar="FILE",
        help="write the snapshot of the sources and the policy here (default: policy.snapshot of the profile)",
    )
    parser.add_argument("--zones-out", metavar="FILE", help="write the zone of every source file here")


def snapshot_path(args: argparse.Namespace, cfg: Config) -> Path | None:
    """The snapshot to write: FILE relative to the working directory, else policy.snapshot relative
    to the repository root; None without --snapshot."""
    if args.snapshot is None:
        return None
    if args.snapshot is not _FROM_CONFIG:
        if not args.snapshot:
            raise FatalError("--snapshot: the file name is empty")
        return Path(args.snapshot)
    if cfg.snapshot is None:
        raise ConfigError(f"{cfg.name}: --snapshot without FILE needs the key policy.snapshot")
    return cfg.root / cfg.snapshot


def snapshot_name(args: argparse.Namespace, cfg: Config) -> str:
    """The snapshot to write as messages name it: FILE as typed, else policy.snapshot as the profile
    names it (repository-relative)."""
    return cfg.snapshot if args.snapshot is _FROM_CONFIG else args.snapshot


def _policy_files(cfg: Config) -> list[tuple[str, str]]:
    """(key, configured path) of every policy file the profile names."""
    return [(key, cfg.get(key)) for key in POLICY_FILES if cfg.get(key) is not None]


def inputs(cfg: Config) -> list[tuple[str, Path]]:
    """Every file the profile names (label, path) but the snapshot, which this command may write;
    protected() keeps the snapshot from a run that does not write it. Like config.named_files, it
    never raises, not even on a profile that failed its check."""
    return named_files(cfg, leave_out=("policy.snapshot",))


def outputs(args: argparse.Namespace, cfg: Config) -> tuple[list[tuple[str, Path, bool, bool]], FatalError | None]:
    """The output files that can be named (label, path, removed before the check, removed when the
    run fails), and the error that keeps one from being named, or None. It never raises: the command
    line checks the named files first and then raises the error."""
    files, error = [], None
    if args.zones_out == "":
        error = FatalError("--zones-out: the file name is empty")
    elif args.zones_out is not None:
        files.append((f"--zones-out {args.zones_out}", Path(args.zones_out), True, True))
    try:
        snapshot = snapshot_path(args, cfg)
    except FatalError as exc:
        snapshot, error = None, error or exc
    if snapshot is not None:
        # Kept while the check runs, so that an unchanged snapshot keeps its created_epoch_ms.
        files.append((f"the snapshot {snapshot_name(args, cfg)}", snapshot, False, True))
    return files, error


def _writes_snapshot(args: argparse.Namespace, cfg: Config) -> bool:
    """Whether this run writes the snapshot of policy.snapshot: --snapshot without FILE, or a FILE
    that resolves to the same path (ignoring case on Windows)."""
    option = getattr(args, "snapshot", None)
    if option is _FROM_CONFIG:
        return True
    if not isinstance(option, str) or not option:
        return False
    try:
        return os.path.normcase(os.path.realpath(option)) == os.path.normcase(os.path.realpath(cfg.root / cfg.snapshot))
    except (OSError, ValueError):  # a name no path can hold, such as one with a NUL, is no file of the profile
        return False


def protected(args: argparse.Namespace, cfg: Config) -> list[tuple[str, Path]]:
    """The repository files no output of any analysis command may be: every file the walk of the
    source and the test folders found, before the include, exclude and generated globs; every JUnit
    file of tests.results; and the snapshot of policy.snapshot unless this run writes it. It never
    raises: the walk is kept in args.source_sets and the JUnit files in args.junit_files for run(),
    which reports a folder that is missing or cannot be read."""
    sets = args.source_sets = source_sets(cfg, defer=True)
    files = [(f"the source file {path}", cfg.root / path) for path in sets.walked_sources]
    files += [(f"the test file {path}", cfg.root / path) for path in sets.walked_tests]
    results = cfg.get("tests.results")
    args.junit_files = resolve_glob(cfg.root, results) if results is not None else []
    files += [(f"the JUnit file {path}", cfg.root / path) for path in args.junit_files]
    if cfg.snapshot is not None and not _writes_snapshot(args, cfg):
        files.append(("the file of policy.snapshot", cfg.root / cfg.snapshot))
    return files


def utc_today(clock=time.time) -> date:
    """Today's date in UTC, whatever the local time zone."""
    return datetime.fromtimestamp(clock(), timezone.utc).date()


def policy_sha256(critical: bytes, standard: bytes, exemptions: bytes, tables: dict) -> str:
    """SHA-256 over the raw SHA-256 digests of the three policy files in this order (so a line moved
    from one file to the next changes it), then the canonical JSON of the [source] and [zones]
    tables. An exemption file that is not configured counts as empty."""
    digests = b"".join(hashlib.sha256(content).digest() for content in (critical, standard, exemptions))
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(digests + canonical.encode("ascii")).hexdigest()


def utf8_name(path: str) -> bytes:
    """The UTF-8 bytes of a file name. A name that UTF-8 cannot encode (a POSIX name with a byte that
    is not UTF-8, a lone surrogate in a Windows name) is a FatalError that names it escaped."""
    try:
        return path.encode("utf-8")
    except UnicodeEncodeError:
        shown = path.encode("utf-8", "backslashreplace").decode("utf-8")
        raise FatalError(f"{shown}: the file name is not valid UTF-8, which --zones-out and --snapshot need") from None


def sources_sha256(sources: list[str]) -> str:
    """SHA-256 over the UTF-8 text of the sorted paths, each followed by a line feed; a path that
    UTF-8 cannot encode is a FatalError (see utf8_name)."""
    return hashlib.sha256(b"".join(utf8_name(path) + b"\n" for path in sources)).hexdigest()


def zones_text(classification: Classification) -> str:
    """One row per source file, sorted: path, zone and the last day of its exemption (or nothing)."""
    rows = []
    for path, zone in sorted(classification.zones.items()):
        until = classification.exempt_until.get(path)
        rows.append(f"{path}\t{zone}\t{until.isoformat() if until else ''}\n")
    return "".join(rows)


def _read(cfg: Config, key: str) -> bytes:
    """The raw bytes of a policy file; a missing one (in exactly this case) is a PolicyError."""
    name = cfg.get(key)
    if not exists_exact(cfg.root, name):
        raise PolicyError(f"{name}: not found ({key})")
    try:
        return (cfg.root / name).read_bytes()
    except OSError as exc:
        raise PolicyError(f"{name}: cannot read: {exc.strerror or exc}") from None


def _lines(name: str, raw: bytes) -> list[str]:
    try:
        return records(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{name}: not valid UTF-8 (byte {exc.start})") from None


def _check_profile(cfg: Config) -> None:
    """The parts of the profile that only this command can check."""
    require(cfg, *REQUIRED)
    if cfg.mutation_targets is not None:
        require(cfg, "mutation.own_test")
    for i, suite in enumerate(cfg.get("coverage.full_run_suites", [])):
        if not exists_exact(cfg.root, suite["file"]):
            raise ConfigError(
                f"{cfg.name}: coverage.full_run_suites[{i}].file: {suite['file']} does not exist"
                " (suite renamed or deleted)"
            )


def _sets(args: argparse.Namespace, cfg: Config) -> SourceSets:
    sets = getattr(args, "source_sets", None) or source_sets(cfg, defer=True)
    if sets.error is not None:
        raise ConfigError(f"{cfg.name}: {sets.error}")
    if not sets.sources:
        raise ConfigError(f"{cfg.name}: no file under source.roots matches source.include")
    return sets


@dataclass(frozen=True)
class CheckedPolicy:
    """The outcome of check_policy(): the walked sets, the classification and the policy files."""

    sets: SourceSets
    classification: Classification
    raw: dict[str, bytes]  # the bytes of each configured policy file, by profile key
    targets: int | None  # the number of mutation target lines; None without mutation.targets
    violations: list[str]  # every violation of the policy; empty when it is valid
    # The repository-relative paths of the mutation targets in file order: None without
    # mutation.targets, empty when the target list has a violation.
    target_files: tuple[str, ...] | None


def check_policy(args: argparse.Namespace, cfg: Config, today: date) -> CheckedPolicy:
    """Check the profile, walk the source sets, read the policy files and classify every source
    file on `today`. A profile or tree problem is a ConfigError, a policy file that is missing,
    unreadable or not UTF-8 a PolicyError; the violations of a policy that could be read are
    returned, not reported, so that each command gives its own verdict."""
    _check_profile(cfg)
    sets = _sets(args, cfg)
    raw = {key: _read(cfg, key) for key, _ in _policy_files(cfg)}
    lines = {key: _lines(cfg.get(key), data) for key, data in raw.items()}
    base = cfg.get("zones.base")
    names = Names(cfg.get("zones.critical"), cfg.get("zones.standard"), cfg.get("zones.exemptions", ""))
    classification = classify(
        lines["zones.critical"],
        lines["zones.standard"],
        lines.get("zones.exemptions", []),
        sets.sources,
        base=base,
        names=names,
        today=today,
        max_days=cfg.exemption_max_days,
    )
    violations = list(classification.violations)
    targets = None
    checked_targets = None
    if "mutation.targets" in lines:
        target_lines = lines["mutation.targets"]
        entries = target_files(target_lines, base)
        targets = len(entries)
        target_problems = target_violations(
            target_lines,
            base,
            Targets(
                name=cfg.mutation_targets,
                sources=frozenset(sets.sources),
                tests=sets.tests,
                roots=cfg.get("source.roots"),
                test_roots=cfg.get("source.tests", []),
                own_test=cfg.own_test,
                forbidden=cfg.own_test_forbidden,
            ),
        )
        violations += target_problems
        checked_targets = () if target_problems else tuple(entries)
    return CheckedPolicy(sets, classification, raw, targets, violations, checked_targets)


def policy_hash(cfg: Config, checked: CheckedPolicy) -> str:
    """The policy_sha256 of a checked policy: its critical, standard and exemption files (empty
    bytes without zones.exemptions) and the [source] and [zones] tables of the profile."""
    tables = {"source": cfg.get("source"), "zones": cfg.get("zones")}
    parts = [checked.raw.get(key, b"") for key in ("zones.critical", "zones.standard", "zones.exemptions")]
    return policy_sha256(*parts, tables)


def snapshot_problems(cfg: Config, checked: CheckedPolicy, snapshot: dict | None) -> list[str]:
    """The reasons why the snapshot no longer matches the checked policy: the policy hash differs,
    or the source set changed (with the counts and the first path of each change); empty when it
    matches or when there is no snapshot."""
    problems: list[str] = []
    if snapshot is None:
        return problems
    if snapshot["policy_sha256"] != policy_hash(cfg, checked):
        problems.append("the policy changed after the snapshot (policy_sha256 differs)")
    current, recorded = set(checked.sets.sources), set(snapshot["sources"])
    added, gone = sorted(current - recorded), sorted(recorded - current)
    parts = []
    if added:
        parts.append(f"{len(added)} added (e.g. {added[0]})")
    if gone:
        parts.append(f"{len(gone)} gone (e.g. {gone[0]})")
    if parts:
        problems.append("the source set changed after the snapshot: " + ", ".join(parts))
    return problems


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def report_invalid(
    rep: Report,
    violations: list[str],
    *,
    heading: str,
    verdict: str,
    message: str,
    summary: str = "**INVALID**: {count}.",
) -> PolicyError:
    """Report the violations of an invalid policy: each one is a violation of the section and an
    error annotation, and the summary lists them under `heading`, after the line `summary`.
    `{count}` in `verdict`, `summary` and `message` (the text of the returned PolicyError) becomes
    the number of violations, as in "1 violation" or "3 violations"."""
    rep.violation = True
    for violation in violations:
        rep.violations.append(violation)
        rep.annotate("error", "Invalid policy", violation)
    count = _plural(len(violations), "violation")
    rep.verdict = verdict.replace("{count}", count)
    lines = [f"- {md_path(violation)}" for violation in violations]
    rep.md = [heading, "", summary.replace("{count}", count), "", *lines]
    return PolicyError(message.replace("{count}", count))


def _invalid(rep: Report, violations: list[str]) -> PolicyError:
    return report_invalid(
        rep,
        violations,
        heading="## Policy",
        verdict="policy: INVALID ({count})",
        message="the policy is invalid: {count}",
    )


def _passed(rep: Report, classification: Classification, targets: int | None) -> None:
    counts: dict[str, int] = {}
    for zone in classification.zones.values():
        counts[zone] = counts.get(zone, 0) + 1
    standard = counts.pop("standard", 0)
    zones = {zone: counts[zone] for zone in sorted(counts, key=lambda zone: int(zone[1:]))}
    critical = sum(zones.values())
    exemptions = [{"path": path, "until": until.isoformat()} for path, until in classification.exempt_until.items()]
    rep.data = {
        "sources": len(classification.zones),
        "critical": critical,
        "standard": standard,
        "zones": zones,
        "exemptions": exemptions,
        "mutation_targets": targets,
    }
    summary = (
        f"{_plural(len(classification.zones), 'source file')}: {critical} critical in {_plural(len(zones), 'zone')},"
        f" {standard} standard; {_plural(len(exemptions), 'exemption')}"
    )
    rep.verdict = f"policy: PASS ({summary})"
    rep.md = ["## Policy", "", f"**PASS**: {summary}.", "", "| Zone | Files |", "|---|---:|"]
    rep.md += [f"| {zone} | {count} |" for zone, count in zones.items()] + [f"| standard | {standard} |"]
    if exemptions:
        rep.md += ["", "| Exempt file | Until |", "|---|---|"]
        rep.md += [f"| {md_path(item['path'])} | {item['until']} |" for item in exemptions]


def _write_zones(target: Path, text: str) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise FatalError(f"cannot write --zones-out {target}: {exc.strerror or exc}") from None


def _snapshot_problem(document: object) -> str | None:
    """What keeps a parsed document from having the shape of a snapshot, or None."""
    if not isinstance(document, dict):
        return "the top level is not an object"

    def integer(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def sha256(value: object) -> bool:
        return isinstance(value, str) and SHA256.fullmatch(value) is not None

    def strings(value: object) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    checks = (
        ("schema", integer, "an integer"),
        ("tool_version", lambda value: isinstance(value, str), "a string"),
        ("created_epoch_ms", lambda value: integer(value) and value >= 0, "a non-negative integer"),
        ("sources", strings, "a list of strings"),
        ("sources_sha256", sha256, "64 lower-case hex digits"),
        ("policy_sha256", sha256, "64 lower-case hex digits"),
    )
    for key, valid, expected in checks:
        if key not in document:
            return f"the key {key} is missing"
        if not valid(document[key]):
            return f"{key} must be {expected}"
        if key == "schema" and document[key] != SNAPSHOT_SCHEMA:
            return f"schema {document[key]} is not supported (only {SNAPSHOT_SCHEMA})"
    return None


def read_snapshot(path: Path, *, name: str | None = None) -> dict:
    """The snapshot in `path`, checked against the shape that --snapshot writes; a key it does not
    know is allowed. A missing file raises FileNotFoundError, for the caller to judge; any other
    problem (unreadable, not UTF-8, not JSON, NaN or an infinity, also in a key it does not know,
    nested too deeply, a missing key, a value of the wrong type, another schema) is a ReportError
    that names the file: `name`, by default the path."""
    name = str(path) if name is None else name
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ReportError(f"{name}: cannot read the snapshot: {exc.strerror or exc}") from None
    try:
        document = strict_json(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ReportError(f"{name}: the snapshot is not valid UTF-8 (byte {exc.start})") from None
    except ValueError as exc:
        raise ReportError(f"{name}: the snapshot is not valid JSON: {exc}") from None
    except RecursionError:
        raise ReportError(f"{name}: the snapshot is nested too deeply") from None
    problem = _snapshot_problem(document)
    if problem:
        raise ReportError(f"{name}: not a snapshot: {problem}")
    return document


def _write_snapshot(target: Path, name: str, sources: list[str], sources_hash: str, policy_hash: str) -> None:
    """Write the snapshot, unless the one there has a valid shape, the same sources and the same
    sources and policy hashes; one that is missing, unreadable or broken is written anew."""
    try:
        old = read_snapshot(target)
        if old["sources"] == sources and (old["sources_sha256"], old["policy_sha256"]) == (sources_hash, policy_hash):
            return
    except (ReportError, OSError):
        pass
    document = {
        "schema": SNAPSHOT_SCHEMA,
        "tool_version": __version__,
        "created_epoch_ms": time.time_ns() // 1_000_000,
        "sources": sources,
        "sources_sha256": sources_hash,
        "policy_sha256": policy_hash,
    }
    try:
        write_json(target, json_text(document))
    except OSError as exc:
        raise FatalError(f"cannot write the snapshot {name}: {exc.strerror or exc}") from None


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Classify the source files and check the policy files; the command line removes the snapshot
    when the run fails."""
    checked = check_policy(args, cfg, utc_today())
    if checked.violations:
        raise _invalid(rep, checked.violations)
    _passed(rep, checked.classification, checked.targets)
    snapshot = snapshot_path(args, cfg)
    if args.zones_out is None and snapshot is None:
        return
    sources_hash = sources_sha256(checked.sets.sources)  # every name is UTF-8 before an output is written
    if args.zones_out is not None:
        _write_zones(Path(args.zones_out), zones_text(checked.classification))
    if snapshot is not None:
        name = snapshot_name(args, cfg)
        _write_snapshot(snapshot, name, checked.sets.sources, sources_hash, policy_hash(cfg, checked))
