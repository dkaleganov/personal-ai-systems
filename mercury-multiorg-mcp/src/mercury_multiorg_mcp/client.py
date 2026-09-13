"""Thin, read-only async client for the Mercury API (one instance per token).

Validated against the live OpenAPI at docs.mercury.com on 2026-09-11
(``/reference/getaccounts.md``, ``/reference/listtransactions.md``,
``/reference/listaccounttransactions.md``, ``/docs/getting-started.md``,
``/docs/api-token-security-policies.md``), on 2026-09-12 for Phase 2
(``/reference/getrecipients.md``, ``/reference/getrecipient.md``,
``/reference/listrecipientsattachments.md``), on 2026-09-12 for Phase 3
(``getorganization``, ``getaccountstatements``, ``getstatementpdf``,
``gettreasury``, ``gettreasurytransactions``, ``gettreasurystatements``,
``listcredit``, ``listcards``, ``getcard``, ``listcategories``,
``listmerchants``, ``listcustomers``, ``listinvoices``, ``getinvoice``,
``getinvoicepdf``, ``listinvoiceattachments``, ``getusers``, ``getevents``,
``getwebhooks``), and again on 2026-09-13 for v0.1.1 (the PDF endpoints
document ``application/pdf`` only; ``getevents`` and
``gettreasurytransactions`` still name no sort key for ``order``). Notes
where the live docs differ from the build brief are marked ``DOCS:`` below.

Only ``GET`` is implemented. There is no method for any endpoint that can
change state, and :meth:`MercuryClient._fetch` is the single choke point for
every request, so adding a write path would have to be deliberate.

Error boundary (v0.1.1): every :class:`MercuryAPIError` raised here carries
fixed text only: an HTTP status, an endpoint label with ids replaced by
``{id}``, and a hint chosen from a table. Upstream response bodies, header
values (including ``Content-Type``), httpx exception reprs, and
caller-supplied ids never reach a message.

Byte limits (v0.1.1): every request declines compression
(``Accept-Encoding: identity``) and a response that arrives with any other
``Content-Encoding`` is refused before its body is read, so the caps below
are enforced on raw wire bytes while streaming: ``MAX_DOWNLOAD_BYTES`` for
PDFs, ``MAX_JSON_BYTES`` for JSON.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from .errors import IncompletePaginationError, MercuryAPIError, redact, token_suffix

DEFAULT_API_BASE = "https://api.mercury.com"
SANDBOX_API_BASE = "https://api-sandbox.mercury.com"
API_BASE_ENV = "MERCURY_API_BASE"
# DOCS: sandbox base is https://api-sandbox.mercury.com (same /api/v1 prefix).
# Users point MERCURY_API_BASE there with a sandbox-created token.
API_PREFIX = "/api/v1"

# DOCS: `limit` on /accounts and /transactions is 1..1000, default 1000.
MAX_PAGE_SIZE = 1000
# Hard stop for the pagination loops so a misbehaving cursor can't spin forever.
MAX_PAGES = 200

_RETRY_STATUSES = frozenset({429, 502, 503, 504})

# Largest binary (statement / invoice PDF) the client will buffer, in wire bytes.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
# Largest JSON body the client will buffer, in wire bytes.
MAX_JSON_BYTES = 32 * 1024 * 1024
# Bytes at the end of a PDF body (after trailing PDF whitespace is ignored) searched for the %%EOF marker.
PDF_EOF_WINDOW = 2048
# PDF whitespace characters (ISO 32000-1 table 1): NUL, TAB, LF, FF, CR, SPACE.
_PDF_WHITESPACE = b"\x00\t\n\x0c\r "
# Content types accepted for a PDF download (parameters such as charset ignored).
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/octet-stream"})

# Mercury ids are UUIDs. Anything that could change the request path (a
# slash, a dot segment, a query) is rejected before it reaches the URL.
_PATH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Fixed hints by HTTP status. Nothing from the response is ever quoted.
_STATUS_HINTS: dict[int, str] = {
    400: "request rejected by Mercury (check the arguments)",
    401: "token rejected (the entity's token is invalid, deleted, or for another environment)",
    403: "token lacks permission for this endpoint",
    404: "not found",
    429: "rate limited (retries exhausted)",
}

# Path segments that are endpoint words; every other segment is an id and is
# shown as `{id}` in error text.
_ENDPOINT_WORDS = frozenset(
    {
        "accounts", "account", "transactions", "recipients", "attachments", "organization",
        "statements", "pdf", "treasury", "credit", "cards", "categories", "merchants",
        "ar", "customers", "invoices", "users", "events", "webhooks",
    }
)


def endpoint_label(path: str) -> str:
    """``GET /account/{id}/statements`` for ``/account/<uuid>/statements``: ids never appear in error text."""
    parts = [seg if seg in _ENDPOINT_WORDS else "{id}" for seg in path.strip("/").split("/") if seg != ""]
    return "GET /" + "/".join(parts)


def validate_path_id(value: str, label: str) -> str:
    """Return ``value`` if it is a safe single path segment, else raise ``ValueError``.

    The message names the parameter, never the value (an argument could be
    anything, including a secret pasted by mistake).
    """
    if not isinstance(value, str) or not _PATH_ID_RE.fullmatch(value):  # fullmatch: `$` would allow a trailing newline
        raise ValueError(f"{label}: invalid id format (expected 1-64 characters: letters, digits, '-' or '_')")
    return value


def validate_pdf_bytes(body: bytes, label: str, *, status_code: int | None = None, path: str | None = None) -> None:
    """Envelope check: the ``%PDF-`` header and a ``%%EOF`` marker within the last ``PDF_EOF_WINDOW`` bytes.

    Trailing PDF whitespace (NUL, TAB, LF, FF, CR, SPACE) is ignored for the
    marker search only; the caller keeps and returns the original bytes.
    This is not PDF parsing: it rejects HTML error pages, empty bodies, and
    truncated downloads without ever quoting the body, and nothing more.
    """
    if not body.startswith(b"%PDF-"):
        raise MercuryAPIError(f"{label} did not return a PDF (missing %PDF- header)", status_code=status_code, path=path)
    if b"%%EOF" not in body.rstrip(_PDF_WHITESPACE)[-PDF_EOF_WINDOW:]:
        raise MercuryAPIError(
            f"{label} did not return a complete PDF (no %%EOF marker in the last {PDF_EOF_WINDOW} bytes)",
            status_code=status_code,
            path=path,
        )


class RowList(list):
    """The rows of a paginated walk plus how many exact duplicate rows were dropped along the way.

    A plain ``list`` for every caller; ``duplicates_dropped`` counts rows whose
    id had already been accepted (within a page or across pages) and whose
    content was identical (B2). A repeated id with *different* content is an
    error, never a silent choice.
    """

    def __init__(self, rows: list[dict[str, Any]] = (), *, duplicates_dropped: int = 0) -> None:  # type: ignore[assignment]
        super().__init__(rows)
        self.duplicates_dropped = duplicates_dropped


def _accept_rows(
    items: list[Any], id_key: str, seen: dict[str, dict[str, Any]], label: str, path: str
) -> tuple[list[dict[str, Any]], int]:
    """Filter one page: dict rows only, ids deduplicated as each row is accepted, conflicts rejected.

    Rows without a usable (non-empty string) id are kept as they come; they
    cannot be deduplicated and cannot form a cursor.
    """
    fresh: list[dict[str, Any]] = []
    dropped = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        row_id = it.get(id_key)
        if not isinstance(row_id, str) or not row_id:
            fresh.append(it)
            continue
        previous = seen.get(row_id)
        if previous is None:
            seen[row_id] = it
            fresh.append(it)
        elif previous == it:
            dropped += 1
        else:
            raise MercuryAPIError(
                f"{label}: conflicting duplicate rows (the same id appeared more than once with different content); "
                "the result cannot be trusted",
                path=path,
            )
    return fresh, dropped


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_HOSTS = frozenset({"api.mercury.com", "api-sandbox.mercury.com"})


def api_base_from_env() -> str:
    """Resolve the API host (not including /api/v1) from ``MERCURY_API_BASE``."""
    return os.environ.get(API_BASE_ENV, "").strip() or DEFAULT_API_BASE


def validate_api_base(url: str, *, allow_custom: bool = False) -> str:
    """Validate the API host and return it with any trailing slash removed.

    Always required: a bare ``https://`` host (``http://`` only on loopback,
    for mocks), no credentials, path, query, or fragment. Unless
    ``allow_custom`` is true the host must also be Mercury's production or
    sandbox host, or loopback: a value inherited from the environment can
    then never point the bearer token at another destination. Raises
    ``ValueError`` with a message safe to print: the rejected value is never
    echoed, since a mistyped URL can carry a credential.
    """
    candidate = (url or "").strip().rstrip("/")
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018  (raises ValueError for a non-numeric port)
    except ValueError:
        raise ValueError(f"api_base must be a bare https:// host such as {DEFAULT_API_BASE}; the value could not be parsed") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError(f"api_base must be a bare https:// host such as {DEFAULT_API_BASE} with no credentials in the URL")
    if not host or parts.scheme not in ("http", "https") or parts.path or parts.query or parts.fragment:
        raise ValueError(
            f"api_base must be a bare https:// host such as {DEFAULT_API_BASE} "
            f"(scheme and host only; no path, query, or fragment); got scheme {parts.scheme or 'none'!r}, host {host or 'none'!r}"
        )
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ValueError(f"api_base must use https:// (plain http is only allowed for localhost/127.0.0.1); got host {host!r}")
    if not allow_custom and host not in _ALLOWED_HOSTS and host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"api_base host {host!r} is not {DEFAULT_API_BASE}, {SANDBOX_API_BASE}, or loopback; "
            "pass --allow-custom-api-base to use another host deliberately"
        )
    return candidate


class MercuryClient:
    """Async, read-only Mercury API client bound to a single organization token.

    Args:
        token: The org's API token (documented shape ``secret-token:...``).
            Held only on this object; never logged or returned beyond its
            last four characters.
        api_base: Host such as ``https://api.mercury.com``; ``/api/v1`` is
            appended here. Shape-validated only: the production/sandbox
            host allowlist is enforced where the value enters the process
            (the CLIs, via ``--allow-custom-api-base``), so programmatic
            callers and tests can point at a mock.
        timeout: Per-request timeout in seconds.
        max_retries: Attempts on 429/502/503/504 before giving up.
        transport: Optional ``httpx.AsyncBaseTransport`` (tests inject a
            ``MockTransport``).
        sleep: Awaitable sleep used between retries (tests inject a no-op).
    """

    def __init__(
        self,
        token: str,
        *,
        api_base: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("token must be a non-empty string")
        self._token = token.strip()
        base = validate_api_base(api_base or api_base_from_env(), allow_custom=True)
        self.base_url = base + API_PREFIX
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or anyio.sleep
        # DOCS: getting-started documents HTTP Basic (token as username) as
        # primary and Bearer "for convenience"; the OpenAPI securityScheme is
        # bearerAuth. Bearer is used here, matching the brief and the schema.
        # Accept-Encoding: identity declines compression so every byte cap
        # applies to what actually crosses the wire (M4).
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "mercury-multiorg-mcp",
            },
            timeout=timeout,
            transport=transport,
        )

    # -- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MercuryClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def token_suffix(self) -> str:
        """Last four characters of the token, for diagnostics. Never more."""
        return token_suffix(self._token)

    def __repr__(self) -> str:  # never leak the token via repr()
        return f"MercuryClient(base_url={self.base_url!r}, token=...{self.token_suffix})"

    # -- transport --------------------------------------------------------

    def scrub(self, text: str) -> str:
        """Redact token shapes, Authorization headers, and this client's own token from ``text``."""
        return redact(text, self._token)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return the decoded JSON body (wire bytes capped at ``MAX_JSON_BYTES``)."""
        label = endpoint_label(path)
        resp = await self._fetch(path, params)
        body = await self._read_body(resp, MAX_JSON_BYTES, label, path)
        try:
            return json.loads(body)
        except ValueError:
            raise MercuryAPIError(f"{label} returned a body that is not JSON", status_code=resp.status_code, path=path) from None

    async def _download(
        self, path: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES, accepted_types: frozenset[str] = PDF_CONTENT_TYPES
    ) -> tuple[bytes, str]:
        """GET a binary body (statement / invoice PDF) without ever buffering more than ``max_bytes`` of wire data.

        Returns ``(body, content_type)``. The declared ``Content-Type`` must
        be one of ``accepted_types`` (checked before any body is read; the
        actual value is never quoted in an error), a ``Content-Length``
        above the cap fails before any body is read, and a body that grows
        past the cap while streaming fails as soon as it does. The caller
        still validates the bytes with :func:`validate_pdf_bytes`.
        """
        label = endpoint_label(path)
        resp = await self._fetch(path, None)
        content_type = resp.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in accepted_types:
            await resp.aclose()
            raise MercuryAPIError(
                f"{label} did not return a PDF (unexpected content type)", status_code=resp.status_code, path=path
            )
        body = await self._read_body(resp, max_bytes, label, path)
        return body, media_type

    async def _read_body(self, resp: httpx.Response, max_bytes: int, label: str, path: str) -> bytes:
        """Read a streamed success response as raw wire bytes, never more than ``max_bytes``; always closes it."""
        try:
            encoding = resp.headers.get("Content-Encoding", "")
            codings = [c.strip().lower() for c in encoding.split(",") if c.strip()]
            if any(c != "identity" for c in codings):
                # Compression was declined; a compressed body would let a
                # small wire payload expand past every cap (M4).
                raise MercuryAPIError(
                    f"{label} returned a compressed body, which this client refuses (identity encoding was requested)",
                    status_code=resp.status_code,
                    path=path,
                )
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise MercuryAPIError(
                    f"{label} declares {int(declared)} bytes, above the {max_bytes}-byte limit",
                    status_code=resp.status_code,
                    path=path,
                )
            chunks: list[bytes] = []
            size = 0
            # A live response streams raw wire bytes. A response whose body
            # was already buffered before it reached us (httpx.MockTransport
            # builds those from `content=`) refuses aiter_raw(); its bytes
            # are served from the buffer instead. Both paths are wire bytes
            # once the identity check above has passed.
            iterator = resp.aiter_bytes() if resp.is_stream_consumed else resp.aiter_raw()
            try:
                async for chunk in iterator:
                    size += len(chunk)
                    if size > max_bytes:
                        raise MercuryAPIError(
                            f"{label} exceeded the {max_bytes}-byte limit while downloading",
                            status_code=resp.status_code,
                            path=path,
                        )
                    chunks.append(chunk)
            except httpx.HTTPError as exc:
                # A read/reset in the middle of the body: not retried, the
                # partial body is discarded, the exception is named by class
                # only (httpx reprs can embed the request and its headers).
                raise MercuryAPIError(
                    f"Transport error while reading {label} after {size} bytes ({exc.__class__.__name__})",
                    status_code=resp.status_code,
                    path=path,
                ) from None
        finally:
            await resp.aclose()
        return b"".join(chunks)

    async def _fetch(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Perform a GET with bounded backoff. The only request method in the package.

        Returns the successful (``< 400``) response *unread* (streamed); the
        caller reads it through :meth:`_read_body` or closes it. Retries (up
        to ``max_retries``) on 429/502/503/504 and on ``httpx.TransportError``
        (connect failures, timeouts, resets): every request here is an
        idempotent GET, so a retry can never double-apply. An error status
        is reported as fixed text; its body is never read.
        """
        label = endpoint_label(path)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                request = self._http.build_request("GET", path, params=clean_params)
                resp = await self._http.send(request, stream=True)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    await self._sleep(self._backoff_seconds(None, attempt))
                    attempt += 1
                    continue
                raise MercuryAPIError(
                    f"Transport error calling {label} after {attempt + 1} attempts ({exc.__class__.__name__})",
                    path=path,
                ) from None
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # InvalidURL is not an HTTPError: it is raised by build_request
                # for a path that cannot be encoded. No request was sent.
                raise MercuryAPIError(f"HTTP error calling {label} ({exc.__class__.__name__})", path=path) from None

            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                await resp.aclose()
                await self._sleep(self._backoff_seconds(resp, attempt))
                attempt += 1
                continue

            if resp.status_code >= 400:
                await resp.aclose()
                hint = _STATUS_HINTS.get(resp.status_code)
                raise MercuryAPIError(
                    f"Mercury returned HTTP {resp.status_code} for {label}" + (f": {hint}" if hint else ""),
                    status_code=resp.status_code,
                    path=path,
                )
            return resp

    @staticmethod
    def _backoff_seconds(resp: httpx.Response | None, attempt: int) -> float:
        """Honour a numeric ``Retry-After`` (capped at 60s); otherwise 0.5s * 2**attempt + jitter, capped at 16s."""
        retry_after = resp.headers.get("Retry-After") if resp is not None else None
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(0.5 * (2**attempt), 16.0) + random.uniform(0, 0.25)

    # -- read endpoints ---------------------------------------------------

    async def list_accounts(self) -> list[dict[str, Any]]:
        """All accounts for the org. Balances ride on this response (no balance endpoint).

        DOCS: ``GET /accounts`` is cursor-paginated (``limit``, ``order``,
        ``start_after``, ``end_before``) and returns ``{"accounts": [...],
        "page": {"nextPage", "previousPage"}}``. The brief described it as a
        flat list; both cursors are honoured here.
        """
        return await self._paginate("/accounts", "accounts", params={}, max_items=None)

    async def list_transactions(
        self,
        *,
        account_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
        search: str | None = None,
        status: str | None = None,
        posted_start: str | None = None,
        posted_end: str | None = None,
        limit: int | None = 100,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """Org-wide transactions via ``GET /transactions``, newest first by default.

        ``limit=None`` walks every page (still bounded by ``MAX_PAGES``).

        DOCS: ``/transactions`` takes ``accountId`` as a repeatable query
        param, so per-account filtering uses the same endpoint. The separate
        ``GET /account/{id}/transactions`` uses offset pagination and a
        different envelope (``{"total", "transactions"}``); it is deliberately
        not used so callers see one pagination model.
        ``start``/``end`` filter on ``createdAt`` (YYYY-MM-DD or ISO 8601);
        the dashboard shows ``postedAt``, which the API filters with
        ``postedStart``/``postedEnd`` (``posted_start``/``posted_end`` here;
        used by the 1099 pass, which attributes a year by posted date).
        """
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        params: dict[str, Any] = {
            "accountId": account_id,
            "start": start,
            "end": end,
            "search": search,
            "status": status,
            "postedStart": posted_start,
            "postedEnd": posted_end,
            "order": order,
        }
        return await self._paginate("/transactions", "transactions", params=params, max_items=limit)

    async def list_recipients(self) -> list[dict[str, Any]]:
        """All payment recipients via ``GET /recipients`` (cursor-paginated like /accounts).

        The raw objects carry bank coordinates and postal addresses; callers
        project through an allowlist before anything leaves the server.
        DOCS: the single-recipient endpoint is ``GET /recipient/{id}``
        (singular), not ``/recipients/{id}`` as the brief says. It is not
        needed here, so no client method exists for it.
        """
        return await self._paginate("/recipients", "recipients", params={}, max_items=None)

    async def list_recipient_attachments(self) -> list[dict[str, Any]]:
        """All recipient tax-form attachments via ``GET /recipients/attachments``.

        DOCS: each item is ``{id, recipientId, fileName, formType
        (w9|w8BEN|w8BENE|unknown|null), uploadedAt, url}``; ``url`` is a
        presigned download link valid for 12 hours and is never surfaced.
        """
        return await self._paginate("/recipients/attachments", "attachments", params={}, max_items=None)

    async def ping(self) -> int:
        """One authenticated ``GET /accounts?limit=1``; returns the HTTP status.

        Used by the keepalive CLI: any authenticated call resets Mercury's
        45-day inactivity clock for the token. The body is not read.
        """
        resp = await self._fetch("/accounts", {"limit": 1})
        await resp.aclose()
        return resp.status_code

    # -- Phase 3 read endpoints -------------------------------------------

    async def get_organization(self) -> dict[str, Any]:
        """``GET /organization``: ``{"organization": {...}}`` (unwrapped here). Carries the EIN in the raw form."""
        data = await self._get("/organization")
        org = data.get("organization") if isinstance(data, dict) else None
        if not isinstance(org, dict):
            raise MercuryAPIError("Unexpected response shape from GET /organization: missing 'organization'", path="/organization")
        return org

    async def list_account_statements(
        self, account_id: str, *, start: str | None = None, end: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """``GET /account/{id}/statements``, newest first (API default ``desc``).

        DOCS: ``start``/``end`` filter on the statement *period start* date
        (YYYY-MM-DD) and may span at most 3 months (checked client-side too).
        Treasury and credit accounts are documented as unsupported here, yet
        the changelog "Credit Statement Endpoint: Updated balance and
        transaction behavior" (2026-06) describes credit-account statements
        from "the statement endpoint"; if credit statements are served, only
        the depository fields in the allowlist surface (autopay and
        credit-specific fields are not projected).
        """
        account_id = validate_path_id(account_id, "account_id")
        return await self._paginate(
            f"/account/{account_id}/statements",
            "statements",
            params={"start": start, "end": end, "order": "desc"},
            max_items=limit,
        )

    async def get_statement_pdf(self, statement_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[bytes, str]:
        """``GET /statements/{id}/pdf``: validated PDF bytes, capped. Returns ``(body, media_type)``.

        DOCS: the path parameter is a bare uuid described only as "ID for the
        account statement"; the documented success content type is
        ``application/pdf``. Treasury statements carry ids of the same
        ``AccountStatementId`` type as depository statements, so they *may*
        be accepted here; the docs do not say. Treasury statements otherwise
        expose only a ``downloadUrl``, which this package never fetches or
        returns.
        """
        statement_id = validate_path_id(statement_id, "statement_id")
        path = f"/statements/{statement_id}/pdf"
        body, media_type = await self._download(path, max_bytes=max_bytes)
        validate_pdf_bytes(body, endpoint_label(path), path=path)
        return body, media_type

    async def list_treasury(self) -> list[dict[str, Any]]:
        """``GET /treasury``: all treasury accounts (cursor-paginated like /accounts)."""
        return await self._paginate("/treasury", "accounts", params={}, max_items=None)

    async def list_treasury_transactions(
        self,
        treasury_id: str,
        *,
        limit: int | None = None,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """``GET /treasury/{id}/transactions``, newest first by default.

        DOCS: this endpoint uses an *integer* ``cursor`` (the response's
        ``cursor`` is passed back to get the next batch; null when done), not
        the ``start_after`` id cursor of the other list endpoints, and it has
        no date filters; ``order`` is documented as asc/desc with no sort key
        named. Callers wanting a date window walk the whole ledger
        (``limit=None``, bounded by ``MAX_PAGES``) and filter on
        ``canonicalDay`` themselves. A non-null cursor that does not advance,
        or a page with no fresh rows while a cursor is still offered, is an
        :class:`IncompletePaginationError` rather than a silently short list.
        """
        treasury_id = validate_path_id(treasury_id, "treasury_id")
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        path = f"/treasury/{treasury_id}/transactions"
        label = endpoint_label(path)
        collected = RowList()
        seen: dict[str, dict[str, Any]] = {}
        cursor: int | None = None
        for _ in range(MAX_PAGES):
            remaining = None if limit is None else limit - len(collected)
            if remaining is not None and remaining <= 0:
                break
            page_size = MAX_PAGE_SIZE if remaining is None else min(MAX_PAGE_SIZE, remaining)
            data = await self._get(path, {"limit": page_size, "order": order, "cursor": cursor})
            items = data.get("transactions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise MercuryAPIError(f"Unexpected response shape from {label}: missing 'transactions' list", path=path)
            # Same dedupe as the id-cursor walk: an overlapping page, or a row
            # repeated inside one page, is never counted twice (B2).
            rows, dropped = _accept_rows(items, "id", seen, label, path)
            collected.extend(rows)
            collected.duplicates_dropped += dropped
            # DOCS: `cursor` is nullable (null when done) and, when present, a
            # SliceSequenceNumber: integer, minimum 0. Absent means done too.
            nxt = data.get("cursor")
            if nxt is None:
                break
            if isinstance(nxt, bool) or not isinstance(nxt, int) or nxt < 0:
                raise IncompletePaginationError(
                    f"{label}: malformed pagination metadata ('cursor' must be null or a non-negative integer); "
                    "incomplete pagination, the result would be partial",
                    path=path,
                )
            if nxt == cursor or not rows:
                raise IncompletePaginationError(
                    f"{label}: incomplete pagination (the API offered another page but the cursor did not advance "
                    "or the page repeated already-seen rows); the result would be partial",
                    path=path,
                )
            cursor = nxt
        else:
            if limit is None or len(collected) < limit:
                raise MercuryAPIError(
                    f"{label} has more than {MAX_PAGES} pages ({len(collected)} items collected); "
                    "narrow the query or raise MAX_PAGES",
                    path=path,
                )
        if limit is not None:
            del collected[limit:]
        return collected

    async def list_treasury_statements(self, treasury_id: str, *, document_type: str | None = None) -> list[dict[str, Any]]:
        """``GET /treasury/{id}/statements`` (metadata incl. tax forms; ``documentType`` filter)."""
        treasury_id = validate_path_id(treasury_id, "treasury_id")
        return await self._paginate(
            f"/treasury/{treasury_id}/statements", "statements", params={"documentType": document_type}, max_items=None
        )

    async def list_credit_accounts(self) -> list[dict[str, Any]]:
        """``GET /credit``: ``{"accounts": [...]}``, not paginated."""
        data = await self._get("/credit")
        items = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MercuryAPIError("Unexpected response shape from GET /credit: missing 'accounts' list", path="/credit")
        return [it for it in items if isinstance(it, dict)]

    async def list_cards(
        self,
        *,
        account_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /cards`` (org-wide, cursor-paginated; ``accountId`` / ``status`` filters).

        DOCS: ``GET /account/{id}/cards`` is documented as the deprecated
        representation; the org-wide endpoint with ``accountId`` is used.
        """
        return await self._paginate(
            "/cards", "cards", params={"accountId": account_id, "status": status}, max_items=limit
        )

    async def get_card(self, card_id: str) -> dict[str, Any]:
        """``GET /cards/{id}``. Never returns PAN/CVC (those live behind the write-scoped Vault API)."""
        card_id = validate_path_id(card_id, "card_id")
        path = f"/cards/{card_id}"
        data = await self._get(path)
        if not isinstance(data, dict):
            raise MercuryAPIError(f"Unexpected response shape from {endpoint_label(path)}", path=path)
        return data

    async def list_categories(self) -> list[dict[str, Any]]:
        """``GET /categories``: custom expense categories (cursor-paginated)."""
        return await self._paginate("/categories", "categories", params={}, max_items=None)

    async def list_merchants(self, *, search: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """``GET /merchants``: priority merchants for spend controls (items under ``data``)."""
        return await self._paginate("/merchants", "data", params={"search": search}, max_items=limit)

    async def list_customers(self) -> list[dict[str, Any]]:
        """``GET /ar/customers`` (soft-deleted customers carry ``deletedAt``)."""
        return await self._paginate("/ar/customers", "customers", params={}, max_items=None)

    async def list_invoices(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """``GET /ar/invoices`` (cursor-paginated).

        DOCS: the endpoint has no status or date filters; callers filter
        client-side, which means walking every invoice.
        """
        return await self._paginate("/ar/invoices", "invoices", params={}, max_items=limit)

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        """``GET /ar/invoices/{id}`` including line items."""
        invoice_id = validate_path_id(invoice_id, "invoice_id")
        path = f"/ar/invoices/{invoice_id}"
        data = await self._get(path)
        if not isinstance(data, dict):
            raise MercuryAPIError(f"Unexpected response shape from {endpoint_label(path)}", path=path)
        return data

    async def get_invoice_pdf(self, invoice_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[bytes, str]:
        """``GET /ar/invoices/{id}/pdf``: validated PDF bytes, capped. Returns ``(body, media_type)``.

        DOCS: the reference page types the path parameter as the invoice
        uuid (``invoiceId``), while the invoice schema says the public
        ``slug`` is "used to construct ... the URL to retrieve the PDF".
        Both are tried: the id first; on 404 the invoice is fetched and its
        slug used instead. Neither the slug nor the id appears in error
        text (endpoint labels mask every id segment).
        """
        invoice_id = validate_path_id(invoice_id, "invoice_id")
        id_path = f"/ar/invoices/{invoice_id}/pdf"
        label = endpoint_label(id_path)
        try:
            body, media_type = await self._download(id_path, max_bytes=max_bytes)
        except MercuryAPIError as exc:
            if exc.status_code != 404:
                raise
            by_id = exc
        else:
            validate_pdf_bytes(body, label, path=id_path)
            return body, media_type
        invoice = await self.get_invoice(invoice_id)
        slug = invoice.get("slug")
        if not isinstance(slug, str) or not _PATH_ID_RE.fullmatch(slug):
            raise by_id
        try:
            body, media_type = await self._download(f"/ar/invoices/{slug}/pdf", max_bytes=max_bytes)
        except MercuryAPIError as exc:
            raise MercuryAPIError(
                f"{exc} (not found by invoice id; the invoice's slug path was tried next)",
                status_code=exc.status_code,
                path=id_path,
            ) from None
        validate_pdf_bytes(body, label, path=id_path)
        return body, media_type

    async def list_invoice_attachments(self, invoice_id: str) -> list[dict[str, Any]]:
        """``GET /ar/invoices/{id}/attachments``: ``{"attachments": [{id, fileName, url}]}`` (not paginated)."""
        invoice_id = validate_path_id(invoice_id, "invoice_id")
        path = f"/ar/invoices/{invoice_id}/attachments"
        data = await self._get(path)
        items = data.get("attachments") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MercuryAPIError(f"Unexpected response shape from {endpoint_label(path)}: missing 'attachments' list", path=path)
        return [it for it in items if isinstance(it, dict)]

    async def list_users(self) -> list[dict[str, Any]]:
        """``GET /users`` (cursor-paginated). DOCS: the item id field is ``userId``, not ``id``."""
        return await self._paginate("/users", "users", params={}, max_items=None, id_key="userId")

    async def list_events(
        self,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int | None = None,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """``GET /events`` (cursor-paginated; ``resourceType`` / ``resourceId`` filters).

        DOCS: there is no time filter; events are kept for 90 days. ``order``
        is documented only as asc/desc with no sort key named, so a caller
        applying a time window must walk the whole feed (``limit=None``) and
        filter and sort the rows itself; no early stop is offered here.
        """
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        return await self._paginate(
            "/events",
            "events",
            params={"resourceType": resource_type, "resourceId": resource_id, "order": order},
            max_items=limit,
        )

    async def list_webhooks(self) -> list[dict[str, Any]]:
        """``GET /webhooks``: endpoint configuration. ``secret`` is never returned by GET per the docs, and is dropped anyway."""
        return await self._paginate("/webhooks", "webhooks", params={}, max_items=None)

    # -- pagination -------------------------------------------------------

    async def _paginate(
        self,
        path: str,
        items_key: str,
        *,
        params: dict[str, Any],
        max_items: int | None,
        id_key: str = "id",
    ) -> list[dict[str, Any]]:
        """Walk a ``start_after`` cursor until exhausted or ``max_items`` collected.

        ``id_key`` names the item's id field (``userId`` on /users).

        DOCS: the OpenAPI describes ``page.nextPage`` only as an ID and
        ``start_after`` as "the ID of the item to start after (exclusive)".
        Whether ``nextPage`` is the last ID of this page or the first of the
        next is not stated, so the cursor is derived from the last item
        actually received and ``nextPage`` is used only as a "more pages"
        signal. That is correct under either reading. Page length is not
        used as a stop signal because the server-side page cap is not
        documented; the seen-id set and ``MAX_PAGES`` bound the loop.

        Completeness (M6): while ``nextPage`` is present, a page that adds no
        fresh rows or yields no usable cursor is an
        :class:`IncompletePaginationError`; exhausting ``MAX_PAGES`` with
        more pages remaining is an error too. A partial list is never
        returned as if it were complete.

        Envelope (B1): every paginated response schema requires ``page`` as
        an object whose ``nextPage`` is an optional, nullable id. A missing
        or non-object ``page``, or a ``nextPage`` that is neither null nor a
        non-empty string, is malformed pagination metadata and an error;
        "no more pages" is only ever concluded from a well-formed envelope.
        Rows repeated inside one page are dropped as duplicates and counted
        (B2); a repeated id with different content is an error.
        """
        label = endpoint_label(path)
        collected = RowList()
        seen: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            remaining = None if max_items is None else max_items - len(collected)
            if remaining is not None and remaining <= 0:
                break
            page_size = MAX_PAGE_SIZE if remaining is None else min(MAX_PAGE_SIZE, remaining)
            data = await self._get(path, {**params, "limit": page_size, "start_after": cursor})
            items = data.get(items_key) if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise MercuryAPIError(f"Unexpected response shape from {label}: missing '{items_key}' list", path=path)
            fresh, dropped = _accept_rows(items, id_key, seen, label, path)
            collected.extend(fresh)
            collected.duplicates_dropped += dropped
            page_info = data.get("page")
            if not isinstance(page_info, dict):
                raise IncompletePaginationError(
                    f"{label}: malformed pagination metadata ('page' missing or not an object); "
                    "completeness cannot be established, the result would be partial",
                    path=path,
                )
            next_page = page_info.get("nextPage")
            if next_page is not None and (not isinstance(next_page, str) or not next_page):
                raise IncompletePaginationError(
                    f"{label}: malformed pagination metadata ('nextPage' must be null or a non-empty string); "
                    "completeness cannot be established, the result would be partial",
                    path=path,
                )
            if next_page is None:
                break
            next_cursor = fresh[-1].get(id_key) if fresh else None
            if not fresh or not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise IncompletePaginationError(
                    f"{label}: incomplete pagination (the API advertised another page but this page contributed "
                    "no fresh rows or no usable cursor); the result would be partial",
                    path=path,
                )
            cursor = next_cursor
        else:
            # Every allowed page was consumed and the server still reports
            # more. If the caller asked for at most ``max_items`` and has
            # them, that is a complete answer; otherwise returning the short
            # list would silently understate a total (the 1099 walk relies on
            # this), so fail loudly instead.
            if max_items is None or len(collected) < max_items:
                raise MercuryAPIError(
                    f"{label} has more than {MAX_PAGES} pages ({len(collected)} items collected); "
                    "narrow the query or raise MAX_PAGES",
                    path=path,
                )
        if max_items is not None:
            del collected[max_items:]
        return collected
