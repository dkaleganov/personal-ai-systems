"""Keepalive CLI: one authenticated GET per configured organization.

Mercury deletes API tokens that go unused for any 45-day period and
downgrades permissions unused for 45 days (docs.mercury.com
``/docs/api-token-security-policies``, 2026-09-12). Run this on a schedule
(weekly is plenty; see ``docs/keepalive.md``) to keep every token alive.

This is deliberately NOT an MCP tool: it exists for cron/launchd, not for a
model. Same registry / ``--entities`` / ``--env-file`` / ``--api-base``
conventions as the server. Prints one line per entity::

    <UTC timestamp> OK|FAIL <entity> [HTTP <status> | reason]

Never any token text. Exit status 0 when every entity succeeded, 1 when any
entity failed or when no entity has a token configured, 2 on a
configuration error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime, timezone

import anyio
from dotenv import load_dotenv

from . import __version__
from .client import MercuryClient, api_base_from_env, validate_api_base
from .errors import MercuryAPIError, MercuryMultiOrgError, MissingTokenError, RegistryError, redact
from .registry import Registry
from .server import install_redacting_excepthooks, install_redacting_logging, quiet_http_loggers

ClientFactory = Callable[[str], MercuryClient]

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
        help="Mercury API host, https:// only. Falls back to $MERCURY_API_BASE, then https://api.mercury.com.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str, limit: int = 160) -> str:
    text = " ".join(redact(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _touch(entity: str, registry: Registry, make_client: ClientFactory) -> tuple[bool, str]:
    """Return (ok, detail) for one entity. ``detail`` is safe to print."""
    try:
        token = registry.resolve_token(entity)
    except MissingTokenError as exc:
        return False, f"no token configured ({exc.token_env} unset)"
    except MercuryMultiOrgError as exc:
        return False, _one_line(str(exc))
    try:
        async with make_client(token) as client:
            status = await client.ping()
    except MercuryAPIError as exc:
        if exc.status_code is not None:
            return False, f"HTTP {exc.status_code}"
        return False, _one_line(str(exc))
    except MercuryMultiOrgError as exc:
        return False, _one_line(str(exc))
    return True, f"HTTP {status}"


async def _run(registry: Registry, make_client: ClientFactory, out) -> int:
    configured = [k for k in registry.keys if registry.token_status(k)]
    if not configured:
        print(f"{_now()} FAIL (no entity has a token configured; nothing to keep alive)", file=out)
        return EXIT_FAIL
    failures = 0
    for key in registry.keys:
        ok, detail = await _touch(key, registry, make_client)
        print(f"{_now()} {'OK' if ok else 'FAIL'} {key} {detail}", file=out)
        if not ok:
            failures += 1
    return EXIT_FAIL if failures else EXIT_OK


def main(argv: list[str] | None = None, *, client_factory: ClientFactory | None = None) -> int:
    """Console entry point. ``client_factory`` lets tests inject a mock transport."""
    args = _parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    try:
        path = Registry.resolve_path(args.entities)
        registry = Registry.from_path(path)
        api_base = validate_api_base(args.api_base or api_base_from_env())
    except (RegistryError, ValueError) as exc:
        print(f"mercury-multiorg-mcp-keepalive: {redact(str(exc))}", file=sys.stderr)
        return EXIT_CONFIG
    install_redacting_logging()
    install_redacting_excepthooks()
    quiet_http_loggers()

    def _default_factory(token: str) -> MercuryClient:
        return MercuryClient(token, api_base=api_base)

    return anyio.run(_run, registry, client_factory or _default_factory, sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
