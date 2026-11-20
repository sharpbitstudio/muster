"""Entry point for `python -I src` and for the zipapp: the folder, or the archive, is sys.path[0].

The version check comes before any import that needs Python 3.11, so this file must stay valid
syntax for older versions (3.8 and newer). On an older Python it still keeps the contract of the
command line: the JSON envelope goes to --json-out and, with --format json, to stdout; text mode
prints an error annotation; the exit code is 2.
"""

import json
import os
import re
import sys

MINIMUM = (3, 11)
# What argparse takes as a negative number, and so as a value rather than an option.
NEGATIVE_NUMBER = re.compile(r"-\d+$|-\d*\.\d+$")
# The options whose next word is their value, never the command.
VALUE_OPTIONS = ("--repo", "--config", "--format", "--json-out", "--summary-file", "--strict-only", "--zones-out")


def _is_value(arg):
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
    """The first word that is not an option or the value of an option that takes one."""
    skip = False
    for arg in words:
        if skip:
            skip = False
        elif arg in VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def refuse_old_python(argv, version):
    """Report that this Python is too old, the way the command line reports a usage error; returns
    the exit code, 2."""
    words = argv[: argv.index("--")] if "--" in argv else argv
    message = "Python %d.%d or newer is needed, this is %d.%d" % (MINIMUM + tuple(version[:2]))
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
        "exit_code": 2,
        "error": {"kind": "usage", "message": message},
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
        sys.stdout.write("::error title=muster::%s\n" % message)
    sys.stderr.write("muster: error: %s\n" % message)
    return 2


if sys.version_info < MINIMUM:
    raise SystemExit(refuse_old_python(sys.argv[1:], sys.version_info))

from muster.cli import main  # noqa: E402 - only after the version check

raise SystemExit(main())
