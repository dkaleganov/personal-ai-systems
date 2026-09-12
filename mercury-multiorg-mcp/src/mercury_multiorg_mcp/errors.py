"""Error types and secret redaction.

Every error that can leave this package passes through :func:`redact` so that
no bearer token (or the ``Authorization`` header carrying it) ever reaches a
log line, an MCP tool result, or an exception repr.
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


def token_suffix(token: str | None) -> str:
    """Return the last four characters of a token, never more."""
    if not token:
        return ""
    return token[-4:]


def redact(text: str, *secrets: str | None) -> str:
    """Scrub Authorization headers, token-shaped strings, and known secrets from ``text``."""
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
    """A tool was called with an entity key that is not in the registry."""

    def __init__(self, entity: str, known: list[str]) -> None:
        self.entity = entity
        self.known = known
        super().__init__(f"Unknown entity {entity!r}. Configured entities: {', '.join(known) or '(none)'}")


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
    """Mercury returned an error status, or the HTTP call itself failed.

    ``message`` is always redacted before it is stored.
    """

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(redact(message))
