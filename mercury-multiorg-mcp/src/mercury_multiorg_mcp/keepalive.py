"""Keepalive CLI: one authenticated GET per configured organization.

Mercury deletes API tokens that go unused for any 45-day period and
downgrades permissions unused for 45 days (docs.mercury.com
``/docs/api-token-security-policies``, 2026-09-12). Run this on a schedule
(weekly is plenty; see ``docs/keepalive.md``) to keep every token alive.

This is deliberately NOT an MCP tool: it exists for cron/launchd, not for a
model. Same registry / ``--entities`` / ``--env-file`` / ``--api-base`` /
``--allow-custom-api-base`` conventions as the server. Prints one line per
entity::

    <UTC timestamp> OK|FAIL <entity> [HTTP <status> | reason]

Never any token text. Exit status 0 when every entity succeeded, 1 when any
entity failed or when no entity has a token configured, 2 on a
configuration error (one line on stderr, never a traceback).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timezone

import anyio

from . import __version__
from .client import MercuryClient
from .errors import MercuryAPIError, MercuryMultiOrgError, MissingTokenError, redact
from .registry import Registry
from .server import install_redacting_excepthooks, install_redacting_logging, load_startup_config, quiet_http_loggers

ClientFactory = Callable[[str, str], MercuryClient]  # (token, api_base)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mercury-multiorg-mcp-keepalive",
        description="Touch every configured Mercury org once so its API token is not deleted for inactivity.",
    )
    p.add_argument("--entities", metavar="PATH", help="Entity registry YAML. Falls back to $MERCURY_ENTITIES_FILE.")
    p.add_argument(
        "--env-file",
        metavar="PATH",
        help="Load this dotenv file before resolving tokens (existing env vars win). No implicit .env is read.",
    )
    p.add_argument(
        "--api-base",
        metavar="URL",
        help=(
            "Mercury API host: https://api.mercury.com (default), https://api-sandbox.mercury.com, or a loopback mock. "
            "Falls back to $MERCURY_API_BASE. Any other host needs --allow-custom-api-base."
        ),
    )
    p.add_argument(
        "--allow-custom-api-base",
        action="store_true",
        help="Permit an --api-base / $MERCURY_API_BASE host other than Mercury production, sandbox, or loopback.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str, limit: int = 160) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _touch(entity: str, registry: Registry, make_client: ClientFactory, api_base: str) -> tuple[bool, str]:
    """Return (ok, detail) for one entity. ``detail`` is safe to print."""
    try:
        token = registry.resolve_token(entity)
    except MissingTokenError as exc:
        return False, f"no token configured ({exc.token_env} unset)"
    except MercuryMultiOrgError as exc:
        return False, _one_line(str(exc))
    try:
        async with make_client(token, api_base) as client:
            status = await client.ping()
    except MercuryAPIError as exc:
        if exc.status_code is not None:
            return False, f"HTTP {exc.status_code}"
        return False, _one_line(redact(str(exc), token))
    except MercuryMultiOrgError as exc:
        return False, _one_line(redact(str(exc), token))
    return True, f"HTTP {status}"


async def _run(registry: Registry, make_client: ClientFactory, api_base: str, out) -> int:
    configured = [k for k in registry.keys if registry.token_status(k)]
    if not configured:
        print(f"{_now()} FAIL (no entity has a token configured; nothing to keep alive)", file=out)
        return EXIT_FAIL
    failures = 0
    for key in registry.keys:
        ok, detail = await _touch(key, registry, make_client, api_base)
        print(f"{_now()} {'OK' if ok else 'FAIL'} {key} {detail}", file=out)
        if not ok:
            failures += 1
    return EXIT_FAIL if failures else EXIT_OK


def main(argv: list[str] | None = None, *, client_factory: ClientFactory | None = None) -> int:
    """Console entry point. ``client_factory`` lets tests inject a mock transport."""
    args = _parse_args(argv)
    install_redacting_excepthooks()
    loaded = load_startup_config(args, prog="mercury-multiorg-mcp-keepalive")
    if isinstance(loaded, int):
        return EXIT_CONFIG
    registry, api_base = loaded
    install_redacting_logging()
    quiet_http_loggers()

    def _default_factory(token: str, base: str) -> MercuryClient:
        return MercuryClient(token, api_base=base)

    return anyio.run(_run, registry, client_factory or _default_factory, api_base, sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
