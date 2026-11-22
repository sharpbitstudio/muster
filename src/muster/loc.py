"""The loc command: the lines of code of the LOC set by group, a trend and never a gate.

The LOC set is every file under [source].roots and [source].tests that matches an include glob and
neither a generated nor a [loc].exclude glob (muster.paths). A file is in the group of the deepest
folder that holds it: "main" under a folder of roots; under a folder of tests "test-support" when a
[loc].test_support glob matches it, else "test". A line of code keeps a character other than white
space outside comments, as the language of the file counts it (muster.lang); a line ends at a line
feed only.

A file of the set without a language, or one that cannot be read, is not counted and is an input
error; the section still reports what it counted. With --base the change split gives the raw lines
added and deleted in each group since the merge base: git's numstat of the changed files, and every
line of an untracked file. A path is grouped by its name, not by the files on disk, so a deleted file
gives deleted lines and a rename across groups is a deletion and an addition; only a path of the LOC
set with a language counts, and an untracked one only when the walk of the set took it as a file. A
base without a merge base, or a git failure, leaves the split out with a note. The command never has
a violation: --strict changes the exit only for an input error.
"""

from __future__ import annotations

import argparse
import posixpath

from muster.config import Config, ConfigError, require
from muster.coverage import ref_argument
from muster.gitio import binary_counts_since, count_file_lines, numstat_since, untracked_under
from muster.lang import Language, count_file, language_of
from muster.paths import group_of, in_loc_set, loc_set
from muster.report import Report, ReportError, md_path

HELP = "lines of code: main, test, test-support (trend only)"
GROUPS = ("main", "test", "test-support")
NO_EXTENSION = "(no extension)"


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base",
        metavar="REF",
        type=ref_argument,
        help="ref for the change split (git diff --numstat against the merge-base)",
    )


def _loc_files(args: argparse.Namespace, cfg: Config) -> tuple[list[str], ConfigError | None]:
    """The LOC set and the first folder that was missing or unreadable. policy.protected() has
    walked the source and the test folders into args.source_sets, so the set is taken from that
    walk with the predicate of the LOC set; without it they are walked here."""
    sets = getattr(args, "source_sets", None)
    if sets is None:
        found = loc_set(cfg, defer=True)
        return found.files, found.error
    walked = sorted({*sets.walked_sources, *sets.walked_tests})
    return [path for path in walked if in_loc_set(cfg, path)], sets.error


def _extension(path: str) -> str:
    """The extension of the file name of `path` as the errors name it: from its last dot, or
    NO_EXTENSION for a name without one (a dot-file too)."""
    return posixpath.splitext(path.rsplit("/", 1)[-1])[1] or NO_EXTENSION


def _group(cfg: Config, langs: list[Language], path: str) -> str | None:
    """The group of a path the change split counts, by its name alone: a path of the LOC set with a
    language; None for any other."""
    if not in_loc_set(cfg, path) or language_of(path, langs) is None:
        return None
    return group_of(cfg, path)


def _raw_lines(cfg: Config, rel: str) -> int:
    try:
        return count_file_lines(cfg.root / rel)
    except OSError as exc:
        raise ReportError(f"{rel}: cannot read: {exc.strerror or exc}") from None


def _change_by_group(
    cfg: Config, langs: list[Language], commit: str, loc_files: set[str]
) -> dict[str, list[int]]:
    """{group: [added, deleted]} raw lines from `commit` to the work tree under the source and the
    test folders: git's numstat, plus every line of an untracked file of the LOC set (`loc_files`,
    the files walked on disk) as added. A path git takes as binary in any of its numstat rows gets
    all of its counts from its patch (binary_counts_since), once. A git failure, or an untracked
    file of the set that cannot be read, raises ReportError."""
    folders = [*cfg.get("source.roots", []), *cfg.get("source.tests", [])]
    change = {group: [0, 0] for group in GROUPS}
    rows = numstat_since(cfg.root, commit, folders)
    # binary to git (a NUL byte, a -diff attribute): the patches of the path count, once, for all its rows
    binary = {path for added, deleted, path in rows if added is None or deleted is None}
    for added, deleted, path in rows:
        group = _group(cfg, langs, path)
        if group is None or path in binary:
            continue
        change[group][0] += added
        change[group][1] += deleted
    for path in sorted(binary):
        group = _group(cfg, langs, path)
        if group is not None:
            added, deleted = binary_counts_since(cfg.root, commit, path)
            change[group][0] += added
            change[group][1] += deleted
    for path in untracked_under(cfg.root, folders):
        group = _group(cfg, langs, path) if path in loc_files else None
        if group is not None:
            change[group][0] += _raw_lines(cfg, path)
    return change


def _split(
    args: argparse.Namespace, cfg: Config, rep: Report, langs: list[Language], loc_files: set[str]
) -> dict | None:
    """The change split against --base into the Markdown; None, with a note, when there is no merge
    base or git fails. Neither is an input error."""
    commit = getattr(args, "base_commit", None)
    if commit is None:
        rep.md += [f"No merge-base with {md_path(args.base)}: change split not computed.", ""]
        return None
    try:
        change = _change_by_group(cfg, langs, commit, loc_files)
    except ReportError as exc:
        rep.md += [f"LOC change split not computed: {exc}", ""]
        return None
    rep.md += [f"This change vs {md_path(args.base)} (raw lines, `git diff --numstat` + untracked):", ""]
    rep.md += ["| group | added | deleted |", "|---|---:|---:|"]
    for name, (added, deleted) in change.items():
        rep.md.append(f"| {name} | {added} | {deleted} |")
    rep.md.append("")
    return change


def run(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Count the LOC set by group; the command line has set args.base_commit when --base was given."""
    require(cfg, "source.roots", "source.include")
    files, error = _loc_files(args, cfg)
    if error is not None:
        raise ConfigError(f"{cfg.name}: {error}")
    langs = cfg.languages
    groups = {name: {"files": 0, "loc": 0} for name in GROUPS}
    errors: list[str] = []
    uncounted: dict[str, int] = {}
    example: dict[str, str] = {}  # the first file of each extension without a language, in path order
    for rel in files:
        lang = language_of(rel, langs)
        if lang is None:
            ext = _extension(rel)
            uncounted[ext] = uncounted.get(ext, 0) + 1
            example.setdefault(ext, rel)
            continue
        try:
            count = count_file(cfg.root / rel, lang)
        except OSError as exc:
            errors.append(f"{rel}: cannot read: {exc.strerror or exc}")
            continue
        acc = groups[group_of(cfg, rel)]
        acc["files"] += 1
        acc["loc"] += count
    uncounted = dict(sorted(uncounted.items()))
    for ext, count in uncounted.items():
        errors.append(
            f"{ext}: {count} file(s) of the LOC set have no language and are not counted (e.g. {example[ext]})"
        )
    rep.md = ["## Lines of code", "", "Lines of code (without blank and comment-only lines); a trend, not a gate.", ""]
    rep.md += ["| group | files | LOC |", "|---|---:|---:|"]
    for name, acc in groups.items():
        rep.md.append(f"| {name} | {acc['files']} | {acc['loc']} |")
    rep.md.append("")
    main_loc = groups["main"]["loc"]
    test_ratio = groups["test"]["loc"] / main_loc if main_loc else None
    all_ratio = (groups["test"]["loc"] + groups["test-support"]["loc"]) / main_loc if main_loc else None
    if main_loc:
        rep.md += [f"test/main {test_ratio:.2f}; (test + test-support)/main {all_ratio:.2f}.", ""]
    if errors:
        rep.md += ["**ERROR**: some files of the LOC set are not counted:", ""]
        rep.md.extend(f"- {error}" for error in errors)
        rep.md.append("")
    change = _split(args, cfg, rep, langs, set(files)) if args.base is not None else None
    for error in errors:
        rep.errored("Lines of code", error)
    ratio_text = f", test/main {test_ratio:.2f}" if test_ratio is not None else ""
    rep.verdict = (
        f"loc: main {main_loc}, test {groups['test']['loc']}, test-support {groups['test-support']['loc']}{ratio_text}"
    )
    if errors:
        rep.verdict = f"loc: ERROR ({errors[0]})"
    rep.data = {
        "groups": groups,
        "test_ratio": test_ratio,
        "all_ratio": all_ratio,
        "change": change,
        "uncounted": uncounted,
    }
