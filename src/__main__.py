"""Entry point for `python -I src` and for the zipapp: the folder, or the archive, is sys.path[0].

The version check comes before any import that needs Python 3.11, so this file must stay valid
syntax for older versions (3.8 and newer). When the command line cannot run, this file still keeps
its contract: the JSON envelope goes to --json-out and, with --format json, to stdout; text mode
prints an error annotation. On an older Python the exit code is 2; when the command line cannot be
imported (a broken install, say), it is 3 and the traceback goes to stderr.
"""

import json
import os
import re
import sys
import traceback

MINIMUM = (3, 11)
# What argparse takes as a negative number, and so as a value rather than an option.
NEGATIVE_NUMBER = re.compile(r"-\d+$|-\d*\.\d+$")
# The commands and every option string of the command line, at every level (tests keep them equal
# to those of muster.cli).
COMMANDS = ("version", "policy", "coverage")
OPTIONS = (
    "-h", "--help", "--repo", "--config", "--format", "--json-out", "--strict", "--strict-only",
    "--fail-on-error", "--summary-file", "--github", "--snapshot", "--zones-out", "--base", "--diff-file",
    "--no-freshness", "--today",
)
# The options before the command word; each takes a value.
GLOBAL_OPTIONS = ("--repo", "--config")


def _is_value(arg):
    """Whether argparse takes `arg`, after an option that needs a value, as that value, or else as a
    positional argument. An option string, alone or with `=VALUE`, never is."""
    if arg in OPTIONS or ("=" in arg and arg.split("=", 1)[0] in OPTIONS):
        return False
    return not arg.startswith("-") or arg == "-" or bool(NEGATIVE_NUMBER.match(arg)) or " " in arg


def _scan_option(words, option):
    """The value of the last `option VALUE` or `option=VALUE`, as argparse would take it."""
    value = None
    for i, arg in enumerate(words):
        if arg == option:
            following = words[i + 1] if i + 1 < len(words) else None
            value = following if following is not None and _is_value(following) else None
        elif arg.startswith(option + "="):
            value = arg.split("=", 1)[1]
    return value


def _scan_command(words):
    """The command named in words, when it is a known one: the first positional argument after the
    global options and their values."""
    skip = False
    for i, arg in enumerate(words):
        if skip:
            skip = False
        elif arg in GLOBAL_OPTIONS:
            skip = i + 1 < len(words) and _is_value(words[i + 1])
        elif _is_value(arg):
            return arg if arg in COMMANDS else None
    return None


def report_error(argv, code, kind, message):
    """Report an error the way the command line reports one, without the command line; returns the
    exit code."""
    words = argv[: argv.index("--")] if "--" in argv else argv
    try:
        from muster import __version__  # muster/__init__.py imports nothing
    except Exception:  # noqa: BLE001 - the version is only for the envelope
        __version__ = None
    document = {
        "schema_version": 1,
        "tool": {"name": "muster", "version": __version__},
        "command": _scan_command(words),
        "commit": None,
        "base": None,
        "verdict": "error",
        "exit_code": code,
        "error": {"kind": kind, "message": message},
        "sections": [],
        "flags": [],
    }
    text = json.dumps(document, indent=2, ensure_ascii=True) + "\n"
    json_out = _scan_option(words, "--json-out")
    if json_out not in (None, "", "-"):
        try:
            folder = os.path.dirname(json_out)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(json_out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            sys.stderr.write("muster: error: cannot write --json-out %s: %s\n" % (json_out, exc.strerror or exc))
    if _scan_option(words, "--format") == "json":
        sys.stdout.write(text)
    else:
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        sys.stdout.write("::error title=muster::%s\n" % escaped)
    sys.stderr.write("muster: error: %s\n" % message)
    return code


def refuse_old_python(argv, version):
    """Report that this Python is too old, the way the command line reports a usage error; returns
    the exit code, 2."""
    message = "Python %d.%d or newer is needed, this is %d.%d" % (MINIMUM + tuple(version[:2]))
    return report_error(argv, 2, "usage", message)


def run(argv):
    """Run the command line; returns the exit code."""
    if sys.version_info < MINIMUM:
        return refuse_old_python(argv, sys.version_info)
    try:
        from muster.cli import main  # only after the version check
    except Exception as exc:  # noqa: BLE001 - a broken install still ends with an envelope and exit 3
        traceback.print_exc()
        return report_error(argv, 3, "internal", "%s: %s" % (type(exc).__name__, exc))
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
