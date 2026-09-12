"""Allow `python -m mercury_multiorg_mcp`."""

import sys

from .server import main

sys.exit(main())
