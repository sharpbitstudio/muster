"""Entry point for `python -I src` and for the zipapp: the folder, or the archive, is sys.path[0].

The version check comes before any import that needs Python 3.11, so this file must stay valid
syntax for older versions.
"""

import sys

if sys.version_info < (3, 11):
    sys.stderr.write("muster: error: Python 3.11 or newer is needed, this is %d.%d\n" % sys.version_info[:2])
    raise SystemExit(2)

from muster.cli import main  # noqa: E402 - only after the version check

raise SystemExit(main())
