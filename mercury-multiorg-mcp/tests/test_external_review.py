"""v0.1.1: one test group per finding of the independent external review of mercury-v0.1.0.

Majors M1-M6, minors m1-m4, and the threat-model hardening items. Every
case here reproduces the reviewer's scenario with synthetic data and a mock
transport, and asserts the fixed behaviour. Nothing touches the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from mcp import Client, StdioServerParameters

from mercury_multiorg_mcp import keepalive as keepalive_mod
from mercury_multiorg_mcp import server as server_mod
from mercury_multiorg_mcp.classify import MAX_THRESHOLD, summarize, validate_threshold
from mercury_multiorg_mcp.client import MAX_DOWNLOAD_BYTES, MercuryClient, endpoint_label, validate_api_base
from mercury_multiorg_mcp.errors import IncompletePaginationError, MercuryAPIError, RegistryError
from mercury_multiorg_mcp.projections import (
    _CARD_FIELDS,
    _INVOICE_DETAIL_FIELDS,
    _ORGANIZATION_FIELDS,
    _TRANSACTION_FIELDS,
    _TREASURY_ACCOUNT_FIELDS,
    _TREASURY_TRANSACTION_FIELDS,
    _project,
    project_card,
    project_event,
    project_invoice,
    project_organization,
    project_treasury_account,
    project_treasury_transaction,
)
from mercury_multiorg_mcp.registry import Registry
from mercury_multiorg_mcp.server import build_server

from .conftest import EXAMPLE_REGISTRY, FAKE_API_BASE, FAKE_TOKEN_MAIN, FakeMercury, load_fixture
from .test_tools import DEFAULT_TOOLS, DOCUMENT_TOOLS, EXPECTED_TOOLS, _error_text, _payload

# An unshaped synthetic secret: it has no `secret-token:` prefix, so only the
# *known-value* scrub (not the shape pattern) can catch it.
UNSHAPED_TOKEN = "SYNTHETIC-REVIEW-ONLY-plainvalue-0011223344"
STATEMENT_1 = "66666666-0001-4666-8666-666666666666"
INVOICE_1 = "1a000000-0001-4a00-8a00-1a0000000000"
TREASURY_1 = "33333333-3333-4333-8333-333333333333"


def _uid(n: int) -> str:
    return str(UUID(int=n))


async def _no_sleep(_: float) -> None:
    return None


def _server(handler, *, token_env_value: str = FAKE_TOKEN_MAIN, allow_documents: bool = True, registry: Registry | None = None):
    os.environ["MERCURY_TOKEN_ACME_MAIN"] = token_env_value

    def factory(token: str) -> MercuryClient:
        return MercuryClient(token, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=0)

    reg = registry or Registry.from_path(EXAMPLE_REGISTRY)
    return build_server(reg, api_base=FAKE_API_BASE, client_factory=factory, allow_documents=allow_documents)


async def _call(handler, tool: str, args: dict, **kw):
    async with Client(_server(handler, **kw)) as client:
        return await client.call_tool(tool, {"entity": "acme_main", **args})


# ---------------------------------------------------------------------------
# M1. One sanitized tool-error boundary
# ---------------------------------------------------------------------------


async def test_m1_content_type_carrying_a_token_never_reaches_the_error(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    handler = lambda req: httpx.Response(200, content=b"not a PDF", headers={"content-type": "text/plain; echoed-token=" + FAKE_TOKEN_MAIN})  # noqa: E731
    res = await _call(handler, "get_statement_pdf", {"statement_id": STATEMENT_1})
    text = _error_text(res)
    assert FAKE_TOKEN_MAIN not in text and "echoed-token" not in text and "text/plain" not in text
    assert text.endswith("[acme_main] GET /statements/{id}/pdf did not return a PDF (unexpected content type)")


async def test_m1_error_body_with_bank_coordinates_never_reaches_the_error(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    record = {
        "routingNumber": "CANARY_ERROR_ROUTING_000000000",
        "accountNumber": "CANARY_ERROR_ACCOUNT_999988887777",
        "address": "CANARY_ERROR_ADDRESS",
        "downloadUrl": "https://download.example/CANARY_ERROR_PRESIGNED?sig=CANARY_ERROR_SIGNATURE",
    }
    handler = lambda req: httpx.Response(400, json={"message": "Rejected account record", "record": record})  # noqa: E731
    res = await _call(handler, "list_accounts", {})
    text = _error_text(res)
    assert text.endswith("[acme_main] Mercury returned HTTP 400 for GET /accounts: request rejected by Mercury (check the arguments)")
    for key, value in record.items():
        assert key not in text and value not in text
    assert "Rejected" not in text


async def test_m1_invalid_caller_ids_are_not_echoed(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    requests: list[httpx.Request] = []

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json={})

    for tool, arg in (("get_card", "card_id"), ("get_invoice", "invoice_id"), ("list_statements", "account_id"), ("list_treasury_transactions", "treasury_id")):
        res = await _call(handler, tool, {arg: FAKE_TOKEN_MAIN})
        text = _error_text(res)
        assert f"[acme_main] {arg}: invalid id format" in text and FAKE_TOKEN_MAIN not in text, tool
    assert requests == []


async def test_m1_known_token_value_is_scrubbed_at_the_boundary_without_the_logging_filter(monkeypatch):
    """An unshaped token cannot be caught by the shape pattern; the boundary scrubs the known value itself."""
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)

    def leaky(self):  # simulate a regression that embeds the token in an error
        raise MercuryAPIError(f"unexpected: {UNSHAPED_TOKEN} in message")

    monkeypatch.setattr(MercuryClient, "list_accounts", leaky)
    res = await _call(lambda req: httpx.Response(200, json={}), "list_accounts", {}, token_env_value=UNSHAPED_TOKEN)
    text = _error_text(res)
    assert UNSHAPED_TOKEN not in text and "[REDACTED]" in text

    def leaky_value(self):
        raise ValueError(f"bad argument {UNSHAPED_TOKEN}")

    monkeypatch.setattr(MercuryClient, "list_accounts", leaky_value)
    res = await _call(lambda req: httpx.Response(200, json={}), "list_accounts", {}, token_env_value=UNSHAPED_TOKEN)
    assert UNSHAPED_TOKEN not in _error_text(res)


def test_m1_endpoint_labels_mask_every_id_segment():
    assert endpoint_label("/accounts") == "GET /accounts"
    assert endpoint_label(f"/account/{STATEMENT_1}/statements") == "GET /account/{id}/statements"
    assert endpoint_label(f"/ar/invoices/{INVOICE_1}/pdf") == "GET /ar/invoices/{id}/pdf"
    assert endpoint_label("/ar/invoices/pub-slug-1-secretish/pdf") == "GET /ar/invoices/{id}/pdf"
    assert endpoint_label(f"/cards/{FAKE_TOKEN_MAIN}") == "GET /cards/{id}"


async def test_m1_every_argument_validation_error_omits_the_value(mcp_client: Client, fake_api: FakeMercury):
    cases = [
        ("list_treasury_transactions", {"treasury_id": TREASURY_1, "start": "CANARY-START"}),
        ("list_invoices", {"end": "CANARY-END"}),
        ("list_invoices", {"status": "CANARY-STATUS"}),
        ("list_statements", {"account_id": TREASURY_1, "start": "2026-02-30"}),
        ("list_statements", {"account_id": TREASURY_1, "start": "2026-03-01", "end": "2026-02-01"}),
        ("list_events", {"since": "CANARY-SINCE"}),
        ("get_card", {"card_id": "CANARY/ID"}),
    ]
    for tool, args in cases:
        res = await mcp_client.call_tool(tool, {"entity": "acme_main", **args})
        text = _error_text(res)
        for value in args.values():
            if value != TREASURY_1:
                assert str(value) not in text, (tool, args, text)
    assert fake_api.requests == []


# ---------------------------------------------------------------------------
# M2. Webhook receiver URL dropped entirely
# ---------------------------------------------------------------------------


async def test_m2_hostname_capability_is_absent_from_every_part_of_the_result(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    capability = "synthetic-canary-webhook-capability-9342"
    url = f"https://{capability}.m.pipedream.net/"
    hook = {
        "id": _uid(1), "url": url, "status": "active", "eventTypes": ["transaction.created"], "filterPaths": None,
        "secret": None, "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
    }
    handler = lambda req: httpx.Response(200, json={"webhooks": [hook], "page": {"nextPage": None}})  # noqa: E731
    res = await _call(handler, "list_webhooks", {})
    assert not res.is_error
    wire = json.dumps({"structured": res.structured_content, "content": [c.model_dump(mode="json", by_alias=True) for c in res.content]})
    assert capability not in wire and "pipedream" not in wire and "https://" not in wire
    row = _payload(res)["webhooks"][0]
    assert set(row) == {"id", "status", "eventTypes", "filterPaths", "createdAt", "updatedAt", "url_fingerprint", "enabled"}
    assert row["url_fingerprint"] == hashlib.sha256(url.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# M3. Document tools are opt-in; PDF validation
# ---------------------------------------------------------------------------


async def test_m3_document_tools_absent_by_default_and_present_with_the_flag(registry, make_client, env_tokens):
    async with Client(build_server(registry, api_base=FAKE_API_BASE, client_factory=make_client)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == DEFAULT_TOOLS and len(names) == 24
        assert not names & DOCUMENT_TOOLS
        info = _payload(await client.call_tool("server_info", {}))
        assert info["documents_enabled"] is False
        res = await client.call_tool("get_statement_pdf", {"entity": "acme_main", "statement_id": STATEMENT_1})
        assert res.is_error
    async with Client(build_server(registry, api_base=FAKE_API_BASE, client_factory=make_client, allow_documents=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == EXPECTED_TOOLS and len(names) == 26
        assert _payload(await client.call_tool("server_info", {}))["documents_enabled"] is True


async def test_m3_real_stdio_process_honours_flag_and_env(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_MAIN", raising=False)
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    base_args = ["-m", "mercury_multiorg_mcp", "--entities", str(EXAMPLE_REGISTRY)]
    async with Client(StdioServerParameters(command=sys.executable, args=base_args)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == DEFAULT_TOOLS
    async with Client(StdioServerParameters(command=sys.executable, args=[*base_args, "--allow-documents"])) as client:
        assert {t.name for t in (await client.list_tools()).tools} == EXPECTED_TOOLS
        assert _payload(await client.call_tool("server_info", {}))["documents_enabled"] is True
    env = {**os.environ, "MERCURY_ALLOW_DOCUMENTS": "1"}
    async with Client(StdioServerParameters(command=sys.executable, args=base_args, env=env)) as client:
        assert {t.name for t in (await client.list_tools()).tools} == EXPECTED_TOOLS


def test_m3_env_flag_parsing(monkeypatch):
    for value, expected in (("1", True), ("true", True), ("YES", True), (" on ", True), ("0", False), ("", False), ("no", False), ("maybe", False)):
        monkeypatch.setenv("MERCURY_ALLOW_DOCUMENTS", value)
        assert server_mod.env_flag("MERCURY_ALLOW_DOCUMENTS") is expected, value


# (async tests are not parametrized here: conftest applies the anyio marker after parametrization)
_NON_PDF_CASES = [
    ("text/html", b"%PDF definitely not a valid PDF", "unexpected content type"),
    ("application/pdf", b"<html>not a pdf</html>%%EOF", "missing %PDF- header"),
    ("application/pdf", b"%PDF-1.4\nno trailer at all", "no %%EOF marker"),
    ("application/pdf", b"", "missing %PDF- header"),
    ("application/json", b"%PDF-1.4\n%%EOF\n", "unexpected content type"),
]


async def test_m3_pdf_validation_rejects_non_pdf_bodies_without_echo(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    for content_type, body, fragment in _NON_PDF_CASES:
        handler = lambda req, b=body, ct=content_type: httpx.Response(200, content=b, headers={"content-type": ct})  # noqa: E731
        for tool, args in (("get_statement_pdf", {"statement_id": STATEMENT_1}), ("get_invoice_pdf", {"invoice_id": INVOICE_1})):
            res = await _call(handler, tool, args)
            text = _error_text(res)
            assert fragment in text, (tool, content_type, text)
            assert "html" not in text and "definitely" not in text and content_type not in text


async def test_m3_accepted_pdf_content_types(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    body = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
    for content_type in ("application/pdf", "application/PDF; charset=binary", "application/octet-stream"):
        handler = lambda req, ct=content_type: httpx.Response(200, content=body, headers={"content-type": ct})  # noqa: E731
        res = await _call(handler, "get_statement_pdf", {"statement_id": STATEMENT_1})
        assert not res.is_error, (content_type, _error_text(res))
        meta = json.loads(res.content[0].text)
        assert meta["redacted"] is False and meta["bytes"] == len(body)


# ---------------------------------------------------------------------------
# M4. Byte caps on wire bytes; compression declined
# ---------------------------------------------------------------------------


async def test_m4_every_request_declines_compression(fake_api):
    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(fake_api.handler), sleep=_no_sleep)
    async with c:
        await c.list_accounts()
        await c.get_statement_pdf(STATEMENT_1)
    assert all(r.headers["Accept-Encoding"] == "identity" for r in fake_api.requests)


async def test_m4_compressed_download_is_rejected_before_any_byte_is_read():
    expanded = 32 * 1024 * 1024
    compressed = gzip.compress(b"%PDF-1.4\n" + b"0" * (expanded - 9))
    assert len(compressed) < 64 * 1024
    yielded = 0

    class Wire(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            yielded += 1
            yield compressed

    def handler(req):
        return httpx.Response(200, headers={"content-type": "application/pdf", "content-encoding": "gzip", "content-length": str(len(compressed))}, stream=Wire())

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    tracemalloc.start()
    try:
        async with c:
            with pytest.raises(MercuryAPIError, match="compressed body, which this client refuses"):
                await c.get_statement_pdf(STATEMENT_1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert yielded == 0  # rejected on the header alone
    assert peak < 4 * len(compressed) + 512 * 1024  # nothing close to the 32 MiB expansion, let alone the 81 MB the reviewer saw


async def test_m4_any_non_identity_content_encoding_is_refused_for_json_too():
    for encoding in ("gzip", "br", "deflate, gzip", "identity, gzip", "GZIP"):

        def handler(req, enc=encoding):
            # streamed, so the mock itself does not try to decode the body before the client sees the header
            return httpx.Response(200, stream=httpx.ByteStream(b'{"accounts": []}'), headers={"content-encoding": enc})

        c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep)
        async with c:
            with pytest.raises(MercuryAPIError, match="compressed body"):
                await c.list_accounts()


async def test_m4_identity_content_encoding_is_accepted():
    def handler(req):
        return httpx.Response(200, content=b'{"accounts": [], "page": {}}', headers={"content-encoding": "identity"})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep)
    async with c:
        assert await c.list_accounts() == []


async def test_m4_json_bodies_are_capped_by_declared_length_and_by_stream(monkeypatch):
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "MAX_JSON_BYTES", 100)
    big = b'{"accounts": [' + b'{"id": "x"},' * 30 + b'{"id": "y"}], "page": {}}'
    assert len(big) > 100

    def declared(req):
        return httpx.Response(200, content=big)  # Content-Length set by httpx

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(declared), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="above the 100-byte limit"):
            await c.list_accounts()

    yielded = 0

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal yielded
            for i in range(0, len(big), 40):
                yielded += 1
                yield big[i : i + 40]

    def streamed(req):
        return httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"})

    c = MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(streamed), sleep=_no_sleep)
    async with c:
        with pytest.raises(MercuryAPIError, match="exceeded the 100-byte limit"):
            await c.list_accounts()
    assert yielded == 3  # stopped as soon as the cap was crossed, not after the whole body
    assert client_mod.MAX_DOWNLOAD_BYTES == 10 * 1024 * 1024 and MAX_DOWNLOAD_BYTES == 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# M5. Client-side windows walk the full feed
# ---------------------------------------------------------------------------


def _event(n: int, day: str) -> dict:
    return {
        "id": _uid(n), "occurredAt": day + "T00:00:00Z", "resourceType": "transaction", "resourceId": _uid(99),
        "operationType": "create", "resourceVersion": 1, "changedPaths": [], "mergePatch": {},
    }


async def test_m5_events_unseen_page_inversion_is_reported_honestly(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    requests: list[str] = []

    def events(req):
        requests.append(str(req.url))
        cursor = req.url.params.get("start_after")
        rows = [_event(9, "2026-09-10"), _event(8, "2026-08-01")] if cursor is None else [_event(7, "2026-09-09")]
        return httpx.Response(200, json={"events": rows, "page": {"nextPage": _uid(8) if cursor is None else None, "previousPage": None}})

    data = _payload(await _call(events, "list_events", {"since": "2026-09-01", "limit": 1}))
    assert [e["id"] for e in data["events"]] == [_uid(9)]
    assert data["truncated"] is True and data["count"] == 1
    assert "order_verified" not in data
    assert len(requests) == 2  # both pages walked
    data = _payload(await _call(events, "list_events", {"since": "2026-09-01"}))
    assert [e["id"] for e in data["events"]] == [_uid(9), _uid(7)] and data["truncated"] is False


async def test_m5_treasury_same_page_inversion_is_reported_honestly(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)

    def treasury(req):
        rows = [
            {"id": _uid(i), "canonicalDay": d, "amount": 1, "type": "depositComplete", "balance": 1, "accountId": _uid(99), "description": "Synthetic"}
            for i, d in [(9, "2026-09-10"), (8, "2026-08-01"), (7, "2026-09-09")]
        ]
        return httpx.Response(200, json={"transactions": rows, "cursor": None})

    data = _payload(await _call(treasury, "list_treasury_transactions", {"treasury_id": _uid(99), "start": "2026-09-01", "limit": 1}))
    assert [r["id"] for r in data["transactions"]] == [_uid(9)] and data["truncated"] is True
    data = _payload(await _call(treasury, "list_treasury_transactions", {"treasury_id": _uid(99), "start": "2026-09-01"}))
    assert [r["canonicalDay"] for r in data["transactions"]] == ["2026-09-10", "2026-09-09"] and data["truncated"] is False


async def test_m5_windowed_walk_that_cannot_complete_is_an_error(monkeypatch):
    import mercury_multiorg_mcp.client as client_mod

    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    monkeypatch.setattr(client_mod, "MAX_PAGES", 2)
    calls = 0

    def endless(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"events": [_event(calls, "2026-09-10")], "page": {"nextPage": _uid(calls), "previousPage": None}})

    res = await _call(endless, "list_events", {"since": "2026-09-01", "limit": 1})
    text = _error_text(res)
    assert "more than 2 pages" in text and "Traceback" not in text


# ---------------------------------------------------------------------------
# M6. Stalled pagination fails loudly
# ---------------------------------------------------------------------------


def _txn(n: int, amount: float = -1500.0) -> dict:
    return {
        "id": _uid(n), "postedAt": "2026-06-01T00:00:00Z", "createdAt": "2026-06-01T00:00:00Z", "amount": amount,
        "status": "sent", "kind": "outgoingPayment", "counterpartyId": _uid(99), "counterpartyName": "Acme Review Payee",
        "accountId": _uid(98),
    }


async def test_m6_reportable_totals_fails_loudly_on_a_stalled_cursor(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    requests: list[str] = []

    def duplicate_page(req):
        requests.append(req.url.path)
        if req.url.path.endswith("/recipients"):
            return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
        return httpx.Response(200, json={"transactions": [_txn(1)], "page": {"nextPage": _uid(2)}})

    res = await _call(duplicate_page, "reportable_totals", {"year": 2026})
    text = _error_text(res)
    assert "[acme_main]" in text and "incomplete pagination" in text and "GET /transactions" in text
    assert "1500" not in text and "Traceback" not in text
    assert requests.count("/api/v1/transactions") == 2 and "/api/v1/recipients" not in requests


async def test_m6_a_clean_walk_still_totals(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)

    def normal(req):
        if req.url.path.endswith("/recipients"):
            return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
        return httpx.Response(200, json={"transactions": [_txn(1)], "page": {"nextPage": None}})

    data = _payload(await _call(normal, "reportable_totals", {"year": 2026}))
    assert data["totals"]["reportable_total"] == 1500.0 and data["totals"]["transactions_scanned"] == 1


def test_m6_incomplete_pagination_is_a_mercury_api_error_subclass():
    assert issubclass(IncompletePaginationError, MercuryAPIError)


# ---------------------------------------------------------------------------
# m1. Allowlists at every level
# ---------------------------------------------------------------------------

_DRIFT_KEYS = ("routingNumber", "accountNumber", "iban", "swiftCode", "address", "cardNumber", "expiration", "cvv", "ein", "downloadUrl", "secret", "phoneNumber")


def _plant(obj: dict) -> dict:
    for i, key in enumerate(_DRIFT_KEYS):
        obj[key] = f"CANARY_NESTED_{i:02d}_END"
    obj["nestedObject"] = {"routingNumber": "CANARY_DEEP_END"}
    return obj


def test_m1_nested_canaries_never_survive_projection():
    """Plant unknown keys inside every nested shape (the reviewer's 216-canary drift stage) and assert none survive."""
    tx = load_fixture("transactions_page1.json")["transactions"][1]
    for key in ("merchant", "categoryData", "currencyExchangeInfo"):
        tx[key] = _plant(dict(tx.get(key) or {}))
    org = load_fixture("organization.json")["organization"]
    for dba in org["dbas"]:
        _plant(dba)
    treasury = load_fixture("treasury_accounts.json")["accounts"][0]
    _plant(treasury["netReturns"][0])
    _plant(treasury["netReturns"][0]["dividends"][0])
    ttx = load_fixture("treasury_transactions.json")["transactions"][0]
    _plant(ttx["details"])
    cards = load_fixture("cards.json")["cards"]
    _plant(cards[0]["spendLimit"])
    _plant(cards[0]["merchantLock"])
    _plant(cards[2]["budgets"][0])
    cards[0]["categoryLocks"] = ["Software", {"routingNumber": "CANARY_IN_LIST_END"}]
    invoice = load_fixture("invoice_detail.json")
    _plant(invoice["lineItems"][0])
    base = load_fixture("events.json")["events"][0]
    events = []
    for kind in ("transaction", "checkingAccount", "treasuryAccount", "investmentAccount", "creditAccount", "savingsAccount"):
        ev = {**base, "resourceType": kind}
        for patch in ("mergePatch", "previousValues"):
            ev[patch] = _plant({"merchant": _plant({}), "netReturns": [_plant({"dividends": [_plant({})]})], "spendLimit": _plant({})})
        events.append(ev)

    outputs = [
        _project(tx, _TRANSACTION_FIELDS),
        project_organization(org),
        project_treasury_account(treasury),
        project_treasury_transaction(ttx),
        *(project_card(c) for c in cards),
        project_invoice(invoice, detail=True),
        *(project_event(e) for e in events),
    ]
    dumped = json.dumps(outputs)
    assert "CANARY" not in dumped and "nestedObject" not in dumped
    for key in _DRIFT_KEYS:
        assert f'"{key}"' not in dumped, key
    # the known shapes are still fully present
    assert _project(tx, _TRANSACTION_FIELDS)["merchant"] == {"id": "m-fake-0001", "category": "OfficeSupplies", "categoryCode": "5943", "currency": "USD", "amount": -4210}
    assert project_card(cards[0])["categoryLocks"] == ["Software"]
    assert project_treasury_account(treasury)["netReturns"][0]["dividends"][0]["securityName"] == "EXAMPLE GOVT MMF"


def test_m1_shape_mismatches_become_null_or_are_dropped():
    assert _project({"dashboardLink": {"routingNumber": "x"}}, _TRANSACTION_FIELDS) == {"dashboardLink": None}
    assert _project({"merchant": "not an object"}, _TRANSACTION_FIELDS) == {"merchant": None}
    assert _project({"dbas": {"dbaName": "x"}}, _ORGANIZATION_FIELDS) == {"dbas": None}
    assert _project({"dbas": [{"dbaName": "x"}, "junk", 3]}, _ORGANIZATION_FIELDS) == {"dbas": [{"dbaName": "x"}]}
    assert _project({"categoryLocks": "Software"}, _CARD_FIELDS) == {"categoryLocks": None}
    assert _project({"lineItems": [{"name": "a", "cvv": "123"}]}, _INVOICE_DETAIL_FIELDS) == {"lineItems": [{"name": "a"}]}
    assert _project({"details": None}, _TREASURY_TRANSACTION_FIELDS) == {"details": None}
    assert _project({"netReturns": [{"month": "2026-03", "dividends": None}]}, _TREASURY_ACCOUNT_FIELDS) == {"netReturns": [{"month": "2026-03", "dividends": None}]}


async def test_m1_end_to_end_nested_canaries_through_the_protocol(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    card = load_fixture("cards.json")["cards"][0]
    _plant(card["spendLimit"])
    _plant(card["merchantLock"])
    card["budgets"] = [_plant({"id": "b1", "name": "Travel", "amountCents": 1, "remainingAmountCents": 1})]
    handler = lambda req: httpx.Response(200, json={"cards": [card], "page": {"nextPage": None}})  # noqa: E731
    res = await _call(handler, "list_cards", {})
    wire = json.dumps({"structured": res.structured_content, "content": [c.model_dump(mode="json") for c in res.content]})
    assert not res.is_error and "CANARY" not in wire
    for key in _DRIFT_KEYS:
        assert f'"{key}"' not in wire


# ---------------------------------------------------------------------------
# m2. Threshold validation
# ---------------------------------------------------------------------------


async def test_m2_unrepresentable_thresholds_are_rejected_never_zero(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)
    requests: list[str] = []

    def normal(req):
        requests.append(req.url.path)
        return httpx.Response(200, json={"transactions": [_txn(1)], "recipients": [], "page": {"nextPage": None}})

    # inf/nan cannot be sent through the SDK client (JSON has no such literals; pydantic serialises them as null),
    # so they are covered at the validator level in test_m2_classifier_never_coerces_a_bad_threshold_to_zero.
    for bad in (1e30, 1e300, -1, -0.01, MAX_THRESHOLD + 1, MAX_THRESHOLD + 0.01):
        res = await _call(normal, "reportable_totals", {"year": 2026, "threshold": bad})
        assert res.is_error, bad
        text = _error_text(res)
        assert "threshold must" in text and "Traceback" not in text and str(bad) not in text, bad
    assert requests == []  # rejected before any request


async def test_m2_boundary_thresholds_are_accepted_and_echoed(monkeypatch):
    monkeypatch.delenv("MERCURY_TOKEN_ACME_OPS", raising=False)

    def normal(req):
        if req.url.path.endswith("/recipients"):
            return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
        return httpx.Response(200, json={"transactions": [_txn(1)], "page": {"nextPage": None}})

    data = _payload(await _call(normal, "reportable_totals", {"year": 2026, "threshold": MAX_THRESHOLD}))
    assert data["threshold"] == float(MAX_THRESHOLD) and data["totals"]["flagged_count"] == 0
    data = _payload(await _call(normal, "reportable_totals", {"year": 2026, "threshold": 0}))
    assert data["threshold"] == 0.0 and data["totals"]["flagged_count"] == 1
    data = _payload(await _call(normal, "reportable_totals", {"year": 2026, "threshold": 1500.005}))
    assert data["threshold"] == 1500.01  # cents, half-up


def test_m2_classifier_never_coerces_a_bad_threshold_to_zero():
    rows = [_txn(1)]
    for bad in (1e30, float("inf"), float("nan"), -1, "600", None, True, MAX_THRESHOLD + 0.01):
        with pytest.raises(ValueError, match="threshold"):
            summarize(rows, year=2026, threshold=bad)  # type: ignore[arg-type]
    assert validate_threshold(600) == validate_threshold(600.0) == validate_threshold(599.995)
    assert summarize(rows, year=2026, threshold=0)["threshold"] == 0.0


# ---------------------------------------------------------------------------
# m4. Every startup error path exits 2 with one line
# ---------------------------------------------------------------------------

_SUBPROCESS_ENV = {"PATH": "/usr/bin:/bin", "MERCURY_TOKEN_ACME_MAIN": "", "MERCURY_TOKEN_ACME_OPS": ""}


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def no_run(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(server_mod.MCPServer, "run", lambda self, transport="stdio", **kw: calls.append(transport))
    return calls


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("MERCURY_ENTITIES_FILE", "MERCURY_API_BASE", "MERCURY_TOKEN_ACME_MAIN", "MERCURY_TOKEN_ACME_OPS", "MERCURY_ALLOW_DOCUMENTS"):
        monkeypatch.delenv(var, raising=False)


def test_m4_non_string_registry_key_is_a_registry_error():
    with pytest.raises(RegistryError, match="top-level keys must be strings"):
        Registry.from_mapping({"entities": [{"key": "s", "display_name": "S", "token_env": "MERCURY_TOKEN_S"}], 123: "extra"})
    with pytest.raises(RegistryError, match=r"entities\[0\].*field names must be strings"):
        Registry.from_mapping({"entities": [{"key": "s", "display_name": "S", "token_env": "MERCURY_TOKEN_S", 7: "x"}]})
    with pytest.raises(RegistryError, match=r"entities\[0\].*must be a mapping"):
        Registry.from_mapping({"entities": ["just a string"]})


@pytest.mark.parametrize(
    "name, text, fragment",
    [
        ("int-key.yaml", "entities:\n  - key: synthetic\n    display_name: Synthetic Example\n    token_env: MERCURY_TOKEN_SYNTHETIC\n123: synthetic-extra-key\n", "keys must be strings"),
        ("null-key.yaml", "entities:\n  - key: s\n    display_name: S\n    token_env: MERCURY_TOKEN_S\n~: x\n", "keys must be strings"),
        ("scalar-entry.yaml", "entities:\n  - just-a-string\n", "must be a mapping"),
        ("bad-env.yaml", "entities:\n  - key: s\n    display_name: S\n    token_env: AWS_SECRET_ACCESS_KEY\n", "MERCURY_TOKEN_"),
    ],
)
def test_m4_malformed_registries_exit_2_with_one_line_on_both_clis(clean_env, capsys, no_run, tmp_path, name, text, fragment):
    registry = _write(tmp_path, name, text)
    assert server_mod.main(["--entities", str(registry)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err and fragment in err and err.startswith("mercury-multiorg-mcp: ")
    assert no_run == []
    assert keepalive_mod.main(["--entities", str(registry)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err and fragment in err and err.startswith("mercury-multiorg-mcp-keepalive: ")


def test_m4_unreadable_and_binary_registries_exit_2(clean_env, capsys, no_run, tmp_path):
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00entities: []")
    assert server_mod.main(["--entities", str(binary)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err and "UTF-8" in err
    assert server_mod.main(["--entities", str(tmp_path)]) == 2  # a directory
    assert "not found" in capsys.readouterr().err
    missing_env = tmp_path / "missing.env"
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--env-file", str(missing_env)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "--env-file" in err and "Traceback" not in err
    assert no_run == []


def test_m4_reviewer_registry_through_the_console_scripts():
    """The reviewer's exact reproduction: both console scripts, integer top-level key, subprocess."""
    scripts = [Path(sys.executable).with_name(n) for n in ("mercury-multiorg-mcp", "mercury-multiorg-mcp-keepalive")]
    assert all(s.is_file() for s in scripts), "console scripts not installed; run `uv sync`"
    registry = Path(__file__).parent / "fixtures" / "registry_integer_key.yaml"
    for script in scripts:
        proc = subprocess.run([str(script), "--entities", str(registry)], capture_output=True, text=True, env=_SUBPROCESS_ENV, timeout=60)
        assert proc.returncode == 2, (script.name, proc.stderr)
        assert proc.stdout == "" and proc.stderr.count("\n") == 1 and not proc.stderr.startswith("Traceback")
        assert "keys must be strings" in proc.stderr


def test_m4_excepthooks_are_installed_before_the_registry_loads(clean_env, monkeypatch, tmp_path):
    import threading

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    before = sys.excepthook
    seen: list[str] = []

    def exploding(cls, path):
        seen.append("hooks installed" if sys.excepthook is not before else "hooks missing")
        raise RegistryError("boom")

    monkeypatch.setattr(Registry, "from_path", classmethod(exploding))
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY)]) == 2
    assert seen == ["hooks installed"]


# ---------------------------------------------------------------------------
# Hardening: token_env namespace, api_base allowlist, token shape warning
# ---------------------------------------------------------------------------


def test_hardening_hostile_registry_cannot_name_an_unrelated_env_var(monkeypatch):
    unrelated = "REVIEW_UNRELATED_SECRET"
    monkeypatch.setenv(unrelated, "SYNTHETIC_UNRELATED_CREDENTIAL_12345678")
    with pytest.raises(RegistryError, match="MERCURY_TOKEN_"):
        Registry.from_mapping({"entities": [{"key": "hostile", "display_name": "Synthetic Hostile Registry", "token_env": unrelated}]})


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://api.mercury.com", True),
        ("https://api-sandbox.mercury.com/", True),
        ("http://localhost:8080", True),
        ("http://127.0.0.1:9999", True),
        ("https://localhost", True),
        ("https://collector.example", False),
        ("https://api.mercury.com.evil.example", False),
        ("https://evil-api.mercury.com", False),
        ("https://api.mercury.example", False),
    ],
)
def test_hardening_api_base_allowlist(url, ok):
    if ok:
        assert validate_api_base(url) == url.rstrip("/")
    else:
        with pytest.raises(ValueError, match="allow-custom-api-base") as info:
            validate_api_base(url)
        assert "collector" not in str(info.value) or url == "https://collector.example"  # the host name is the only echo
        assert validate_api_base(url, allow_custom=True) == url.rstrip("/")


def test_hardening_custom_api_base_needs_the_flag_on_both_clis(clean_env, capsys, no_run, monkeypatch):
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://collector.example"]) == 2
    err = capsys.readouterr().err
    assert "allow-custom-api-base" in err and err.count("\n") == 1 and no_run == []
    monkeypatch.setenv("MERCURY_API_BASE", "https://collector.example")
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY)]) == 2  # the environment alone cannot redirect the token
    assert no_run == []
    monkeypatch.delenv("MERCURY_API_BASE")
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://collector.example", "--allow-custom-api-base"]) == 0
    assert no_run == ["stdio"] and "api_base=https://collector.example" in capsys.readouterr().err
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://api-sandbox.mercury.com"]) == 0

    def factory(token: str, api_base: str) -> MercuryClient:
        return MercuryClient(token, api_base=api_base, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"accounts": [], "page": {}})))

    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    assert keepalive_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://collector.example"], client_factory=factory) == 2
    assert "allow-custom-api-base" in capsys.readouterr().err
    assert keepalive_mod.main(["--entities", str(EXAMPLE_REGISTRY), "--api-base", "https://collector.example", "--allow-custom-api-base"], client_factory=factory) == 1  # ops has no token
    assert "OK acme_main" in capsys.readouterr().out


def test_hardening_token_shape_warning_shows_last_four_only(clean_env, capsys, no_run, monkeypatch):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", UNSHAPED_TOKEN)
    monkeypatch.setenv("MERCURY_TOKEN_ACME_OPS", FAKE_TOKEN_MAIN)
    assert server_mod.main(["--entities", str(EXAMPLE_REGISTRY)]) == 0
    err = capsys.readouterr().err
    warnings = [ln for ln in err.splitlines() if "does not look like a Mercury API token" in ln]
    assert len(warnings) == 1 and "'acme_main'" in warnings[0] and "MERCURY_TOKEN_ACME_MAIN" in warnings[0]
    assert "...3344" in warnings[0] and UNSHAPED_TOKEN not in err and UNSHAPED_TOKEN[:-4] not in err
    assert FAKE_TOKEN_MAIN not in err and "acme_ops" not in " ".join(warnings)
    reg = Registry.from_path(EXAMPLE_REGISTRY)
    assert len(reg.token_shape_warnings()) == 1
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", FAKE_TOKEN_MAIN)
    assert reg.token_shape_warnings() == []


def test_hardening_keepalive_warns_too(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("MERCURY_TOKEN_ACME_MAIN", UNSHAPED_TOKEN)

    def factory(token: str, api_base: str) -> MercuryClient:
        return MercuryClient(token, api_base=api_base, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"accounts": [], "page": {}})))

    assert keepalive_mod.main(["--entities", str(EXAMPLE_REGISTRY)], client_factory=factory) == 1
    out, err = capsys.readouterr()
    assert "does not look like a Mercury API token" in err and "...3344" in err and UNSHAPED_TOKEN not in err + out


def test_hardening_readme_and_docs_carry_the_new_contract():
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    tools = (root / "docs" / "tools.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "](CLAUDE.md)" not in readme and "`CLAUDE.md`" not in readme  # the sdist omits it; link to GitHub instead
    # v0.1.3: pinned to the release tag, since PyPI renders this README out of context
    assert "https://github.com/dkaleganov/personal-ai-systems/blob/mercury-v0.1.3/mercury-multiorg-mcp/CLAUDE.md" in readme
    assert "blob/main/mercury-multiorg-mcp/CLAUDE.md" not in readme
    for needle in ("--allow-documents", "unredacted", "history note", "url_fingerprint", "allowlisted at every level", "--allow-custom-api-base", "wire bytes"):
        assert needle in readme.lower(), needle
    for needle in ("url_fingerprint", "--allow-documents", "walked in full", "every level"):
        assert needle in tools, needle
    assert "order_verified" not in tools and "order_verified" not in readme and "path_fingerprint" not in readme
    from mercury_multiorg_mcp import __version__

    assert "0.1.1" in changelog and __version__ in readme and __version__ in changelog
