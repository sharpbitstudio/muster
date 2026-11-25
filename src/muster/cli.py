"""The command line: global options, the commands, exit codes and the JSON envelope on every exit.

Exit codes: 0 pass (violations and input errors in warn-only mode); 1 a violation in a strict
section, or an input error under strict or --fail-on-error; 2 a usage or configuration error,
always; 3 an internal error (a bug), always.

The envelope is written to --json-out, and printed with --format json, on every exit, a usage error
and a bug included. When argparse fails, or a file name it read is empty (or '-' for --json-out),
--json-out, --format and the summary file are read from argv the way argparse would have read them,
and --repo and --config from the words before the command; otherwise the parsed values count. No
output file (the envelope, the summary, the command's own outputs) may be the config file, an input
of the command (also one its options name), a file under a source or test folder, a protected file
of the repository or another output: on such a clash nothing is removed or written. A profile that
fails its check, or one that no command reads (after a failed parse of argv, where the default
config file of the repository is refused too, and for `version`), still names every file and folder
it can, as far as it parses; there it is read only when it is a regular file, and a path that
cannot be resolved (a busy named pipe on Windows, say) is no clash. After a failed parse, the files
that --before, --after and --diff-file name as read from argv, and the JUnit files under the folders
of --junit-before and --junit-after, are refused too, as far as they can be found. These
best-effort checks do not refuse what only a command would find, the JUnit files of tests.results
(a glob); and the fallback in src/__main__.py, for an older Python or a broken install, writes
--json-out without any check. Once that holds, the outputs that must not outlive a failed run are
removed before the command does anything else, and those removed when the run fails go on any exit
2 or 3.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from muster import __version__, coverage, diffs, duration, loc, mutation, policy
from muster.config import (
    DEFAULT_CONFIG,
    SECTIONS,
    Config,
    ConfigError,
    config_path,
    listed_folders,
    load,
    load_unchecked,
    named_files,
)
from muster.gitio import find_repo_root, git, merge_base
from muster.report import (
    FatalError,
    Report,
    envelope,
    escape_data,
    escape_property,
    json_text,
    one_line,
    write_json,
    write_markdown,
)

EXIT_PASS, EXIT_VIOLATION, EXIT_USAGE, EXIT_INTERNAL = 0, 1, 2, 3


class UsageError(FatalError):
    """Bad arguments; `usage` is the usage line of the parser that failed."""

    kind = "usage"

    def __init__(self, message: str, usage: str = "") -> None:
        super().__init__(message)
        self.usage = usage


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would print and exit 2; main() reports it instead
        raise UsageError(message, self.format_usage())


# (label, path, removed before the command runs, removed when the run ends with exit 2 or 3)
Outputs = list[tuple[str, Path, bool, bool]]
Inputs = list[tuple[str, Path]]  # (label, path)
# (label, path, appended to, the name messages give it: as typed, or repository-relative from the profile)
Summary = tuple[str, Path, bool, str]


@dataclass(frozen=True)
class Analysis:
    """An analysis command: `run` judges one section into the Report; `arguments` adds its own options.
    `option_inputs` names the input files of its options, checked before the profile is read. Once
    the profile is read: `inputs` names the files of the profile, every one the profile names by its
    path but the command's own outputs, `protected` the other repository files no output may be,
    and `outputs` its own output files with the error that keeps one from being named, raised once
    the others are checked (none of them may raise)."""

    run: Callable[[argparse.Namespace, Config, Report], None]
    help: str
    arguments: Callable[[argparse.ArgumentParser], None] = lambda parser: None
    inputs: Callable[[Config], Inputs] = lambda cfg: []
    outputs: Callable[[argparse.Namespace, Config], tuple[Outputs, FatalError | None]] = lambda args, cfg: ([], None)
    protected: Callable[[argparse.Namespace, Config], Inputs] = lambda args, cfg: []
    option_inputs: Callable[[argparse.Namespace], Inputs] = lambda args: []


# Every analysis command protects the same repository files (policy.protected).
ANALYSES: dict[str, Analysis] = {
    "policy": Analysis(policy.run, policy.HELP, policy.arguments, policy.inputs, policy.outputs, policy.protected),
    "coverage": Analysis(
        coverage.run,
        coverage.HELP,
        coverage.arguments,
        named_files,
        protected=policy.protected,
        option_inputs=coverage.option_inputs,
    ),
    "duration": Analysis(duration.run, duration.HELP, inputs=named_files, protected=policy.protected),
    "loc": Analysis(loc.run, loc.HELP, loc.arguments, named_files, protected=policy.protected),
    "mutation": Analysis(mutation.run, mutation.HELP, inputs=named_files, protected=policy.protected),
    "coverage-diff": Analysis(
        diffs.coverage_diff,
        diffs.COVERAGE_HELP,
        diffs.coverage_arguments,
        named_files,
        protected=policy.protected,
        option_inputs=diffs.option_inputs,
    ),
    "mutation-diff": Analysis(
        diffs.mutation_diff,
        diffs.MUTATION_HELP,
        diffs.mutation_arguments,
        named_files,
        protected=policy.protected,
        option_inputs=diffs.option_inputs,
    ),
}
# The names --strict-only takes: the sections of [check] sections, and the diff commands, which
# are no section of the profile.
STRICT_SECTIONS = SECTIONS + tuple(name for name in ANALYSES if name not in SECTIONS)


def _section_list(text: str) -> frozenset[str]:
    names = [name.strip() for name in text.split(",")]
    for name in names:
        if name not in STRICT_SECTIONS:
            raise argparse.ArgumentTypeError(f"unknown section '{name}' (choose from {', '.join(STRICT_SECTIONS)})")
    return frozenset(names)


def _output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text", help="stdout format (default: text)")
    parser.add_argument(
        "--json-out", metavar="FILE", help="also write the JSON envelope here (relative to the working directory)"
    )


def common_options() -> argparse.ArgumentParser:
    """The options every analysis command takes, as a parent parser."""
    common = _Parser(add_help=False, allow_abbrev=False)
    strict = common.add_mutually_exclusive_group()
    strict.add_argument("--strict", action="store_true", help="every section is strict: a violation exits 1")
    strict.add_argument(
        "--strict-only",
        metavar="A,B",
        type=_section_list,
        help=f"only these sections are strict ({', '.join(STRICT_SECTIONS)})",
    )
    common.add_argument("--fail-on-error", action="store_true", help="an input error exits 1")
    _output_options(common)
    common.add_argument("--summary-file", metavar="FILE", help="append the Markdown summary here")
    common.add_argument("--github", action="store_true", help="the summary file is $GITHUB_STEP_SUMMARY")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="muster", description="Check test reports against the testing policy.", allow_abbrev=False)
    parser.add_argument(
        "--repo", metavar="DIR", help="repository root (default: the git top level, else the working directory)"
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default=DEFAULT_CONFIG,
        help=f"profile, relative to the repository root (default: {DEFAULT_CONFIG})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    _output_options(sub.add_parser("version", help="print the version", allow_abbrev=False))
    common = common_options()
    for name, analysis in ANALYSES.items():
        analysis.arguments(sub.add_parser(name, parents=[common], help=analysis.help, allow_abbrev=False))
    return parser


# What argparse takes as a negative number, and so as a value rather than an option.
_NEGATIVE_NUMBER = re.compile(r"-\d+$|-\d*\.\d+$")
# Every option string of build_parser(), at every level (a test keeps the two equal).
OPTIONS = frozenset(
    "-h --help --repo --config --format --json-out --strict --strict-only --fail-on-error --summary-file"
    " --github --snapshot --zones-out --base --diff-file --no-freshness --today --before --after --junit-before"
    " --junit-after".split()
)
# The options before the command word; each takes a value.
GLOBAL_OPTIONS = ("--repo", "--config")


def _options(argv: list[str]) -> list[str]:
    """argv up to `--`: what follows it is never an option."""
    return argv[: argv.index("--")] if "--" in argv else argv


def _is_value(arg: str) -> bool:
    """Whether argparse takes `arg`, after an option that needs a value, as that value, or else as a
    positional argument. An option string, alone or with `=VALUE`, never is (argparse checks that
    before the space rule)."""
    if arg in OPTIONS or ("=" in arg and arg.split("=", 1)[0] in OPTIONS):
        return False
    return not arg.startswith("-") or arg == "-" or bool(_NEGATIVE_NUMBER.match(arg)) or " " in arg


def _scan_values(words: list[str], option: str) -> list[str | None]:
    """The value of each `option VALUE` or `option=VALUE` in words, in order, as argparse would read
    each one; None for an occurrence without a value."""
    values: list[str | None] = []
    for i, arg in enumerate(words):
        if arg == option:
            following = words[i + 1] if i + 1 < len(words) else None
            values.append(following if following is not None and _is_value(following) else None)
        elif arg.startswith(option + "="):
            values.append(arg.split("=", 1)[1])
    return values


def _scan_option(words: list[str], option: str) -> str | None:
    """The value of the last `option VALUE` or `option=VALUE` in words, as argparse would take it."""
    values = _scan_values(words, option)
    return values[-1] if values else None


def _command_index(words: list[str]) -> int | None:
    """Where the command word is: the first positional argument after the global options and their
    values, or None."""
    skip = False
    for i, arg in enumerate(words):
        if skip:
            skip = False
        elif arg in GLOBAL_OPTIONS:
            skip = i + 1 < len(words) and _is_value(words[i + 1])
        elif _is_value(arg):
            return i
    return None


def _global_words(words: list[str]) -> list[str]:
    """The words before the command word: the only place argparse takes --repo and --config from."""
    index = _command_index(words)
    return words if index is None else words[:index]


def _scan_command(words: list[str]) -> str | None:
    """The command named in words, when it is a known one."""
    index = _command_index(words)
    command = None if index is None else words[index]
    return command if command == "version" or command in ANALYSES else None


def _repo_root(explicit: str | None) -> Path:
    """The repository root: --repo, else the git top level, else the working directory, resolved.
    A folder that cannot be resolved is a UsageError, never a bug: a busy named pipe on Windows
    raises OSError, and a link loop raises RuntimeError on Python 3.11 and 3.12."""
    try:
        return find_repo_root(explicit)
    except (OSError, RuntimeError):  # no git on PATH, or a folder that cannot be resolved
        pass
    try:
        return Path(explicit or ".").resolve()
    except (OSError, RuntimeError) as exc:
        name = f"--repo {explicit}" if explicit else "the working directory"
        raise UsageError(f"{name}: cannot resolve the folder: {getattr(exc, 'strerror', None) or exc}") from None


def _head_commit(root: Path) -> str | None:
    try:
        return git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
    except OSError:  # no git on PATH, or no such folder
        return None


def _merge_base(root: Path, ref: str) -> str | None:
    try:
        return merge_base(root, ref)
    except OSError:  # no git on PATH, or no such folder
        return None


def _same_file(first: Path, second: Path) -> bool:
    """Whether two paths name one file: the same file on disk, else the same resolved path (ignoring
    case on Windows)."""
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def _same_file_if_resolved(first: Path, second: Path) -> bool:
    """_same_file(), with paths that cannot be resolved (os.path.realpath raises for a busy named
    pipe on Windows, say) never the same file."""
    try:
        return _same_file(first, second)
    except OSError:
        return False


def _index(files: Inputs) -> dict[str, str]:
    """The resolved path (case-folded on Windows) of each file -> its label; a file that is a link is
    also keyed by the file it leads to. Each folder is resolved once, so that a large set of files
    costs one link check per file and a full resolution only per folder and per link."""
    folders: dict[Path, str] = {}
    index: dict[str, str] = {}
    for label, path in files:
        folder = folders.get(path.parent)
        if folder is None:
            folder = folders[path.parent] = os.path.realpath(path.parent)
        index.setdefault(os.path.normcase(os.path.join(folder, path.name)), label)
        if os.path.islink(path):
            index.setdefault(os.path.normcase(os.path.realpath(path)), label)
    return index


@dataclass
class Invocation:
    """What one run has learned so far; the envelope is built from it on every exit. `json_out` and
    `summary` are set only once they are known not to clash with an input."""

    argv: list[str]
    command: str | None
    fmt: str
    json_out: str | None = None
    summary: tuple[Path, bool] | None = None  # (path, appended to): where the failure line goes
    summary_name: str = ""  # the summary as messages name it
    summary_written: bool = False
    remove_on_failure: list[tuple[str, Path]] = field(default_factory=list)  # (label, path)
    commit: str | None = None
    base: dict | None = None  # {"ref", "commit"} once --base is parsed; the commit is None without a merge base
    sections: list[Report] = field(default_factory=list)


def _refuse_clashes(
    inv: Invocation,
    named: Inputs,
    inputs: Inputs,
    protected: dict[str, str] | None = None,
    *,
    best_effort: bool = False,
) -> None:
    """Refuse an output that is an input, a protected file or another output: exit 2, and nothing is
    removed or written, the envelope and the summary included. With `best_effort`, two paths of
    which one cannot be resolved are no clash."""
    same = _same_file_if_resolved if best_effort else _same_file
    for i, (label, path) in enumerate(named):
        clash = next((f"is {other}" for other, file in inputs if same(path, file)), None)
        if clash is None and protected:
            # The file a link leads to, and the link itself: _index() keys a protected link by both.
            keys = (os.path.realpath(path), os.path.join(os.path.realpath(path.parent), path.name))
            other = next((protected[key] for key in map(os.path.normcase, keys) if key in protected), None)
            clash = None if other is None else f"is {other}"
        if clash is None:
            clash = next((f"is the same file as {other}" for other, file in named[:i] if same(path, file)), None)
        if clash is not None:
            inv.json_out, inv.summary, inv.remove_on_failure = None, None, []
            raise UsageError(f"{label} {clash}")


def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _output_keys(path: Path) -> tuple[str, str]:
    """An output as _refuse_under_folders() compares it: resolved, and with only its folder resolved."""
    return _real(path), os.path.normcase(os.path.join(os.path.realpath(path.parent), path.name))


def _refuse_under_folders(inv: Invocation, named: Inputs, cfg: Config, *, best_effort: bool = False) -> None:
    """Refuse an output under a folder of source.roots or source.tests, on every run alike: exit 2,
    and nothing is removed or written. The paths are compared resolved (ignoring case on Windows),
    so a link that leads into such a folder counts, and so does a path inside one whose last part is
    a link. On a profile that failed its check only the entries that listed_folders() keeps count.
    Each output is resolved and compared in turn, so an earlier clash is refused before a later
    output can fail to resolve. With `best_effort`, a folder or an output that cannot be resolved
    is left out."""
    folders = [("source", folder) for folder in listed_folders(cfg, "source.roots")]
    folders += [("test", folder) for folder in listed_folders(cfg, "source.tests")]
    resolved = []
    for kind, folder in folders:
        try:
            resolved.append((kind, folder, _real(cfg.root / folder)))
        except OSError:
            if not best_effort:
                raise
    for label, path in named:
        try:
            keys = _output_keys(path)
        except OSError:
            if not best_effort:
                raise
            continue
        for kind, folder, top in resolved:
            if any(key == top or key.startswith(top.rstrip(os.sep) + os.sep) for key in keys):
                inv.json_out, inv.summary, inv.remove_on_failure = None, None, []
                raise UsageError(f"{label} is under the {kind} folder {folder}")


def _named(json_out: str | None, summary: Summary | None) -> Inputs:
    named = [(f"--json-out {json_out}", Path(json_out))] if json_out else []
    return named + ([summary[:2]] if summary else [])


def _arm(inv: Invocation, json_out: str | None, summary: Summary | None) -> None:
    """From now on the envelope and the failure line go to these files."""
    inv.json_out = json_out
    inv.summary = None if summary is None else (summary[1], summary[2])
    inv.summary_name = "" if summary is None else summary[3]


def _clear(inv: Invocation, json_out: str | None, summary: Summary | None, outputs: Outputs) -> None:
    """Remove the old envelope, an overwritten summary and the outputs that must not outlive a
    failed run, every one of them even when another cannot be removed; a file that cannot be
    removed cannot be written either, and the first such file is the error."""
    files = [(f"--json-out {json_out}", Path(json_out), "envelope")] if json_out else []
    if summary is not None and not summary[2]:
        files.append((summary[0], summary[1], "summary"))
    files += [(label, path, "output") for label, path, before, _ in outputs if before]
    error = None
    for label, path, kind in files:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            if kind == "envelope":
                inv.json_out = None
            elif kind == "summary":
                inv.summary = None
            error = error or UsageError(f"cannot write {label}: {exc.strerror or exc}")
    if error is not None:
        raise error


def _file_option(option: str, value: str | None) -> str | None:
    if value == "":
        raise UsageError(f"{option}: the file name is empty")
    if value == "-" and option == "--json-out":
        raise UsageError("--json-out: '-' is not a file; use --format json to print the envelope")
    return value


def _option_summary(args: argparse.Namespace) -> Summary | None:
    """The summary named on the command line: --summary-file, else $GITHUB_STEP_SUMMARY with --github."""
    summary_file = _file_option("--summary-file", getattr(args, "summary_file", None))
    if summary_file:
        return f"--summary-file {summary_file}", Path(summary_file), True, summary_file
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY") if getattr(args, "github", False) else None
    if not step_summary:
        return None
    return f"$GITHUB_STEP_SUMMARY {step_summary}", Path(step_summary), True, step_summary


def _refuse_profile_clashes(
    inv: Invocation, named: Inputs, repo: str | None, value: str, *, default: bool, inputs: Inputs = ()
) -> None:
    """For a profile that no command reads: refuse an output that is another output, one of the
    `inputs` of the command line, the config file `value` of the repository `repo` (--repo and
    --config, which may be anything), with `default` also the default config file, or, as far as
    the profile parses without its check, a file it names or a file under one of its source or test
    folders: exit 2, and nothing is removed or written. The profile is read only when it is a
    regular file, so a named pipe never blocks the run; one that cannot be read or is not TOML adds
    nothing. A path that cannot be resolved is no clash. Only a UsageError is raised: a NUL in
    `repo`, or a folder `repo` that cannot be resolved, names no repository, so the outputs are
    checked only against each other and against `inputs`; a NUL in `value` names no file.

    It does not refuse what only a command would find: a JUnit file of tests.results (a glob). Nor
    does the fallback in src/__main__.py, for an older Python or a broken install, run it: that one
    writes --json-out without any check."""
    try:
        root = None if "\0" in (repo or "") else _repo_root(repo)
    except UsageError:  # the folder cannot be resolved
        root = None
    if root is None:
        _refuse_clashes(inv, named, list(inputs), best_effort=True)
        return
    given = [*inputs] + ([] if "\0" in value else [("the config file", config_path(root, value))])
    if default:
        given.append(("the default config file", config_path(root, DEFAULT_CONFIG)))
    unchecked = load_unchecked(root, value) if os.path.isfile(config_path(root, value)) else None
    _refuse_clashes(inv, named, given + (named_files(unchecked) if unchecked is not None else []), best_effort=True)
    if unchecked is not None:
        _refuse_under_folders(inv, named, unchecked, best_effort=True)


def _scanned_inputs(words: list[str]) -> Inputs:
    """The input files of the options as read from argv: those that each --before, --after and
    --diff-file names, every one of them and not only the last that argparse takes, and the JUnit
    files under the folder of each --junit-before and --junit-after, as far as they can be found.
    It never raises."""
    inputs: Inputs = []
    for option in ("--before", "--after", "--diff-file"):
        for value in _scan_values(words, option):
            if value and "\0" not in value:
                inputs.append((f"{option} {value}", Path(value)))
    for option in ("--junit-before", "--junit-after"):
        for value in _scan_values(words, option):
            if value and "\0" not in value:
                inputs += diffs.folder_inputs(value)
    return inputs


def _arm_scanned(inv: Invocation) -> None:
    """argparse failed, or a file name it read is empty or '-': the --json-out and the summary file
    read from argv are refused when they are the config file named before the command or the default
    one of the repository, a file that profile names or a file under one of its source or test
    folders, as far as it parses (the --repo and --config read from argv may be anything), or an
    input file of the options as read from argv; else the envelope and the line go there."""
    words = _options(inv.argv)
    json_out = _scan_option(words, "--json-out")
    json_out = json_out if json_out not in ("", "-") else None
    summary_file = _scan_option(words, "--summary-file")
    summary = None
    if summary_file:
        summary = f"--summary-file {summary_file}", Path(summary_file), True, summary_file
    elif summary_file is None and "--github" in words and os.environ.get("GITHUB_STEP_SUMMARY"):
        step_summary = os.environ["GITHUB_STEP_SUMMARY"]
        summary = f"$GITHUB_STEP_SUMMARY {step_summary}", Path(step_summary), True, step_summary
    named = _named(json_out, summary)
    if named:
        before = _global_words(words)
        value = _scan_option(before, "--config") or DEFAULT_CONFIG
        repo = _scan_option(before, "--repo")
        _refuse_profile_clashes(inv, named, repo, value, default=True, inputs=_scanned_inputs(words))
    _arm(inv, json_out, summary)


def run_version(args: argparse.Namespace, inv: Invocation) -> int:
    if inv.fmt == "text":
        print(f"muster {__version__}")
    return EXIT_PASS


# A surrogate that no surrogate-escape decoding of a file name makes.
_OTHER_SURROGATE = re.compile("[\ud800-\udc7f\udd00-\udfff]")


def _markdown_text(text: str) -> str:
    """Text that UTF-8 can encode: a name that is not valid UTF-8 on disk (decoded with surrogate
    escapes) shows U+FFFD where its bad bytes were; any other lone surrogate becomes '?'."""
    text = _OTHER_SURROGATE.sub("?", text)
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _write_summary(inv: Invocation, rep: Report) -> None:
    """Append to the summary file of the command line; overwrite <[output].dir>/<command>.md (append
    once this run wrote there); without a summary file write nothing."""
    if inv.summary is None:
        return
    target, append = inv.summary
    try:
        text = _markdown_text("\n".join(rep.md).rstrip("\n") + "\n\n")
        write_markdown(target, text, append=append or inv.summary_written)
    except OSError as exc:
        raise UsageError(f"cannot write the summary {inv.summary_name}: {exc.strerror or exc}") from None
    inv.summary_written = True


def _commits(inv: Invocation, args: argparse.Namespace) -> None:
    """The repository root, HEAD and, with --base, the merge base, found once right after argv
    parses: the envelope carries them on every later exit, a usage error included, and the section
    uses the same HEAD (args.head_commit, None without one) and the same merge base."""
    args.repo_root = _repo_root(args.repo)
    inv.commit = args.head_commit = _head_commit(args.repo_root)
    if getattr(args, "base", None) is not None:
        args.base_commit = _merge_base(args.repo_root, args.base)
        inv.base = {"ref": args.base, "commit": args.base_commit}


def run_analysis(
    args: argparse.Namespace, inv: Invocation, analysis: Analysis, json_out: str | None, summary: Summary | None
) -> int:
    """Run an analysis command; _commits() has set args.repo_root."""
    root = args.repo_root
    given = [("the config file", config_path(root, args.config)), *analysis.option_inputs(args)]
    _refuse_clashes(inv, _named(json_out, summary), given)
    _arm(inv, json_out, summary)
    try:
        cfg = load(root, args.config)
    except ConfigError:
        unchecked = load_unchecked(root, args.config)
        if unchecked is not None:  # no command runs on it, so no file it names is an output
            _refuse_clashes(inv, _named(json_out, summary), given + named_files(unchecked))
            _refuse_under_folders(inv, _named(json_out, summary), unchecked)
        raise
    if summary is None and cfg.output_dir is not None:
        name = f"{cfg.output_dir}/{args.command}.md"
        summary = f"the summary {name}", cfg.root / name, False, name
    named = _named(json_out, summary)
    inputs = [("the config file", cfg.path), *analysis.option_inputs(args), *analysis.inputs(cfg)]
    _refuse_clashes(inv, named, inputs)
    outputs, error = analysis.outputs(args, cfg)
    every = named + [(label, path) for label, path, _, _ in outputs]
    _refuse_under_folders(inv, every, cfg)  # before protected() reads the tree
    protected = _index(analysis.protected(args, cfg))
    _refuse_clashes(inv, named, inputs, protected)
    _arm(inv, json_out, summary)
    _refuse_clashes(inv, every, inputs, protected)
    if error is not None:  # only now: the outputs that could be named are checked, none is removed
        raise error
    inv.remove_on_failure = [(label, path) for label, path, _, on_failure in outputs if on_failure]
    _clear(inv, json_out, summary, outputs)
    strict = args.strict or (args.strict_only is not None and args.command in args.strict_only)
    rep = Report(args.command, strict=strict, fail_on_error=args.fail_on_error)
    try:
        analysis.run(args, cfg, rep)
    except FatalError:
        if rep.violations:  # a fatal finding, such as an invalid policy: the section is reported too
            _report_section(inv, rep, fatal=True)
        raise
    _report_section(inv, rep)
    return EXIT_VIOLATION if (rep.violation and rep.strict) or (rep.error and rep.fail_on_error) else EXIT_PASS


def _report_section(inv: Invocation, rep: Report, fatal: bool = False) -> None:
    inv.sections.append(rep)
    try:
        _write_summary(inv, rep)
    except UsageError as exc:
        if not fatal:
            raise
        print(f"muster: error: {exc}", file=sys.stderr)  # the fatal finding stays the error reported
    if inv.fmt == "text":
        for line in rep.annotations:
            print(line)
        print(one_line(rep.verdict))


def _run(inv: Invocation) -> int:
    try:
        args = build_parser().parse_args(inv.argv)  # --help exits here, before any output is checked
        inv.command, inv.fmt = args.command, args.format
        if args.command != "version":
            _commits(inv, args)
        json_out = _file_option("--json-out", args.json_out)
        summary = _option_summary(args)
    except UsageError:  # bad arguments, or an empty file name: the envelope goes where argv says
        _arm_scanned(inv)
        raise
    if args.command != "version":
        return run_analysis(args, inv, ANALYSES[args.command], json_out, summary)
    if json_out:  # version reads no profile, but its envelope may not land where any run may not write
        _refuse_profile_clashes(inv, _named(json_out, None), args.repo, args.config, default=False)
        _arm(inv, json_out, None)
        _clear(inv, json_out, None, [])
    return run_version(args, inv)


def _remove_failed_outputs(inv: Invocation) -> None:
    for label, path in inv.remove_on_failure:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"muster: error: cannot remove {label}: {exc.strerror or exc}", file=sys.stderr)


def _write_failure_line(inv: Invocation, line: str) -> None:
    """Best effort: the summary says the command failed. A summary of the command line is appended
    to; <[output].dir>/<command>.md is overwritten unless this run already wrote there."""
    if inv.summary is None:
        return
    target, append = inv.summary
    if append:
        _summary_crash_line([f"--summary-file={target}"], _markdown_text(line))
        return
    try:
        write_markdown(target, _markdown_text(line) + "\n\n", append=inv.summary_written)
    except Exception:  # noqa: BLE001 - the failure report itself must never raise
        pass


def _report_failure(inv: Invocation, message: str, usage: str) -> None:
    """Exit 2 or 3: an error annotation (text mode), the message on stderr and a line in the summary."""
    name = f"muster {inv.command}" if inv.command else "muster"
    if inv.fmt == "text":
        print(f"::error title={escape_property(name)}::{escape_data(message)}")
    if usage:
        sys.stderr.write(usage)
    print(f"{name}: error: {message}", file=sys.stderr)
    _write_failure_line(inv, f"{name}: error: {one_line(message)}")


def _finish(inv: Invocation, code: int, error: tuple[str, str] | None, usage: str = "") -> int:
    def build() -> str:
        return json_text(
            envelope(inv.command, exit_code=code, commit=inv.commit, sections=inv.sections, error=error, base=inv.base)
        )

    try:
        text = build()
    except (TypeError, ValueError, RecursionError) as exc:  # section data that JSON cannot carry is a bug
        code, error, inv.sections = EXIT_INTERNAL, ("internal", f"envelope: {type(exc).__name__}: {exc}"), []
        text = build()
    message = _write_json_out(inv, text)
    if message is not None:
        if code in (EXIT_USAGE, EXIT_INTERNAL):  # the first error stays the one reported
            print(f"muster: error: {message}", file=sys.stderr)
        else:
            code, error, usage = EXIT_USAGE, ("usage", message), ""
            text = build()
    if code in (EXIT_USAGE, EXIT_INTERNAL):
        _remove_failed_outputs(inv)
        _report_failure(inv, error[1], usage)
    if inv.fmt == "json":
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError) as exc:  # a closed pipe or stream: the run fails, and the files say so
            code, error = EXIT_INTERNAL, ("internal", f"cannot write stdout: {getattr(exc, 'strerror', None) or exc}")
            message = _write_json_out(inv, build())
            if message is not None:
                print(f"muster: error: {message}", file=sys.stderr)
            _remove_failed_outputs(inv)
            _report_failure(inv, error[1], "")  # in JSON mode it writes nothing to stdout
    return code


def _write_json_out(inv: Invocation, text: str) -> str | None:
    """Write the envelope to --json-out, if there is one; the error message when it cannot be written."""
    if not inv.json_out:
        return None
    try:
        write_json(Path(inv.json_out), text)
    except OSError as exc:
        return f"cannot write --json-out {inv.json_out}: {exc.strerror or exc}"
    return None


def _main(argv: list[str]) -> int:
    words = _options(argv)
    inv = Invocation(
        argv=argv,
        command=_scan_command(words),
        fmt="json" if _scan_option(words, "--format") == "json" else "text",
    )
    usage = ""
    try:
        code, error = _run(inv), None
    except FatalError as exc:
        code, error, usage = EXIT_USAGE, (exc.kind, str(exc)), getattr(exc, "usage", "")
    except SystemExit as exc:  # argparse --help
        if exc.code in (0, None):
            return EXIT_PASS
        code, error = EXIT_USAGE, ("usage", f"bad arguments (exit {exc.code})")
    except Exception as exc:  # noqa: BLE001 - a bug still ends with an envelope and exit 3
        traceback.print_exc()
        code, error = EXIT_INTERNAL, ("internal", f"{type(exc).__name__}: {exc}")
    return _finish(inv, code, error, usage)


def main(argv: list[str] | None = None) -> int:
    """Run muster; returns the exit code and never raises an Exception."""
    try:
        _reconfigure_utf8()
        return _main(list(sys.argv[1:] if argv is None else argv))
    except Exception:  # noqa: BLE001 - the output itself failed (a closed stdout, say)
        return EXIT_INTERNAL


def _reconfigure_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _summary_crash_line(argv: list[str], line: str) -> None:
    """Best effort: the summary says the command failed (--summary-file, else $GITHUB_STEP_SUMMARY with --github)."""
    try:
        summary = None
        for i, arg in enumerate(argv):
            if arg == "--summary-file" and i + 1 < len(argv):
                summary = argv[i + 1]
            elif arg.startswith("--summary-file="):
                summary = arg.split("=", 1)[1]
        if not summary and "--github" in argv:
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            write_markdown(Path(summary), line + "\n\n")
    except Exception:  # noqa: BLE001 - the crash report itself must never raise
        pass
