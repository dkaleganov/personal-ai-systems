# QuickBooks Multi-Company MCP Server

## What this is

An MCP server exposing read-only tools across multiple QuickBooks Online
company files that live under one Intuit login. Intuit's model is one
realmId per company file with a separate OAuth token pair per realm, so
official integrations bind one company per connection. This server
stores a token pair per realm and routes every tool call by an explicit
entity key, so one AI session can see a whole multi-company setup.

This package lives in a public monorepo. It is public from its first
commit. No real names, company names, realmIds, EINs, or tokens in
tracked files, fixtures, comments, or commit messages, ever.

Bring-your-own-Intuit-app: Intuit's terms forbid redistributing an
app's client ID/secret, so every user registers their own Intuit
developer app. The README must walk through that.

## Ground rules (every session in this folder)

1. Public-first. Fake data only (`acme_main`, `acme_ops`, realm_id
   `0000000000000000`) in examples, fixtures, and tests. Real
   configuration never exists inside this repo working copy, not even
   gitignored: operators wire real entities from a separate private
   environment that installs this package.
2. Never ask the user to paste a secret into chat. Name the env var and
   wait for confirmation that it is set.
3. Read-only only. No state-changing endpoint gets a client method, even
   as a stub. (The auth CLI's token exchange/refresh POSTs to Intuit's
   OAuth endpoints are auth plumbing, not accounting mutations, and are
   the sole exception.)
4. Every tool takes an explicit `entity` parameter. No default entity
   anywhere. Every tool result carries the entity key it came from.
5. Live docs beat this brief. Before writing or changing the API client,
   fetch the docs listed under API notes; note any discrepancy in a code
   comment.
6. gitleaks on the full history before declaring any phase done.

## Tool surface (all read-only, `entity` required, no defaults)

Phase 1 — auth + guard:
- `list_entities`
- `get_company_info(entity)`: calls CompanyInfo and asserts the returned
  CompanyName matches the registry display_name for that key. Mismatch
  raises with both names shown. This is the wrong-entity guard and it
  runs inside every other tool's client path too (display_name must
  match the QBO company file name verbatim, not a nickname).
- `server_info`: package version and configured entity count (no secrets)

Phase 2 — data tools:
- `list_accounts(entity)`
- `vendor_spend(entity, year)`: per-vendor totals across bills, bill
  payments, checks, and expense transactions for the calendar year
- `list_purchases(entity, since?)`
- `qbo_query(entity, query)`: raw QBO query passthrough. The QBO query
  language is select-only by construction; still validate the statement
  begins with SELECT and reject otherwise.

Phase 3 — reports:
- `run_report(entity, report, params?)` over the documented report
  names (ProfitAndLoss, ProfitAndLossDetail, BalanceSheet, CashFlow,
  APAgingSummary/Detail, ARAgingSummary/Detail, GeneralLedger,
  TrialBalance, TransactionList + ByCustomer/ByVendor/WithSplits,
  VendorExpenses, VendorBalance/Detail, CustomerBalance/Detail,
  CustomerIncome, SalesByCustomer/Product/Department/ClassSummary,
  AccountListDetail, JournalReport, TaxSummary, InventoryValuation
  Summary/Detail). Use the DOCUMENTED names, not SDK aliases.

Cross-server workflows (e.g. comparing vendor spend here against
reportable payment totals from a banking MCP server) are deliberately
NOT tools. The MCP client computes them by calling the servers side by
side. Keep servers independent.

## Auth design (validated July 2026)

- OAuth 2.0 authorization code flow. One Intuit app, authorized once per
  company file; the human picks the company on Intuit's consent screen
  and the realmId comes back on the redirect.
- IMPORTANT: production Intuit apps cannot register `http://localhost`
  redirect URIs — production requires a hosted HTTPS redirect
  (localhost works only with sandbox/dev keys). The supported pattern:
  a static HTTPS forwarder page (e.g. GitHub Pages) registered as the
  production redirect URI, containing only
  `location.replace("http://localhost:8765/callback" + location.search)`
  with a HARDCODED target (never read the destination from a query
  param — that would be an open redirector). HTTPS→localhost top-level
  navigation is permitted by browsers. Ship a copyable forwarder page in
  docs/ so users host their own.
- Setup CLI, not an MCP tool: `python -m qbo_multicompany_mcp.auth
  <entity>`. Binds 127.0.0.1:8765 only while a flow is active, generates
  a cryptographically random `state` and verifies it on callback, opens
  the consent URL, captures code + realmId, exchanges (client secret
  from env, never leaves the machine), stores tokens, prints the realmId
  for the user to place in their registry.
- Fallback bootstrap: `python -m qbo_multicompany_mcp.import_token
  <entity>` accepts a refresh token minted via Intuit's OAuth Playground
  (prompted interactively, never via chat/argv) — also the documented
  recovery path after a refresh-token expiry lockout.
- Rotation-safe token store (`tokens.sqlite`, path configurable via
  `QBO_TOKENS_DB`, chmod 600, SQLite WAL, single-writer transactions):
  Intuit rotates the refresh token roughly every 24-26 hours; the
  refresh response's new refresh token MUST be persisted atomically
  BEFORE the new access token is used. Losing a rotated refresh token
  breaks the chain and forces re-auth.
- Access tokens last 1 hour; the client refreshes on 401 and retries
  once. Refresh tokens: 100-day rolling expiry PLUS an absolute 5-year
  cap per consent (document that re-consent is eventually required).
- keepalive CLI (`python -m qbo_multicompany_mcp.keepalive`) refreshes
  every stored realm; recommend daily via cron/launchd (daily keeps the
  100-day window trivially satisfied and surfaces breakage within 24h).
- MCP tools read `INTUIT_CLIENT_ID` and `INTUIT_CLIENT_SECRET` from env
  only. Support `QBO_ENV=sandbox|production` to select Intuit base URLs.
- Note for docs: Intuit's production app checklist requires an app
  assessment questionnaire (even for private apps) plus EULA, privacy
  policy, host domain, disconnect URL, and a Reconnect URL.

## API notes (validated July 2026; live docs win)

- Endpoint pattern `/v3/company/{realmId}/{entity}`. Rate limits: 500
  req/min per realm, 10 concurrent per realm, 10 req/sec per realm+app.
  Handle HTTP 429 (ThrottleExceeded, errorCode 003001) honoring
  Retry-After.
- `minorversion` is frozen at 75 and optional (requests default to 75;
  lower values are ignored). Omit the parameter.
- REPORTS MIGRATION (critical): Intuit is migrating all 29 report
  entities to a modernized backend, mandatory from August 31, 2026.
  Build report parsing against the NEW format from day one (send the
  `_testing_migration` query param while the old backend is default):
  nulls return `""` (not 0), row ordering is dynamic (parse by row
  group/id, never positionally), child accounts always nest under
  parents, ColTitle is Title Case, StartPeriod/EndPeriod always present.
  Record all fixtures from the new backend.
- Reports practical limits: hard cap 400,000 cells/response; keep date
  ranges ~6 months; request only needed columns.
- Ground truth is developer.intuit.com: OAuth 2.0 docs, token refresh
  semantics, realmId capture, and the Accounting API reference. Read
  them before writing the client; contradictions get a code comment.

## Registry and secrets

- `entities.example.yaml` committed with fake entries and realm_id
  values of `0000000000000000`. The server takes `--entities <path>` (or
  `QBO_ENTITIES_FILE`); the real registry lives outside this repo.
- `.env.example` committed with placeholders; `.env`, `entities.yaml`,
  and `tokens.sqlite` are gitignored repo-wide and must not exist in
  this working copy.
- Token redaction: last 4 characters max, in logs and every error path,
  including httpx exception reprs. Never print a refresh token.

## Untrusted data

Vendor names, memos, and descriptions are third-party text. Return
verbatim, never interpret. README carries the same untrusted-output
warning as the Mercury server.

## Stack

Python 3.11+, official `mcp` Python SDK pinned `>=1.28,<2` (FastMCP
pattern; SDK v2 renames FastMCP to MCPServer — do NOT upgrade past <2
without a deliberate migration pass), httpx, pydantic>=2, pyyaml,
python-dotenv, stdlib sqlite3. Pin versions. Read modelcontextprotocol.io
and the python-sdk README before scaffolding. Do not use the third-party
standalone `fastmcp` package — official SDK only.

## Monorepo packaging rules

- Self-contained package: own `pyproject.toml` (hatchling, src layout,
  `[project.scripts]` console entry `qbo-multicompany-mcp`), tests,
  docs. No path dependencies on sibling folders, ever.
- Install/run via
  `uvx --from 'git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=qbo-multicompany-mcp' qbo-multicompany-mcp`
  — docs must tell users to pin a full commit SHA.
- Release tags are repo-wide and prefixed: `qbo-v0.1.0`.

## Phases (stop for review after each)

1. Scaffold, registry loader, auth CLI with the rotation-safe store +
   forwarder flow + import-token fallback, `list_entities`,
   `get_company_info` with the guard, `server_info`, wired via
   `.mcp.json`. Unit tests with synthetic fixtures; auth flow tested
   against Intuit sandbox. Acceptance: sandbox auth completes and
   get_company_info passes the guard. (Production/real-company
   verification happens in the operator's private environment.)
2. Data tools: `list_accounts`, `vendor_spend`, `list_purchases`,
   `qbo_query` with SELECT validation. Acceptance: fixture-tested;
   operator hand-checks vendor_spend for one entity and year against
   the QBO UI privately.
3. `run_report` on the new-format backend + keepalive CLI with cron and
   launchd snippets. Acceptance: fixture tests recorded with
   `_testing_migration`; docs/tools.md complete.
4. Release pass: README (install, bring-your-own-app walkthrough,
   forwarder hosting, config, security model incl. untrusted-output
   warning, tool reference); MIT (monorepo LICENSE applies); gitleaks
   across full history; tag `qbo-v0.1.0`. Acceptance: gitleaks clean; a
   fresh clone installs and passes tests from the README alone.

## Definition of done for public

A stranger can clone the monorepo, register their own Intuit app,
configure fake entities, and run everything without learning anything
about the author's businesses. History contains zero real identifiers.
gitleaks passes on the full history.
