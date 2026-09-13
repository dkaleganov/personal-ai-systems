"""v0.1.2: documentation accuracy and schema metadata from the third external review (published-tag validation).

No runtime behaviour changed. These tests pin (a) the JSON-Schema metadata
added to four enum-like arguments and the YYYY-MM-DD-only date arguments,
and that the server still passes values through unchanged, (b) every
reviewer correction row in the docs, and (c) that the returned-field
inventories in README.md and docs/tools.md are derived from the projection
allowlists and the live tool results rather than remembered.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from mcp import Client

from mercury_multiorg_mcp import __version__
from mercury_multiorg_mcp import projections as proj
from mercury_multiorg_mcp import server as server_mod
from mercury_multiorg_mcp.server import CARD_STATUSES, EVENT_RESOURCE_TYPES, TREASURY_DOCUMENT_TYPES, _INVOICE_STATUSES

from .conftest import KNOWN_ACCOUNT_ID, KNOWN_TREASURY_ID, FakeMercury
from .test_tools import DEFAULT_TOOLS, _error_text, _payload

ROOT = Path(__file__).resolve().parent.parent


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


README = _flat(ROOT / "README.md")
TOOLS = _flat(ROOT / "docs" / "tools.md")
KEEPALIVE = _flat(ROOT / "docs" / "keepalive.md")
AGENTS = _flat(ROOT / "AGENTS.md")
BRIEF = _flat(ROOT / "CLAUDE.md")
CHANGELOG = _flat(ROOT / "CHANGELOG.md")


# ---------------------------------------------------------------------------
# Schema metadata: enum and date-format hints, server acceptance unchanged
# ---------------------------------------------------------------------------

_ENUMS = {
    ("list_cards", "status"): list(CARD_STATUSES),
    ("list_invoices", "status"): list(_INVOICE_STATUSES),
    ("list_treasury_statements", "document_type"): list(TREASURY_DOCUMENT_TYPES),
    ("list_events", "resource_type"): list(EVENT_RESOURCE_TYPES),
}
_DATE_ONLY = {("list_statements", "start"), ("list_statements", "end"), ("list_treasury_transactions", "start"),
              ("list_treasury_transactions", "end"), ("list_invoices", "start"), ("list_invoices", "end")}
_DATE_OR_TIMESTAMP = {("list_transactions", "start"), ("list_transactions", "end"), ("list_events", "since")}


def test_enum_values_match_the_live_reference_pages():
    """Values re-verified against docs.mercury.com (listcards, listinvoices, gettreasurystatements, getevents) on 2026-09-13."""
    assert CARD_STATUSES == ("active", "frozen", "cancelled", "inactive", "expired", "suspended")
    assert _INVOICE_STATUSES == ("Unpaid", "Paid", "Cancelled", "Processing")
    assert TREASURY_DOCUMENT_TYPES == ("MonthlyStatement", "TradeConfirmation", "1099", "1099R", "1042S", "5498", "5498ESA", "1099Q", "FMV", "SDIRA")
    assert EVENT_RESOURCE_TYPES == ("transaction", "checkingAccount", "savingsAccount", "treasuryAccount", "investmentAccount", "creditAccount")
    # the fake API's own 400 enums (from the live OpenAPI) agree
    from .conftest import _ENUM_PARAMS, _TREASURY_DOCUMENT_TYPES

    assert set(CARD_STATUSES) == _ENUM_PARAMS["/api/v1/cards"]["status"]
    assert set(EVENT_RESOURCE_TYPES) == _ENUM_PARAMS["/api/v1/events"]["resourceType"]
    assert set(TREASURY_DOCUMENT_TYPES) == _TREASURY_DOCUMENT_TYPES


async def test_input_schemas_carry_enum_and_date_format_metadata(mcp_client: Client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for (tool, arg), values in _ENUMS.items():
        prop = tools[tool].input_schema["properties"][arg]
        assert prop.get("enum") == values, (tool, arg)
        assert {"type": "string"} in prop["anyOf"] and {"type": "null"} in prop["anyOf"]  # still an optional string
    for tool, arg in _DATE_ONLY:
        assert tools[tool].input_schema["properties"][arg].get("format") == "date", (tool, arg)
    for tool, arg in _DATE_OR_TIMESTAMP:
        assert "format" not in tools[tool].input_schema["properties"][arg], (tool, arg)  # accepts ISO 8601 too


async def test_server_side_pass_through_is_unchanged(mcp_client: Client, fake_api: FakeMercury):
    """The metadata is advisory: unknown values still reach Mercury (and get its 400); invoice status stays case-insensitive."""
    for tool, args in (
        ("list_cards", {"status": "melted"}),
        ("list_treasury_statements", {"treasury_id": KNOWN_TREASURY_ID, "document_type": "W2"}),
        ("list_events", {"resource_type": "unicorn"}),
    ):
        text = _error_text(await mcp_client.call_tool(tool, {"entity": "acme_main", **args}))
        assert "[acme_main]" in text and "400" in text and "invalid arguments" not in text, tool
    assert fake_api.requests, "the unknown values were sent to the API, not rejected by the schema"
    data = _payload(await mcp_client.call_tool("list_invoices", {"entity": "acme_main", "status": "unpaid"}))
    assert data["filters"]["status"] == "Unpaid" and data["count"] == 2
    data = _payload(await mcp_client.call_tool("list_statements", {"entity": "acme_main", "account_id": KNOWN_ACCOUNT_ID, "start": "2026-02-01"}))
    assert data["count"] == 2
    text = _error_text(await mcp_client.call_tool("list_statements", {"entity": "acme_main", "account_id": KNOWN_ACCOUNT_ID, "start": "not-a-date"}))
    assert "start must be YYYY-MM-DD" in text and "not-a-date" not in text  # our own validator, as before


# ---------------------------------------------------------------------------
# Reviewer rows: wording applied
# ---------------------------------------------------------------------------


def test_readme_security_model_rows():
    assert "Upstream HTTP-status errors contain the status, a masked endpoint label and a fixed hint. Validation and configuration errors use their own actionable formats. Resolved known-token values of at least eight characters are scrubbed." in README
    assert "JSON/PDF reads reject nonidentity encoding before reading. Keepalive closes bodies unread. Limits are 10 MiB (10,485,760 bytes) for PDF and 32 MiB (33,554,432 bytes) for JSON" in README
    assert "Walks stop at the requested limit or API end. Missing/wrong `page` objects fail; optional terminal `nextPage` may be absent or null. Exact duplicate IDs are dropped and counted; conflicting contents fail. A page with no fresh usable rows while more are advertised fails." in README
    assert "Every paginated walk either completes or fails" not in README


def test_readme_hygiene_sentence_keeps_both_exceptions():
    assert "This release passed a full-history gitleaks scan." in README
    assert "maintainer's own name appears in the package `authors` metadata and the monorepo README" in README
    assert "replaced with an obviously fake value in the next commit" in README
    assert "absent from every tagged file tree but remains in their ancestry" in README
    assert "gitleaks runs on the full history before every release" not in README
    assert "public from its first commit" not in README


def test_readme_client_section():
    assert "Compatible with MCP clients that support local stdio servers and the negotiated protocol version. Configure the following command on the client host, with access to the private registry and environment file. Document display and client approval policies vary. This server does not expose HTTP or SSE." in README
    assert "A private dotenv file passed with `--env-file /private/path/mercury.env` avoids client-specific placeholder expansion. The client host must be able to read it; existing process environment variables win." in README
    assert "Claude Code expands `${VAR}` from its environment. This repository includes an example `.mcp.json`; clients that discover this format may offer to launch it. It points to the synthetic example registry and contains no credentials" in README
    assert "harmless for other clients, which ignore it" not in README and "Claude Code convention" not in README
    assert "Open Settings → Developer → Edit Config. Use `--env-file` so this setup does not depend on client-specific placeholder expansion:" in README
    assert "does **not** expand" not in README
    assert "### Cursor / Windsurf legacy Cascade (`mcp.json`)" in README
    assert "Cursor uses `.cursor/mcp.json` or `~/.cursor/mcp.json`. Windsurf legacy Cascade uses `~/.codeium/windsurf/mcp_config.json`. The current default Devin Local agent uses its own CLI configuration; this example targets legacy Cascade." in README
    assert "### VS Code (`.vscode/mcp.json`)" in README
    assert "### Gemini CLI (`~/.gemini/settings.json` or `.gemini/settings.json`)" in README
    vscode = re.search(r"### VS Code \(`\.vscode/mcp\.json`\).*?```json(.*?)```", README)
    assert vscode and '"servers": {' in vscode.group(1) and '"type": "stdio"' in vscode.group(1) and "mcpServers" not in vscode.group(1)
    # the same command and arguments in every snippet
    args = re.findall(r'"--from", "git\+https://github\.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp", "mercury-multiorg-mcp", "--entities", "/private/path/entities\.yaml"', README)
    assert len(args) >= 5


def test_readme_tool_inventory_wording():
    assert "The seven tools with a `limit` argument return `count` and `truncated`. Full-list tools have no public limit. Paginated results also expose duplicate diagnostics" in README
    assert "Every tool below takes `entity` first" not in README
    assert "Mercury API `desc` order" in README and "chronological (newest-first) order is guaranteed only for windowed calls" in README
    assert "windowed treasury calls sort by `canonicalDay`, and events with `since` sort by `occurredAt`" in README
    assert "newest-first transactions" not in README and "statement metadata, newest first" not in README
    assert "The default is for nonemployee services and certain MISC payments; supply the applicable category/year threshold" in README.replace("It is the default", "The default is")
    assert "duplicates_dropped (transaction rows), recipient_duplicates_dropped" in README
    assert "duplicates_dropped {attachments, recipients}" in README


def test_readme_and_docs_entity_rule_everywhere():
    sentence = "Every tool that accesses Mercury requires an explicit `entity` and identifies it in its successful result. `list_entities` and `server_info` require no entity argument."
    assert sentence in README and sentence in TOOLS and sentence in AGENTS and sentence in BRIEF
    assert sentence in " ".join(server_mod.INSTRUCTIONS.split())
    assert "Every tool takes an explicit `entity`" not in AGENTS
    assert "pass an explicit `entity` to every other tool" not in server_mod.INSTRUCTIONS
    assert "## Tool surface (all read-only; Mercury API tools require entity, with no default)" in BRIEF


def test_docs_tools_md_rows():
    assert "The seven tools with a `limit` argument (`list_transactions`, `list_statements`, `list_treasury_transactions`, `list_cards`, `list_merchants`, `list_invoices`, `list_events`) return `count` and `truncated`" in TOOLS
    assert "The MIME/header/EOF checks validate the envelope only; a passing document may still be malformed or incomplete internally" in TOOLS
    assert "a truncated file or an HTML error page fails" not in TOOLS
    assert "in Mercury API `desc` order" in TOOLS and "which are requested newest first" not in TOOLS
    assert "classification table in `CLAUDE.md`" not in TOOLS and "| include | `outgoingPayment` |" in TOOLS
    assert "The default is for nonemployee services and certain MISC payments; supply the applicable category/year threshold." in TOOLS


def test_keepalive_doc_rows():
    assert "token inactivity clock" in KEEPALIVE and "unused-permission expiry" in KEEPALIVE
    assert "one logical ping" in KEEPALIVE.lower() and "with bounded retries" in KEEPALIVE
    assert "if no tokens exist, one aggregate failure line" in KEEPALIVE
    assert "Any authenticated call resets the clock" not in KEEPALIVE
    assert "token inactivity clock" in README


def test_agents_and_changelog_rows():
    assert "The build brief and history are in [CLAUDE.md](https://github.com/dkaleganov/personal-ai-systems/blob/main/mercury-multiorg-mcp/CLAUDE.md). The filename is historical; its requirements apply to any agent working on this package." in AGENTS
    assert "This is an MCP server for clients that support local stdio (see README.md). The `.mcp.json` here is an example project-discovery configuration; client discovery rules vary." in AGENTS
    assert "](CLAUDE.md)" not in AGENTS and "one client's convention" not in AGENTS
    assert "two new majors and six minors" in CHANGELOG and "two new majors, four minors" not in CHANGELOG
    assert "## 0.1.2 (2026-09-13)" in CHANGELOG and "no runtime changes" in CHANGELOG
    assert __version__ == "0.1.2" and "Version 0.1.2" in README


def test_server_docstring_points_to_public_docs(mcp_client_sync=None):
    src = inspect.getsource(server_mod)
    assert "classification table in docs/tools.md" in " ".join(src.split()) and "full table in CLAUDE.md" not in src
    assert "for nonemployee services and certain MISC payments" in src


async def test_tool_descriptions_use_api_desc_order_for_unwindowed_lists(mcp_client: Client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("list_transactions", "list_statements", "list_treasury_transactions", "list_events"):
        assert "Mercury API `desc` order" in tools[name].description, name
        assert "newest first" not in tools[name].description.split("\n")[0], name
    assert len(DEFAULT_TOOLS) == 24


# ---------------------------------------------------------------------------
# Field inventories derived from code
# ---------------------------------------------------------------------------


def _keys(spec: dict) -> set[str]:
    out: set[str] = set()
    for k, v in spec.items():
        out.add(k)
        if isinstance(v, dict):
            out |= _keys(v)
        elif isinstance(v, list) and isinstance(v[0], dict):
            out |= _keys(v[0])
    return out


_SPECS = [
    proj._ACCOUNT_FIELDS, proj._TRANSACTION_FIELDS, proj._RECIPIENT_FIELDS, proj._RECIPIENT_ATTACHMENT_FIELDS,
    proj._ORGANIZATION_FIELDS, proj._STATEMENT_FIELDS, proj._TREASURY_ACCOUNT_FIELDS, proj._TREASURY_TRANSACTION_FIELDS,
    proj._TREASURY_STATEMENT_FIELDS, proj._CREDIT_ACCOUNT_FIELDS, proj._CARD_FIELDS, proj._CATEGORY_FIELDS,
    proj._MERCHANT_FIELDS, proj._CUSTOMER_FIELDS, proj._INVOICE_DETAIL_FIELDS, proj._INVOICE_ATTACHMENT_FIELDS,
    proj._USER_FIELDS, proj._EVENT_FIELDS, proj._WEBHOOK_FIELDS,
]


def test_every_projected_key_is_documented_in_both_references():
    for spec in _SPECS:
        for key in _keys(spec):
            assert key in README, key
            assert key in TOOLS, key
    for masked in ("accountNumberLast4", "einLast4", "transactionCount", "url_fingerprint", "enabled", "patchOmitted"):
        assert masked in README and masked in TOOLS, masked
    for dropped in ("routingNumber", "accountNumber ", "downloadUrl", "secret", "slug", "expiration"):
        assert dropped not in README.replace("`accountNumber` (masked)", "").replace("(masked account number", "") or dropped in ("secret", "slug", "expiration"), dropped


_CALLS = {
    "list_accounts": {},
    "list_transactions": {},
    "reportable_totals": {"year": 2026},
    "list_recipients": {},
    "list_tax_docs": {},
    "get_org": {},
    "list_statements": {"account_id": KNOWN_ACCOUNT_ID},
    "list_treasury": {},
    "list_treasury_transactions": {"treasury_id": KNOWN_TREASURY_ID},
    "list_treasury_statements": {"treasury_id": KNOWN_TREASURY_ID},
    "list_credit_accounts": {},
    "list_cards": {},
    "get_card": {"card_id": "dddddddd-0001-4ddd-8ddd-dddddddddddd"},
    "list_categories": {},
    "list_merchants": {},
    "list_customers": {},
    "list_invoices": {},
    "get_invoice": {"invoice_id": "1a000000-0001-4a00-8a00-1a0000000000"},
    "list_invoice_attachments": {"invoice_id": "1a000000-0001-4a00-8a00-1a0000000000"},
    "list_users": {},
    "list_events": {},
    "list_webhooks": {},
}
_LIMIT_TOOLS = {"list_transactions", "list_statements", "list_treasury_transactions", "list_cards", "list_merchants", "list_invoices", "list_events"}
# Every tool that walks a cursor (id or integer). list_credit_accounts and list_invoice_attachments hit unpaginated endpoints.
_PAGINATED = _LIMIT_TOOLS | {"list_accounts", "list_recipients", "list_treasury", "list_treasury_statements", "list_categories", "list_customers", "list_users", "list_webhooks"}


async def test_live_result_keys_match_the_documented_inventories(mcp_client: Client, fake_api: FakeMercury):
    """Every top-level key of every JSON tool result appears in both references; count/truncated only on the seven limit tools."""
    from .conftest import load_fixture

    seen_limit_tools = set()
    for tool, args in _CALLS.items():
        if tool == "reportable_totals":
            fake_api.transactions = load_fixture("transactions_1099_2026.json")["transactions"]
        else:
            fake_api.transactions = None
        res = await mcp_client.call_tool(tool, {"entity": "acme_main", **args})
        assert not res.is_error, (tool, _error_text(res))
        data = _payload(res)
        assert data["entity"] == "acme_main", tool
        for key in data:
            assert key in README and key in TOOLS, (tool, key)
        if tool == "reportable_totals":
            for key in data["totals"]:
                assert key in README and key in TOOLS, key
        if "truncated" in data:
            seen_limit_tools.add(tool)
        if tool in _PAGINATED:
            assert isinstance(data["duplicates_dropped"], int), tool
        elif tool == "list_tax_docs":
            assert set(data["duplicates_dropped"]) == {"attachments", "recipients"}
        else:
            assert "duplicates_dropped" not in data, tool  # single-object or unpaginated endpoints
    assert seen_limit_tools == _LIMIT_TOOLS
    for tool in _LIMIT_TOOLS:
        assert re.search(rf"`{tool}`.*`limit=100`", README), tool
