"""Allowlist projections: the only way a Mercury object leaves this server.

Every tool result is an explicit projection of the live schema, never the
raw object. Each allowlist below copies field names verbatim from the
schema page named in its comment and enumerates every excluded field with
a reason. Extend them deliberately; a field that is not listed does not
leave the server.

Masking rules (Phase 1, unchanged): account numbers only as ``...Last4``;
routing numbers, IBANs, SWIFT codes, counterparty bank coordinates, tax ids
beyond the last four digits, presigned download URLs, webhook secrets, and
card expiry never leave the server.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ALLOWLIST of fields copied verbatim from the live Mercury `Account` schema
# (docs.mercury.com/reference/getaccounts, 2026-09-11). Anything not listed
# here never leaves the server. Deliberately excluded:
#   accountNumber          -> replaced by `accountNumberLast4`
#   routingNumber          -> dropped; an AI transcript is no place for full
#                             bank coordinates and no read-only workflow needs them
#   canSendRealTimePayments -> payment-rail capability, irrelevant to a read-only surface
_ACCOUNT_FIELDS = (
    "id",
    "name",
    "nickname",
    "legalBusinessName",
    "kind",
    "type",
    "status",
    "availableBalance",
    "currentBalance",
    "createdAt",
    "canReceiveTransactions",
    "dashboardLink",
)

# ALLOWLIST of fields copied verbatim from the live Mercury `Transaction`
# schema (docs.mercury.com/reference/listtransactions, 2026-09-11). Anything
# not listed here never leaves the server. Deliberately excluded:
#   details                  -> counterparty routing/account numbers (TransactionMethodData)
#   attachments              -> filenames/URLs; Phase 3 surfaces attachments explicitly
#   glAllocations            -> bookkeeping allocations; not needed for Phase 1/2
#   relatedTransactions      -> nested transaction refs; revisit in Phase 3
#   compliantWithReceiptPolicy, hasGeneratedReceipt -> receipt-policy flags
#   creditAccountPeriodId, feeId, requestId, trackingNumber -> internal ids
#   generalLedgerCodeName    -> bookkeeping label; revisit if the 1099 pass needs it
_TRANSACTION_FIELDS = (
    "id",
    "accountId",
    "amount",
    "status",
    "kind",
    "createdAt",
    "postedAt",
    "estimatedDeliveryDate",
    "failedAt",
    "reasonForFailure",
    "counterpartyId",
    "counterpartyName",
    "counterpartyNickname",
    "bankDescription",
    "externalMemo",
    "note",
    "mercuryCategory",
    "categoryData",
    "merchant",
    "checkNumber",
    "cardId",
    "currencyExchangeInfo",
    "dashboardLink",
)

# ALLOWLIST of fields copied verbatim from the live Mercury `RecipientInfo`
# schema (docs.mercury.com/reference/getrecipients, 2026-09-12). Anything not
# listed here never leaves the server. Deliberately excluded:
#   electronicRoutingInfo, domesticWireRoutingInfo, internationalWireRoutingInfo,
#   realTimePaymentRoutingInfo -> the recipient's bank coordinates (account,
#                                  routing, IBAN, SWIFT); never returned
#   address, defaultAddress, checkInfo -> postal addresses; not needed for a
#                                  1099 cross-check inside an AI transcript
#   attachments               -> tax-form files; `list_tax_docs` inventories
#                                  them without the presigned download URL
#   inviteId                  -> onboarding-invite slug; write-side workflow
_RECIPIENT_FIELDS = (
    "id",
    "name",
    "nickname",
    "status",
    "defaultPaymentMethod",
    "dateLastPaid",
    "emails",
    "contactEmail",
    "isBusiness",
)

# ALLOWLIST for items of `GET /recipients/attachments`
# (docs.mercury.com/reference/listrecipientsattachments, 2026-09-12).
# Deliberately excluded:
#   url -> presigned S3 download link valid for 12 hours; a transcript is no
#          place for one, and this server does not fetch files in Phase 2
_RECIPIENT_ATTACHMENT_FIELDS = (
    "id",
    "recipientId",
    "fileName",
    "formType",
    "uploadedAt",
)

# ALLOWLIST for `OrganizationInfo` (docs.mercury.com/reference/getorganization,
# 2026-09-12). Deliberately excluded:
#   ein -> replaced by `einLast4`; a full tax id never leaves the server
_ORGANIZATION_FIELDS = (
    "id",
    "legalBusinessName",
    "dbas",
    "kind",
    "subscriptionTier",
    "billingCadence",
)

# ALLOWLIST for `DepositoryAccountStatement`
# (docs.mercury.com/reference/getaccountstatements, 2026-09-12): statement
# METADATA only. Deliberately excluded:
#   accountNumber       -> replaced by `accountNumberLast4`
#   routingNumber       -> bank coordinates; never returned
#   ein                 -> replaced by `einLast4`
#   companyLegalAddress -> postal address; not needed to pick a statement
#   downloadUrl         -> presigned link; `get_statement_pdf` fetches the
#                          PDF through the versioned endpoint instead
#   transactions        -> per-statement transaction id list, potentially
#                          thousands of rows; replaced by `transactionCount`.
#                          `list_transactions` covers the content.
_STATEMENT_FIELDS = (
    "id",
    "startDate",
    "endDate",
    "endingBalance",
    "companyLegalName",
)

# ALLOWLIST for `TreasuryAccount` (docs.mercury.com/reference/gettreasury,
# 2026-09-12). Nothing excluded: balances, status, and the monthly
# netReturns breakdown (dividends and fees) carry no coordinates.
_TREASURY_ACCOUNT_FIELDS = (
    "id",
    "status",
    "availableBalance",
    "currentBalance",
    "createdAt",
    "netReturns",
)

# ALLOWLIST for `TreasuryTxn` (docs.mercury.com/reference/gettreasurytransactions,
# 2026-09-12). `details` (TreasuryTransactionDetails) holds descriptions,
# security, sweep/trade labels and two counterparty *ids*; no bank
# coordinates, so it is passed through. Nothing excluded.
_TREASURY_TRANSACTION_FIELDS = (
    "id",
    "accountId",
    "type",
    "amount",
    "balance",
    "canonicalDay",
    "description",
    "additionalDetails",
    "security",
    "details",
)

# ALLOWLIST for `TreasuryStatement` (docs.mercury.com/reference/gettreasurystatements,
# 2026-09-12). Deliberately excluded:
#   downloadUrl -> presigned link; never returned or fetched
_TREASURY_STATEMENT_FIELDS = (
    "id",
    "accountId",
    "documentType",
    "description",
    "periodStart",
    "periodEnd",
    "creationDate",
    "createdAt",
    "updatedAt",
)

# ALLOWLIST for `CreditAccount` (docs.mercury.com/reference/listcredit,
# 2026-09-12). Nothing excluded.
_CREDIT_ACCOUNT_FIELDS = (
    "id",
    "status",
    "availableBalance",
    "currentBalance",
    "createdAt",
)

# ALLOWLIST for `Card` (docs.mercury.com/reference/listcards and getcard,
# 2026-09-12). The API itself never returns PAN or CVC on these endpoints.
# Deliberately excluded:
#   expiration -> month/year of expiry; together with last four and name it is
#                 card-present data with no read-only use
_CARD_FIELDS = (
    "id",
    "accountId",
    "userId",
    "nameOnCard",
    "nickname",
    "lastFour",
    "kind",
    "type",
    "status",
    "physicalCardStatus",
    "isAgentCard",
    "spendLimitType",
    "spendLimit",
    "budgets",
    "merchantLock",
    "categoryLocks",
    "createdAt",
    "updatedAt",
)

# ALLOWLIST for `CategoryData` (docs.mercury.com/reference/listcategories,
# 2026-09-12). Nothing excluded.
_CATEGORY_FIELDS = (
    "id",
    "name",
    "visibleForCardSpend",
    "visibleForOther",
    "visibleForReimbursements",
)

# ALLOWLIST for `MerchantInfo` (docs.mercury.com/reference/listmerchants,
# 2026-09-12). Nothing excluded.
_MERCHANT_FIELDS = ("id", "name")

# ALLOWLIST for `ApiV1ArCustomerResponseData`
# (docs.mercury.com/reference/listcustomers, 2026-09-12). Deliberately excluded:
#   address -> postal address; not needed to match invoices to customers
_CUSTOMER_FIELDS = (
    "id",
    "name",
    "email",
    "deletedAt",
)

# ALLOWLIST for `ApiV1ArInvoicesData` / `ApiV1ArInvoiceResponse`
# (docs.mercury.com/reference/listinvoices and getinvoice, 2026-09-12).
# Deliberately excluded:
#   slug -> builds the public pay-page and public PDF URLs for the invoice;
#           a capability token, not data. `get_invoice_pdf` fetches the PDF
#           through the authenticated endpoint instead.
_INVOICE_FIELDS = (
    "id",
    "invoiceNumber",
    "status",
    "amount",
    "currencyCode",
    "customerId",
    "destinationAccountId",
    "invoiceDate",
    "dueDate",
    "createdAt",
    "updatedAt",
    "canceledAt",
    "poNumber",
    "payerMemo",
    "internalNote",
    "ccEmails",
    "achDebitEnabled",
    "creditCardEnabled",
    "useRealAccountNumber",
)
# `get_invoice` (ApiV1ArInvoiceResponse) additionally carries the service
# period and the line items (name, quantity, unitPrice, salesTaxRate); the
# list endpoint's ApiV1ArInvoicesData has neither.
_INVOICE_DETAIL_FIELDS = _INVOICE_FIELDS + ("servicePeriodStartDate", "servicePeriodEndDate", "lineItems")

# ALLOWLIST for `ApiV1ArAttachmentResponseData`
# (docs.mercury.com/reference/listinvoiceattachments, 2026-09-12).
# Deliberately excluded:
#   url -> signed S3 download link; never returned
_INVOICE_ATTACHMENT_FIELDS = ("id", "fileName")

# ALLOWLIST for `UserDetails` (docs.mercury.com/reference/getusers,
# 2026-09-12). Nothing excluded.
_USER_FIELDS = (
    "userId",
    "firstName",
    "lastName",
    "email",
    "organizationRole",
)

# ALLOWLIST for `ApiEventResponse` (docs.mercury.com/reference/getevents,
# 2026-09-12). `mergePatch` and `previousValues` are partial copies of the
# changed resource and are re-projected through that resource's own
# allowlist (see `project_event`); for a resource type this server does not
# know, they are omitted and `patchOmitted` is set.
_EVENT_FIELDS = (
    "id",
    "resourceType",
    "resourceId",
    "operationType",
    "resourceVersion",
    "occurredAt",
    "changedPaths",
)
_EVENT_PATCH_FIELDS: dict[str, tuple[str, ...]] = {
    "transaction": _TRANSACTION_FIELDS,
    "checkingAccount": _ACCOUNT_FIELDS,
    "savingsAccount": _ACCOUNT_FIELDS,
    "treasuryAccount": _TREASURY_ACCOUNT_FIELDS,
    "investmentAccount": _TREASURY_ACCOUNT_FIELDS,
    "creditAccount": _CREDIT_ACCOUNT_FIELDS,
}

# ALLOWLIST for `ApiWebhookResponse` (docs.mercury.com/reference/getwebhooks,
# 2026-09-12). Deliberately excluded:
#   secret -> signing secret; the docs say GET never returns it, and it is
#             dropped here regardless
# `url` is returned as scheme://host/path only: receiver URLs routinely
# carry a capability token in the query string or credentials in the
# userinfo, which are secrets in the same sense as `secret`.
_WEBHOOK_FIELDS = (
    "id",
    "url",
    "status",
    "eventTypes",
    "filterPaths",
    "createdAt",
    "updatedAt",
)


def _project(obj: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {k: obj.get(k) for k in fields if k in obj}


def _last4(value: Any) -> str | None:
    """Last four characters of an identifier, or None when there is nothing to mask."""
    if isinstance(value, str) and value.strip():
        digits = value.strip()
        return digits[-4:]
    return None


def _project_account(acct: dict[str, Any]) -> dict[str, Any]:
    out = _project(acct, _ACCOUNT_FIELDS)
    number = acct.get("accountNumber")
    if isinstance(number, str) and number:
        out["accountNumberLast4"] = number[-4:]
    return out


def project_organization(org: dict[str, Any]) -> dict[str, Any]:
    out = _project(org, _ORGANIZATION_FIELDS)
    out["einLast4"] = _last4(org.get("ein"))
    return out


def project_statement(stmt: dict[str, Any]) -> dict[str, Any]:
    out = _project(stmt, _STATEMENT_FIELDS)
    out["accountNumberLast4"] = _last4(stmt.get("accountNumber"))
    out["einLast4"] = _last4(stmt.get("ein"))
    txns = stmt.get("transactions")
    out["transactionCount"] = len(txns) if isinstance(txns, list) else None
    return out


def project_treasury_account(acct: dict[str, Any]) -> dict[str, Any]:
    return _project(acct, _TREASURY_ACCOUNT_FIELDS)


def project_treasury_transaction(txn: dict[str, Any]) -> dict[str, Any]:
    return _project(txn, _TREASURY_TRANSACTION_FIELDS)


def project_treasury_statement(stmt: dict[str, Any]) -> dict[str, Any]:
    return _project(stmt, _TREASURY_STATEMENT_FIELDS)


def project_credit_account(acct: dict[str, Any]) -> dict[str, Any]:
    return _project(acct, _CREDIT_ACCOUNT_FIELDS)


def project_card(card: dict[str, Any]) -> dict[str, Any]:
    return _project(card, _CARD_FIELDS)


def project_category(cat: dict[str, Any]) -> dict[str, Any]:
    return _project(cat, _CATEGORY_FIELDS)


def project_merchant(m: dict[str, Any]) -> dict[str, Any]:
    return _project(m, _MERCHANT_FIELDS)


def project_customer(c: dict[str, Any]) -> dict[str, Any]:
    return _project(c, _CUSTOMER_FIELDS)


def project_invoice(inv: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    return _project(inv, _INVOICE_DETAIL_FIELDS if detail else _INVOICE_FIELDS)


def project_invoice_attachment(a: dict[str, Any]) -> dict[str, Any]:
    return _project(a, _INVOICE_ATTACHMENT_FIELDS)


def project_user(u: dict[str, Any]) -> dict[str, Any]:
    return _project(u, _USER_FIELDS)


def project_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Project an event; its patch objects go through the changed resource's own allowlist."""
    out = _project(ev, _EVENT_FIELDS)
    fields = _EVENT_PATCH_FIELDS.get(str(ev.get("resourceType")))
    omitted = False
    for key in ("mergePatch", "previousValues"):
        value = ev.get(key)
        if value is None:
            out[key] = None
        elif isinstance(value, dict) and fields is not None:
            patch = _project(value, fields)
            if fields is _ACCOUNT_FIELDS and "accountNumber" in value:
                patch["accountNumberLast4"] = _last4(value.get("accountNumber"))
            out[key] = patch
        else:
            out[key] = None
            omitted = True
    if omitted:
        out["patchOmitted"] = True
    return out


def _strip_url_secrets(url: Any) -> Any:
    """Drop userinfo, query string, and fragment from a URL; keep scheme, host, port, path."""
    if not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def project_webhook(wh: dict[str, Any]) -> dict[str, Any]:
    out = _project(wh, _WEBHOOK_FIELDS)
    if "url" in out:
        out["url"] = _strip_url_secrets(out["url"])
    out["enabled"] = wh.get("status") == "active"
    return out
