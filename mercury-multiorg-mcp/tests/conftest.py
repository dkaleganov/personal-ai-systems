"""Shared fixtures: a fake Mercury API served from synthetic JSON, wired into the MCP server in-process."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from mercury_multiorg_mcp.client import MercuryClient
from mercury_multiorg_mcp.registry import Registry
from mercury_multiorg_mcp.server import build_server

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE_REGISTRY = PACKAGE_ROOT / "entities.example.yaml"

FAKE_TOKEN_MAIN = "secret-token:mercury_test_fake_main_ABCDEFGH1234"
FAKE_API_BASE = "https://api.mercury.example"

# A tiny but structurally plausible PDF, served for statement and invoice downloads.
FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"

# Ids the Phase 3 fixtures know about; anything else is a 404 from the fake.
KNOWN_ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
KNOWN_TREASURY_ID = "33333333-3333-4333-8333-333333333333"

# Generic cursor-paginated list routes: path -> (fixture, items key, fixture order, filterable query params)
_LIST_ROUTES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "/api/v1/treasury": ("treasury_accounts.json", "accounts", "asc", ()),
    "/api/v1/cards": ("cards.json", "cards", "asc", ("accountId", "status")),
    "/api/v1/categories": ("categories.json", "categories", "asc", ()),
    "/api/v1/merchants": ("merchants.json", "data", "asc", ()),
    "/api/v1/ar/customers": ("customers.json", "customers", "asc", ()),
    "/api/v1/ar/invoices": ("invoices.json", "invoices", "asc", ()),
    "/api/v1/users": ("users.json", "users", "asc", ()),  # items keyed by userId
    "/api/v1/events": ("events.json", "events", "asc", ("resourceType", "resourceId")),
    "/api/v1/webhooks": ("webhooks.json", "webhooks", "asc", ("status",)),
}
_ACCOUNT_STATEMENTS_RE = re.compile(r"^/api/v1/account/([^/]+)/statements$")
_TREASURY_STATEMENTS_RE = re.compile(r"^/api/v1/treasury/([^/]+)/statements$")
_TREASURY_TXNS_RE = re.compile(r"^/api/v1/treasury/([^/]+)/transactions$")
_STATEMENT_PDF_RE = re.compile(r"^/api/v1/statements/([^/]+)/pdf$")
_CARD_RE = re.compile(r"^/api/v1/cards/([^/]+)$")
_INVOICE_RE = re.compile(r"^/api/v1/ar/invoices/([^/]+)$")
_INVOICE_PDF_RE = re.compile(r"^/api/v1/ar/invoices/([^/]+)/pdf$")
_INVOICE_ATTACHMENTS_RE = re.compile(r"^/api/v1/ar/invoices/([^/]+)/attachments$")


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeMercury:
    """Serves the fixture pages by path + ``start_after`` cursor and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rate_limit_first: int = 0  # respond 429 to the first N requests
        self.retry_after: str | None = None
        self.force_status: int | None = None  # respond with this status to every request
        self.force_body: str = ""
        self.fail_paths: dict[str, int] = {}  # path -> status to force for that path only
        # When set, /transactions is served from this list with API-like
        # filtering (postedStart/postedEnd, status) and cursor pagination,
        # instead of the two Phase 1 page files.
        self.transactions: list[dict[str, Any]] | None = None
        # PDF download knobs
        self.pdf_bytes: bytes = FAKE_PDF
        self.pdf_content_type: str = "application/pdf"
        self.pdf_send_content_length: bool = True
        self._served = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._served < self.rate_limit_first:
            self._served += 1
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(429, headers=headers, json={"message": "slow down"})
        if self.force_status is not None:
            return httpx.Response(self.force_status, text=self.force_body)

        path = request.url.path
        if path in self.fail_paths:
            return httpx.Response(self.fail_paths[path], json={"message": "forced failure"})
        cursor = request.url.params.get("start_after")
        if path == "/api/v1/accounts":
            page = "accounts_page2.json" if cursor else "accounts_page1.json"
            return httpx.Response(200, json=load_fixture(page))
        if path == "/api/v1/recipients":
            page = "recipients_page2.json" if cursor else "recipients_page1.json"
            return httpx.Response(200, json=load_fixture(page))
        if path == "/api/v1/recipients/attachments":
            return httpx.Response(200, json=load_fixture("recipient_attachments_page1.json"))
        if path == "/api/v1/transactions" and self.transactions is not None:
            return httpx.Response(200, json=self._page_transactions(request))
        phase3 = self._phase3(request, path)
        if phase3 is not None:
            return phase3
        if path == "/api/v1/transactions":
            page = "transactions_page2.json" if cursor else "transactions_page1.json"
            data = load_fixture(page)
            limit = int(request.url.params.get("limit", "1000"))
            data["transactions"] = data["transactions"][:limit]
            return httpx.Response(200, json=data)
        return httpx.Response(404, json={"message": f"no fake route for {path}"})


    # -- Phase 3 routes ----------------------------------------------------

    def _phase3(self, request: httpx.Request, path: str) -> httpx.Response | None:
        p = request.url.params
        if path == "/api/v1/organization":
            return httpx.Response(200, json=load_fixture("organization.json"))
        if path == "/api/v1/credit":
            return httpx.Response(200, json=load_fixture("credit_accounts.json"))
        if path in _LIST_ROUTES:
            fixture, key, natural, filters = _LIST_ROUTES[path]
            rows = load_fixture(fixture)[key]
            for name in filters:
                if name in p:
                    wanted = set(p.get_list(name))
                    rows = [r for r in rows if r.get(name) in wanted]
            if path == "/api/v1/merchants" and p.get("search"):
                needle = p["search"].casefold()
                rows = [r for r in rows if needle in r["name"].casefold()]
            return httpx.Response(200, json=self._page(rows, key, request, natural))
        m = _ACCOUNT_STATEMENTS_RE.match(path)
        if m:
            if m.group(1) != KNOWN_ACCOUNT_ID:
                return httpx.Response(404, json={"message": "account not found"})
            rows = load_fixture("account_statements.json")["statements"]
            if p.get("start"):
                rows = [r for r in rows if r["startDate"][:10] >= p["start"]]
            if p.get("end"):
                rows = [r for r in rows if r["startDate"][:10] <= p["end"]]
            return httpx.Response(200, json=self._page(rows, "statements", request, "desc"))
        m = _TREASURY_STATEMENTS_RE.match(path)
        if m:
            if m.group(1) != KNOWN_TREASURY_ID:
                return httpx.Response(404, json={"message": "treasury account not found"})
            rows = load_fixture("treasury_statements.json")["statements"]
            if p.get("documentType"):
                rows = [r for r in rows if r["documentType"] == p["documentType"]]
            return httpx.Response(200, json=self._page(rows, "statements", request, "asc"))
        m = _TREASURY_TXNS_RE.match(path)
        if m:
            if m.group(1) != KNOWN_TREASURY_ID:
                return httpx.Response(404, json={"message": "treasury account not found"})
            rows = load_fixture("treasury_transactions.json")["transactions"]  # newest first
            if p.get("order", "desc") == "asc":
                rows = list(reversed(rows))
            offset = int(p.get("cursor", "0"))
            limit = int(p.get("limit", "100"))
            page = rows[offset : offset + limit]
            nxt = offset + limit if offset + limit < len(rows) else None
            return httpx.Response(200, json={"transactions": page, "cursor": nxt})
        m = _STATEMENT_PDF_RE.match(path)
        if m:
            known = {r["id"] for r in load_fixture("account_statements.json")["statements"]}
            if m.group(1) not in known:
                return httpx.Response(404, json={"message": "statement not found"})
            return self._pdf_response()
        m = _CARD_RE.match(path)
        if m:
            for c in load_fixture("cards.json")["cards"]:
                if c["id"] == m.group(1):
                    return httpx.Response(200, json=c)
            return httpx.Response(404, json={"message": "card not found"})
        m = _INVOICE_PDF_RE.match(path)
        if m:
            known = {r["id"] for r in load_fixture("invoices.json")["invoices"]}
            if m.group(1) not in known:
                return httpx.Response(404, json={"message": "invoice not found"})
            return self._pdf_response()
        m = _INVOICE_ATTACHMENTS_RE.match(path)
        if m:
            known = {r["id"] for r in load_fixture("invoices.json")["invoices"]}
            if m.group(1) not in known:
                return httpx.Response(404, json={"message": "invoice not found"})
            return httpx.Response(200, json=load_fixture("invoice_attachments.json"))
        m = _INVOICE_RE.match(path)
        if m:
            detail = load_fixture("invoice_detail.json")
            if m.group(1) == detail["id"]:
                return httpx.Response(200, json=detail)
            for inv in load_fixture("invoices.json")["invoices"]:
                if inv["id"] == m.group(1):
                    return httpx.Response(200, json={**inv, "lineItems": []})
            return httpx.Response(404, json={"message": "invoice not found"})
        return None

    def _pdf_response(self) -> httpx.Response:
        headers = {"Content-Type": self.pdf_content_type}
        if self.pdf_send_content_length:
            return httpx.Response(200, content=self.pdf_bytes, headers=headers)
        # A streamed body carries no Content-Length, so only the streaming cap can catch it.
        return httpx.Response(200, stream=httpx.ByteStream(self.pdf_bytes), headers=headers)

    @staticmethod
    def _page(rows: list[dict[str, Any]], key: str, request: httpx.Request, natural: str) -> dict[str, Any]:
        """Generic id-cursor paging: honour order (relative to the fixture's natural order), start_after, limit."""
        p = request.url.params
        rows = list(rows)
        if p.get("order", natural) != natural:
            rows.reverse()
        id_key = "userId" if key == "users" else "id"
        cursor = p.get("start_after")
        if cursor:
            ids = [r[id_key] for r in rows]
            rows = rows[ids.index(cursor) + 1 :] if cursor in ids else []
        limit = int(p.get("limit", "1000"))
        page, rest = rows[:limit], rows[limit:]
        return {key: page, "page": {"nextPage": page[-1][id_key] if rest and page else None, "previousPage": None}}

    def _page_transactions(self, request: httpx.Request) -> dict[str, Any]:
        """Mimic GET /transactions: posted-date and status filters, then start_after + limit paging."""
        p = request.url.params
        rows = list(self.transactions or [])
        posted_start, posted_end = p.get("postedStart"), p.get("postedEnd")
        if posted_start or posted_end:
            # The API filters on postedAt; a row without one cannot match a posted range.
            rows = [
                t
                for t in rows
                if t.get("postedAt")
                and (not posted_start or t["postedAt"] >= posted_start)
                and (not posted_end or t["postedAt"] <= posted_end)
            ]
        if "status" in p:
            wanted = set(p.get_list("status"))
            rows = [t for t in rows if t.get("status") in wanted]
        if p.get("order", "asc") == "desc":
            rows.reverse()
        cursor = p.get("start_after")
        if cursor:
            ids = [t["id"] for t in rows]
            rows = rows[ids.index(cursor) + 1 :] if cursor in ids else []
        limit = int(p.get("limit", "1000"))
        page, rest = rows[:limit], rows[limit:]
        return {
            "transactions": page,
            "page": {"nextPage": page[-1]["id"] if rest and page else None, "previousPage": None},
        }


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run every coroutine test under anyio without per-module marks."""
    for item in items:
        if isinstance(item, pytest.Function) and inspect.iscoroutinefunction(item.obj):
            item.add_marker(pytest.mark.anyio)


@pytest.fixture
def fake_api() -> Iterator[FakeMercury]:
    fake = FakeMercury()
    yield fake
    # Behavioural read-only guarantee: whatever a test did, only GETs reached the wire.
    assert [r.method for r in fake.requests] == ["GET"] * len(fake.requests)


@pytest.fixture
def registry() -> Registry:
    return Registry.from_path(EXAMPLE_REGISTRY)


@pytest.fixture
def env_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """acme_main has a token; acme_ops deliberately does not."""
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)


@pytest.fixture
def make_client(fake_api: FakeMercury):
    def factory(token: str) -> MercuryClient:
        return MercuryClient(
            token,
            api_base=FAKE_API_BASE,
            transport=httpx.MockTransport(fake_api.handler),
            sleep=_no_sleep,
        )

    return factory


@pytest.fixture
def server(registry: Registry, make_client, env_tokens: None):
    return build_server(registry, api_base=FAKE_API_BASE, client_factory=make_client)


@pytest.fixture
async def mcp_client(server) -> AsyncIterator[Client]:
    async with Client(server) as client:
        yield client
