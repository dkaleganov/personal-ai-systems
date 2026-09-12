# mercury-multiorg-mcp

Read-only [MCP](https://modelcontextprotocol.io) server that exposes **several
Mercury organizations to one AI session**. Mercury's hosted MCP and its API
tokens are single-organization per connection; this server holds one
read-only token per org and routes every tool call by an explicit `entity`
key.

Status: **Phase 2** (core tools, 1099 cross-check, keepalive CLI). See
`CLAUDE.md` for the full build brief and the later phases (holistic read
surface, release pass).

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
- **Reduced identifiers.** `list_accounts` masks account numbers to the last
  four digits and omits routing numbers; `list_transactions` omits
  counterparty bank details; `list_recipients` omits bank coordinates and
  postal addresses; `list_tax_docs` omits download URLs. `reportable_totals`
  reads counterparty bank details internally to tell ACH from wire from
  check, and returns only the method label.
- **Never files anything.** `reportable_totals` is a pre-filing cross-check.
  Mercury has no 1099 filing endpoint; filing happens in each org's
  dashboard.

## Install

From a clone:

```bash
cd mercury-multiorg-mcp
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

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_entities` | — | entity keys, display names, whether each token env var is set |
| `list_accounts` | `entity` | accounts with `availableBalance` / `currentBalance` |
| `list_transactions` | `entity`, `account_id?`, `start?`, `end?`, `search?`, `limit=100` | newest-first transactions, `truncated` flag |
| `reportable_totals` | `entity`, `year`, `threshold?` (default 600 through 2025, 2000 from 2026) | per-recipient totals of payments made in the year, classified for a 1099 cross-check; `flagged` at or above the threshold; `needs_review` buckets, `unclassified` rows, and an `excluded_summary` |
| `list_recipients` | `entity` | recipients: id, name, nickname, status, default payment method, date last paid, emails, `isBusiness` |
| `list_tax_docs` | `entity` | tax-form attachments (W-9 / W-8BEN / W-8BEN-E) per recipient, plus `recipients_without_docs` |
| `server_info` | — | package version, API base, entity count (no secrets) |

`start` / `end` on `list_transactions` filter on `createdAt` (`YYYY-MM-DD` or
ISO 8601). The Mercury dashboard displays `postedAt`, so a date range may
differ slightly from the UI.

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
                                   would_flag (total >= threshold), sample_transaction_ids (max 3), hint (fixed string)
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
