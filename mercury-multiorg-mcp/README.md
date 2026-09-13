# mercury-multiorg-mcp

Read-only [MCP](https://modelcontextprotocol.io) server that exposes **several
Mercury organizations to one AI session**. Mercury's hosted MCP and its API
tokens are single-organization per connection; this server holds one
read-only token per org and routes every tool call by an explicit `entity`
key.

Version 0.1.0. The maintainer tags releases as `mercury-vX.Y.Z` on the
monorepo; find the commit to pin with
`git ls-remote --tags https://github.com/dkaleganov/personal-ai-systems 'mercury-v0.1.0^{}'`.
The complete tool reference with every returned field is in
[docs/tools.md](docs/tools.md); design notes and the build history are in
`CLAUDE.md`.

## Security model

- **Read-only.** Only `GET` endpoints have client methods; the package has no
  code path that can move money, edit recipients, or change anything.
- **Stdio only.** The server never opens a network listener.
- **Explicit entity, always.** Every tool that touches Mercury takes an
  `entity` argument. There is no default. Every result carries the entity it
  came from.
- **Tokens stay in the environment.** The registry names an env var per org;
  the server reads that env var and nothing else. Errors and logs never
  contain more than the last four characters of a token.
- **Untrusted output.** Transaction memos, counterparty names, bank
  descriptions, and filenames are third-party text returned verbatim. Clients
  and models must treat tool output as data, never as instructions.
- **Reduced identifiers.** Account numbers and tax ids (EIN) are masked to
  their last four digits everywhere they appear (accounts, statements,
  organization). Routing numbers, IBANs, SWIFT codes, counterparty bank
  details, postal addresses, card expiry, presigned download URLs, invoice
  pay-page slugs, and webhook signing secrets are never returned.
  Webhook receiver URLs are reduced to their origin plus a path
  fingerprint, since the capability token usually sits in the path.
  `reportable_totals` reads counterparty bank details internally to tell
  ACH from wire from check, and returns only the method label. Event
  patches are re-projected through the changed resource's allowlist.
- **Binary documents stay in memory.** Statement and invoice PDFs come back
  as an embedded `application/pdf` blob (base64), capped at 10 MB by
  declared length and by a streaming cap, never written to disk.
- **Path ids are validated.** Every id that becomes part of a request path
  must be a single safe segment; nothing can redirect a call to another
  endpoint.
- **Never files anything.** `reportable_totals` is a pre-filing cross-check.
  Mercury has no 1099 filing endpoint; filing happens in each org's
  dashboard.

## Install

From a clone:

```bash
git clone https://github.com/dkaleganov/personal-ai-systems.git   # or git@github.com:dkaleganov/personal-ai-systems.git
cd personal-ai-systems/mercury-multiorg-mcp
uv sync
uv run mercury-multiorg-mcp --entities /private/path/entities.yaml
```

Or pin a full commit SHA with `uvx` (pin a SHA, not a tag: `@tag` +
`#subdirectory=` has had resolver bugs in uv, and SHAs are cache-safe):

```bash
uvx --from 'git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp' \
  mercury-multiorg-mcp --entities /private/path/entities.yaml
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Configure

1. In each Mercury org: org switcher → All Settings → Tokens → create a
   **Read Only** token (no IP allowlist required).
2. Copy `entities.example.yaml` to a private location outside this repo and
   list your orgs (`key`, `display_name`, `token_env`).
3. Export one env var per org, named as in the registry, in the environment
   that launches the server (`.env.example` shows the names). Configuration
   is read from process environment variables only; **no `.env` file is
   read unless you pass `--env-file <path>`**, so nothing is picked up by
   accident from the repo, your home directory, or a `uvx` cache.
4. Optional: `MERCURY_API_BASE=https://api-sandbox.mercury.com` with
   sandbox-created tokens.

### Command line

| Flag / env var | Meaning |
| --- | --- |
| `--entities PATH` / `MERCURY_ENTITIES_FILE` | Entity registry YAML. Required (flag wins over env var); there is no implicit default. |
| `--env-file PATH` | Load this dotenv file before resolving tokens. Existing env vars win. Without the flag no dotenv file is read from anywhere. |
| `--api-base URL` / `MERCURY_API_BASE` | Mercury API host, default `https://api.mercury.com`. Must be `https://`; plain `http://` is accepted only for `localhost` / `127.0.0.1` mocks. |
| `--version` | Print the package version and exit. |

Startup problems (missing registry, invalid YAML, bad API base) print one
line to stderr and exit with status 2. Stdout is reserved for the MCP
protocol.

Mercury deletes tokens unused for 45 days and downgrades unused permissions on
the same clock. Run `mercury-multiorg-mcp-keepalive` on a schedule; see
[docs/keepalive.md](docs/keepalive.md) for cron and launchd snippets.

## Register in Claude Code

`.mcp.json` in this folder registers the server against the example registry
(no tokens, so `list_accounts` returns a clean per-entity error). For real use,
copy the block into your private project's `.mcp.json` and point `--entities`
at your private registry:

```json
{
  "mcpServers": {
    "mercury-multiorg": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp",
        "mercury-multiorg-mcp",
        "--entities",
        "/private/path/entities.yaml"
      ],
      "env": {
        "MERCURY_TOKEN_ACME_MAIN": "${MERCURY_TOKEN_ACME_MAIN}"
      }
    }
  }
}
```

### Claude Desktop

Claude Desktop reads `claude_desktop_config.json` (Settings → Developer →
Edit Config). It does **not** expand `${VAR}` placeholders, so put the
token env vars in a private dotenv file and pass `--env-file` (existing
process env vars still win):

```json
{
  "mcpServers": {
    "mercury-multiorg": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp",
        "mercury-multiorg-mcp",
        "--entities",
        "/private/path/entities.yaml",
        "--env-file",
        "/private/path/mercury.env"
      ]
    }
  }
}
```

Keep both files outside any repository and readable only by your user.

## Tools

Every tool below takes `entity` first (except the two registry tools) and
returns it in the result. Paginated lists take `limit` and return `count`
and `truncated`. Full field-by-field reference: [docs/tools.md](docs/tools.md).

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_entities` | — | entity keys, display names, whether each token env var is set |
| `server_info` | — | package version, API base, entity count (no secrets) |
| `list_accounts` | `entity` | accounts with balances, `accountNumberLast4` |
| `list_transactions` | `entity`, `account_id?`, `start?`, `end?`, `search?`, `limit=100` | newest-first transactions, `truncated` flag |
| `reportable_totals` | `entity`, `year`, `threshold?` (default 600 through 2025, 2000 from 2026) | per-recipient 1099 cross-check totals; `needs_review` buckets, `unclassified`, `excluded_summary` |
| `list_recipients` | `entity` | recipients: id, name, nickname, status, default payment method, date last paid, emails, `isBusiness` |
| `list_tax_docs` | `entity` | tax-form attachments per recipient, plus `recipients_without_docs` |
| `get_org` | `entity` | id, legal name, DBAs, kind, subscription tier, billing cadence, `einLast4` |
| `list_statements` | `entity`, `account_id`, `start?`, `end?`, `limit=100` | statement metadata, newest first (masked account number and EIN, `transactionCount`) |
| `get_statement_pdf` | `entity`, `statement_id` | the statement PDF as an embedded blob (≤ 10 MB) |
| `list_treasury` | `entity` | treasury accounts with balances and monthly net returns |
| `list_treasury_transactions` | `entity`, `treasury_id`, `start?`, `end?`, `limit=100` | treasury ledger rows, newest first (date window applied client-side) |
| `list_treasury_statements` | `entity`, `treasury_id`, `document_type?` | treasury statements and tax documents (metadata) |
| `list_credit_accounts` | `entity` | credit accounts with balances |
| `list_cards` | `entity`, `account_id?`, `status?`, `limit=100` | cards: last four, name, nickname, kind, type, status, limits, budgets, locks |
| `get_card` | `entity`, `card_id` | one card, same fields |
| `list_categories` | `entity` | custom expense categories |
| `list_merchants` | `entity`, `search?`, `limit=100` | priority merchants (id, name) |
| `list_customers` | `entity` | AR customers: id, name, email, `deletedAt` |
| `list_invoices` | `entity`, `status?`, `start?`, `end?`, `limit=100` | AR invoices (filters applied client-side) |
| `get_invoice` | `entity`, `invoice_id` | one invoice with service period and line items |
| `get_invoice_pdf` | `entity`, `invoice_id` | the invoice PDF as an embedded blob (≤ 10 MB) |
| `list_invoice_attachments` | `entity`, `invoice_id` | attachment ids and file names (no URLs) |
| `list_users` | `entity` | users: id, first and last name, email, role |
| `list_events` | `entity`, `since?`, `resource_type?`, `limit=100` | change events, newest first (`order_verified` reports whether the feed really was), patches re-projected per resource allowlist |
| `list_webhooks` | `entity` | webhook endpoints: id, url origin, `path_fingerprint`, status, `enabled`, event types, filter paths (never the secret or the path) |

`start` / `end` on `list_transactions` filter on `createdAt` (`YYYY-MM-DD` or
ISO 8601). The Mercury dashboard displays `postedAt`, so a date range may
differ slightly from the UI.

### Returns for the Phase 3 tools

```text
get_org                     entity, organization {id, legalBusinessName, dbas [{dbaName, dbaIsDefault}], kind,
                            subscriptionTier, billingCadence, einLast4}
list_statements             entity, account_id, filters, count, truncated,
                            statements[] {id, startDate, endDate, endingBalance, companyLegalName,
                            accountNumberLast4, einLast4, transactionCount}
get_statement_pdf           content[0] text {entity, statement_id, mimeType, bytes, encoding};
                            content[1] embedded resource {uri, mimeType: application/pdf, blob (base64)}
list_treasury               entity, count, treasury_accounts[] {id, status, availableBalance, currentBalance,
                            createdAt, netReturns[]}
list_treasury_transactions  entity, treasury_id, filters, count, truncated, transactions[] {id, accountId, type,
                            amount, balance, canonicalDay, description, additionalDetails, security, details}
list_treasury_statements    entity, treasury_id, filters, count, statements[] {id, accountId, documentType,
                            description, periodStart, periodEnd, creationDate, createdAt, updatedAt}
list_credit_accounts        entity, count, credit_accounts[] {id, status, availableBalance, currentBalance, createdAt}
list_cards                  entity, filters, count, truncated, cards[] {id, accountId, userId, nameOnCard, nickname,
                            lastFour, kind, type, status, physicalCardStatus, isAgentCard, spendLimitType, spendLimit,
                            budgets, merchantLock, categoryLocks, createdAt, updatedAt}
get_card                    entity, card {same fields as one list_cards row}
list_categories             entity, count, categories[] {id, name, visibleForCardSpend, visibleForOther,
                            visibleForReimbursements}
list_merchants              entity, filters, count, truncated, merchants[] {id, name}
list_customers              entity, count, customers[] {id, name, email, deletedAt}
list_invoices               entity, filters, count, truncated, invoices[] {id, invoiceNumber, status, amount,
                            currencyCode, customerId, destinationAccountId, invoiceDate, dueDate, createdAt,
                            updatedAt, canceledAt, poNumber, payerMemo, internalNote, ccEmails, achDebitEnabled,
                            creditCardEnabled, useRealAccountNumber}
get_invoice                 entity, invoice {list fields + servicePeriodStartDate, servicePeriodEndDate,
                            lineItems[] {name, quantity, unitPrice, salesTaxRate}}
get_invoice_pdf             same two blocks as get_statement_pdf, keyed by invoice_id
list_invoice_attachments    entity, invoice_id, count, attachments[] {id, fileName}
list_users                  entity, count, users[] {userId, firstName, lastName, email, organizationRole}
list_events                 entity, filters, order_verified, count, truncated, events[] {id, resourceType,
                            resourceId, operationType, resourceVersion, occurredAt, changedPaths, mergePatch,
                            previousValues, patchOmitted?}
list_webhooks               entity, count, webhooks[] {id, url (scheme://host[:port] only), path_fingerprint,
                            status, enabled, eventTypes, filterPaths, createdAt, updatedAt}
```

Lists keep the API's default order (ascending by an undocumented sort key)
except transactions, statements, treasury transactions, and events, which
are newest first; when `truncated` is true, the rows kept are the head of
that order.

Where the Mercury API has no server-side filter for a documented argument
(`since` on events, `start`/`end` on treasury transactions and invoices,
`status` on invoices) the tool applies it client-side and says so in
`docs/tools.md`.

### `reportable_totals`

Counts only completed money movement (status `sent`) with an outgoing
amount, attributed to the calendar year by **`postedAt` in UTC** (the date
the dashboard shows). The API is queried with `postedStart` / `postedEnd`
padded by one day on each side; rows outside the year are dropped
client-side and counted under `excluded_summary.outside_year`. The live
docs define no semantics for transaction `kind`, so the table only asserts
what the kind name supports; real-organization acceptance (September 2026)
showed that negative `externalTransfer` rows were the organization's own
linked external accounts and cross-org transfers, while genuine
vendor-initiated ACH debits arrived as kind `other`. Those two kinds are
therefore set aside for a human rather than counted.

| Decision | Kinds | Notes |
| --- | --- | --- |
| include | `outgoingPayment` | method from the payment details: `ach`, `domesticWire`, `internationalWire`, `check`, or `unknown` |
| include | `exogenousWireDrawdown` (negative amount) | wire drawdown, presumed counterparty-initiated; undocumented (`wireDrawdown`) |
| needs review | `externalTransfer` (negative amount) | `linked_account_transfers`: usually your own linked/external accounts or cross-org transfers; a vendor-initiated ACH debit could also appear |
| needs review | `other` (negative amount) | `unlabeled_debits`: no method signal; typically vendor-initiated ACH debits or Mercury product payments |
| exclude | `internalTransfer`, `treasuryTransfer` | the org's own accounts |
| exclude | `creditCardTransaction`, `debitCardTransaction`, `creditCardCredit`, `debitCardCredit` | the card processor files 1099-K |
| exclude | `wireFee`, `personalBankingSubscriptionFee`, `billingEngineSubscriptionFee`, `cardInternationalTransactionFee*` | bank fees and rebates |
| exclude | `incomingDomesticWire`, `incomingInternationalWire`, `checkDeposit`, `interestPayment` | money received |
| exclude | `currencyCloudReturn` | an international wire returned; the original may already be counted, net it by hand |
| exclude | `expenseReimbursement` | employee reimbursements |
| exclude | any includable, needs-review, or unclassified kind not `sent`, or with a non-negative amount | `not_settled:<status>` / `incoming` |
| unclassified | any kind not in the table, or a missing amount | listed one by one with a reason |

Recipients group by `counterpartyId` (confidence `high` when it matches a
recipient from `GET /recipients`, else `medium`) or, failing that, by
counterparty name (`low`). Id-groups that share a normalised name are
cross-referenced so a payee split across two ids is visible. The default
threshold is year-aware: 600 through tax year 2025, 2000 from 2026 (the
federal 1099-NEC/MISC figure, inflation-indexed from 2027, so pass the
current value); the resolved value is echoed. Real-time payments appear
under `ach` or `unknown` depending on whether the API returns routing
details for them.

Returns:

```text
entity, year, threshold            resolved threshold (default depends on year)
date_basis                         {field: "postedAt", timezone: "UTC",
                                    fallback_to_createdAt_count, api_filter: {postedStart, postedEnd}}
status_basis                       ["sent"]
totals                             reportable_total, reportable_payment_count, recipient_count,
                                   flagged_count, needs_review_total, needs_review_count,
                                   reportable_total_upper_bound (= reportable_total + needs_review_total),
                                   unclassified_count, transactions_scanned
recipients[]                       display_name, recipient_id (known recipient) | null, counterparty_id | null,
                                   grouping (counterparty_id | name | transaction), confidence (high | medium | low),
                                   total, payment_count, by_method {label: {count, total}}, flagged,
                                   possible_same_payee [other counterparty ids with the same normalised name],
                                   name_merged_total, flagged_for_review
needs_review                       {linked_account_transfers: [...], unlabeled_debits: [...]}; each entry:
                                   display_name, counterparty_id | null, count, total, by_kind {kind: {count, total}},
                                   would_flag (total >= threshold), sample_transaction_ids (max 3), hint (fixed string),
                                   possible_same_payee [ids], name_merged_total, would_flag_merged
unclassified[]                     id, kind, status, amount, postedAt, counterpartyName, reason
excluded_summary                   {category: {count, amount (signed, as returned by Mercury)}}
```

`fallback_to_createdAt_count` counts included rows that had no `postedAt`
and were placed by `createdAt` instead. Such rows cannot come back from the
posted-date filter, so the count is normally 0. Hints are fixed strings
chosen by kind and by a `Mercury ` name prefix; counterparty text itself
is data, never an instruction.

### `list_tax_docs`

Returns:

```text
entity
document_count, recipient_count, recipients_with_docs
documents[]                        id, recipientId, recipientName | null, fileName (verbatim third-party text),
                                   formType (w9 | w8BEN | w8BENE | unknown | null), uploadedAt
recipients_without_docs[]          id, name, status   (every recipient of any status with no attachment)
```

Download URLs are never returned.

## Keepalive

```bash
uv run mercury-multiorg-mcp-keepalive --entities /private/path/entities.yaml
```

One authenticated `GET /accounts` per configured entity, one line each
(`<timestamp> OK|FAIL <entity> HTTP <status>`), exit 1 if any entity fails
or no entity has a token. Details, cadence, and cron / launchd snippets in
[docs/keepalive.md](docs/keepalive.md).

## Develop

```bash
uv sync
uv run pytest
```

Tests use synthetic JSON fixtures and a mock HTTP transport only. Nothing in
this package, its tests, or its history may contain real names, tokens, or
account identifiers.

## License

MIT (the monorepo `LICENSE` applies).
