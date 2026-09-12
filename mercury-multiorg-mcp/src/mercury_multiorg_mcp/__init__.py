"""Read-only MCP server exposing multiple Mercury organizations to one AI session.

Every tool takes an explicit ``entity`` key (no defaults) and every result
carries that key back. The server never opens a network listener; transport
is stdio only. No state-changing Mercury endpoint has a client method.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mercury-multiorg-mcp")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
