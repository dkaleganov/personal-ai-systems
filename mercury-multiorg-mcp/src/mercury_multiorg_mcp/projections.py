"""Allowlist projections: the only way a Mercury object leaves this server.

Every tool result is an explicit projection of the live schema, never the
raw object, and the allowlist applies **at every level** (v0.1.1): each
nested object that is allowed (transaction ``merchant``, ``categoryData``,
``currencyExchangeInfo``; organization ``dbas``; treasury ``netReturns``,
``dividends`` and ``details``; card ``spendLimit``, ``budgets``,
``merchantLock``; invoice ``lineItems``; event patches) has its own
allowlist copied from the schema page named in its comment. A key that is
not listed does not leave the server, at any depth, so a field Mercury adds
tomorrow is dropped rather than passed through.

Spec language (see :func:`_project`):

- ``S`` (``None``): a scalar (string, number, boolean, or null). A dict or
  list arriving where a scalar is expected is replaced by ``null``.
- ``[S]``: a list of scalars; non-scalar items are dropped.
- ``{...}``: a nested object with its own allowlist; anything else becomes ``null``.
- ``[{...}]``: a list of such objects; non-dict items are dropped.

Masking rules (Phase 1, unchanged): account numbers only as ``...Last4``;
routing numbers, IBANs, SWIFT codes, counterparty bank coordinates, tax ids
beyond the last four digits, presigned download URLs, webhook receiver URLs
(v0.1.1: the whole URL, not just the path) and secrets, and card expiry
never leave the server.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Spec markers. A spec is a dict {key: S | [S] | Spec | [Spec]}.
S = None
Spec = dict[str, Any]

_SCALARS = (str, int, float, bool)


def _project_value(value: Any, sub: Any) -> Any:
    if value is None:
        return None
    if sub is S:
        return value if isinstance(value, _SCALARS) else None
    if isinstance(sub, dict):
        return _project(value, sub) if isinstance(value, dict) else None
    # list spec: [S] or [Spec]
    if not isinstance(value, list):
        return None
    item_spec = sub[0]
    if item_spec is S:
        return [v for v in value if isinstance(v, _SCALARS)]
    return [_project(v, item_spec) for v in value if isinstance(v, dict)]


def _project(obj: dict[str, Any], spec: Spec) -> dict[str, Any]:
    """Recursive allowlist projection: only listed keys, each shaped by its spec entry."""
    return {k: _project_value(obj[k], sub) for k, sub in spec.items() if k in obj}


def scalar(value: Any) -> Any:
    """``value`` if it is a JSON scalar (string, number, boolean, null), else ``None``.

    For derived outputs (joins, display names, diagnostic rows) that copy a
    single field out of a raw object: a field documented as a scalar that
    arrives as an object or array is replaced by ``null`` rather than
    passed through (B4).
    """
    return value if value is None or isinstance(value, _SCALARS) else None


def scalar_str(value: Any) -> str | None:
    """``value`` if it is a string, else ``None``."""
    return value if isinstance(value, str) else None


# -- nested shapes (each pinned to the live OpenAPI, docs.mercury.com, fetched 2026-09-13) --

# `MerchantData` (listtransactions). Nothing excluded.
_MERCHANT_DATA: Spec = {"id": S, "category": S, "categoryCode": S, "currency": S, "amount": S}

# `CategoryData` (listcategories; also nested on transactions). Nothing excluded.
_CATEGORY_FIELDS: Spec = {
    "id": S,
    "name": S,
    "visibleForCardSpend": S,
    "visibleForOther": S,
    "visibleForReimbursements": S,
}

# `CurrencyExchangeInfo` (listtransactions). Nothing excluded.
_CURRENCY_EXCHANGE_INFO: Spec = {
    "convertedFromAmount": S,
    "convertedFromCurrency": S,
    "convertedToAmount": S,
    "convertedToCurrency": S,
    "exchangeRate": S,
    "feeAmount": S,
    "feePercentage": S,
    "feeTransactionId": S,
}

# `OrganizationDBA` (getorganization). Nothing excluded.
_ORGANIZATION_DBA: Spec = {"dbaName": S, "dbaIsDefault": S}

# `TreasuryDividend` and `TreasuryNetReturn` (gettreasury). Nothing excluded.
_TREASURY_DIVIDEND: Spec = {"id": S, "type": S, "securityName": S, "amount": S}
_TREASURY_NET_RETURN: Spec = {
    "month": S,
    "netAmount": S,
    "treasuryFee": S,
    "status": S,
    "dividends": [_TREASURY_DIVIDEND],
}

# `TreasuryTransactionDetails` (gettreasurytransactions): descriptions,
# security, sweep/trade labels and two counterparty *ids*; no bank coordinates.
_TREASURY_TRANSACTION_DETAILS: Spec = {
    "creditDescription": S,
    "depositCounterpartyId": S,
    "feeDescription": S,
    "manualAmendmentDescription": S,
    "security": S,
    "sweepDirection": S,
    "tradeAction": S,
    "withdrawalCounterpartyId": S,
}

# `SpendLimit`, `CardBudget`, `MerchantInfo` (listcards / getcard / listmerchants). Nothing excluded.
_SPEND_LIMIT: Spec = {"amountCents": S, "atmAmountCents": S, "interval": S}
_CARD_BUDGET: Spec = {"id": S, "name": S, "amountCents": S, "remainingAmountCents": S}
_MERCHANT_FIELDS: Spec = {"id": S, "name": S}

# `ApiV1ArLineItemData` (getinvoice). Nothing excluded.
_INVOICE_LINE_ITEM: Spec = {"name": S, "quantity": S, "unitPrice": S, "salesTaxRate": S}


# -- top-level shapes --------------------------------------------------------

# ALLOWLIST of top-level fields copied verbatim from the live Mercury
# `Account` schema (docs.mercury.com/reference/getaccounts, 2026-09-11). A
# top-level field not listed here never leaves the server. Deliberately excluded:
#   accountNumber          -> replaced by `accountNumberLast4`
#   routingNumber          -> dropped; an AI transcript is no place for full
#                             bank coordinates and no read-only workflow needs them
#   canSendRealTimePayments -> payment-rail capability, irrelevant to a read-only surface
_ACCOUNT_FIELDS: Spec = {
    "id": S,
    "name": S,
    "nickname": S,
    "legalBusinessName": S,
    "kind": S,
    "type": S,
    "status": S,
    "availableBalance": S,
    "currentBalance": S,
    "createdAt": S,
    "canReceiveTransactions": S,
    "dashboardLink": S,
}

# ALLOWLIST of top-level fields copied verbatim from the live Mercury
# `Transaction` schema (docs.mercury.com/reference/listtransactions,
# 2026-09-11; nested shapes re-pinned 2026-09-13). Deliberately excluded:
#   details                  -> counterparty routing/account numbers (TransactionMethodData)
#   attachments              -> filenames/URLs; Phase 3 surfaces attachments explicitly
#   glAllocations            -> bookkeeping allocations; not needed for Phase 1/2
#   relatedTransactions      -> nested transaction refs; revisit in Phase 3
#   compliantWithReceiptPolicy, hasGeneratedReceipt -> receipt-policy flags
#   creditAccountPeriodId, feeId, requestId, trackingNumber -> internal ids
#   generalLedgerCodeName    -> bookkeeping label; revisit if the 1099 pass needs it
_TRANSACTION_FIELDS: Spec = {
    "id": S,
    "accountId": S,
    "amount": S,
    "status": S,
    "kind": S,
    "createdAt": S,
    "postedAt": S,
    "estimatedDeliveryDate": S,
    "failedAt": S,
    "reasonForFailure": S,
    "counterpartyId": S,
    "counterpartyName": S,
    "counterpartyNickname": S,
    "bankDescription": S,
    "externalMemo": S,
    "note": S,
    "mercuryCategory": S,
    "categoryData": _CATEGORY_FIELDS,
    "merchant": _MERCHANT_DATA,
    "checkNumber": S,
    "cardId": S,
    "currencyExchangeInfo": _CURRENCY_EXCHANGE_INFO,
    "dashboardLink": S,
}

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
_RECIPIENT_FIELDS: Spec = {
    "id": S,
    "name": S,
    "nickname": S,
    "status": S,
    "defaultPaymentMethod": S,
    "dateLastPaid": S,
    "emails": [S],
    "contactEmail": S,
    "isBusiness": S,
}

# ALLOWLIST for items of `GET /recipients/attachments`
# (docs.mercury.com/reference/listrecipientsattachments, 2026-09-12).
# Deliberately excluded:
#   url -> presigned S3 download link valid for 12 hours; a transcript is no
#          place for one, and this server does not fetch files in Phase 2
_RECIPIENT_ATTACHMENT_FIELDS: Spec = {
    "id": S,
    "recipientId": S,
    "fileName": S,
    "formType": S,
    "uploadedAt": S,
}

# ALLOWLIST for `OrganizationInfo` (docs.mercury.com/reference/getorganization,
# 2026-09-12). Deliberately excluded:
#   ein -> replaced by `einLast4`; a full tax id never leaves the server
_ORGANIZATION_FIELDS: Spec = {
    "id": S,
    "legalBusinessName": S,
    "dbas": [_ORGANIZATION_DBA],
    "kind": S,
    "subscriptionTier": S,
    "billingCadence": S,
}

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
_STATEMENT_FIELDS: Spec = {
    "id": S,
    "startDate": S,
    "endDate": S,
    "endingBalance": S,
    "companyLegalName": S,
}

# ALLOWLIST for `TreasuryAccount` (docs.mercury.com/reference/gettreasury,
# 2026-09-12). Nothing excluded: balances, status, and the monthly
# netReturns breakdown (dividends and fees) carry no coordinates.
_TREASURY_ACCOUNT_FIELDS: Spec = {
    "id": S,
    "status": S,
    "availableBalance": S,
    "currentBalance": S,
    "createdAt": S,
    "netReturns": [_TREASURY_NET_RETURN],
}

# ALLOWLIST for `TreasuryTxn` (docs.mercury.com/reference/gettreasurytransactions,
# 2026-09-12). Nothing excluded.
_TREASURY_TRANSACTION_FIELDS: Spec = {
    "id": S,
    "accountId": S,
    "type": S,
    "amount": S,
    "balance": S,
    "canonicalDay": S,
    "description": S,
    "additionalDetails": S,
    "security": S,
    "details": _TREASURY_TRANSACTION_DETAILS,
}

# ALLOWLIST for `TreasuryStatement` (docs.mercury.com/reference/gettreasurystatements,
# 2026-09-12). Deliberately excluded:
#   downloadUrl -> presigned link; never returned or fetched
_TREASURY_STATEMENT_FIELDS: Spec = {
    "id": S,
    "accountId": S,
    "documentType": S,
    "description": S,
    "periodStart": S,
    "periodEnd": S,
    "creationDate": S,
    "createdAt": S,
    "updatedAt": S,
}

# ALLOWLIST for `CreditAccount` (docs.mercury.com/reference/listcredit,
# 2026-09-12). Nothing excluded.
_CREDIT_ACCOUNT_FIELDS: Spec = {
    "id": S,
    "status": S,
    "availableBalance": S,
    "currentBalance": S,
    "createdAt": S,
}

# ALLOWLIST for `Card` (docs.mercury.com/reference/listcards and getcard,
# 2026-09-12). The API itself never returns PAN or CVC on these endpoints.
# `categoryLocks` is a list of `MercuryCategory` enum strings.
# Deliberately excluded:
#   expiration -> month/year of expiry; together with last four and name it is
#                 card-present data with no read-only use
_CARD_FIELDS: Spec = {
    "id": S,
    "accountId": S,
    "userId": S,
    "nameOnCard": S,
    "nickname": S,
    "lastFour": S,
    "kind": S,
    "type": S,
    "status": S,
    "physicalCardStatus": S,
    "isAgentCard": S,
    "spendLimitType": S,
    "spendLimit": _SPEND_LIMIT,
    "budgets": [_CARD_BUDGET],
    "merchantLock": _MERCHANT_FIELDS,
    "categoryLocks": [S],
    "createdAt": S,
    "updatedAt": S,
}

# ALLOWLIST for `ApiV1ArCustomerResponseData`
# (docs.mercury.com/reference/listcustomers, 2026-09-12). Deliberately excluded:
#   address -> postal address; not needed to match invoices to customers
_CUSTOMER_FIELDS: Spec = {
    "id": S,
    "name": S,
    "email": S,
    "deletedAt": S,
}

# ALLOWLIST for `ApiV1ArInvoicesData` / `ApiV1ArInvoiceResponse`
# (docs.mercury.com/reference/listinvoices and getinvoice, 2026-09-12).
# Deliberately excluded:
#   slug -> builds the public pay-page and public PDF URLs for the invoice;
#           a capability token, not data. `get_invoice_pdf` fetches the PDF
#           through the authenticated endpoint instead.
_INVOICE_FIELDS: Spec = {
    "id": S,
    "invoiceNumber": S,
    "status": S,
    "amount": S,
    "currencyCode": S,
    "customerId": S,
    "destinationAccountId": S,
    "invoiceDate": S,
    "dueDate": S,
    "createdAt": S,
    "updatedAt": S,
    "canceledAt": S,
    "poNumber": S,
    "payerMemo": S,
    "internalNote": S,
    "ccEmails": [S],
    "achDebitEnabled": S,
    "creditCardEnabled": S,
    "useRealAccountNumber": S,
}
# `get_invoice` (ApiV1ArInvoiceResponse) additionally carries the service
# period and the line items (name, quantity, unitPrice, salesTaxRate); the
# list endpoint's ApiV1ArInvoicesData has neither.
_INVOICE_DETAIL_FIELDS: Spec = {
    **_INVOICE_FIELDS,
    "servicePeriodStartDate": S,
    "servicePeriodEndDate": S,
    "lineItems": [_INVOICE_LINE_ITEM],
}

# ALLOWLIST for `ApiV1ArAttachmentResponseData`
# (docs.mercury.com/reference/listinvoiceattachments, 2026-09-12).
# Deliberately excluded:
#   url -> signed S3 download link; never returned
_INVOICE_ATTACHMENT_FIELDS: Spec = {"id": S, "fileName": S}

# ALLOWLIST for `UserDetails` (docs.mercury.com/reference/getusers,
# 2026-09-12). Nothing excluded.
_USER_FIELDS: Spec = {
    "userId": S,
    "firstName": S,
    "lastName": S,
    "email": S,
    "organizationRole": S,
}

# ALLOWLIST for `ApiEventResponse` (docs.mercury.com/reference/getevents,
# 2026-09-12). `mergePatch` and `previousValues` are partial copies of the
# changed resource and are re-projected through that resource's own
# allowlist (see `project_event`); for a resource type this server does not
# know, they are omitted and `patchOmitted` is set.
_EVENT_FIELDS: Spec = {
    "id": S,
    "resourceType": S,
    "resourceId": S,
    "operationType": S,
    "resourceVersion": S,
    "occurredAt": S,
    "changedPaths": [S],
}
# Event patches for account resources also carry `inFlightBalance`, which
# the webhook `filterPaths` enum documents for all five account types but
# which no GET schema exposes. It is allowed on event patches only.
_EVENT_ACCOUNT_PATCH_FIELDS: Spec = {**_ACCOUNT_FIELDS, "inFlightBalance": S}
_EVENT_TREASURY_PATCH_FIELDS: Spec = {**_TREASURY_ACCOUNT_FIELDS, "inFlightBalance": S}
_EVENT_CREDIT_PATCH_FIELDS: Spec = {**_CREDIT_ACCOUNT_FIELDS, "inFlightBalance": S}
_EVENT_PATCH_FIELDS: dict[str, Spec] = {
    "transaction": _TRANSACTION_FIELDS,
    "checkingAccount": _EVENT_ACCOUNT_PATCH_FIELDS,
    "savingsAccount": _EVENT_ACCOUNT_PATCH_FIELDS,
    "treasuryAccount": _EVENT_TREASURY_PATCH_FIELDS,
    "investmentAccount": _EVENT_TREASURY_PATCH_FIELDS,
    "creditAccount": _EVENT_CREDIT_PATCH_FIELDS,
}
_ACCOUNT_NUMBER_PATCH_TYPES = frozenset({"checkingAccount", "savingsAccount"})

# ALLOWLIST for `ApiWebhookResponse` (docs.mercury.com/reference/getwebhooks,
# 2026-09-12; revised 2026-09-13). Deliberately excluded:
#   secret -> signing secret; the docs say GET never returns it, and it is
#             dropped here regardless
#   url    -> the receiver URL is a capability in every part: the path
#             (Slack, Discord, Zapier, Make, n8n), the query, the userinfo,
#             and the HOSTNAME itself (e.g. <secret>.m.pipedream.net). Only
#             `url_fingerprint`, the first 8 hex chars of sha256(url), is
#             returned so two hooks stay distinguishable.
_WEBHOOK_FIELDS: Spec = {
    "id": S,
    "status": S,
    "eventTypes": [S],
    "filterPaths": [S],
    "createdAt": S,
    "updatedAt": S,
}


# -- projection helpers ----------------------------------------------------


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
    """Project an event; its patch objects go through the changed resource's own allowlist (recursively)."""
    out = _project(ev, _EVENT_FIELDS)
    resource_type = str(ev.get("resourceType"))
    fields = _EVENT_PATCH_FIELDS.get(resource_type)
    omitted = False
    for key in ("mergePatch", "previousValues"):
        value = ev.get(key)
        if value is None:
            out[key] = None
        elif isinstance(value, dict) and fields is not None:
            patch = _project(value, fields)
            if resource_type in _ACCOUNT_NUMBER_PATCH_TYPES and "accountNumber" in value:
                patch["accountNumberLast4"] = _last4(value.get("accountNumber"))
            out[key] = patch
        else:
            out[key] = None
            omitted = True
    if omitted:
        out["patchOmitted"] = True
    return out


def url_fingerprint(url: Any) -> str | None:
    """First 8 hex chars of sha256 of the full receiver URL; None when there is no string to hash."""
    if not isinstance(url, str) or not url:
        return None
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def project_webhook(wh: dict[str, Any]) -> dict[str, Any]:
    out = _project(wh, _WEBHOOK_FIELDS)
    out["url_fingerprint"] = url_fingerprint(wh.get("url"))
    out["enabled"] = wh.get("status") == "active"
    return out
