"""The mutation command: the test strength of the mutation targets in a PIT report, against the
threshold of the profile.

The order is fixed, as in coverage: the keys the command needs, then the policy (an invalid one
exits 2, as in the policy command, also without a report), then the report, its freshness and the
verdict. Test strength is the detected mutants over the covered ones, those a test reaches (every
mutant but those without coverage); the mutation score is the detected mutants over all of them.
`detected` is the attribute of the report, never derived from the status.

The report and the allowlist of mutation targets must agree: a mutated file that is not listed,
also one under no source root, is an input error, and so is a report without any covered mutant;
a listed file without a mutant, or without a covered one, gets a warning. A report that is missing
or cannot be read is an input error, and only the heading and the errors are shown.

The table of the zones comes from the current classification, and is shown only while the snapshot
that `muster policy --snapshot` wrote before the run matches the current policy and source set and
the classification set holds every mutated file. Otherwise, a missing or broken snapshot included,
one warning gives the reasons and the rest of the report stays; that is never an input error or a
violation.
"""

from __future__ import annotations

import argparse
import tomllib
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from muster import policy
from muster.adapters import SourceResolver, exists, pit
from muster.adapters.pit import Mutant, MutationReport
from muster.config import Config, require
from muster.paths import exists_exact
from muster.policy import CheckedPolicy
from muster.report import Report, ReportError, md_path, one_line, percent_digits

HELP = "mutation testing of the allowlist: test strength"
SURVIVED_FILES_CAP = 50
SURVIVED_ITEMS_CAP = 40
UNKNOWN = "unknown"


@dataclass
class Mutants:
    """The counters of a set of mutants, by the normalized status and the detected attribute."""

    total: int = 0
    no_coverage: int = 0
    detected: int = 0
    killed: int = 0
    survived: int = 0

    def add(self, mutant: Mutant) -> None:
        self.total += 1
        self.no_coverage += int(mutant.status == "no-coverage")
        self.detected += int(mutant.detected)
        self.killed += int(mutant.status == "killed")
        self.survived += int(mutant.status == "survived")

    @property
    def covered(self) -> int:
        """The mutants a test reaches: every one but those without coverage."""
        return self.total - self.no_coverage

    def strength(self) -> float | None:
        """Test strength, detected over covered; None when no mutant is covered."""
        return None if self.covered == 0 else self.detected / self.covered

    def score(self) -> float | None:
        """Mutation score, detected over all; None without a mutant."""
        return None if self.total == 0 else self.detected / self.total

    def to_json(self) -> dict:
        return {
            "total": self.total,
            "no_coverage": self.no_coverage,
            "detected": self.detected,
            "killed": self.killed,
            "survived": self.survived,
            "covered": self.covered,
            "strength": self.strength(),
            "score": self.score(),
        }


def pit_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f} %"


def strength_order(item: tuple[str, Mutants]) -> tuple:
    """n/a first (nothing measured is the worst news), then ascending test strength, then name."""
    name, acc = item
    strength = acc.strength()
    return (strength is not None, strength or 0.0, name)


def mutants_row(name: str, acc: Mutants, note: str) -> str:
    return (
        f"| {name} | {acc.total} | {acc.covered} | {acc.no_coverage} | {acc.detected} | {pit_pct(acc.strength())}"
        f" | {pit_pct(acc.score())} | {note} |"
    )


def mutants_header(first: str, last: str = "note", last_align: str = "---") -> list[str]:
    return [
        f"| {first} | mutations | covered | NO_COVERAGE | detected | test strength | mutation score | {last} |",
        f"|---|---:|---:|---:|---:|---:|---:|{last_align}|",
    ]


def _percent(fraction: float) -> str:
    """A threshold as the report names it, exactly as the profile gives it: 0.6 is `60 %`, 0.625 is
    `62.5 %`."""
    return percent_digits(fraction) + " %"


def _below(acc: Mutants, threshold: float) -> bool:
    """Whether the test strength is measured and below the threshold, compared exactly with the
    value as the profile gives it: equality is not below."""
    return acc.covered > 0 and Fraction(acc.detected, acc.covered) < Fraction(repr(threshold))


def _code(text: str) -> str:
    """Text of the report as an inline code span, on one line."""
    return md_path(one_line(text))


def _cell(text: str) -> str:
    """Text of the report as an inline code span in a table cell, where a | would end the cell."""
    return _code(text).replace("|", "\\|")


def _by_count(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Most frequent first, then by name."""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _version(value: object) -> str:
    """A version: a string that is not empty and has no control character, else unknown."""
    usable = isinstance(value, str) and value and not any(unicodedata.category(ch) == "Cc" for ch in value)
    return value if usable else UNKNOWN


def _versions(cfg: Config) -> dict[str, str]:
    """The version of each key of mutation.tool_versions, from the [versions] table of its TOML
    file. A file that is missing (in exactly this case), not a file, unreadable, not UTF-8 or not
    TOML, and a `versions` that is not a table, give unknown for every key; it is never an error."""
    keys = cfg.get("mutation.tool_versions.keys", [])
    if not keys:
        return {}
    name = cfg.get("mutation.tool_versions.file")
    table: object = None
    try:
        if exists_exact(cfg.root, name):
            table = tomllib.loads((cfg.root / name).read_bytes().decode("utf-8")).get("versions")
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        table = None
    found = table if isinstance(table, dict) else {}
    return {key: _version(found.get(key)) for key in keys}


def _read_snapshot(cfg: Config, reasons: list[str]) -> dict | None:
    """The snapshot, or None with the reason why there is none: here a missing or broken snapshot
    only leaves the zones out."""
    name = cfg.snapshot
    try:
        if exists(cfg.root, name):
            return policy.read_snapshot(cfg.root / name, name=name)
        reasons.append(policy.NO_SNAPSHOT)
    except FileNotFoundError:  # removed since it was seen
        reasons.append(policy.NO_SNAPSHOT)
    except ReportError as exc:
        reasons.append(str(exc))
    return None


def _zone_reasons(cfg: Config, checked: CheckedPolicy, report: MutationReport) -> list[str]:
    """Why the zones cannot be judged: no snapshot, or one that no longer matches the policy and the
    source set, or a mutated file outside the classification set (under no source root, or
    resolved outside the set); empty when they can."""
    reasons: list[str] = []
    snapshot = _read_snapshot(cfg, reasons)
    reasons += policy.snapshot_problems(cfg, checked, snapshot)
    classified = set(checked.sets.sources)
    outside = {mutant.file for mutant in report.mutants if mutant.file is not None and mutant.file not in classified}
    missing = sorted(report.unresolved + sorted(outside))
    if missing:
        reasons.append(f"{len(missing)} mutated source file(s) not in the classification set (e.g. {missing[0]})")
    return reasons


def _zones(checked: CheckedPolicy, report: MutationReport, files: list[str]) -> dict[str, dict]:
    """Every zone of the classification, in the order of the zone numbers with standard last: the
    number of the listed and the mutated files in it, and the counters of its mutants."""
    zone_of = checked.classification.zones
    names = set(zone_of.values())
    order = sorted(names - {"standard"}, key=lambda zone: int(zone[1:])) + sorted(names & {"standard"})
    zones = {zone: {"files": 0, "acc": Mutants()} for zone in order}
    for path in files:
        zones[zone_of[path]]["files"] += 1
    for mutant in report.mutants:
        zones[zone_of[mutant.file]]["acc"].add(mutant)
    return zones


def _errored(rep: Report, errors: list[str], data: dict) -> None:
    """The report could not be read: every error is reported, with only the heading, and no verdict."""
    for error in errors:
        rep.errored("Mutation testing", error)
    rep.evaluated = False
    rep.data = {"outcome": "error", **data}
    rep.md = ["## Mutation testing", "", "**ERROR**: the inputs could not be read, no verdict:", ""]
    rep.md.extend(f"- {error}" for error in errors)
    rep.verdict = f"mutation: ERROR ({errors[0]})"


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Judge the mutation report; the command line has set args.head_commit."""
    require(cfg, *policy.REQUIRED, "mutation.report", "mutation.targets", "mutation.own_test", "policy.snapshot")
    checked = policy.check_policy(args, cfg, policy.utc_today())
    if checked.violations:
        raise policy.report_invalid(
            rep,
            checked.violations,
            heading="## Mutation testing",
            verdict="mutation: NOT EVALUATED (invalid policy: {count})",
            message="the policy is invalid: {count}",
            summary="**NOT EVALUATED**: invalid policy, {count}.",
        )
    data = {"thresholds": {"threshold": cfg.mutation_threshold, "target": cfg.mutation_target}}
    name = cfg.get("mutation.report")
    try:
        if not exists(cfg.root, name):
            raise ReportError(f"no PIT report at {name}")
        classified = frozenset(checked.sets.sources)
        resolver = SourceResolver(cfg.root, cfg.get("source.roots"), classified, checked.sets.walked_sources)
        report = pit.load(cfg.root / name, resolver, name=name)
    except ReportError as exc:
        _errored(rep, [str(exc)], data)
        return
    _evaluate(args, cfg, rep, checked, report, data)


def _evaluate(
    args: argparse.Namespace, cfg: Config, rep: Report, checked: CheckedPolicy, report: MutationReport, data: dict
) -> None:
    """The cross-check with the allowlist, the zones, the verdict, the Markdown and the data."""
    threshold_value, target_value = cfg.mutation_threshold, cfg.mutation_target
    threshold, target = _percent(threshold_value), _percent(target_value)
    listed = list(checked.target_files)
    listed_set = set(listed)
    total = Mutants()
    files = {path: Mutants() for path in listed}
    unresolved = {report_name: Mutants() for report_name in report.unresolved}
    classes: dict[str, Mutants] = {}
    statuses: dict[str, int] = {}
    mutators: dict[str, int] = {}
    for mutant in report.mutants:
        total.add(mutant)
        if mutant.file is None:
            unresolved[str(mutant.meta["report_name"])].add(mutant)
        else:
            files.setdefault(mutant.file, Mutants()).add(mutant)
        classes.setdefault(str(mutant.meta["class"]), Mutants()).add(mutant)
        statuses[mutant.status_raw] = statuses.get(mutant.status_raw, 0) + 1
        mutators[mutant.mutator] = mutators.get(mutant.mutator, 0) + 1
    outside = sorted(path for path in files if path not in listed_set)
    no_mutations = [path for path in listed if files[path].total == 0]
    not_covered = [path for path in listed if files[path].total > 0 and files[path].covered == 0]

    reasons = _zone_reasons(cfg, checked, report)
    zones = None if reasons else _zones(checked, report, [*listed, *outside])
    if reasons:
        rep.annotate("warning", "Mutation zones", "zones not evaluated: " + "; ".join(reasons))
    not_listed = sorted(outside + report.unresolved)
    if not_listed:
        rep.errored(
            "Mutation allowlist",
            f"{len(not_listed)} mutated file(s) not on the allowlist: " + ", ".join(not_listed),
        )
    if no_mutations:
        rep.annotate(
            "warning",
            "Mutation allowlist",
            f"{len(no_mutations)} allowlisted file(s) without any mutation: " + ", ".join(no_mutations),
        )
    if not_covered:
        rep.annotate(
            "warning",
            "Mutation allowlist",
            f"{len(not_covered)} allowlisted file(s) not covered by tests (no covered mutant): "
            + ", ".join(not_covered),
        )

    strength = total.strength()
    measured = f"test strength {pit_pct(strength)}"
    fraction = f"{total.detected}/{total.covered}"
    if rep.errors:
        outcome, gate = "error", "no verdict (input error)"
    elif strength is None:
        outcome, gate = "error", "no verdict (no covered mutant)"
        rep.errored("Mutation testing", f"no covered mutant among {total.total} mutation(s): test strength n/a")
    elif _below(total, threshold_value):
        outcome, gate = "below", f"below the {threshold} threshold"
        rep.violated(
            "Mutation testing",
            f"{measured} ({total.detected}/{total.covered} covered mutations detected) below {threshold}",
        )
        rep.verdict = f"mutation: BELOW THRESHOLD {measured} ({fraction}, < {threshold})"
    else:
        outcome, gate = "pass", f"pass (>= {threshold})"
        rep.verdict = f"mutation: PASS {measured} ({fraction}, >= {threshold})"
    if rep.errors:
        rep.evaluated = False
        rep.verdict = f"mutation: ERROR ({rep.errors[0]})"

    versions = _versions(cfg)
    head = getattr(args, "head_commit", None) or UNKNOWN
    rep.md = ["## Mutation testing", ""]
    parts = [f"Commit `{head}`"] + [f"{key} {_code(value)}" for key, value in versions.items()]
    rep.md.append(" - ".join(parts))
    rep.md.append("")
    observed = ", ".join(f"{_code(name)} {n}" for name, n in _by_count(mutators))
    rep.md.append(f"Mutators observed ({len(mutators)}): {observed}")
    rep.md.append("")
    if rep.errors:
        rep.md.append("**ERROR**: no verdict:")
        rep.md.append("")
        rep.md.extend(f"- {error}" for error in rep.errors)
        rep.md.append("")
    reached = "reached" if strength is not None and not _below(total, target_value) else "not reached"
    rep.md.append(
        f"**Test strength {pit_pct(strength)}** ({total.detected}/{total.covered} covered mutations detected)"
        f" - {gate}; target {target}: {reached}. Mutation score {pit_pct(total.score())}"
        f" ({total.detected}/{total.total} mutations detected). Detected is PIT's `detected` attribute;"
        " test strength leaves `NO_COVERAGE` out of the denominator."
    )
    rep.md.append("")
    rep.md.append("| mutations | killed | survived | no coverage | other detected | test strength | mutation score |")
    rep.md.append("|---:|---:|---:|---:|---:|---:|---:|")
    rep.md.append(
        f"| {total.total} | {total.killed} | {total.survived} | {total.no_coverage} | {total.detected - total.killed}"
        f" | {pit_pct(strength)} | {pit_pct(total.score())} |"
    )
    rep.md.append("")
    rep.md.append("Statuses: " + ", ".join(f"{_code(status)} {n}" for status, n in _by_count(statuses)))
    rep.md.append("")

    rep.md.append("### By zone")
    rep.md.append("")
    if zones is None:
        rep.md.append("Zones not evaluated (stale or missing snapshot):")
        rep.md.append("")
        rep.md.extend(f"- {reason}" for reason in reasons)
    else:
        rep.md.extend(mutants_header("zone", "files", "---:"))
        for zone in zones:
            rep.md.append(mutants_row(zone, zones[zone]["acc"], str(zones[zone]["files"])))
    rep.md.append("")

    rep.md.append(f"### By file (allowlist entries, ascending by test strength; mark `< {threshold}`)")
    rep.md.append("")
    rep.md.extend(mutants_header("file"))
    # A name under no source root keeps its own key space: it never joins a resolved path of the same text.
    rows = [(path, acc, False) for path, acc in files.items()]
    rows += [(report_name, acc, True) for report_name, acc in unresolved.items()]
    notes_of: dict[bool, dict[str, list[str]]] = {False: {}, True: {}}
    for key, acc, is_unresolved in sorted(rows, key=lambda row: (strength_order(row[:2]), row[2])):
        notes = []
        if is_unresolved or key not in listed_set:
            notes.append("not on the allowlist")
        if is_unresolved:
            notes.append("under no source root")
        if acc.total == 0:
            notes.append("0 mutations")
        elif acc.covered == 0:
            notes.append("not covered by tests")
        elif _below(acc, threshold_value):
            notes.append(f"< {threshold}")
        notes_of[is_unresolved][key] = notes
        rep.md.append(mutants_row(_cell(key), acc, "; ".join(notes)))
    rep.md.append("")

    rep.md.append(f"<details><summary>By class ({len(classes)}; information, not a verdict)</summary>")
    rep.md.append("")
    rep.md.extend(mutants_header("class"))
    for cls, acc in sorted(classes.items(), key=strength_order):
        rep.md.append(mutants_row(_cell(cls), acc, f"< {threshold}" if _below(acc, threshold_value) else ""))
    rep.md.append("")
    rep.md.append("</details>")
    rep.md.append("")
    _survivors(rep, report)

    rep.data = {
        "outcome": outcome,
        "totals": total.to_json(),
        "statuses": dict(statuses),
        "mutators": dict(mutators),
        "files": {path: {**files[path].to_json(), "notes": notes_of[False][path]} for path in [*listed, *outside]},
        "classes": {cls: acc.to_json() for cls, acc in classes.items()},
        "zones": None if zones is None else {z: {**v["acc"].to_json(), "files": v["files"]} for z, v in zones.items()},
        "zone_reasons": reasons,
        "no_mutations": no_mutations,
        "not_covered": not_covered,
        "outside": outside,
        "unresolved": {n: {**acc.to_json(), "notes": notes_of[True][n]} for n, acc in unresolved.items()},
        "versions": versions,
        **data,
    }


def _survivors(rep: Report, report: MutationReport) -> None:
    """The mutants that were not detected though a test reached them, by file: the resolved path,
    else the report name. At most SURVIVED_FILES_CAP files and SURVIVED_ITEMS_CAP items per file."""
    survivors: dict[tuple[str, bool], list[Mutant]] = {}
    for mutant in report.mutants:
        if not mutant.detected and mutant.status != "no-coverage":
            key = (mutant.file, False) if mutant.file is not None else (str(mutant.meta["report_name"]), True)
            survivors.setdefault(key, []).append(mutant)
    if not survivors:
        return
    rep.md.append(f"### Survived mutants ({sum(len(items) for items in survivors.values())})")
    rep.md.append("")
    for (key, _), items in sorted(survivors.items())[:SURVIVED_FILES_CAP]:
        rep.md.append(f"- {_code(key)} ({len(items)})")
        items.sort(key=lambda mutant: (mutant.line, str(mutant.meta["class"]), mutant.mutator, mutant.description))
        for mutant in items[:SURVIVED_ITEMS_CAP]:
            simple = str(mutant.meta["class"]).rpartition(".")[2]
            rep.md.append(f"  - {_code(simple)} L{mutant.line} {_code(mutant.mutator)}: {_code(mutant.description)}")
        if len(items) > SURVIVED_ITEMS_CAP:
            rep.md.append(f"  - ... (+{len(items) - SURVIVED_ITEMS_CAP} more)")
    if len(survivors) > SURVIVED_FILES_CAP:
        rep.md.append(f"- ... {len(survivors) - SURVIVED_FILES_CAP} more file(s)")
    rep.md.append("")
