"""Thin, read-only async client for the Mercury API (one instance per token).

Validated against the live OpenAPI at docs.mercury.com on 2026-09-11
(``/reference/getaccounts.md``, ``/reference/listtransactions.md``,
``/reference/listaccounttransactions.md``, ``/docs/getting-started.md``,
``/docs/api-token-security-policies.md``). Notes where the live docs differ
from the July 2026 build brief are marked ``DOCS:`` below.

Only ``GET`` is implemented. There is no method for any endpoint that can
change state, and :meth:`MercuryClient._get` is the single choke point for
every request, so adding a write path would have to be deliberate.
"""

from __future__ import annotations

import os
import random
from collections.abc import Awaitable, Callable
from typing import Any

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


def api_base_from_env() -> str:
    """Resolve the API host (not including /api/v1) from ``MERCURY_API_BASE``."""
    return os.environ.get(API_BASE_ENV, "").strip() or DEFAULT_API_BASE


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
        base = (api_base or api_base_from_env()).rstrip("/")
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
        """Perform a GET with 429/5xx backoff. The only request method in the package."""
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                resp = await self._http.get(path, params=clean_params)
            except httpx.HTTPError as exc:
                # httpx exception reprs can embed the request (and its headers).
                raise MercuryAPIError(
                    f"HTTP error calling GET {path}: {self._scrub(repr(exc))}", path=path
                ) from None

            if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                await self._sleep(self._backoff_seconds(resp, attempt))
                attempt += 1
                continue

            if resp.status_code >= 400:
                body = self._scrub(resp.text[:500])
                raise MercuryAPIError(
                    f"Mercury returned HTTP {resp.status_code} for GET {path}: {body}",
                    status_code=resp.status_code,
                    path=path,
                )
            try:
                return resp.json()
            except ValueError:
                raise MercuryAPIError(
                    f"Mercury returned non-JSON body for GET {path}", status_code=resp.status_code, path=path
                ) from None

    @staticmethod
    def _backoff_seconds(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
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
        limit: int = 100,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """Org-wide transactions via ``GET /transactions``, newest first by default.

        DOCS: ``/transactions`` takes ``accountId`` as a repeatable query
        param, so per-account filtering uses the same endpoint. The separate
        ``GET /account/{id}/transactions`` uses offset pagination and a
        different envelope (``{"total", "transactions"}``); it is deliberately
        not used so callers see one pagination model.
        ``start``/``end`` filter on ``createdAt`` (YYYY-MM-DD or ISO 8601);
        the dashboard shows ``postedAt``, which the API exposes as
        ``postedStart``/``postedEnd`` (not surfaced in Phase 1).
        """
        if order not in ("asc", "desc"):
            raise ValueError("order must be 'asc' or 'desc'")
        params: dict[str, Any] = {
            "accountId": account_id,
            "start": start,
            "end": end,
            "search": search,
            "status": status,
            "order": order,
        }
        return await self._paginate("/transactions", "transactions", params=params, max_items=limit)

    # -- pagination -------------------------------------------------------

    async def _paginate(
        self,
        path: str,
        items_key: str,
        *,
        params: dict[str, Any],
        max_items: int | None,
    ) -> list[dict[str, Any]]:
        """Walk a ``start_after`` cursor until exhausted or ``max_items`` collected.

        DOCS: the OpenAPI describes ``page.nextPage`` only as an ID and
        ``start_after`` as "the ID of the item to start after (exclusive)".
        Whether ``nextPage`` is the last ID of this page or the first of the
        next is not stated, so the cursor is derived from the last item
        actually received and ``nextPage`` is used only as a "more pages"
        signal. That is correct under either reading. Page length is not
        used as a stop signal because the server-side page cap is not
        documented; the seen-id set and ``MAX_PAGES`` bound the loop.
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
            fresh = [it for it in items if isinstance(it, dict) and it.get("id") not in seen]
            for it in fresh:
                seen.add(it["id"])
            collected.extend(fresh)
            page_info = data.get("page") or {}
            has_more = bool(page_info.get("nextPage")) and bool(fresh)
            if not has_more:
                break
            cursor = fresh[-1].get("id")
            if not cursor:
                break
        if max_items is not None:
            del collected[max_items:]
        return collected
