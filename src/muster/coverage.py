"""The coverage command: the global floor, the table of the zones and the gate on the changed lines
of critical files, over a coverage report that is checked to come from the current test run.

The order is fixed: the keys the command needs, then the policy (an invalid one exits 2, as in the
policy command, also without a report), then the report and its freshness. Freshness compares the
snapshot that `muster policy --snapshot` wrote before the tests with the current policy and source
set, and the sessions of the report and the JUnit results with each other and with the time of the
snapshot; a check that fails leaves the section not evaluated. Zones and exemptions always come
from the current classification. A report, a snapshot or a JUnit file that cannot be read, or a
configured path that is there but is not a file, is an input error, and the section then has no
verdict.

With --base, the changed lines of critical files since the merge base must be covered at least as
far as the delta thresholds; a base without a merge base, or a git failure, leaves the delta not
evaluated, which fails under --strict.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

from muster import policy
from muster.adapters import SourceResolver, exists, jacoco, junit
from muster.adapters.jacoco import CoverageReport, FileCoverage
from muster.config import Config, require
from muster.gitio import count_file_lines, diff_since, parse_diff, ref_problem, untracked_under
from muster.paths import resolve_glob
from muster.policy import CheckedPolicy
from muster.report import FatalError, Report, ReportError, md_path, percent_digits
from muster.zones import zone_sort_key

HELP = "global floor, zones table and the critical delta gate"
EXPIRY_WARN_DAYS = 7  # an exemption that ends within this many days, today included, gets a warning
UNCOVERED_FILES_CAP = 50
UNCOVERED_ITEMS_CAP = 40
SHOWN_PATHS = 10  # paths listed in data for the excluded and the unresolved files
SKIPPED_NOTE = "; freshness not checked"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def ref_argument(text: str) -> str:
    """The argparse type of --base: a ref git could read as an option, or an empty one, is a usage error."""
    problem = ref_problem(text)
    if problem:
        raise argparse.ArgumentTypeError(f"the ref {text!r} {problem}")
    return text


def _file_name(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("the file name is empty")
    if "\0" in text:
        raise argparse.ArgumentTypeError("the file name has a NUL")
    return text


def _day(text: str) -> date:
    if not _DATE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"no such date: {text}") from None


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base",
        metavar="REF",
        type=ref_argument,
        help="ref for the delta gate (diff against git merge-base <base> HEAD)",
    )
    # Hidden, for tests: a unified diff instead of git (paths relative to the profile root, with a/
    # and b/ prefixes), no freshness checks, and the day that exemptions are judged on.
    parser.add_argument("--diff-file", type=_file_name, help=argparse.SUPPRESS)
    parser.add_argument("--no-freshness", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--today", type=_day, help=argparse.SUPPRESS)


def option_inputs(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """The input files the command line names: the --diff-file. It never raises; the command line
    checks them with the profile, before the profile is read."""
    return [(f"--diff-file {args.diff_file}", Path(args.diff_file))] if args.diff_file else []


def pct(missed_covered: list[int] | tuple[int, int]) -> str:
    missed, covered = missed_covered
    total = missed + covered
    return "n/a" if total == 0 else f"{100.0 * covered / total:.1f}%"


def ratio(missed_covered) -> float | None:
    missed, covered = missed_covered
    return None if missed + covered == 0 else covered / (missed + covered)


def frac(missed_covered) -> str:
    missed, covered = missed_covered
    return f"{covered}/{missed + covered}"


def compress(numbers: list[int]) -> list[str]:
    out, start, prev = [], None, None
    for n in sorted(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"L{start}" if start == prev else f"L{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"L{start}" if start == prev else f"L{start}-{prev}")
    return out


def new_acc() -> dict:
    return {"files": 0, "line": [0, 0], "branch": [0, 0]}


def add_to(acc: dict, sf: FileCoverage) -> None:
    acc["files"] += 1
    for key, counter in (("line", sf.line), ("branch", sf.branch)):
        acc[key][0] += counter[0]
        acc[key][1] += counter[1]


def _percent(fraction: float) -> str:
    """A threshold as a percentage with its sign, as percent_digits() gives the number: 0.825 is 82.5%."""
    return percent_digits(fraction) + "%"


def _require(cfg: Config, no_freshness: bool) -> None:
    require(cfg, *policy.REQUIRED, "coverage.report")
    if not no_freshness:
        require(cfg, "policy.snapshot")
        if cfg.get("coverage.full_run_suites"):
            require(cfg, "tests.results")


def _read_diff_file(name: str) -> dict[str, set[int]]:
    """The added lines of a unified diff file, by path relative to the profile root; it is read as
    bytes, so a lone CR stays inside its line."""
    try:
        raw = Path(name).read_bytes()
    except OSError as exc:
        raise FatalError(f"cannot read --diff-file {name}: {exc.strerror or exc}") from None
    try:
        return parse_diff(raw.decode("utf-8", "replace"), "")
    except ValueError:  # such as a line number of more digits than int() converts
        raise FatalError(f"cannot read --diff-file {name}: not a readable unified diff") from None


def _read_snapshot(cfg: Config, problems: list[str], errors: list[str]) -> dict | None:
    name = cfg.snapshot
    try:
        if not exists(cfg.root, name):
            problems.append(policy.NO_SNAPSHOT)
            return None
        return policy.read_snapshot(cfg.root / name, name=name)
    except FileNotFoundError:  # removed since it was seen
        problems.append(policy.NO_SNAPSHOT)
    except ReportError as exc:
        errors.append(str(exc))
    return None


def _inside(suite: junit.Suite, first: int, last: int) -> bool:
    """Whether the suite ran within [first, last] (epoch ms) give or take the tolerance, compared as
    exact integers."""
    return junit.within(suite.start, suite.end, first, last)


def _freshness(
    cfg: Config, checked: CheckedPolicy, report: CoverageReport | None, snapshot: dict | None, suites: list
) -> list[str]:
    """The reasons why the report is not from a full test run after the snapshot; empty when it is."""
    problems = policy.snapshot_problems(cfg, checked, snapshot)
    if report is None:
        return problems
    if report.unresolved:
        problems.append(
            f"{len(report.unresolved)} JaCoCo source file(s) under no source root (e.g. {report.unresolved[0]})"
        )
    if not report.sessions:
        problems.append("the JaCoCo XML has no sessioninfo")
        return problems
    first = min(start for start, _ in report.sessions)
    last = max(dump for _, dump in report.sessions)
    if snapshot is not None and snapshot["created_epoch_ms"] > first + junit.TOLERANCE_MS:
        problems.append("the snapshot is newer than the test run: run the tests again after writing the snapshot")
    for entry in cfg.get("coverage.full_run_suites", []):
        name = entry["name"]
        if not any(suite.name == name and _inside(suite, first, last) for suite in suites):
            problems.append(f"full-run suite {name} has no result in this run (filtered run, or the suite was renamed)")
    return problems


def _measured(report: CoverageReport) -> list[str]:
    """Why the report measures nothing; empty when it measures a file of the classification set."""
    if report.sourcefiles == 0:
        return ["the JaCoCo XML has no <sourcefile>"]
    if not report.files:
        return ["no JaCoCo source file is in the classification set"]
    return []


def _errored(rep: Report, errors: list[str], data: dict) -> None:
    """An input could not be read: every error is reported, and no verdict is given."""
    for error in errors:
        rep.errored("Coverage", error)
    rep.evaluated = False
    rep.data = {"outcome": "error", **data}
    rep.md = ["## Coverage", "", "**ERROR**: the inputs could not be read, no verdict:", ""]
    rep.md.extend(f"- {error}" for error in errors)
    rep.verdict = f"coverage: ERROR ({errors[0]})"


def _not_evaluated(rep: Report, problems: list[str], data: dict, note: str) -> None:
    rep.evaluated = False
    if rep.strict:  # a section that cannot judge fails under --strict; the reasons are its violations
        rep.violation = True
        rep.violations.extend(problems)
    rep.data = {"outcome": "not-evaluated", "reasons": problems, **data}
    rep.md = ["## Coverage", "", "**Coverage not evaluated** (filtered or stale run, or missing inputs):", ""]
    rep.md.extend(f"- {p}" for p in problems)
    rep.annotate("warning", "Coverage", "coverage not evaluated: " + "; ".join(problems))
    rep.verdict = "coverage: NOT EVALUATED (" + "; ".join(problems) + note + ")"


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Judge the coverage report; the command line has set args.base_commit when --base was given."""
    if args.base is not None and args.diff_file is not None:
        raise FatalError("--base and --diff-file cannot be used together")
    _require(cfg, args.no_freshness)
    today = args.today or policy.utc_today()
    checked = policy.check_policy(args, cfg, today)
    if checked.violations:
        raise policy.report_invalid(
            rep,
            checked.violations,
            heading="## Coverage",
            verdict="coverage: NOT EVALUATED (invalid policy: {count})",
            message="the policy is invalid: {count}",
            summary="**NOT EVALUATED**: invalid policy, {count}.",
        )
    changed = _read_diff_file(args.diff_file) if args.diff_file is not None else None
    data: dict = {
        "freshness": "skipped" if args.no_freshness else "checked",
        "thresholds": {"floor": cfg.coverage_floor, "delta": cfg.coverage_delta},
    }
    if args.today is not None:
        data["today"] = args.today.isoformat()
    note = SKIPPED_NOTE if args.no_freshness else ""
    problems: list[str] = []
    errors: list[str] = []
    report = snapshot = None
    suites: list[junit.Suite] = []
    name = cfg.get("coverage.report")
    try:
        if not exists(cfg.root, name):
            problems.append(f"no JaCoCo XML at {name}")
        else:
            classified = frozenset(checked.sets.sources)
            resolver = SourceResolver(cfg.root, cfg.get("source.roots"), classified, checked.sets.walked_sources)
            report = jacoco.load(cfg.root / name, resolver, name=name)
            data["excluded"] = {"count": len(report.excluded), "paths": report.excluded[:SHOWN_PATHS]}
            data["unresolved"] = {"count": len(report.unresolved), "names": report.unresolved[:SHOWN_PATHS]}
    except ReportError as exc:
        errors.append(str(exc))
    if not args.no_freshness:
        snapshot = _read_snapshot(cfg, problems, errors)
        if cfg.get("coverage.full_run_suites"):
            files = getattr(args, "junit_files", None)
            files = resolve_glob(cfg.root, cfg.get("tests.results")) if files is None else files
            suites = junit.read_suites(cfg.root, files, errors)
    if errors:
        _errored(rep, errors, data)
        return
    if not args.no_freshness:
        problems += _freshness(cfg, checked, report, snapshot, suites)
    if report is not None:
        problems += _measured(report)
    if problems:
        _not_evaluated(rep, problems, data, note)
        return
    _evaluate(args, cfg, rep, checked, report, changed, today, data, note)


def _evaluate(
    args: argparse.Namespace,
    cfg: Config,
    rep: Report,
    checked: CheckedPolicy,
    report: CoverageReport,
    changed: dict[str, set[int]] | None,
    today: date,
    data: dict,
    note: str,
) -> None:
    """The floor, the table of the zones, the exemptions and the delta, over the measured files."""
    floor = cfg.coverage_floor
    floor_line, floor_branch = _percent(floor["line"]), _percent(floor["branch"])
    zones = checked.classification.zones
    total, critical, standard = new_acc(), new_acc(), new_acc()
    per_zone: dict[str, dict] = {}
    for rel, sf in sorted(report.files.items()):
        add_to(total, sf)
        zone = zones[rel]
        if zone == "standard":
            add_to(standard, sf)
        else:
            add_to(critical, sf)
            add_to(per_zone.setdefault(zone, new_acc()), sf)
    per_zone = {zone: per_zone[zone] for zone in sorted(per_zone, key=zone_sort_key)}
    line_ok = ratio(total["line"]) is None or ratio(total["line"]) >= floor["line"]
    branch_ok = ratio(total["branch"]) is None or ratio(total["branch"]) >= floor["branch"]
    floor_ok = line_ok and branch_ok
    rep.md = ["## Coverage", ""]
    rep.md.append(
        f"**Global floor** ({floor_line} line / {floor_branch} branch): line {pct(total['line'])}"
        f" ({frac(total['line'])}), branch {pct(total['branch'])} ({frac(total['branch'])})"
        f" - {'pass' if floor_ok else 'below the floor'}"
    )
    rep.md.append("")
    rep.md.append("| scope | files | line % | lines covered | branch % | branches covered |")
    rep.md.append("|---|---:|---:|---:|---:|---:|")
    rows = list(per_zone.items())
    rows += [("critical (all zones)", critical), ("standard", standard), ("global", total)]
    for name, acc in rows:
        rep.md.append(
            f"| {name} | {acc['files']} | {pct(acc['line'])} | {frac(acc['line'])} | {pct(acc['branch'])}"
            f" | {frac(acc['branch'])} |"
        )
    rep.md.append("")
    if not floor_ok:
        parts = []
        if not line_ok:
            parts.append(f"line {pct(total['line'])} < {floor_line}")
        if not branch_ok:
            parts.append(f"branch {pct(total['branch'])} < {floor_branch}")
        rep.violated("Coverage floor", "global coverage below the floor: " + "; ".join(parts))
    rep.data = {
        "outcome": "pass",
        "global": total,
        "critical": critical,
        "standard": standard,
        "zones": per_zone,
        "floor_ok": floor_ok,
        **data,
    }
    _exemptions(rep, checked, today)
    delta_summary = "no delta (no --base)"
    if args.base is not None or changed is not None:
        delta_summary = _delta(args, cfg, rep, checked, report, changed)
    floor_text = "floor ok" if floor_ok else "BELOW FLOOR"
    incomplete = rep.data.get("delta", {}).get("evaluated") is False
    status = "WARN" if rep.violation else "INCOMPLETE" if incomplete else "PASS"
    if rep.violation and rep.strict:
        status = "FAIL"
    rep.data["outcome"] = "violation" if rep.violation else "incomplete" if incomplete else "pass"
    rep.verdict = (
        f"coverage: {status} (global line {pct(total['line'])} branch {pct(total['branch'])}, {floor_text};"
        f" {delta_summary}{note})"
    )


def _exemptions(rep: Report, checked: CheckedPolicy, today: date) -> None:
    """The valid exemptions (an expired one already made the policy invalid); those that end within
    EXPIRY_WARN_DAYS get a warning, never a violation."""
    active = sorted(checked.classification.exempt_until.items())
    expiring = [(r, u) for r, u in active if (u - today).days <= EXPIRY_WARN_DAYS]
    if active:
        rep.md.append("**Coverage exemptions** (valid through the date, UTC):")
        rep.md.append("")
        rep.md.extend(f"- {md_path(r)} until {u.isoformat()}" for r, u in active)
        rep.md.append("")
    if expiring:
        rep.annotate(
            "warning",
            "Coverage exemptions",
            f"exemption(s) expiring within {EXPIRY_WARN_DAYS} days: "
            + ", ".join(f"{r} (until {u.isoformat()})" for r, u in expiring),
        )
    rep.data["exemptions"] = {
        "active": [r for r, _ in active],
        "expired": [],
        "expiring": [r for r, _ in expiring],
    }


def _delta_not_evaluated(rep: Report, summary: str, reason: str) -> str:
    """The delta could not be computed: the section is not evaluated, a violation under --strict
    whose text is `reason`."""
    rep.md.append("")
    rep.data["delta"] = {"evaluated": False}
    rep.evaluated = False
    if rep.strict:
        rep.violation = True
        rep.violations.append(reason)
    return summary


def _line_count(root: Path, rel: str) -> int:
    try:
        return count_file_lines(root / rel)
    except OSError as exc:
        raise ReportError(f"{rel}: cannot read: {exc.strerror or exc}") from None


def _delta(
    args: argparse.Namespace,
    cfg: Config,
    rep: Report,
    checked: CheckedPolicy,
    report: CoverageReport,
    changed: dict[str, set[int]] | None,
) -> str:
    """The gate on the changed lines of critical files; returns the delta part of the verdict. Only
    files of the classification set count; an untracked one counts as wholly added."""
    zones = checked.classification.zones
    gate = cfg.coverage_delta
    delta_line, delta_branch = _percent(gate["line"]), _percent(gate["branch"])
    untracked: list[str] = []
    if changed is not None:
        source = "diff-file"
        label = f"diff file {md_path(Path(args.diff_file).name)}"
    else:
        source = "git"
        base_commit = getattr(args, "base_commit", None)
        if base_commit is None:
            reason = f"no merge-base with {args.base}; delta not evaluated"
            rep.annotate("warning", "Critical delta coverage", reason)
            rep.md.append(f"**Critical delta:** no merge-base with {md_path(args.base)} - not evaluated.")
            return _delta_not_evaluated(rep, "delta not evaluated (no merge-base)", reason)
        roots = cfg.get("source.roots")
        summary = "delta not evaluated (git error)"
        try:
            changed = parse_diff(diff_since(cfg.root, base_commit, roots), "")
            untracked = [rel for rel in untracked_under(cfg.root, roots) if rel in zones]
            summary = "delta not evaluated (cannot read a changed file)"
            for rel in untracked:  # a standard or an exempt file needs only its path
                if zones[rel] == "standard" or rel in checked.classification.exempt_until:
                    changed.setdefault(rel, set())
                else:
                    changed[rel] = set(range(1, _line_count(cfg.root, rel) + 1))
        except ReportError as exc:
            rep.annotate("warning", "Critical delta coverage", f"delta not evaluated: {exc}")
            rep.md.append(f"**Critical delta:** delta not evaluated: {exc}")
            return _delta_not_evaluated(rep, summary, f"delta not evaluated: {exc}")
        label = f"{md_path(args.base)} (merge-base `{base_commit[:10]}`)"
    files, uncovered, no_data, exempt = [], [], [], []
    standard_count = 0
    line_acc, branch_acc = [0, 0], [0, 0]
    for rel, numbers in sorted(changed.items()):
        zone = zones.get(rel)
        if zone is None:  # outside the classification set
            continue
        if zone == "standard":
            standard_count += 1
            continue
        until = checked.classification.exempt_until.get(rel)
        if until is not None:
            exempt.append((rel, zone, until))
            continue
        sf = report.files.get(rel)
        if sf is None:
            no_data.append((rel, zone))
            continue
        lacc, bacc = [0, 0], [0, 0]
        missed, partial = [], []
        for nr in sorted(numbers):
            if nr not in sf.lines:
                continue  # not a coverable line
            _, ci, mb, cb = sf.lines[nr]
            bacc[0] += mb
            bacc[1] += cb
            if ci == 0:
                lacc[0] += 1
                missed.append(nr)
                continue
            lacc[1] += 1
            if mb > 0:
                partial.append(f"L{nr} ({mb}/{mb + cb} branches missed)")
        for acc, part in ((line_acc, lacc), (branch_acc, bacc)):
            acc[0] += part[0]
            acc[1] += part[1]
        note = "untracked (new)" if rel in untracked else ""
        files.append((rel, zone, len(numbers), lacc, bacc, note))
        if missed or partial:
            uncovered.append((rel, compress(missed) + partial))
    rep.md.append(f"### Critical delta vs {label}")
    rep.md.append("")
    rep.md.append(
        f"Gate: {delta_line} line / {delta_branch} branch over changed lines of critical files"
        " (a change without branches is measured by line only)."
    )
    rep.md.append("")
    if files:
        rep.md.append("| critical file | zone | changed lines | line cov | branch cov | note |")
        rep.md.append("|---|---|---:|---:|---:|---|")
        for rel, zone, n_changed, lacc, bacc, note in files:
            rep.md.append(
                f"| {md_path(rel)} | {zone} | {n_changed} | {frac(lacc)} ({pct(lacc)}) | {frac(bacc)} ({pct(bacc)})"
                f" | {note} |"
            )
        rep.md.append("")
    fails: list[str] = []
    if not files and not no_data:
        result = "no critical changes"
        rep.md.append("No changed critical lines with coverage data - pass.")
    elif sum(line_acc) == 0:
        result = "n/a, no coverable changed lines"
        rep.md.append("Result: n/a (no coverable changed critical lines) - pass.")
    else:
        if ratio(line_acc) < gate["line"]:
            fails.append(f"line {pct(line_acc)} ({frac(line_acc)}) < {delta_line}")
        if sum(branch_acc) > 0 and ratio(branch_acc) < gate["branch"]:
            fails.append(f"branch {pct(branch_acc)} ({frac(branch_acc)}) < {delta_branch}")
        result = f"line {pct(line_acc)} ({frac(line_acc)}), branch {pct(branch_acc)} ({frac(branch_acc)})"
        rep.md.append(f"Result: {result} - {'would fail: ' + '; '.join(fails) if fails else 'pass'}.")
    rep.md.append("")
    if uncovered:
        rep.md.append("**Uncovered changed lines** (missed lines, then partly covered branches):")
        rep.md.append("")
        for rel, items in uncovered[:UNCOVERED_FILES_CAP]:
            shown = ", ".join(items[:UNCOVERED_ITEMS_CAP])
            more = f", ... (+{len(items) - UNCOVERED_ITEMS_CAP} more)" if len(items) > UNCOVERED_ITEMS_CAP else ""
            rep.md.append(f"- {md_path(rel)}: {shown}{more}")
        if len(uncovered) > UNCOVERED_FILES_CAP:
            rep.md.append(f"- ... {len(uncovered) - UNCOVERED_FILES_CAP} more file(s)")
        rep.md.append("")
    if no_data:
        rep.md.append("**No coverage data** for changed critical file(s) (no `<sourcefile>` in the JaCoCo XML):")
        rep.md.append("")
        rep.md.extend(f"- {md_path(r)} ({z})" for r, z in no_data)
        rep.md.append("")
        rep.annotate(
            "warning",
            "Critical delta coverage",
            f"no coverage data for {len(no_data)} changed critical file(s): " + ", ".join(r for r, _ in no_data),
        )
    if exempt:
        rep.md.append(
            "Exempt changed critical file(s): " + ", ".join(f"{md_path(r)} (until {u})" for r, _, u in exempt)
        )
        rep.md.append("")
    if standard_count:
        rep.md.append(f"{standard_count} changed standard file(s): outside the delta gate.")
        rep.md.append("")
    if fails:
        rep.violated(
            "Critical delta coverage",
            "changed critical lines below the gate: " + "; ".join(fails) + f" (vs {args.base or 'diff file'})",
        )
    rep.data["delta"] = {
        "evaluated": True,
        "source": source,
        "line": line_acc,
        "branch": branch_acc,
        "fails": fails,
        "files": [
            {"path": r, "zone": z, "changed": n, "line": la, "branch": ba, "note": note}
            for r, z, n, la, ba, note in files
        ],
        "uncovered": {r: items for r, items in uncovered},
        "no_data": [r for r, _ in no_data],
        "exempt": [r for r, _, _ in exempt],
        "standard_files": standard_count,
        "unclassified": [],
        "untracked": untracked,
    }
    return f"critical delta {'WOULD FAIL: ' + '; '.join(fails) if fails else result}"
