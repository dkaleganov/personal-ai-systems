"""v0.1.1 re-validation: one test group per finding of the reviewer's second pass (B1-B8) plus the doc contract.

Synthetic data and a mock transport only; nothing touches the network.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ValidationError

from mercury_multiorg_mcp import keepalive as keepalive_mod
from mercury_multiorg_mcp import server as server_mod
from mercury_multiorg_mcp.classify import summarize
from mercury_multiorg_mcp.client import PDF_EOF_WINDOW, MercuryClient, RowList, validate_pdf_bytes
from mercury_multiorg_mcp.errors import IncompletePaginationError, MercuryAPIError, RegistryError
from mercury_multiorg_mcp.projections import scalar, scalar_str
from mercury_multiorg_mcp.registry import Registry
from mercury_multiorg_mcp.server import SanitizingMCPServer, build_server, render_validation_error

from .conftest import EXAMPLE_REGISTRY, FAKE_API_BASE, FAKE_TOKEN_MAIN, KNOWN_ACCOUNT_ID, FakeMercury
from .test_tools import DEFAULT_TOOLS, _error_text, _payload

STATEMENT_1 = "66666666-0001-4666-8666-666666666666"
TREASURY_1 = "33333333-3333-4333-8333-333333333333"
ROOT = Path(__file__).resolve().parent.parent


def _uid(n: int) -> str:
    return str(UUID(int=n))


async def _no_sleep(_: float) -> None:
    return None


def _txn(n: int, amount: float = -1500.0, **extra) -> dict:
    return {
        "id": _uid(n), "postedAt": "2026-06-01T00:00:00Z", "createdAt": "2026-06-01T00:00:00Z", "amount": amount,
        "status": "sent", "kind": "outgoingPayment", "counterpartyId": _uid(99), "counterpartyName": "Acme Review Payee",
        "accountId": _uid(98), **extra,
    }


def _client(handler, **kw) -> MercuryClient:
    return MercuryClient(FAKE_TOKEN_MAIN, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=0, **kw)


async def _call(handler, tool: str, args: dict, *, allow_documents: bool = True, token: str = FAKE_TOKEN_MAIN):
    os.environ["MERCURY_TOKEN_ACME_MAIN"] = token
    os.environ.pop("MERCURY_TOKEN_ACME_OPS", None)
    server = build_server(
        Registry.from_path(EXAMPLE_REGISTRY),
        api_base=FAKE_API_BASE,
        client_factory=lambda t: MercuryClient(t, api_base=FAKE_API_BASE, transport=httpx.MockTransport(handler), sleep=_no_sleep, max_retries=0),
        allow_documents=allow_documents,
    )
    async with Client(server) as client:
        return await client.call_tool(tool, {"entity": "acme_main", **args})


# The id-cursor list tools, with the envelope key each walks (used to sweep B1/B2 over every walker).
_LIST_TOOLS = [
    ("list_accounts", {}, "accounts", "id"),
    ("list_transactions", {}, "transactions", "id"),
    ("list_recipients", {}, "recipients", "id"),
    ("list_statements", {"account_id": KNOWN_ACCOUNT_ID}, "statements", "id"),
    ("list_treasury", {}, "accounts", "id"),
    ("list_treasury_statements", {"treasury_id": TREASURY_1}, "statements", "id"),
    ("list_cards", {}, "cards", "id"),
    ("list_categories", {}, "categories", "id"),
    ("list_merchants", {}, "data", "id"),
    ("list_customers", {}, "customers", "id"),
    ("list_invoices", {}, "invoices", "id"),
    ("list_users", {}, "users", "userId"),
    ("list_events", {}, "events", "id"),
    ("list_webhooks", {}, "webhooks", "id"),
]


# ---------------------------------------------------------------------------
# B1. Malformed pagination metadata is never "complete"
# ---------------------------------------------------------------------------


async def test_b1_missing_or_wrong_type_page_is_an_error_not_a_total():
    for page in ("synthetic-invalid-pagination", None, [], 7, "MISSING"):
        def handler(req, page=page):
            if req.url.path.endswith("/recipients"):
                return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
            payload = {"transactions": [_txn(1)]}
            if page != "MISSING":
                payload["page"] = page
            return httpx.Response(200, json=payload)

        res = await _call(handler, "reportable_totals", {"year": 2026})
        text = _error_text(res)
        assert "malformed pagination metadata" in text and "'page'" in text, page
        assert res.structured_content is None and '"totals"' not in text and "1500" not in text


async def test_b1_next_page_must_be_null_or_a_non_empty_string():
    for bad in (7, "", {"id": "x"}, [], True):
        def handler(req, bad=bad):
            return httpx.Response(200, json={"accounts": [{"id": "a"}], "page": {"nextPage": bad, "previousPage": None}})

        async with _client(handler) as c:
            with pytest.raises(IncompletePaginationError, match="malformed pagination metadata.*'nextPage'"):
                await c.list_accounts()


async def test_b1_legitimate_terminal_envelopes_still_complete():
    """Reviewer controls: null cursor, absent nextPage key, and an empty final page all mean done."""
    for terminal in ({"nextPage": None, "previousPage": None}, {"nextPage": None}, {}):
        calls = 0

        def handler(req, terminal=terminal):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"accounts": [{"id": "a"}, {"id": "b"}], "page": {"nextPage": "b"}})
            return httpx.Response(200, json={"accounts": [], "page": terminal})

        async with _client(handler) as c:
            rows = await c.list_accounts()
        assert [r["id"] for r in rows] == ["a", "b"] and calls == 2 and rows.duplicates_dropped == 0


async def test_b1_every_id_cursor_tool_rejects_a_malformed_envelope():
    for tool, args, key, id_key in _LIST_TOOLS:
        def handler(req, key=key, id_key=id_key):
            return httpx.Response(200, json={key: [{id_key: "x"}], "page": "not-an-object"})

        res = await _call(handler, tool, args)
        text = _error_text(res)
        assert "malformed pagination metadata" in text and "[acme_main]" in text, tool


async def test_b1_treasury_cursor_absent_means_done_but_wrong_types_do_not():
    def absent(req):
        return httpx.Response(200, json={"transactions": [{"id": "t1"}]})

    async with _client(absent) as c:
        assert [r["id"] for r in await c.list_treasury_transactions(TREASURY_1)] == ["t1"]
    for bad in (True, 1.5, "2", {}, [1]):
        def handler(req, bad=bad):
            return httpx.Response(200, json={"transactions": [{"id": "t1"}], "cursor": bad})

        calls = 0

        def counting(req, handler=handler):
            nonlocal calls
            calls += 1
            return handler(req)

        async with _client(counting) as c:
            with pytest.raises(IncompletePaginationError, match="malformed pagination metadata.*'cursor'"):
                await c.list_treasury_transactions(TREASURY_1)
        assert calls == 1


# ---------------------------------------------------------------------------
# B2. Duplicate rows inside one page
# ---------------------------------------------------------------------------


async def test_b2_same_page_duplicate_is_dropped_counted_and_not_double_totalled():
    def handler(req):
        if req.url.path.endswith("/recipients"):
            return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
        return httpx.Response(200, json={"transactions": [_txn(1), _txn(1)], "page": {"nextPage": None}})

    data = _payload(await _call(handler, "reportable_totals", {"year": 2026}))
    assert data["totals"]["reportable_total"] == 1500.0 and data["totals"]["transactions_scanned"] == 1
    assert data["totals"]["duplicates_dropped"] == 1 and data["totals"]["recipient_duplicates_dropped"] == 0


async def test_b2_cross_page_and_same_page_duplicates_are_counted_together():
    pages = {
        None: ([{"id": "a"}, {"id": "a"}, {"id": "b"}], "b"),
        "b": ([{"id": "b"}, {"id": "c"}, {"id": "c"}], None),
    }

    def handler(req):
        rows, nxt = pages[req.url.params.get("start_after")]
        return httpx.Response(200, json={"accounts": rows, "page": {"nextPage": nxt}})

    async with _client(handler) as c:
        rows = await c.list_accounts()
    assert isinstance(rows, RowList) and [r["id"] for r in rows] == ["a", "b", "c"] and rows.duplicates_dropped == 3


async def test_b2_conflicting_duplicate_is_an_error_never_a_silent_choice():
    def handler(req):
        if req.url.path.endswith("/recipients"):
            return httpx.Response(200, json={"recipients": [], "page": {"nextPage": None}})
        return httpx.Response(200, json={"transactions": [_txn(1, -1500.0), _txn(1, -2500.0)], "page": {"nextPage": None}})

    res = await _call(handler, "reportable_totals", {"year": 2026})
    text = _error_text(res)
    assert "conflicting duplicate rows" in text and "1500" not in text and "2500" not in text

    def treasury(req):
        return httpx.Response(200, json={"transactions": [{"id": "t", "amount": 1}, {"id": "t", "amount": 2}], "cursor": None})

    async with _client(treasury) as c:
        with pytest.raises(MercuryAPIError, match="conflicting duplicate rows"):
            await c.list_treasury_transactions(TREASURY_1)


async def test_b2_treasury_same_page_duplicate_dropped_and_rows_without_ids_kept():
    def handler(req):
        rows = [{"id": "t1", "amount": 1}, {"id": "t1", "amount": 1}, {"amount": 5}, {"amount": 5}, {"id": None, "amount": 6}]
        return httpx.Response(200, json={"transactions": rows, "cursor": None})

    async with _client(handler) as c:
        rows = await c.list_treasury_transactions(TREASURY_1)
    assert len(rows) == 4 and rows.duplicates_dropped == 1  # rows without a usable id cannot be deduplicated


async def test_b2_every_paginated_result_exposes_duplicates_dropped(mcp_client: Client, fake_api: FakeMercury):
    for tool, args, key, id_key in _LIST_TOOLS:
        res = await mcp_client.call_tool(tool, {"entity": "acme_main", **args})
        assert not res.is_error, (tool, _error_text(res))
        assert _payload(res)["duplicates_dropped"] == 0, tool
    data = _payload(await mcp_client.call_tool("list_treasury_transactions", {"entity": "acme_main", "treasury_id": TREASURY_1}))
    assert data["duplicates_dropped"] == 0
    data = _payload(await mcp_client.call_tool("list_tax_docs", {"entity": "acme_main"}))
    assert data["duplicates_dropped"] == {"attachments": 0, "recipients": 0}
    data = _payload(await mcp_client.call_tool("list_events", {"entity": "acme_main", "since": "2026-03-01"}))
    assert data["duplicates_dropped"] == 0


async def test_b2_duplicates_dropped_is_reported_per_tool():
    for tool, args, key, id_key in _LIST_TOOLS:
        def handler(req, key=key, id_key=id_key):
            row = {id_key: "same-id"}
            return httpx.Response(200, json={key: [row, dict(row)], "page": {"nextPage": None}})

        res = await _call(handler, tool, args)
        assert not res.is_error, (tool, _error_text(res))
        data = _payload(res)
        assert data["duplicates_dropped"] == 1, tool


# ---------------------------------------------------------------------------
# B3. SDK argument validation never echoes caller values
# ---------------------------------------------------------------------------


async def test_b3_sdk_validation_errors_carry_field_path_and_type_only():
    requests: list[str] = []

    def handler(req):
        requests.append(req.url.path)
        return httpx.Response(200, json={})

    cases = [
        ("reportable_totals", {"year": FAKE_TOKEN_MAIN}, "year: expected an integer (int_parsing)"),
        ("reportable_totals", {"year": "SYNTHETIC_OPAQUE_SDK_CANARY_ABCDEF"}, "year: expected an integer (int_parsing)"),
        ("get_card", {"card_id": {"routingNumber": "SYNTHETIC_ARGUMENT_CANARY"}}, "card_id: expected a string (string_type)"),
        ("list_events", {"limit": FAKE_TOKEN_MAIN}, "limit: expected an integer (int_parsing)"),
        ("list_events", {"limit": 0}, "limit: below the allowed minimum (greater_than_equal)"),
        ("get_card", {}, "card_id: required (missing)"),
    ]
    for tool, args, expected in cases:
        res = await _call(handler, tool, args)
        text = _error_text(res)
        assert text == f"Error executing tool {tool}: invalid arguments: {expected}", (tool, text)
        for value in ("SYNTHETIC", FAKE_TOKEN_MAIN, "routingNumber", "input_value", "acme_main"):
            assert value not in text, (tool, value)
    assert requests == []


async def test_b3_missing_field_error_does_not_echo_the_other_arguments():
    """pydantic's `missing` error quotes the whole input object; a token passed in another field must not surface."""
    res = await _call(lambda req: httpx.Response(200, json={}), "get_card", {"note": FAKE_TOKEN_MAIN})
    text = _error_text(res)
    assert FAKE_TOKEN_MAIN not in text and "note" not in text and text.endswith("card_id: required (missing)")


async def test_b3_unknown_tool_name_is_echoed_only_when_identifier_shaped(registry, make_client, env_tokens):
    async with Client(build_server(registry, api_base=FAKE_API_BASE, client_factory=make_client)) as client:
        res = await client.call_tool("get_statement_pdf", {"entity": "acme_main", "statement_id": STATEMENT_1})
        assert _error_text(res) == "Unknown tool: get_statement_pdf"
        res = await client.call_tool(FAKE_TOKEN_MAIN, {})
        assert _error_text(res) == "Unknown tool" and FAKE_TOKEN_MAIN not in _error_text(res)


def test_b3_render_validation_error_shapes():
    from pydantic import BaseModel, conint

    class Args(BaseModel):
        year: int
        limit: conint(ge=1, le=5) = 1
        nested: dict[str, int] | None = None

    with pytest.raises(ValidationError) as info:
        Args(year="x", limit=9, nested={"a b": "z"})
    rendered = render_validation_error(info.value)
    assert "year: expected an integer (int_parsing)" in rendered
    assert "limit: above the allowed maximum (less_than_equal)" in rendered
    assert "nested.?: expected an integer (int_parsing)" in rendered  # a non-identifier segment (caller key) is masked
    assert "9" not in rendered and " z" not in rendered and "a b" not in rendered


async def test_b3_sdk_contract_pin_fails_loudly_if_the_sdk_changes():
    """What SanitizingMCPServer relies on in mcp 2.2.x: call_tool is the public seam and carries the ValidationError cause."""
    assert list(inspect.signature(MCPServer.call_tool).parameters) == ["self", "name", "arguments", "context"]
    assert SanitizingMCPServer.call_tool is not MCPServer.call_tool and issubclass(SanitizingMCPServer, MCPServer)
    plain = MCPServer(name="pin")

    @plain.tool()
    async def probe(n: int) -> int:
        return n

    with pytest.raises(ToolError) as info:
        await plain.call_tool("probe", {"n": "PIN_CANARY_VALUE"})
    # The base class still quotes the input and still attaches the cause our override keys on.
    assert isinstance(info.value.__cause__, ValidationError) and not isinstance(info.value, UnexpectedToolError)
    assert "PIN_CANARY_VALUE" in str(info.value) and "input_value" in str(info.value)
    sanitized = SanitizingMCPServer(name="pin2")

    @sanitized.tool()
    async def probe2(n: int) -> int:
        return n

    with pytest.raises(ToolError) as info:
        await sanitized.call_tool("probe2", {"n": "PIN_CANARY_VALUE"})
    assert str(info.value) == "Error executing tool probe2: invalid arguments: n: expected an integer (int_parsing)"
    assert info.value.__cause__ is None


async def test_b3_our_own_tool_errors_are_untouched_by_the_hook(mcp_client: Client, fake_api: FakeMercury):
    fake_api.force_status = 404
    text = _error_text(await mcp_client.call_tool("get_card", {"entity": "acme_main", "card_id": "dddddddd-0001-4ddd-8ddd-dddddddddddd"}))
    assert text.endswith("[acme_main] Mercury returned HTTP 404 for GET /cards/{id}: not found")


# ---------------------------------------------------------------------------
# B4. Derived outputs are scalar-projected
# ---------------------------------------------------------------------------

CANARY = "CANARY_NESTED_ROUTING_999_END"


async def test_b4_tax_docs_joins_never_copy_malformed_recipient_values():
    recipients = [
        {"id": "cccccccc-0001-4ccc-8ccc-cccccccccccc", "name": {"routingNumber": CANARY}, "status": "active"},
        {"id": "cccccccc-0002-4ccc-8ccc-cccccccccccc", "name": {"routingNumber": CANARY}, "status": ["active", {"x": CANARY}]},
    ]
    attachments = [{"id": "eeeeeeee-0001-4eee-8eee-eeeeeeeeeeee", "recipientId": recipients[0]["id"], "fileName": "synthetic.pdf"}]

    def handler(req):
        if req.url.path.endswith("/recipients/attachments"):
            return httpx.Response(200, json={"attachments": attachments, "page": {"nextPage": None}})
        return httpx.Response(200, json={"recipients": recipients, "page": {"nextPage": None}})

    res = await _call(handler, "list_tax_docs", {})
    assert not res.is_error
    wire = json.dumps({"s": res.structured_content, "c": [c.model_dump(mode="json") for c in res.content]})
    assert CANARY not in wire and "routingNumber" not in wire
    data = _payload(res)
    assert data["documents"][0]["recipientName"] is None
    assert data["recipients_without_docs"] == [{"id": recipients[1]["id"], "name": None, "status": None}]
    assert data["recipients_with_docs"] == 1


async def test_b4_reportable_totals_display_name_and_unclassified_rows_are_scalar_projected():
    cid = "cccccccc-0001-4ccc-8ccc-cccccccccccc"
    state: dict = {}

    def handler(req):
        if req.url.path.endswith("/transactions"):
            return httpx.Response(200, json={"transactions": [state["txn"]], "page": {"nextPage": None}})
        return httpx.Response(200, json={"recipients": state["recipients"], "page": {"nextPage": None}})

    base = _txn(1, -500.0, counterpartyId=cid, counterpartyName="SyntheticVendor")
    state.update(txn=base, recipients=[{"id": cid, "name": {"routingNumber": CANARY}, "status": "active"}])
    res = await _call(handler, "reportable_totals", {"year": 2026})
    wire = json.dumps({"s": res.structured_content, "c": [c.model_dump(mode="json") for c in res.content]})
    assert not res.is_error and CANARY not in wire
    data = _payload(res)
    assert data["recipients"][0]["display_name"] == "SyntheticVendor" and data["recipients"][0]["confidence"] == "high"

    state.update(txn={**base, "amount": {"routingNumber": CANARY}, "counterpartyName": {"routingNumber": CANARY}, "id": ["x"]}, recipients=[])
    res = await _call(handler, "reportable_totals", {"year": 2026})
    wire = json.dumps({"s": res.structured_content, "c": [c.model_dump(mode="json") for c in res.content]})
    assert not res.is_error and CANARY not in wire
    row = _payload(res)["unclassified"][0]
    assert row == {"id": None, "kind": "outgoingPayment", "status": "sent", "amount": None, "postedAt": "2026-06-01T00:00:00Z", "counterpartyName": None, "reason": "transaction has no usable amount"}


def test_b4_scalar_helpers_and_sample_ids():
    assert scalar("x") == "x" and scalar(1) == 1 and scalar(1.5) == 1.5 and scalar(True) is True and scalar(None) is None
    assert scalar({"a": 1}) is None and scalar([1]) is None
    assert scalar_str("x") == "x" and scalar_str(1) is None and scalar_str(None) is None
    rows = [_txn(1, -100.0, kind="other", id={"nested": CANARY}), _txn(2, -100.0, kind="other")]
    report = summarize(rows, year=2026, threshold=2000)
    entries = report["needs_review"]["unlabeled_debits"]
    assert entries[0]["sample_transaction_ids"] == [_uid(2)] and CANARY not in json.dumps(report)


# ---------------------------------------------------------------------------
# B5. Treasury cursor domain
# ---------------------------------------------------------------------------


async def test_b5_negative_cursor_is_rejected_before_a_second_request():
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"transactions": [{"id": f"t{calls}"}], "cursor": -1})

    async with _client(handler) as c:
        with pytest.raises(IncompletePaginationError, match="non-negative integer"):
            await c.list_treasury_transactions(TREASURY_1)
    assert calls == 1


async def test_b5_zero_and_decreasing_non_negative_cursors_are_followed():
    for sequence in ([0, 2, None], [5, 3, None]):
        calls = 0

        def handler(req, sequence=sequence):
            nonlocal calls
            calls += 1
            nxt = sequence[calls - 1] if calls <= len(sequence) else None
            return httpx.Response(200, json={"transactions": [{"id": f"t{calls}"}], "cursor": nxt})

        async with _client(handler) as c:
            rows = await c.list_treasury_transactions(TREASURY_1)
        assert len(rows) == len(sequence) and calls == len(sequence)


# ---------------------------------------------------------------------------
# B6. Trailing newline in registry identifiers
# ---------------------------------------------------------------------------


def test_b6_trailing_newline_is_rejected_in_token_env_and_key():
    for token_env in ("MERCURY_TOKEN_REVIEW\n", "MERCURY_TOKEN_REVIEW\r\n", "MERCURY_TOKEN_REVIEW "):
        with pytest.raises(RegistryError, match="token_env"):
            Registry.from_mapping({"entities": [{"key": "s", "display_name": "S", "token_env": token_env}]})
    for key in ("synthetic\n", "synthetic ", "\nsynthetic"):
        with pytest.raises(RegistryError, match=r"entities\[0\].*key"):
            Registry.from_mapping({"entities": [{"key": key, "display_name": "S", "token_env": "MERCURY_TOKEN_S"}]})
    reg = Registry.from_mapping({"entities": [{"key": "synthetic", "display_name": "S", "token_env": "MERCURY_TOKEN_REVIEW"}]})
    assert reg.entities()[0].token_env == "MERCURY_TOKEN_REVIEW"


# ---------------------------------------------------------------------------
# B7. Malformed YAML is one line on stderr
# ---------------------------------------------------------------------------

_INVALID_YAML = "entities: [\n  SYNTHETIC_YAML_SOURCE_CANARY\n"


def test_b7_invalid_yaml_registry_error_is_one_line_with_position(tmp_path: Path):
    p = tmp_path / "invalid.yaml"
    p.write_text(_INVALID_YAML, encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        Registry.from_path(p)
    msg = str(info.value)
    assert "\n" not in msg and "not valid YAML" in msg and "line 3, column 1" in msg
    assert "SYNTHETIC_YAML_SOURCE_CANARY" not in msg and "expected ',' or ']'" in msg


def test_b7_both_clis_exit_2_with_one_line(tmp_path: Path, monkeypatch):
    for var in ("MERCURY_ENTITIES_FILE", "MERCURY_API_BASE", "MERCURY_TOKEN_ACME_MAIN", "MERCURY_TOKEN_ACME_OPS"):
        monkeypatch.delenv(var, raising=False)
    p = tmp_path / "invalid.yaml"
    p.write_text(_INVALID_YAML, encoding="utf-8")
    for main, prog in ((server_mod.main, "mercury-multiorg-mcp"), (keepalive_mod.main, "mercury-multiorg-mcp-keepalive")):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--entities", str(p)])
        out = err.getvalue()
        assert code == 2 and out.count("\n") == 1 and out.startswith(f"{prog}: ") and "Traceback" not in out
        assert "line 3, column 1" in out and "SYNTHETIC_YAML_SOURCE_CANARY" not in out
    # and through the real console scripts
    for name in ("mercury-multiorg-mcp", "mercury-multiorg-mcp-keepalive"):
        script = Path(sys.executable).with_name(name)
        proc = subprocess.run([str(script), "--entities", str(p)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
        assert proc.returncode == 2 and proc.stderr.count("\n") == 1 and not proc.stderr.startswith("Traceback"), name


# ---------------------------------------------------------------------------
# B8. PDF envelope check tolerates trailing whitespace, returns original bytes
# ---------------------------------------------------------------------------

_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\nstartxref\n9\n%%EOF"


def test_b8_trailing_pdf_whitespace_is_ignored_for_the_marker_search():
    for pad in (b" " * 2044, b" " * 100_000, b"\r\n" * 3000, b"\x00\t\n\x0c\r " * 1000, b""):
        validate_pdf_bytes(_PDF + pad, "GET /statements/{id}/pdf")  # no error
    with pytest.raises(MercuryAPIError, match="no %%EOF marker"):
        validate_pdf_bytes(_PDF + b"%" + b"synthetic-padding-" * 124 + b"\n", "GET /statements/{id}/pdf")  # a comment is not whitespace
    with pytest.raises(MercuryAPIError, match="no %%EOF marker"):
        validate_pdf_bytes(_PDF + b"\x01" * (PDF_EOF_WINDOW + 1), "GET /statements/{id}/pdf")
    validate_pdf_bytes(_PDF + b"\x00" * 100, "x")  # NUL is PDF whitespace
    validate_pdf_bytes(_PDF + b"junk" * 100 + b"\n" * 5000, "x")  # marker within the window once whitespace is stripped


async def test_b8_padded_pdf_is_returned_byte_identical(mcp_client: Client, fake_api: FakeMercury):
    import base64

    fake_api.pdf_bytes = _PDF + b" " * 2044
    res = await mcp_client.call_tool("get_statement_pdf", {"entity": "acme_main", "statement_id": STATEMENT_1})
    assert not res.is_error, _error_text(res)
    assert base64.b64decode(res.content[1].resource.blob) == fake_api.pdf_bytes
    assert json.loads(res.content[0].text)["bytes"] == len(fake_api.pdf_bytes)


# ---------------------------------------------------------------------------
# Documentation corrections and vendor neutrality
# ---------------------------------------------------------------------------


def test_docs_corrections_and_client_neutral_wording():
    def flat(path: Path) -> str:  # prose wraps at ~72 columns; compare with whitespace collapsed
        return " ".join(path.read_text(encoding="utf-8").split())

    readme = flat(ROOT / "README.md")
    tools = flat(ROOT / "docs" / "tools.md")
    brief = flat(ROOT / "CLAUDE.md")
    changelog = flat(ROOT / "CHANGELOG.md")
    agents = flat(ROOT / "AGENTS.md")
    root_readme = flat(ROOT.parent / "README.md")
    assert "two commits later" not in readme and "two commits later" not in changelog and "in the next commit" in readme
    assert "8 or more characters" in readme
    assert "Works with any MCP client" in readme
    for client_name in ("Codex", "Cursor", "Windsurf", "VS Code", "Gemini CLI", "Claude Desktop", "Claude Code"):
        assert client_name in readme, client_name
    assert "[mcp_servers.mercury-multiorg]" in readme and "Claude Code convention" in readme
    assert "duplicates_dropped" in readme and "duplicates_dropped" in tools and "malformed pagination metadata" in tools
    assert "API `desc` order" in readme and "guaranteed only for windowed" in readme
    assert "last 2 KiB" in readme and "not PDF parsing" in readme
    assert "invoice id you pass appears" in tools and "neither the slug nor the id appears" not in tools and "neither the slug nor the id appears" not in brief
    assert "argument-validation" in readme.lower() or "argument validation" in readme.lower()
    assert "CLAUDE.md" in agents and "AGENTS.md" in brief
    assert "PDF" in root_readme and "unredacted" in root_readme
    # server text stays client-neutral
    for text in (server_mod.INSTRUCTIONS, inspect.getsource(server_mod)):
        assert "Claude" not in text and "Anthropic" not in text
    assert "0.1.1" in changelog and "B1" in changelog and "B8" in changelog


async def test_docs_tool_descriptions_are_client_neutral(mcp_client: Client):
    for tool in (await mcp_client.list_tools()).tools:
        assert "Claude" not in (tool.description or "") and "Anthropic" not in (tool.description or ""), tool.name
    assert len(DEFAULT_TOOLS) == 24
