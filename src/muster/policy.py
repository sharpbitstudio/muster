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
import time
from datetime import date, datetime, timezone
from pathlib import Path

from muster import __version__
from muster.config import Config, ConfigError, require
from muster.gitio import records
from muster.globs import check_globs
from muster.paths import SourceSets, exists_exact, source_sets
from muster.report import FatalError, Report, json_text, md_path, write_json
from muster.zones import Classification, Names, Targets, classify, parse_date, target_violations, trim

HELP = "classify the source files by the policy files and check the policy"
SNAPSHOT_SCHEMA = 1
REQUIRED = ("source.roots", "source.include", "zones.base", "zones.critical", "zones.standard")
_FROM_CONFIG = "<policy.snapshot>"  # --snapshot without FILE


class PolicyError(FatalError):
    """The policy files are missing, unreadable or break a rule."""

    kind = "policy"


def _iso_date(text: str) -> date:
    day = parse_date(text)
    if day is None:
        raise argparse.ArgumentTypeError(f"expected a date YYYY-MM-DD, got '{text}'")
    return day


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const=_FROM_CONFIG,
        metavar="FILE",
        help="write the snapshot of the sources and the policy here (default: policy.snapshot of the profile)",
    )
    parser.add_argument("--zones-out", metavar="FILE", help="write the zone of every source file here")
    parser.add_argument("--today", type=_iso_date, help=argparse.SUPPRESS)  # a fixed date, for tests


def snapshot_path(args: argparse.Namespace, cfg: Config) -> Path | None:
    """The snapshot to write: FILE relative to the working directory, else policy.snapshot relative
    to the repository root; None without --snapshot."""
    if args.snapshot is None:
        return None
    if args.snapshot != _FROM_CONFIG:
        if not args.snapshot:
            raise FatalError("--snapshot: the file name is empty")
        return Path(args.snapshot)
    if cfg.snapshot is None:
        raise ConfigError(f"{cfg.name}: --snapshot without FILE needs the key policy.snapshot")
    return cfg.root / cfg.snapshot


def _policy_files(cfg: Config) -> list[tuple[str, str]]:
    """(key, configured path) of every policy file the profile names."""
    keys = ("zones.critical", "zones.standard", "zones.exemptions", "mutation.targets")
    return [(key, cfg.get(key)) for key in keys if cfg.get(key) is not None]


def files(args: argparse.Namespace, cfg: Config) -> tuple[list[tuple[str, Path, bool]], list[tuple[str, Path]]]:
    """The output files (label, path, removed before the check) and the input files (label, path)
    of this run; the command line checks that no two of them are the same file."""
    require(cfg, *REQUIRED)
    outputs = []
    if args.zones_out is not None:
        outputs.append((f"--zones-out {args.zones_out}", Path(args.zones_out), True))
    snapshot = snapshot_path(args, cfg)
    if snapshot is not None:
        outputs.append((f"the snapshot {snapshot}", snapshot, False))
    inputs = [(f"the file of {key}", cfg.root / value) for key, value in _policy_files(cfg)]
    return outputs, inputs


def utc_today(clock=time.time) -> date:
    """Today's date in UTC, whatever the local time zone."""
    return datetime.fromtimestamp(clock(), timezone.utc).date()


def policy_sha256(critical: bytes, standard: bytes, exemptions: bytes, tables: dict) -> str:
    """SHA-256 over the raw bytes of the three policy files in this order, then the canonical JSON of
    the [source] and [zones] tables."""
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(critical + standard + exemptions + canonical.encode("ascii")).hexdigest()


def sources_sha256(sources: list[str]) -> str:
    """SHA-256 over the UTF-8 text of the sorted paths, each followed by a line feed."""
    return hashlib.sha256("".join(f"{path}\n" for path in sources).encode("utf-8", "surrogateescape")).hexdigest()


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
    if cfg.mutation_targets is not None:
        require(cfg, "mutation.own_test")
        try:
            check_globs("mutation.own_test_forbidden", cfg.own_test_forbidden)
        except ConfigError as exc:
            raise ConfigError(f"{cfg.name}: {exc}") from None
    for i, suite in enumerate(cfg.get("coverage.full_run_suites", [])):
        if not exists_exact(cfg.root, suite["file"]):
            raise ConfigError(
                f"{cfg.name}: coverage.full_run_suites[{i}].file: {suite['file']} does not exist"
                " (suite renamed or deleted)"
            )


def _sets(cfg: Config) -> SourceSets:
    try:
        sets = source_sets(cfg)
    except ConfigError as exc:
        raise ConfigError(f"{cfg.name}: {exc}") from None
    if not sets.sources:
        raise ConfigError(f"{cfg.name}: no file under source.roots matches source.include")
    return sets


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _invalid(rep: Report, violations: list[str]) -> PolicyError:
    rep.violation = True
    for message in violations:
        rep.violations.append(message)
        rep.annotate("error", "Invalid policy", message)
    count = _plural(len(violations), "violation")
    rep.verdict = f"policy: INVALID ({count})"
    rep.md = ["## Policy", "", f"**INVALID**: {count}.", "", *(f"- {md_path(message)}" for message in violations)]
    return PolicyError(f"the policy is invalid: {count}")


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
        target.write_bytes(text.encode("utf-8", "surrogateescape"))
    except OSError as exc:
        raise FatalError(f"cannot write --zones-out {target}: {exc.strerror or exc}") from None


def _write_snapshot(target: Path, sources: list[str], policy_hash: str) -> None:
    """Write the snapshot, unless the one there already has the same sources and policy hashes."""
    sources_hash = sources_sha256(sources)
    try:
        old = json.loads(target.read_bytes())
        if old.get("sources_sha256") == sources_hash and old.get("policy_sha256") == policy_hash:
            return
    except (OSError, ValueError, AttributeError):
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
        raise FatalError(f"cannot write the snapshot {target}: {exc.strerror or exc}") from None


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    snapshot = snapshot_path(args, cfg)
    try:
        _run(args, cfg, rep, snapshot)
    except BaseException:
        if snapshot is not None:  # a failed check leaves no snapshot behind
            try:
                snapshot.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _run(args: argparse.Namespace, cfg: Config, rep: Report, snapshot: Path | None) -> None:
    _check_profile(cfg)
    sets = _sets(cfg)
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
        today=args.today or utc_today(),
        max_days=cfg.exemption_max_days,
    )
    violations = list(classification.violations)
    targets = None
    if "mutation.targets" in lines:
        target_lines = lines["mutation.targets"]
        targets = sum(1 for raw_line in target_lines if trim(raw_line) and not trim(raw_line).startswith("#"))
        violations += target_violations(
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
    if violations:
        raise _invalid(rep, violations)
    _passed(rep, classification, targets)
    if args.zones_out is not None:
        _write_zones(Path(args.zones_out), zones_text(classification))
    if snapshot is not None:
        tables = {"source": cfg.get("source"), "zones": cfg.get("zones")}
        parts = [raw.get(key, b"") for key in ("zones.critical", "zones.standard", "zones.exemptions")]
        _write_snapshot(snapshot, sets.sources, policy_sha256(*parts, tables))
