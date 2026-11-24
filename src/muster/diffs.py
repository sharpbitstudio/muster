"""The diff commands: the evidence of a change that removes tests (POLICY.md point 8), from the
reports of two runs, one before the change and one after it.

coverage-diff compares two JaCoCo reports file by file. A file is compared only when both reports
have it with the same lines and, on each line, the same totals of instructions and of branches;
otherwise the file is not comparable, and so is a report without any source file, and the section
gives no verdict (NOT COMPARABLE, a violation under --strict). A critical file, one in a critical
zone or one without a zone (outside the classification set, or under no source root), warns when
it loses a covered line or more than BRANCH_TOLERANCE covered branches, the loss of each line
summed, so that a branch gained on one line never offsets one lost on another; every other loss is
information. With both JUnit folders the test identities of the two runs are listed as evidence,
never as a verdict.

The repository (--repo, the root of the profile) is the tree after the change: the zones, the
source roots and the classification set come from it, so it must be the tree after the tests were
removed, while the reports may be anywhere. The files and folders of the options are relative to
the working directory, and messages name them as typed.

The order is fixed: the pair of JUnit options, the keys the command needs, the policy (an invalid
one exits 2), then every input is read, in the order of the options, and each one that cannot be
read is an input error; any input error leaves the section without a verdict. A source name found
under two source roots, or two names found as one file, exits 2 whatever else failed.
"""

from __future__ import annotations

import argparse
import os
import stat
from collections import Counter
from pathlib import Path

from muster import policy
from muster.adapters import SourceResolver, jacoco, junit
from muster.adapters.jacoco import FileCoverage
from muster.config import Config, require
from muster.coverage import compress, frac, pct
from muster.paths import file_argument
from muster.report import FatalError, Report, ReportError, cell_span, code_span, shown_names
from muster.zones import ZONE_NAME, zone_sort_key

COVERAGE_HELP = "test trim proof: JaCoCo before/after per source file"
BRANCH_TOLERANCE = 1  # covered branches a critical file may lose, summed over its lines, without a warning
TEST_IDS_CAP = 100  # identities in each list of the summary; the data keeps every one
FILES_CAP = 100  # files in each list of the summary
ITEMS_CAP = 40  # line runs in a cell of the changed files
SHOWN_NAMES = 10  # names in an annotation; the data keeps every one
NO_ROOT = "under no source root"

# A source file of a report: its repository-relative path, or its report name under no source root,
# and whether it is under no source root. The two kinds never share a key, even with the same text.
Key = tuple[str, bool]


def capped(items: list[str], cap: int, indent: str = "") -> list[str]:
    out = [f"{indent}- {item}" for item in items[:cap]]
    if len(items) > cap:
        out.append(f"{indent}- ... (+{len(items) - cap} more)")
    return out


def covered_lines(sf: FileCoverage) -> set[int]:
    return {nr for nr, (_, ci, _, _) in sf.lines.items() if ci > 0}


def line_totals(counters: tuple[int, int, int, int]) -> tuple[int, int]:
    """(instructions, branches) of one JaCoCo line (mi, ci, mb, cb), covered or not."""
    mi, ci, mb, cb = counters
    return mi + ci, mb + cb


def line_counters(files: dict[str, FileCoverage]) -> tuple[list[int], list[int]]:
    """([missed, covered] lines, [missed, covered] branches) from the per-line data of every file."""
    line, branch = [0, 0], [0, 0]
    for sf in files.values():
        for _, ci, mb, cb in sf.lines.values():
            line[ci > 0] += 1
            branch[0] += mb
            branch[1] += cb
    return line, branch


def coverage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--before",
        required=True,
        metavar="FILE",
        type=file_argument,
        help="JaCoCo XML of the run before the trim, relative to the working directory",
    )
    parser.add_argument(
        "--after",
        required=True,
        metavar="FILE",
        type=file_argument,
        help="JaCoCo XML of the run after the trim, relative to the working directory;"
        " --repo is the tree after the trim",
    )
    parser.add_argument(
        "--junit-before",
        metavar="DIR",
        type=file_argument,
        help="JUnit XML dir of the before run (*.xml, recursive), relative to the working directory",
    )
    parser.add_argument(
        "--junit-after",
        metavar="DIR",
        type=file_argument,
        help="JUnit XML dir of the after run; together with --junit-before",
    )


def _folder_key(folder: str) -> str:
    """A folder named on the command line, as two names of the same folder share it."""
    return os.path.normcase(os.path.abspath(folder))


def _walk(folder: str) -> tuple[list[str], list[str]]:
    """The JUnit files under a folder named on the command line, and why they cannot all be read:
    the folder is missing or is no folder, a folder below it cannot be read, or it holds no file.
    It never raises."""
    try:
        mode = os.stat(folder).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return [], [f"no JUnit XML folder at {folder}"]
    except OSError as exc:
        return [], [f"{folder}: cannot read: {exc.strerror or exc}"]
    if not stat.S_ISDIR(mode):
        return [], [f"no JUnit XML folder at {folder}"]
    errors: list[str] = []
    files = junit.xml_files(folder, errors)
    if not files and not errors:
        errors.append(f"no JUnit XML under {folder}")
    return files, errors


def _walked(args: argparse.Namespace, folder: str) -> tuple[list[str], list[str]]:
    """_walk() once per folder and run: option_inputs() keeps it in args for the command."""
    walks = getattr(args, "junit_folders", None)
    if walks is None:
        walks = args.junit_folders = {}
    key = _folder_key(folder)
    if key not in walks:
        walks[key] = _walk(folder)
    return walks[key]


def folder_inputs(folder: str) -> list[tuple[str, Path]]:
    """The JUnit files under a folder named on the command line, as inputs no output may be. It
    never raises."""
    return [(f"the JUnit file {file}", Path(file)) for file in _walk(folder)[0]]


def option_inputs(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """The input files the command line names: --before, --after and every JUnit file under the
    folders of the JUnit options. It never raises; the command line checks them with the profile,
    before the profile is read."""
    inputs = [(f"--before {args.before}", Path(args.before)), (f"--after {args.after}", Path(args.after))]
    for folder in (getattr(args, "junit_before", None), getattr(args, "junit_after", None)):
        if folder is not None:
            inputs += [(f"the JUnit file {file}", Path(file)) for file in _walked(args, folder)[0]]
    return inputs


def _file_problem(name: str, missing: str) -> str | None:
    """Why the report named on the command line cannot be read, or None: nothing is there (the text
    `missing` and the name), it cannot be looked at, or it is no regular file. What is in it is for
    the adapter to judge."""
    try:
        mode = os.stat(name).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return f"{missing} {name}"
    except OSError as exc:
        return f"{name}: cannot read: {exc.strerror or exc}"
    return None if stat.S_ISREG(mode) else f"{name}: not a file"


def _identities(args: argparse.Namespace, folder: str, errors: list[str]) -> Counter[str]:
    """The test identities under a JUnit folder; every problem of the walk and of the files goes to
    `errors`, in that order."""
    files, problems = _walked(args, folder)
    errors.extend(problems)
    return junit.identities(files, errors)


def _errored(rep: Report, title: str, heading: str, errors: list[str]) -> None:
    """An input could not be read: every error is reported, with only the heading, and no verdict."""
    for error in errors:
        rep.errored(title, error)
    rep.evaluated = False
    rep.data = {"outcome": "error"}
    rep.md = [heading, "", "**ERROR**: the inputs could not be read, no verdict:", ""]
    rep.md.extend(f"- {code_span(error)}" for error in errors)
    rep.verdict = f"{rep.name}: ERROR ({errors[0]})"


def _shown_key(path: str, unresolved: bool) -> str:
    """A source file of the reports as a list item names it."""
    return code_span(path) + (f" ({NO_ROOT})" if unresolved else "")


def _read_jacoco(
    name: str, resolver: SourceResolver, classified: set[str], errors: list[str]
) -> dict[Key, FileCoverage] | None:
    """The source files of a JaCoCo report named on the command line, or None with its error in
    `errors`; the resolved paths in the classification set are added to `classified`."""
    problem = _file_problem(name, "no JaCoCo XML at")
    if problem is not None:
        errors.append(problem)
        return None
    try:
        entries = jacoco.source_files(Path(name), resolver, name=name)
    except ReportError as exc:
        errors.append(str(exc))
        return None
    classified.update(path for _, path, in_set, _ in entries if in_set)
    return {(path or report_name, path is None): coverage for report_name, path, _, coverage in entries}


def coverage_diff(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Compare the JaCoCo reports of the runs before and after the change."""
    if (args.junit_before is None) != (args.junit_after is None):
        raise FatalError("--junit-before and --junit-after go together (only one given)")
    require(cfg, *policy.REQUIRED)
    checked = policy.check_policy(args, cfg, policy.utc_today())
    if checked.violations:
        raise policy.report_invalid(
            rep,
            checked.violations,
            heading="## Coverage diff",
            verdict="coverage-diff: NOT EVALUATED (invalid policy: {count})",
            message="the policy is invalid: {count}",
            summary="**NOT EVALUATED**: invalid policy, {count}.",
        )
    sets = checked.sets
    resolver = SourceResolver(cfg.root, cfg.get("source.roots"), frozenset(sets.sources), sets.walked_sources)
    errors: list[str] = []
    classified: set[str] = set()
    before = _read_jacoco(args.before, resolver, classified, errors)
    after = _read_jacoco(args.after, resolver, classified, errors)
    identities = None
    if args.junit_before is not None:
        read: dict[str, Counter[str]] = {}  # the same folder named twice is read once
        for folder in (args.junit_before, args.junit_after):
            if _folder_key(folder) not in read:
                read[_folder_key(folder)] = _identities(args, folder, errors)
        identities = read[_folder_key(args.junit_before)], read[_folder_key(args.junit_after)]
    if errors:
        _errored(rep, "Coverage diff", "## Coverage diff", errors)
        return
    zones = {path: zone for path, zone in checked.classification.zones.items() if path in classified}
    _coverage_verdict(args, rep, zones, before, after, identities)


def _incomparable(before: FileCoverage | None, after: FileCoverage | None) -> str | None:
    """Why a source file of the reports cannot be compared, or None."""
    if before is None:
        return "only in the after report"
    if after is None:
        return "only in the before report"
    gone, new = before.lines.keys() - after.lines.keys(), after.lines.keys() - before.lines.keys()
    if gone or new:
        return f"line set differs (-{len(gone)}/+{len(new)} line(s) with bytecode)"
    changed = [nr for nr, counters in before.lines.items() if line_totals(counters) != line_totals(after.lines[nr])]
    if changed:
        return f"line bytecode differs ({len(changed)} line(s) with other instruction or branch totals)"
    return None


def _changed_files(
    zones: dict[str, str], before: dict[Key, FileCoverage], after: dict[Key, FileCoverage]
) -> tuple[list[dict], list[tuple[Key, str]]]:
    """The rows of the comparable files whose covered lines or branches changed, critical first,
    and the files that are not comparable with the reason, both in the order of the keys."""
    rows: list[dict] = []
    not_comparable: list[tuple[Key, str]] = []
    for key in sorted(before.keys() | after.keys()):
        b, a = before.get(key), after.get(key)
        why = _incomparable(b, a)
        if why is not None:
            not_comparable.append((key, why))
            continue
        lines_b, lines_a = covered_lines(b), covered_lines(a)
        branches_b = sum(cb for _, _, _, cb in b.lines.values())
        branches_a = sum(cb for _, _, _, cb in a.lines.values())
        branch_lost = sorted(nr for nr, counters in b.lines.items() if counters[3] > a.lines[nr][3])
        branch_loss = sum(b.lines[nr][3] - a.lines[nr][3] for nr in branch_lost)
        if lines_b == lines_a and branches_b == branches_a and branch_loss == 0:
            continue
        path, unresolved = key
        zone = "unknown" if unresolved else zones.get(path, "unknown")
        critical = zone == "unknown" or bool(ZONE_NAME.fullmatch(zone))
        lost = sorted(lines_b - lines_a)
        rows.append(
            {
                "path": path,
                "unresolved": unresolved,
                "zone": zone,
                "critical": critical,
                "lines": [len(lines_b), len(lines_a)],
                "branches": [branches_b, branches_a],
                "lost_lines": compress(lost),
                "branch_loss": branch_loss,
                "lost_branch_lines": compress(branch_lost),
                "loss": bool(lost) or branch_loss > 0,
                "warn": critical and (bool(lost) or branch_loss > BRANCH_TOLERANCE),
            }
        )
    rows.sort(key=lambda r: (not r["critical"], zone_sort_key(r["zone"]), r["path"], r["unresolved"]))
    return rows, not_comparable


def _runs(items: list[str]) -> str:
    """The line runs of a cell: at most ITEMS_CAP, then how many more there are."""
    more = len(items) - ITEMS_CAP
    return ", ".join(items[:ITEMS_CAP]) + (f", ... (+{more} more)" if more > 0 else "")


def _coverage_verdict(
    args: argparse.Namespace,
    rep: Report,
    zones: dict[str, str],
    before: dict[Key, FileCoverage],
    after: dict[Key, FileCoverage],
    identities: tuple[Counter[str], Counter[str]] | None,
) -> None:
    """The comparison of the two reports: the verdict, the Markdown and the data."""
    rows, not_comparable = _changed_files(zones, before, after)
    warned = [r for r in rows if r["warn"]]
    info = [r for r in rows if r["loss"] and not r["warn"]]
    empty = [label for label, files in (("before", before), ("after", after)) if not files]
    totals = {"before": line_counters(before), "after": line_counters(after)}
    lost_text = f"{len(warned)} critical file(s) lost coverage"
    reasons = []
    if empty:
        reasons.append("empty report: " + " and ".join(empty) + " without any sourcefile")
    if not_comparable:
        reasons.append(f"{len(not_comparable)} file(s) not comparable (main source changed)")
    if reasons:
        outcome = "not-comparable"
        detail = "; ".join(reasons + ([lost_text] if warned else []))
        rep.evaluated = False
        rep.violated("Coverage diff", f"before and after are not comparable: {detail}")
        status = f"NOT COMPARABLE ({detail})"
    elif warned:
        outcome = "warn"
        names = ", ".join(r["path"].rsplit("/", 1)[-1] for r in warned[:3])
        more = f", ... (+{len(warned) - 3} more)" if len(warned) > 3 else ""
        rep.violated(
            "Coverage diff",
            f"{lost_text}: " + shown_names([r["path"] for r in warned], SHOWN_NAMES) + " (lost lines or more than"
            f" {BRANCH_TOLERANCE} branch)",
        )
        status = f"WARN ({lost_text}: {names}{more})"
    else:
        outcome = "pass"
        losses = (
            f"; {len(info)} file(s) lost coverage within the tolerance or outside the critical zones" if info else ""
        )
        status = f"PASS (no critical coverage lost{losses})"
    line_b, branch_b = totals["before"]
    line_a, branch_a = totals["after"]
    trend = f"line {pct(line_b)} -> {pct(line_a)}, branch {pct(branch_b)} -> {pct(branch_a)}"
    rep.verdict = f"coverage-diff: {status}; {trend}"

    rep.md = ["## Coverage diff", ""]
    rep.md.append(
        f"Before {code_span(args.before)} - after {code_span(args.after)}; the zones and the source paths are those"
        " of the repository, the tree after the trim."
    )
    rep.md.append("")
    rep.md.append(
        f"Gate: a critical-zone file (or one without a zone) loses no covered line and at most"
        f" {BRANCH_TOLERANCE} covered branch, summed per line (a branch gained on one line does not offset"
        " one lost on another); other losses are information."
    )
    rep.md.append("")
    rep.md.append("| scope | before | after |")
    rep.md.append("|---|---:|---:|")
    rep.md.append(f"| lines covered | {frac(line_b)} ({pct(line_b)}) | {frac(line_a)} ({pct(line_a)}) |")
    rep.md.append(f"| branches covered | {frac(branch_b)} ({pct(branch_b)}) | {frac(branch_a)} ({pct(branch_a)}) |")
    rep.md.append(f"| source files | {len(before)} | {len(after)} |")
    rep.md.append("")
    if empty:
        rep.md.append("**Empty report:** " + " and ".join(empty) + " without any `<sourcefile>`.")
        rep.md.append("")
    if rows:
        rep.md.append(f"### Changed files ({len(rows)}; critical first)")
        rep.md.append("")
        rep.md.append("| file | zone | lines covered | branches covered | lost lines | lost branches | note |")
        rep.md.append("|---|---|---:|---:|---|---|---|")
        for r in rows[:FILES_CAP]:
            (lb, la), (bb, ba) = r["lines"], r["branches"]
            lost_branches = f"-{r['branch_loss']} ({_runs(r['lost_branch_lines'])})" if r["branch_loss"] else ""
            notes = ["**lost critical coverage**" if r["warn"] else "loss, information"] if r["loss"] else []
            notes += [NO_ROOT] if r["unresolved"] else []
            rep.md.append(
                f"| {cell_span(r['path'])} | {r['zone']} | {lb} -> {la} ({la - lb:+d}) | {bb} -> {ba} ({ba - bb:+d})"
                f" | {_runs(r['lost_lines'])} | {lost_branches} | {'; '.join(notes)} |"
            )
        if len(rows) > FILES_CAP:
            rep.md.append("")
            rep.md.append(f"... {len(rows) - FILES_CAP} more file(s)")
        rep.md.append("")
    else:
        rep.md.append("No file changed its covered lines or branches.")
        rep.md.append("")
    if info:
        rep.md.append(
            f"Information: {len(info)} file(s) lost coverage within the tolerance or outside the critical zones: "
            + ", ".join(_shown_key(r["path"], r["unresolved"]) for r in info[:FILES_CAP])
            + (f", ... (+{len(info) - FILES_CAP} more)" if len(info) > FILES_CAP else "")
        )
        rep.md.append("")
    if not_comparable:
        rep.md.append(f"### Not comparable ({len(not_comparable)}; main source changed between the runs)")
        rep.md.append("")
        rep.md.extend(capped([f"{_shown_key(*key)}: {why}" for key, why in not_comparable], FILES_CAP))
        rep.md.append("")
    junit_data = None
    if identities is not None:
        ids_b, ids_a = identities
        removed, added = sorted((ids_b - ids_a).elements()), sorted((ids_a - ids_b).elements())
        n_b, n_a = ids_b.total(), ids_a.total()
        junit_data = {"before": n_b, "after": n_a, "removed": removed, "added": added}
        rep.md.append("### Test identities (JUnit XML; evidence, not a verdict)")
        rep.md.append("")
        rep.md.append(
            f"`classname#name` testcases over `*.xml`, skipped included: {n_b} -> {n_a};"
            f" {len(removed)} removed, {len(added)} added."
        )
        rep.md.append("")
        for title, items in (("Removed", removed), ("Added", added)):
            if items:
                rep.md.append(f"{title} ({len(items)}):")
                rep.md.append("")
                rep.md.extend(capped([code_span(item) for item in items], TEST_IDS_CAP))
                rep.md.append("")
    rep.data = {
        "outcome": outcome,
        "totals": {k: {"line": v[0], "branch": v[1]} for k, v in totals.items()},
        "files": rows,
        "warned": [r["path"] for r in warned],
        "info": [r["path"] for r in info],
        "not_comparable": [
            {"path": path, "unresolved": unresolved, "reason": why} for (path, unresolved), why in not_comparable
        ],
        "empty": empty,
        "junit": junit_data,
    }
