"""The command line: global options, the commands, exit codes and the JSON envelope on every exit.

Exit codes: 0 pass (violations and input errors in warn-only mode); 1 a violation in a strict
section, or an input error under strict or --fail-on-error; 2 a usage or configuration error,
always; 3 an internal error (a bug), always.

--json-out and --format are read from argv before argparse runs, so that the envelope is written,
and printed with --format json, on every exit, a usage error and a bug included.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from muster import __version__
from muster.config import DEFAULT_CONFIG, SECTIONS, Config, config_path, load
from muster.gitio import find_repo_root, git
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


@dataclass(frozen=True)
class Analysis:
    """An analysis command: `run` judges one section into the Report; `arguments` adds its own options."""

    run: Callable[[argparse.Namespace, Config, Report], None]
    help: str
    arguments: Callable[[argparse.ArgumentParser], None] = lambda parser: None


ANALYSES: dict[str, Analysis] = {}


def _section_list(text: str) -> frozenset[str]:
    names = [name.strip() for name in text.split(",")]
    for name in names:
        if name not in SECTIONS:
            raise argparse.ArgumentTypeError(f"unknown section '{name}' (choose from {', '.join(SECTIONS)})")
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
        help=f"only these sections are strict ({', '.join(SECTIONS)})",
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


def _scan_option(argv: list[str], option: str) -> str | None:
    """The value of the last `option VALUE` or `option=VALUE` in argv, as argparse would take it."""
    value = None
    for i, arg in enumerate(argv):
        if arg == option:
            following = argv[i + 1] if i + 1 < len(argv) else None
            value = following if following is not None and not following.startswith("-") else None
        elif arg.startswith(option + "="):
            value = arg.split("=", 1)[1]
    return value


def _scan_command(argv: list[str]) -> str | None:
    """The command named in argv, when it is a known one: the first word after the global options."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg in ("--repo", "--config"):
            skip = True
        elif not arg.startswith("-"):
            return arg if arg == "version" or arg in ANALYSES else None
    return None


def _repo_root(explicit: str | None) -> Path:
    try:
        return find_repo_root(explicit)
    except OSError:  # no git on PATH
        return Path(explicit or ".").resolve()


def _head_commit(root: Path) -> str | None:
    try:
        return git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
    except OSError:  # no git on PATH, or no such folder
        return None


def _same_file(first: Path, second: Path) -> bool:
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


@dataclass
class Invocation:
    """What one run has learned so far; the envelope is built from it on every exit."""

    argv: list[str]
    command: str | None
    fmt: str
    json_out: str | None
    commit: str | None = None
    sections: list[Report] = field(default_factory=list)


def _prepare_json_out(inv: Invocation) -> None:
    """Refuse a --json-out that is the config file; else remove the old envelope before any check."""
    out = Path(inv.json_out)
    root = _repo_root(_scan_option(inv.argv, "--repo"))
    profile = config_path(root, _scan_option(inv.argv, "--config") or DEFAULT_CONFIG)
    if _same_file(out, profile):
        inv.json_out = None  # nothing is written or deleted
        raise UsageError(f"--json-out {out} is the config file")
    try:
        out.unlink(missing_ok=True)
    except OSError as exc:
        inv.json_out = None
        raise UsageError(f"cannot write --json-out {out}: {exc.strerror or exc}") from None


def run_version(args: argparse.Namespace, inv: Invocation) -> int:
    if inv.fmt == "text":
        print(f"muster {__version__}")
    return EXIT_PASS


def _write_summary(args: argparse.Namespace, cfg: Config, rep: Report) -> None:
    """Append to --summary-file (with --github: $GITHUB_STEP_SUMMARY); else overwrite
    <[output].dir>/<command>.md when the profile sets that folder; else write nothing."""
    summary = args.summary_file or (os.environ.get("GITHUB_STEP_SUMMARY") if args.github else None)
    if summary:
        target, append = Path(summary), True
    elif cfg.output_dir is not None:
        target, append = cfg.root / cfg.output_dir / f"{rep.name}.md", False
    else:
        return
    try:
        write_markdown(target, "\n".join(rep.md).rstrip("\n") + "\n\n", append=append)
    except OSError as exc:
        raise UsageError(f"cannot write the summary {target}: {exc.strerror or exc}") from None


def run_analysis(args: argparse.Namespace, inv: Invocation, analysis: Analysis) -> int:
    root = _repo_root(args.repo)
    inv.commit = _head_commit(root)
    cfg = load(root, args.config)
    strict = args.strict or (args.strict_only is not None and args.command in args.strict_only)
    rep = Report(args.command, strict=strict, fail_on_error=args.fail_on_error)
    analysis.run(args, cfg, rep)
    inv.sections.append(rep)
    _write_summary(args, cfg, rep)
    if inv.fmt == "text":
        for line in rep.annotations:
            print(line)
        print(one_line(rep.verdict))
    return EXIT_VIOLATION if (rep.violation and rep.strict) or (rep.error and rep.fail_on_error) else EXIT_PASS


def _run(inv: Invocation) -> int:
    if inv.json_out:
        _prepare_json_out(inv)
    args = build_parser().parse_args(inv.argv)
    inv.command, inv.fmt = args.command, args.format
    if args.command == "version":
        return run_version(args, inv)
    return run_analysis(args, inv, ANALYSES[args.command])


def _report_failure(inv: Invocation, message: str, usage: str) -> None:
    """Exit 2 or 3: an error annotation (text mode), the message on stderr and a line in the summary."""
    name = f"muster {inv.command}" if inv.command else "muster"
    if inv.fmt == "text":
        print(f"::error title={escape_property(name)}::{escape_data(message)}")
    if usage:
        sys.stderr.write(usage)
    print(f"{name}: error: {message}", file=sys.stderr)
    _summary_crash_line(inv.argv, f"{name}: error: {one_line(message)}")


def _finish(inv: Invocation, code: int, error: tuple[str, str] | None, usage: str = "") -> int:
    def build() -> str:
        return json_text(envelope(inv.command, exit_code=code, commit=inv.commit, sections=inv.sections, error=error))

    try:
        text = build()
    except (TypeError, ValueError) as exc:  # section data that JSON cannot carry is a bug
        code, error, inv.sections = EXIT_INTERNAL, ("internal", f"envelope: {type(exc).__name__}: {exc}"), []
        text = build()
    if inv.json_out:
        try:
            write_json(Path(inv.json_out), text)
        except OSError as exc:
            message = f"cannot write --json-out {inv.json_out}: {exc.strerror or exc}"
            if code in (EXIT_USAGE, EXIT_INTERNAL):  # the first error stays the one reported
                print(f"muster: error: {message}", file=sys.stderr)
            else:
                code, error, usage = EXIT_USAGE, ("usage", message), ""
                text = build()
    if code in (EXIT_USAGE, EXIT_INTERNAL):
        _report_failure(inv, error[1], usage)
    if inv.fmt == "json":
        sys.stdout.write(text)
    return code


def _main(argv: list[str]) -> int:
    inv = Invocation(
        argv=argv,
        command=_scan_command(argv),
        fmt="json" if _scan_option(argv, "--format") == "json" else "text",
        json_out=_scan_option(argv, "--json-out"),
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
