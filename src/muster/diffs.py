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
never as a verdict; two names of one folder on disk (os.path.samefile) are walked and read once.

mutation-diff compares two PIT reports mutant by mutant, by the identity of the mutant as a whole
(class, method, descriptor, line, mutator and indexes); the same mutant twice in a report is an
input error. A mutant of one report only, and a report without any mutant, leave the section
without a verdict (NOT COMPARABLE). A mutant detected before and not after is lost and warns,
unless every lost mutant is a configured known variation whose killing test is present in the after
run: an entry of [[mutation.known_variation]] names a mutant by the simple names of its class and
mutator, its method and its line, and it is a tolerance only for the one mutant of the two runs it
names. With --junit-after the killing test of a lost mutant is looked up among the JUnit test
cases of the after run that were not skipped; a skipped test case of its class leaves it unresolved
unless a plain name shows the method. When a display name or a parameterized invocation hides the
method name, the Java source of its class under [source].tests decides, read only when exactly one
test folder holds it in exactly that case. An ambiguous entry of [[mutation.known_variation]] is
shown also when nothing is lost. The command checks no policy and judges no zone.

The repository (--repo, the root of the profile) is the tree after the change: the zones, the
source roots, the classification set and the test sources come from it, so it must be the tree
after the tests were removed, while the reports may be anywhere. The files and folders of the
options are relative to the working directory, and messages name them as typed.

The order is fixed: the pair of JUnit options, the keys the command needs, the policy of
coverage-diff (an invalid one exits 2), then every input is read, in the order of the options, and
each one that cannot be read is an input error; any input error leaves the section without a
verdict. A source name found under two source roots, or two names found as one file, and a folder
of [source] that is missing or cannot be read, exit 2 whatever else failed.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from muster import policy
from muster.adapters import SourceResolver, jacoco, junit, pit
from muster.adapters.jacoco import FileCoverage
from muster.adapters.pit import Mutant
from muster.config import Config, ConfigError, require
from muster.coverage import compress, frac, pct
from muster.lang.java import PLAIN_TEST_NAME, parse_killing_test, source_declares
from muster.mutation import Mutants, mutants_header, mutants_row, pit_pct
from muster.paths import SourceSets, file_argument, normalize, source_sets
from muster.report import FatalError, Report, ReportError, cell_span, code_span, has_control, shown_names
from muster.zones import ZONE_NAME, zone_sort_key

COVERAGE_HELP = "test trim proof: JaCoCo before/after per source file"
MUTATION_HELP = "test trim proof: PIT before/after per mutant"
BRANCH_TOLERANCE = 1  # covered branches a critical file may lose, summed over its lines, without a warning
TEST_IDS_CAP = 100  # identities in each list of the summary; the data keeps every one
FILES_CAP = 100  # files in each list of the summary
ITEMS_CAP = 40  # line runs in a cell of the changed files, and mutants in each list of mutation-diff
SHOWN_NAMES = 10  # names in an annotation; the data keeps every one
NO_ROOT = "under no source root"
# The label of a JUnit file under a folder of the options; the command line compares the outputs
# with these files as it does with the protected files, not one by one.
JUNIT_FILE = "the JUnit file"

# A source file of a report: its repository-relative path, or its report name under no source root,
# and whether it is under no source root. The two kinds never share a key, even with the same text.
Key = tuple[str, bool]
# The identity of a mutant (pit.Mutant.id), compared as a whole.
MutantId = tuple[str, str, str, int, str, tuple[int, ...]]
# The parts of a test class name between its dots and dollar signs.
_CLASS_PARTS = re.compile(r"[.$]")


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


def mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--before",
        required=True,
        metavar="FILE",
        type=file_argument,
        help="PIT mutations.xml of the run before the trim, relative to the working directory",
    )
    parser.add_argument(
        "--after",
        required=True,
        metavar="FILE",
        type=file_argument,
        help="PIT mutations.xml of the run after the trim, relative to the working directory;"
        " --repo is the tree after the trim",
    )
    parser.add_argument(
        "--junit-after",
        metavar="DIR",
        type=file_argument,
        help="JUnit XML dir of the after run (*.xml, recursive), relative to the working directory: does the"
        " killing test of a lost mutant still exist",
    )


def _same_folder(first: str, second: str) -> bool:
    """Whether two folders named on the command line are one folder on disk; one that cannot be
    looked at is no other folder. It never raises."""
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


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
    """_walk() once per folder and run: option_inputs() keeps it in args for the command, by the
    folder as typed. A folder that is one on disk with a folder walked before (os.path.samefile)
    shares that walk, the very same result; any other name is walked on its own, so a name that
    cannot be looked at never hides another."""
    walks = getattr(args, "junit_folders", None)
    if walks is None:
        walks = args.junit_folders = {}
    if folder not in walks:
        same = next((walk for other, walk in walks.items() if _same_folder(other, folder)), None)
        walks[folder] = same if same is not None else _walk(folder)
    return walks[folder]


def folder_inputs(folder: str) -> list[tuple[str, Path]]:
    """The JUnit files under a folder named on the command line, as inputs no output may be. It
    never raises."""
    return [(f"{JUNIT_FILE} {file}", Path(file)) for file in _walk(folder)[0]]


def option_inputs(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """The input files the command line names: --before, --after and every JUnit file under the
    folders of the JUnit options, labelled `JUNIT_FILE <file>`. It never raises; the command line
    checks them with the profile, before the profile is read."""
    inputs = [(f"--before {args.before}", Path(args.before)), (f"--after {args.after}", Path(args.after))]
    for folder in (getattr(args, "junit_before", None), getattr(args, "junit_after", None)):
        if folder is not None:
            inputs += [(f"{JUNIT_FILE} {file}", Path(file)) for file in _walked(args, folder)[0]]
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


def _junit(args: argparse.Namespace, folder: str, errors: list[str], read: Callable[..., Counter]) -> Counter:
    """What `read` (junit.identities or junit.testcases) finds in the files under a JUnit folder;
    every problem of the walk and of the files goes to `errors`, in that order."""
    files, problems = _walked(args, folder)
    errors.extend(problems)
    return read(files, errors)


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
        ids_before = _junit(args, args.junit_before, errors, junit.identities)
        same = _walked(args, args.junit_before) is _walked(args, args.junit_after)  # one folder is read once
        ids_after = ids_before if same else _junit(args, args.junit_after, errors, junit.identities)
        identities = ids_before, ids_after
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


def mutant_text(mutant: Mutant) -> str:
    """A mutant as messages and the summary name it, by the full name of its class."""
    meta = mutant.meta
    indexes = ",".join(map(str, meta["indexes"]))
    return f"{meta['class']}.{meta['method']}{meta['descriptor']} L{mutant.line} {mutant.mutator} [{indexes}]"


def _mutant_order(mutant: Mutant) -> tuple:
    """The order of the mutant lists: class, method, descriptor, line, the simple name of the
    mutator and the indexes, then the full name of the mutator."""
    meta = mutant.meta
    return (
        str(meta["class"]),
        str(meta["method"]),
        str(meta["descriptor"]),
        mutant.line,
        mutant.mutator,
        meta["indexes"],
        str(meta["mutator_full"]),
    )


def killing_test_text(entry: str) -> str:
    """A killing test as the summary names it: `<class>#<member>()` of a method, `<class>#<member>
    (<kind>)` of another kind, the entry as given when it is no JUnit Platform unique id."""
    parsed = parse_killing_test(entry)
    if parsed is None:
        return entry
    cls, member, kind = parsed
    return f"{cls}#{member}" + ("()" if kind == "method" else f" ({kind})")


def _read_pit(name: str, resolver: SourceResolver, errors: list[str]) -> dict[MutantId, Mutant] | None:
    """The mutants of a PIT report named on the command line by their identity, or None with its
    errors in `errors`: one for each mutant the report holds more than once."""
    problem = _file_problem(name, "no PIT report at")
    if problem is not None:
        errors.append(problem)
        return None
    try:
        report = pit.load(Path(name), resolver, name=name, allow_empty=True)
    except ReportError as exc:
        errors.append(str(exc))
        return None
    mutants: dict[MutantId, Mutant] = {}
    repeated: dict[MutantId, Mutant] = {}
    for mutant in report.mutants:
        if mutant.id in mutants:
            repeated.setdefault(mutant.id, mutant)
        mutants.setdefault(mutant.id, mutant)
    errors.extend(f"{name}: the mutant {mutant_text(mutant)} occurs twice" for mutant in repeated.values())
    return None if repeated else mutants


def _no_path(cls: str) -> bool:
    """Whether a test class name of a unique id names no source file: it has a /, a backslash, a
    colon or a control character, or an empty part between its dots and dollar signs."""
    return any(ch in "/\\:" for ch in cls) or has_control(cls) or "" in _CLASS_PARTS.split(cls)


class _Killers:
    """The status of a killing test in the after run, by the JUnit test cases of its class that were
    not skipped, else by the Java source of the class in the test folders: `present`, `deleted` or
    `unresolved`."""

    def __init__(self, cfg: Config, sets: SourceSets, cases: Counter[tuple[str, str, bool]]) -> None:
        self.root = cfg.root
        self.by_class: dict[str, list[str]] = {}  # the names of the test cases that were not skipped
        self.skipped: set[str] = set()  # the classes with a skipped test case
        for cls, name, skipped in cases:
            if skipped:
                self.skipped.add(cls)
            else:
                self.by_class.setdefault(cls, []).append(name)
        self.folders = list(dict.fromkeys(normalize(folder) for folder in cfg.get("source.tests", [])))
        self.walked = frozenset(sets.walked_tests)
        self.texts: dict[str, str | None] = {}

    def status(self, entry: str) -> str:
        """An entry that is no unique id is `unresolved`. Else, by the test cases of the class that
        were not skipped: none, and none skipped either, is `deleted`; a test case `method(...)` of
        the method is `present`; a skipped test case of the class is `unresolved`; every name a
        plain method signature and none the method (a test template or factory too) is `deleted`;
        otherwise a display name or an invocation hides the method name, and the source decides."""
        parsed = parse_killing_test(entry)
        if parsed is None:
            return "unresolved"
        cls, member, kind = parsed
        names = self.by_class.get(cls, [])
        if not names and cls not in self.skipped:
            return "deleted"
        plain = [PLAIN_TEST_NAME.fullmatch(name) for name in names]
        if kind == "method" and any(match and match.group(1) == member for match in plain):
            return "present"
        if cls in self.skipped:
            return "unresolved"
        if all(plain):
            return "deleted"
        return self._in_source(cls, member)

    def _in_source(self, cls: str, member: str) -> str:
        """The source of the top-level class, `<folder>/<package as folders>/<class>.java` in one
        of the test folders, walked and in exactly that case, where the package is everything
        before the last dot of the class name: one is read; none (the class ran, so the lookup
        failed), two, a source that cannot be read, a class name that names no file and no test
        folder at all are `unresolved`."""
        if _no_path(cls) or not self.folders:
            return "unresolved"
        package, _, chain = cls.rpartition(".")
        stem = posixpath.join(package.replace(".", "/"), chain.partition("$")[0])
        candidates = [posixpath.join(folder, stem) + ".java" for folder in self.folders]
        found = [path for path in candidates if path in self.walked]
        if len(found) != 1:
            return "unresolved"
        text = self._text(found[0])
        return "unresolved" if text is None else source_declares(text, cls, member)

    def _text(self, path: str) -> str | None:
        """The text of a test source, UTF-8 with replacement, read once; None when it cannot be read."""
        if path not in self.texts:
            try:
                self.texts[path] = (self.root / path).read_bytes().decode("utf-8", errors="replace")
            except OSError:
                self.texts[path] = None
        return self.texts[path]


def _killer_after(mutant: Mutant, killers: _Killers | None) -> str:
    """The killing tests of a lost mutant in the after run: `not checked` without --junit-after,
    `no killing test recorded`, `present` when one is, `deleted` when all are, else `unresolved`."""
    if killers is None:
        return "not checked"
    if not mutant.killing_tests:
        return "no killing test recorded"
    statuses = [killers.status(test) for test in mutant.killing_tests]
    if "present" in statuses:
        return "present"
    if all(status == "deleted" for status in statuses):
        return "deleted"
    return "unresolved"


def _variation_key(mutant: Mutant) -> tuple[str, str, int, str]:
    """A mutant as an entry of [[mutation.known_variation]] names it: the simple names of its class
    (a nested class keeps its $) and of its mutator, its method and its line."""
    return str(mutant.meta["class"]).rpartition(".")[2], str(mutant.meta["method"]), mutant.line, mutant.mutator


def _known_variation(
    entries: frozenset[tuple[str, str, int, str]], *reports: dict[MutantId, Mutant]
) -> tuple[set[tuple[str, str, int, str]], list[dict]]:
    """The entries that name exactly one mutant of the two reports together, and those that name
    more than one, which give no tolerance, with the number of mutants they name."""
    named: dict[tuple[str, str, int, str], set[MutantId]] = {}
    for mutants in reports:
        for mutant_id, mutant in mutants.items():
            key = _variation_key(mutant)
            if key in entries:
                named.setdefault(key, set()).add(mutant_id)
    single = {key for key, ids in named.items() if len(ids) == 1}
    ambiguous = [
        {"class": key[0], "method": key[1], "line": key[2], "mutator": key[3], "matches": len(named[key])}
        for key in sorted(named)
        if len(named[key]) > 1
    ]
    return single, ambiguous


def mutation_diff(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Compare the PIT reports of the runs before and after the change."""
    require(cfg, "source.roots")
    sets = getattr(args, "source_sets", None) or source_sets(cfg, defer=True)
    if sets.error is not None:
        raise ConfigError(f"{cfg.name}: {sets.error}")
    resolver = SourceResolver(cfg.root, cfg.get("source.roots"), frozenset(sets.sources), sets.walked_sources)
    errors: list[str] = []
    before = _read_pit(args.before, resolver, errors)
    after = _read_pit(args.after, resolver, errors)
    cases = None if args.junit_after is None else _junit(args, args.junit_after, errors, junit.testcases)
    if errors:
        _errored(rep, "Mutation diff", "## Mutation diff", errors)
        return
    killers = None if cases is None else _Killers(cfg, sets, cases)
    _mutation_verdict(args, cfg, rep, before, after, killers)


def _file_key(mutant: Mutant) -> Key:
    return (mutant.file, False) if mutant.file is not None else (str(mutant.meta["report_name"]), True)


def _counters(mutants: dict[MutantId, Mutant]) -> tuple[Mutants, dict[Key, Mutants]]:
    """The counters of every mutant of a report, and of those of each source file."""
    total, files = Mutants(), {}
    for mutant in mutants.values():
        total.add(mutant)
        files.setdefault(_file_key(mutant), Mutants()).add(mutant)
    return total, files


def _strength(acc: Mutants) -> str:
    return f"{pit_pct(acc.strength())} ({acc.detected}/{acc.covered})"


@dataclass(frozen=True)
class _Lost:
    before: Mutant
    after: Mutant
    killer: str  # the status of its killing tests in the after run
    known: bool  # a configured known variation with a present killing test


def _mutation_verdict(
    args: argparse.Namespace,
    cfg: Config,
    rep: Report,
    before: dict[MutantId, Mutant],
    after: dict[MutantId, Mutant],
    killers: _Killers | None,
) -> None:
    """The comparison of the two reports: the verdict, the Markdown and the data."""
    total_b, files_b = _counters(before)
    total_a, files_a = _counters(after)

    def differs(key: Key) -> bool:
        """Whether a source file has other counters in the two reports; one it is not in has none."""
        b, a = files_b.get(key, Mutants()), files_a.get(key, Mutants())
        return (b.total, b.covered, b.detected) != (a.total, a.covered, a.detected)

    changed = sorted(key for key in files_b.keys() | files_a.keys() if differs(key))
    common = sorted(before.keys() & after.keys(), key=lambda mutant_id: _mutant_order(before[mutant_id]))
    only_b = sorted(before.keys() - after.keys(), key=lambda mutant_id: _mutant_order(before[mutant_id]))
    only_a = sorted(after.keys() - before.keys(), key=lambda mutant_id: _mutant_order(after[mutant_id]))
    entries = cfg.known_variation
    single, ambiguous = _known_variation(entries, before, after)
    lost: list[_Lost] = []
    gained: list[tuple[Mutant, Mutant]] = []
    for mutant_id in common:
        b, a = before[mutant_id], after[mutant_id]
        if b.detected and not a.detected:
            killer = _killer_after(b, killers)
            lost.append(_Lost(b, a, killer, _variation_key(b) in single and killer == "present"))
        elif a.detected and not b.detected:
            gained.append((b, a))
    deleted = [item for item in lost if item.killer == "deleted"]
    empty = [label for label, mutants in (("before", before), ("after", after)) if not mutants]

    counted = f"{len(lost)} lost, {len(gained)} gained"
    reasons = []
    if empty:
        reasons.append("empty report: " + " and ".join(empty) + " without any mutation")
    if only_b or only_a:
        reasons.append(f"mutant sets differ ({len(only_b)} only before, {len(only_a)} only after)")
    if reasons:
        outcome = "not-comparable"
        detail = "; ".join(reasons)
        rep.evaluated = False
        rep.violated("Mutation diff", f"before and after are not comparable: {detail}")
        status = f"NOT COMPARABLE ({detail}; {counted})"
    elif not lost:
        outcome, status = "pass", f"PASS ({counted})"
    elif all(item.known for item in lost):
        outcome = "known-variation"
        status = f"PASS (known variation: {counted}; configured known variation, killing test present)"
    else:
        outcome = "warn"
        with_deleted = f"; {len(deleted)} with a deleted killing test" if deleted else ""
        rep.violated(
            "Mutation diff",
            f"{len(lost)} lost mutant(s){with_deleted}: "
            + shown_names([mutant_text(item.before) for item in lost], SHOWN_NAMES, "; "),
        )
        status = f"WARN ({counted}{with_deleted})"
    rep.verdict = f"mutation-diff: {status}; test strength {_strength(total_b)} -> {_strength(total_a)}"

    rep.md = ["## Mutation diff", ""]
    rep.md.append(
        "Mutant key: class, method, descriptor, line, mutator, indexes. Detected is PIT's `detected` attribute;"
        " test strength leaves `NO_COVERAGE` out of the denominator."
    )
    rep.md.append("")
    rep.md.extend(mutants_header("run", "report"))
    rep.md.append(mutants_row("before", total_b, cell_span(args.before)))
    rep.md.append(mutants_row("after", total_a, cell_span(args.after)))
    rep.md.append("")
    if empty:
        rep.md.append("**Empty report:** " + " and ".join(empty) + " without any mutation.")
        rep.md.append("")
    if changed:
        rep.md.append(f"### Changed files ({len(changed)})")
        rep.md.append("")
        rep.md.append("| file | test strength before | test strength after |")
        rep.md.append("|---|---:|---:|")
        for key in changed[:FILES_CAP]:
            path, unresolved = key
            name = cell_span(path) + (f" ({NO_ROOT})" if unresolved else "")
            b, a = files_b.get(key, Mutants()), files_a.get(key, Mutants())
            rep.md.append(f"| {name} | {_strength(b)} | {_strength(a)} |")
        if len(changed) > FILES_CAP:
            rep.md.append("")
            rep.md.append(f"... {len(changed) - FILES_CAP} more file(s)")
        rep.md.append("")
    if lost:
        _lost_markdown(rep, lost, deleted, entries, ambiguous, killers)
    elif ambiguous:
        _ambiguous_markdown(rep, ambiguous)
    if gained:
        rep.md.append(f"### Gained mutants ({len(gained)}; detected after, not before)")
        rep.md.append("")
        items = [
            f"{code_span(mutant_text(a))}: {code_span(b.status_raw)} -> {code_span(a.status_raw)}" for b, a in gained
        ]
        rep.md.extend(capped(items, ITEMS_CAP))
        rep.md.append("")
    for title, ids, side in (("Only before", only_b, before), ("Only after", only_a, after)):
        if ids:
            rep.md.append(f"### {title} ({len(ids)}; not comparable)")
            rep.md.append("")
            rep.md.extend(capped([code_span(mutant_text(side[mutant_id])) for mutant_id in ids], ITEMS_CAP))
            rep.md.append("")

    def sides(key: Key) -> dict:
        return {"before": files_b.get(key, Mutants()).to_json(), "after": files_a.get(key, Mutants()).to_json()}

    rep.data = {
        "outcome": outcome,
        "totals": {"before": total_b.to_json(), "after": total_a.to_json()},
        "files": {path: sides((path, unresolved)) for path, unresolved in changed if not unresolved},
        "unresolved": {name: sides((name, unresolved)) for name, unresolved in changed if unresolved},
        "lost": [
            {
                "mutant": mutant_text(item.before),
                "status": [item.before.status_raw, item.after.status_raw],
                "killing_tests": item.before.killing_tests,
                "killing_test_after": item.killer,
                "known_variation": item.known,
            }
            for item in lost
        ],
        "gained": [{"mutant": mutant_text(a), "status": [b.status_raw, a.status_raw]} for b, a in gained],
        "only_before": [mutant_text(before[mutant_id]) for mutant_id in only_b],
        "only_after": [mutant_text(after[mutant_id]) for mutant_id in only_a],
        "empty": empty,
        "known_variation_ambiguous": ambiguous,
    }


def _ambiguous_markdown(rep: Report, ambiguous: list[dict]) -> None:
    """The entries of [[mutation.known_variation]] that name more than one mutant: at most
    ITEMS_CAP, then how many more there are."""
    names = [
        code_span(f"{entry['class']}.{entry['method']} L{entry['line']} {entry['mutator']}")
        + f" ({entry['matches']} mutants)"
        for entry in ambiguous
    ]
    rep.md.append(
        "No tolerance for an entry of `[[mutation.known_variation]]` that names more than one mutant: "
        + shown_names(names, ITEMS_CAP)
    )
    rep.md.append("")


def _lost_markdown(
    rep: Report,
    lost: list[_Lost],
    deleted: list[_Lost],
    entries: frozenset[tuple[str, str, int, str]],
    ambiguous: list[dict],
    killers: _Killers | None,
) -> None:
    """The lost mutants with their killing tests, and how the killing tests were checked."""
    rep.md.append(f"### Lost mutants ({len(lost)}; detected before, not after)")
    rep.md.append("")
    if deleted:
        rep.md.append(f"**{len(deleted)} lost mutant(s) whose killing test was deleted** - never known variation.")
        rep.md.append("")
    if any(_variation_key(item.before) in entries for item in lost):
        rep.md.append(
            "Known variation (configured, a tolerance): the one mutant of the two runs that an entry of"
            " `[[mutation.known_variation]]` names, when its killing test is present in the after run; with a"
            " deleted, unresolved or unchecked killing test it is a plain loss."
        )
        rep.md.append("")
    if ambiguous:
        _ambiguous_markdown(rep, ambiguous)
    items = []
    for item in lost:
        tests = ", ".join(code_span(killing_test_text(test)) for test in item.before.killing_tests) or "none"
        flag = "**deleted**" if item.killer == "deleted" else item.killer
        mark = "; **known variation** (configured)" if item.known else ""
        items.append(
            f"{code_span(mutant_text(item.before))}: {code_span(item.before.status_raw)} ->"
            f" {code_span(item.after.status_raw)}; killed before by {tests} - killing test after: {flag}{mark}"
        )
    rep.md.extend(capped(items, ITEMS_CAP))
    rep.md.append("")
    if killers is None:
        rep.md.append("Killing tests not checked (no `--junit-after`).")
        rep.md.append("")
        return
    if killers.folders:
        rep.md.append(
            "A killing test hidden by a display name or a parameterized invocation in the JUnit XML is"
            f" resolved against the test sources in {', '.join(map(code_span, killers.folders))} of the"
            " repository, the tree after the trim."
        )
        rep.md.append("")
    if any(item.killer == "unresolved" for item in lost):
        rep.md.append(
            "`unresolved`: the killing test could be neither confirmed nor ruled out. A display name or a"
            " parameterized invocation hides the method name in the JUnit XML and the source does not tell"
            " (no test sources, no source of its class found, its class body not found, a source of its"
            " class in two test folders or that cannot be read, a class name that names no file, unbalanced"
            " source, a test the class may inherit, a method of that name with an annotation the scan does"
            " not know), a test case of its class was skipped, or the entry is not a JUnit Platform unique id."
        )
        rep.md.append("")
