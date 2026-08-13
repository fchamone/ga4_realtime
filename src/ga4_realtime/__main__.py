"""Entry point for ``python -m ga4_realtime``.

Documented as equivalent to the ``ga4-realtime`` console script throughout,
which is why the root ``ga4_realtime.py`` script was deleted rather than kept
as a shim: run from the repo root it would sit on ``sys.path[0]`` and shadow
the installed package here.
"""

import sys

from .cli import main

sys.exit(main())
