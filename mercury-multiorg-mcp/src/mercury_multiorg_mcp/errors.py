"""Error types and secret redaction.

Every error that can leave this package passes through :func:`redact` so that
no bearer token (or the ``Authorization`` header carrying it) ever reaches a
log line, an MCP tool result, or an exception repr.

Since v0.1.1 every message is also *fixed text*: an HTTP status, an endpoint
label with ids replaced by ``{id}``, and a short hint chosen from a table.
No upstream response body, header value, or caller-supplied argument is ever
interpolated into an error, so redaction is a second line of defence rather
than the only one.
"""

from __future__ import annotations

import re

# Matches an Authorization header value wherever it appears in free text,
# e.g. inside an httpx request repr: "Authorization: Bearer secret-token:..."
_AUTH_HEADER_RE = re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?)(bearer\s+)?([^\s'\",}]+)", re.IGNORECASE)
# Mercury tokens carry a documented "secret-token:" prefix (see the
# bearerAuth securityScheme in the live OpenAPI). Scrub anything shaped like
# that even if it shows up outside a header.
_TOKEN_SHAPE_RE = re.compile(r"secret-token:[A-Za-z0-9_\-]+")

REDACTED = "[REDACTED]"

# Documented prefix of every Mercury API token (production and sandbox).
TOKEN_PREFIX = "secret-token:"


def token_suffix(token: str | None) -> str:
    """Return the last four characters of a token, never more."""
    if not token:
        return ""
    return token[-4:]


def redact(text: str, *secrets: str | None) -> str:
    """Scrub Authorization headers, token-shaped strings, and known secrets from ``text``.

    ``secrets`` are the *known* token values in play (the entity's resolved
    token at request time); each is replaced wherever it appears, whatever
    its shape. Values shorter than 8 characters are ignored so a short
    secret cannot mangle unrelated text.
    """
    if not text:
        return text
    out = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2) or ''}{REDACTED}", text)
    out = _TOKEN_SHAPE_RE.sub(REDACTED, out)
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, REDACTED)
    return out


class MercuryMultiOrgError(Exception):
    """Base class for every error raised by this package."""


class RegistryError(MercuryMultiOrgError):
    """The entity registry file is missing, unreadable, or fails validation."""


class UnknownEntityError(MercuryMultiOrgError):
    """A tool was called with an entity key that is not in the registry.

    The offending key is kept on ``entity`` for callers but is never echoed
    into the message: tool errors carry no caller input.
    """

    def __init__(self, entity: str, known: list[str]) -> None:
        self.entity = entity
        self.known = known
        super().__init__(f"Unknown entity. Configured entities: {', '.join(known) or '(none)'}")


class MissingTokenError(MercuryMultiOrgError):
    """The env var named by a registry entry is unset or blank.

    This is a per-entity condition: other entities keep working.
    """

    def __init__(self, entity: str, token_env: str) -> None:
        self.entity = entity
        self.token_env = token_env
        super().__init__(
            f"Entity {entity!r} has no API token: environment variable {token_env} is unset or empty. "
            "Set it in the environment that launches the server (never in this repo)."
        )


class MercuryAPIError(MercuryMultiOrgError):
    """Mercury returned an error status, the HTTP call itself failed, or a walk could not complete.

    ``message`` is always redacted before it is stored, and by construction
    (see :mod:`mercury_multiorg_mcp.client`) never contains upstream body
    text, header values, or caller-supplied ids.
    """

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(redact(message))


class IncompletePaginationError(MercuryAPIError):
    """A paginated walk stalled while the API still advertised more pages.

    Raised instead of returning a partial list as if it were complete: a
    page that repeats already-seen ids, yields no usable cursor, or does not
    advance the cursor while ``nextPage`` (or a non-null treasury ``cursor``)
    is present. Totals built on such a walk would silently understate.
    """
