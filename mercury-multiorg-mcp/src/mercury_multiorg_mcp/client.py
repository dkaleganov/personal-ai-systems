"""Thin, read-only async client for the Mercury API (one instance per token).

Validated against the live OpenAPI at docs.mercury.com on 2026-09-11
(``/reference/getaccounts.md``, ``/reference/listtransactions.md``,
``/reference/listaccounttransactions.md``, ``/docs/getting-started.md``,
``/docs/api-token-security-policies.md``), on 2026-09-12 for Phase 2
(``/reference/getrecipients.md``, ``/reference/getrecipient.md``,
``/reference/listrecipientsattachments.md``), and on 2026-09-12 for Phase 3
(``getorganization``, ``getaccountstatements``, ``getstatementpdf``,
``gettreasury``, ``gettreasurytransactions``, ``gettreasurystatements``,
``listcredit``, ``listcards``, ``getcard``, ``listcategories``,
``listmerchants``, ``listcustomers``, ``listinvoices``, ``getinvoice``,
``getinvoicepdf``, ``listinvoiceattachments``, ``getusers``, ``getevents``,
``getwebhooks``). Notes where the live docs differ from the build brief
are marked ``DOCS:`` below.

Only ``GET`` is implemented. There is no method for any endpoint that can
change state, and :meth:`MercuryClient._fetch` is the single choke point for
every request, so adding a write path would have to be deliberate.
"""

from __future__ import annotations

import os
import random
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from .errors import MercuryAPIError, redact, token_suffix

DEFAULT_API_BASE = "https://api.mercury.com"
API_BASE_ENV = "MERCURY_API_BASE"
# DOCS: sandbox base is https://api-sandbox.mercury.com (same /api/v1 prefix).
# Users point MERCURY_API_BASE there with a sandbox-created token.
API_PREFIX = "/api/v1"

# DOCS: `limit` on /accounts and /transactions is 1..1000, default 1000.
MAX_PAGE_SIZE = 1000
# Hard stop for the pagination loops so a misbehaving cursor can't spin forever.
MAX_PAGES = 200

_RETRY_STATUSES = frozenset({429, 502, 503, 504})

# Largest binary (statement / invoice PDF) the client will buffer.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
# Largest error body read on a streamed response before it is scrubbed and truncated.
ERROR_BODY_CAP = 64 * 1024

# Mercury ids are UUIDs. Anything that could change the request path (a
# slash, a dot segment, a query) is rejected before it reaches the URL.
_PATH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_path_id(value: str, label: str) -> str:
    """Return ``value`` if it is a safe single path segment, else raise ``ValueError``."""
    if not isinstance(value, str) or not _PATH_ID_RE.fullmatch(value):  # fullmatch: `$` would allow a trailing newline
        raise ValueError(f"{label} must be an id of letters, digits, '-' or '_' (got {value!r})")
    return value


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def api_base_from_env() -> str:
    """Resolve the API host (not including /api/v1) from ``MERCURY_API_BASE``."""
    return os.environ.get(API_BASE_ENV, "").strip() or DEFAULT_API_BASE


def validate_api_base(url: str) -> str:
    """Require ``https://`` for the API host; plain ``http://`` only on loopback (mocks).

    Returns the host URL with any trailing slash removed. Raises ``ValueError``
    with a message safe to print: the rejected value is never echoed, since a
    mistyped URL can carry a credential in its userinfo, path, or query.
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
    return candidate


class MercuryClient:
    """Async, read-only Mercury API client bound to a single organization token.

    Args:
        token: The org's API token (documented shape ``secret-token:...``).
            Held only on this object; never logged or returned beyond its
            last four characters.
        api_base: Host such as ``https://api.mercury.com``; ``/api/v1`` is
            appended here.
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
        base = validate_api_base(api_base or api_base_from_env())
        self.base_url = base + API_PREFIX
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or anyio.sleep
        # DOCS: getting-started documents HTTP Basic (token as username) as
        # primary and Bearer "for convenience"; the OpenAPI securityScheme is
        # bearerAuth. Bearer is used here, matching the brief and the schema.
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
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

    def _scrub(self, text: str) -> str:
        return redact(text, self._token)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return the decoded JSON body. See :meth:`_fetch`."""
        resp = await self._fetch(path, params)
        try:
            return resp.json()
        except ValueError:
            raise MercuryAPIError(
                f"Mercury returned non-JSON body for GET {path}", status_code=resp.status_code, path=path
            ) from None

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET ``path`` with the body read; see :meth:`_fetch`."""
        return await self._fetch(path, params)

    async def _download(self, path: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[bytes, str]:
        """GET a binary body (statement / invoice PDF) without ever buffering more than ``max_bytes``.

        Returns ``(body, content_type)``. A ``Content-Length`` above the cap
        fails before any body is read; a body that grows past the cap while
        streaming fails as soon as it does; an error body is read to at most
        ``ERROR_BODY_CAP`` before being scrubbed.
        """
        resp = await self._fetch(path, None, stream=True)
        try:
            declared = resp.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise MercuryAPIError(
                    f"GET {path} is {int(declared)} bytes, above the {max_bytes}-byte limit",
                    status_code=resp.status_code,
                    path=path,
                )
            chunks: list[bytes] = []
            size = 0
            try:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise MercuryAPIError(
                            f"GET {path} exceeded the {max_bytes}-byte limit while downloading",
                            status_code=resp.status_code,
                            path=path,
                        )
                    chunks.append(chunk)
            except httpx.HTTPError as exc:
                # A read/reset in the middle of the body: same wrapping and
                # scrubbing as every other transport failure. Not retried, the
                # partial body is discarded.
                raise MercuryAPIError(
                    f"Transport error while downloading GET {path} after {size} bytes: {self._scrub(repr(exc))}",
                    status_code=resp.status_code,
                    path=path,
                ) from None
        finally:
            await resp.aclose()
        return b"".join(chunks), resp.headers.get("Content-Type", "")

    async def _fetch(self, path: str, params: dict[str, Any] | None = None, *, stream: bool = False) -> httpx.Response:
        """Perform a GET with bounded backoff. The only request method in the package.

        Returns the successful (``< 400``) response, body already read unless
        ``stream`` is true (then the caller reads and closes it). Retries (up
        to ``max_retries``) on 429/502/503/504 and on ``httpx.TransportError``
        (connect failures, timeouts, resets): every request here is an
        idempotent GET, so a retry can never double-apply.
        """
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                request = self._http.build_request("GET", path, params=clean_params)
                resp = await self._http.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    await self._sleep(self._backoff_seconds(None, attempt))
                    attempt += 1
                    continue
                # httpx exception reprs can embed the request (and its headers).
                raise MercuryAPIError(
                    f"Transport error calling GET {path} after {attempt + 1} attempts: {self._scrub(repr(exc))}",
                    path=path,
                ) from None
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # InvalidURL is not an HTTPError: it is raised by build_request
                # for a path that cannot be encoded. No request was sent.
                raise MercuryAPIError(
                    f"HTTP error calling GET {path}: {self._scrub(repr(exc))}", path=path
                ) from None

            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                await resp.aclose()
                await self._sleep(self._backoff_seconds(resp, attempt))
                attempt += 1
                continue

            if resp.status_code >= 400:
                try:
                    raw = await self._read_capped(resp, ERROR_BODY_CAP) if stream else resp.content
                except httpx.HTTPError as exc:
                    await resp.aclose()
                    raise MercuryAPIError(
                        f"Mercury returned HTTP {resp.status_code} for GET {path}; body unreadable: {self._scrub(repr(exc))}",
                        status_code=resp.status_code,
                        path=path,
                    ) from None
                # Scrub first, then truncate: a cut in the middle of a token
                # would otherwise defeat the token-shape pattern.
                body = self._scrub(raw.decode("utf-8", "replace"))[:500]
                await resp.aclose()
                raise MercuryAPIError(
                    f"Mercury returned HTTP {resp.status_code} for GET {path}: {body}",
                    status_code=resp.status_code,
                    path=path,
                )
            return resp

    @staticmethod
    async def _read_capped(resp: httpx.Response, cap: int) -> bytes:
        """Read at most ``cap`` bytes of a streamed body (error bodies never need more)."""
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk[: cap - size])
            size += len(chunk)
            if size >= cap:
                break
        return b"".join(chunks)

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
        45-day inactivity clock for the token.
        """
        resp = await self._request("/accounts", {"limit": 1})
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
        """``GET /statements/{id}/pdf``: binary PDF, capped. Returns ``(body, content_type)``.

        DOCS: the path parameter is a bare uuid described only as "ID for the
        account statement". Treasury statements carry ids of the same
        ``AccountStatementId`` type as depository statements, so they *may*
        be accepted here; the docs do not say. Treasury statements otherwise
        expose only a ``downloadUrl``, which this package never fetches or
        returns.
        """
        statement_id = validate_path_id(statement_id, "statement_id")
        return await self._download(f"/statements/{statement_id}/pdf", max_bytes=max_bytes)

    async def list_treasury(self) -> list[dict[str, Any]]:
        """``GET /treasury``: all treasury accounts (cursor-paginated like /accounts)."""
        return await self._paginate("/treasury", "accounts", params={}, max_items=None)

    async def list_treasury_transactions(
        self,
        treasury_id: str,
        *,
        limit: int | None = None,
        order: str = "desc",
        stop_at: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /treasury/{id}/transactions``, newest first by default.

        DOCS: this endpoint uses an *integer* ``cursor`` (the response's
        ``cursor`` is passed back to get the next batch; null when done), not
        the ``start_after`` id cursor of the other list endpoints, and it has
        no date filters. Callers wanting a date window filter on
        ``canonicalDay`` client-side; ``stop_at`` ends the walk early once
        rows are older than wanted (rows arrive newest first).
        """
        treasury_id = validate_path_id(treasury_id, "treasury_id")
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        path = f"/treasury/{treasury_id}/transactions"
        collected: list[dict[str, Any]] = []
        seen: set[Any] = set()
        cursor: int | None = None
        for _ in range(MAX_PAGES):
            remaining = None if limit is None else limit - len(collected)
            if remaining is not None and remaining <= 0:
                break
            page_size = MAX_PAGE_SIZE if remaining is None else min(MAX_PAGE_SIZE, remaining)
            data = await self._get(path, {"limit": page_size, "order": order, "cursor": cursor})
            items = data.get("transactions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise MercuryAPIError(f"Unexpected response shape from GET {path}: missing 'transactions' list", path=path)
            # Same seen-id dedupe as the id-cursor walk: an overlapping page is never counted twice.
            rows = [it for it in items if isinstance(it, dict) and it.get("id") not in seen]
            for it in rows:
                seen.add(it.get("id"))
            if stop_at is not None:
                cut = next((i for i, it in enumerate(rows) if stop_at(it)), None)
                if cut is not None:
                    collected.extend(rows[:cut])
                    break
            collected.extend(rows)
            nxt = data.get("cursor")
            if not rows or not isinstance(nxt, int) or isinstance(nxt, bool) or nxt == cursor:  # non-advancing cursor
                break
            cursor = nxt
        else:
            if limit is None or len(collected) < limit:
                raise MercuryAPIError(
                    f"GET {path} has more than {MAX_PAGES} pages ({len(collected)} items collected); "
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
        data = await self._get(f"/cards/{card_id}")
        if not isinstance(data, dict):
            raise MercuryAPIError(f"Unexpected response shape from GET /cards/{card_id}", path=f"/cards/{card_id}")
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
        data = await self._get(f"/ar/invoices/{invoice_id}")
        if not isinstance(data, dict):
            raise MercuryAPIError(f"Unexpected response shape from GET /ar/invoices/{invoice_id}", path=f"/ar/invoices/{invoice_id}")
        return data

    async def get_invoice_pdf(self, invoice_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[bytes, str]:
        """``GET /ar/invoices/{id}/pdf``: binary PDF, capped. Returns ``(body, content_type)``.

        DOCS: the reference page types the path parameter as the invoice
        uuid (``invoiceId``), while the invoice schema says the public
        ``slug`` is "used to construct ... the URL to retrieve the PDF".
        Both are tried: the id first; on 404 the invoice is fetched and its
        slug used instead. The slug never leaves the client: errors name the
        invoice id only.
        """
        invoice_id = validate_path_id(invoice_id, "invoice_id")
        id_path = f"/ar/invoices/{invoice_id}/pdf"
        try:
            return await self._download(id_path, max_bytes=max_bytes)
        except MercuryAPIError as exc:
            if exc.status_code != 404:
                raise
            by_id = exc
        invoice = await self.get_invoice(invoice_id)
        slug = invoice.get("slug")
        if not isinstance(slug, str) or not _PATH_ID_RE.fullmatch(slug):
            raise by_id
        try:
            return await self._download(f"/ar/invoices/{slug}/pdf", max_bytes=max_bytes)
        except MercuryAPIError as exc:
            message = str(exc).replace(slug, invoice_id)
            raise MercuryAPIError(
                f"{message} (invoice {invoice_id}: not found by id, then tried by slug)",
                status_code=exc.status_code,
                path=id_path,
            ) from None

    async def list_invoice_attachments(self, invoice_id: str) -> list[dict[str, Any]]:
        """``GET /ar/invoices/{id}/attachments``: ``{"attachments": [{id, fileName, url}]}`` (not paginated)."""
        invoice_id = validate_path_id(invoice_id, "invoice_id")
        path = f"/ar/invoices/{invoice_id}/attachments"
        data = await self._get(path)
        items = data.get("attachments") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise MercuryAPIError(f"Unexpected response shape from GET {path}: missing 'attachments' list", path=path)
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
        stop_at: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """``GET /events`` (cursor-paginated; ``resourceType`` / ``resourceId`` filters).

        DOCS: there is no time filter; events are kept for 90 days. ``order``
        is documented only as asc/desc with no sort key named; the example
        ids are time-based (UUIDv1), so ``desc`` is taken to mean newest
        first. A ``since`` window is applied client-side on that assumption
        by walking newest-first and stopping at the first event older than
        wanted (``stop_at``); the server tool verifies the ordering as it
        goes and falls back to a full walk if a page breaks it.
        """
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        return await self._paginate(
            "/events",
            "events",
            params={"resourceType": resource_type, "resourceId": resource_id, "order": order},
            max_items=limit,
            stop_at=stop_at,
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
        stop_at: Callable[[dict[str, Any]], bool] | None = None,
        id_key: str = "id",
    ) -> list[dict[str, Any]]:
        """Walk a ``start_after`` cursor until exhausted or ``max_items`` collected.

        ``stop_at`` (for server-ordered streams) ends the walk at the first
        item it accepts; that item and everything after it are dropped. It
        is called on every item of a page before the cut is applied, and a
        predicate exposing a true ``disabled`` attribute cancels the cut.
        ``id_key`` names the item's id field (``userId`` on /users).

        DOCS: the OpenAPI describes ``page.nextPage`` only as an ID and
        ``start_after`` as "the ID of the item to start after (exclusive)".
        Whether ``nextPage`` is the last ID of this page or the first of the
        next is not stated, so the cursor is derived from the last item
        actually received and ``nextPage`` is used only as a "more pages"
        signal. That is correct under either reading. Page length is not
        used as a stop signal because the server-side page cap is not
        documented; the seen-id set and ``MAX_PAGES`` bound the loop, and
        exhausting ``MAX_PAGES`` with more pages remaining is an error rather
        than a silently short result.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            remaining = None if max_items is None else max_items - len(collected)
            if remaining is not None and remaining <= 0:
                break
            page_size = MAX_PAGE_SIZE if remaining is None else min(MAX_PAGE_SIZE, remaining)
            data = await self._get(path, {**params, "limit": page_size, "start_after": cursor})
            items = data.get(items_key) if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise MercuryAPIError(f"Unexpected response shape from GET {path}: missing '{items_key}' list", path=path)
            fresh = [it for it in items if isinstance(it, dict) and it.get(id_key) not in seen]
            for it in fresh:
                seen.add(it.get(id_key))
            if stop_at is not None:
                # Evaluate every row (a stateful predicate may need to see the
                # whole page); only then honour the first stop, and never if
                # the predicate disabled itself meanwhile.
                flags = [stop_at(it) for it in fresh]
                cut = next((i for i, flag in enumerate(flags) if flag), None)
                if cut is not None and not getattr(stop_at, "disabled", False):
                    collected.extend(fresh[:cut])
                    break
            collected.extend(fresh)
            page_info = data.get("page") or {}
            has_more = bool(page_info.get("nextPage")) and bool(fresh)
            if not has_more:
                break
            cursor = fresh[-1].get(id_key)
            if not cursor:
                break
        else:
            # Every allowed page was consumed and the server still reports
            # more. If the caller asked for at most ``max_items`` and has
            # them, that is a complete answer; otherwise returning the short
            # list would silently understate a total (the 1099 walk relies on
            # this), so fail loudly instead.
            if max_items is None or len(collected) < max_items:
                raise MercuryAPIError(
                    f"GET {path} has more than {MAX_PAGES} pages ({len(collected)} items collected); "
                    "narrow the query or raise MAX_PAGES",
                    path=path,
                )
        if max_items is not None:
            del collected[max_items:]
        return collected
