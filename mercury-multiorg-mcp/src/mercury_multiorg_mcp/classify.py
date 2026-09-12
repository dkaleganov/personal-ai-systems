"""1099 classification of Mercury transactions. Pure functions, no I/O.

Given one calendar year of an organization's transactions, decide which
ones are payments the organization *made* to a payee that belong on a
1099 cross-check, group them per recipient, and expose everything that was
left out so a reviewer can audit the decision.

Classification table (live ``TransactionKind`` enum, docs.mercury.com
``/reference/listtransactions``, fetched 2026-09-12). The same table is
reproduced in CLAUDE.md and in the ``reportable_totals`` tool docstring;
keep the three in sync.

INCLUDE (settled, outgoing amount only; method from ``details``):
  outgoingPayment        payment to a recipient. Method: electronicRoutingInfo
                         -> ach, domesticWireRoutingInfo -> domesticWire,
                         internationalWireRoutingInfo -> internationalWire,
                         address / checkNumber -> check, none -> unknown
  externalTransfer       counterparty-initiated ACH debit ("ACH pull")
                         when the amount is negative
  exogenousWireDrawdown  wire drawdown initiated by the counterparty
                         ("wire pull") when the amount is negative

EXCLUDE (category in ``excluded_summary``):
  internalTransfer, treasuryTransfer          internal_transfer: the org's own
                                              accounts (incl. treasury)
  creditCardTransaction, debitCardTransaction card: the card processor files
                                              1099-K
  creditCardCredit, debitCardCredit           card: card refunds / credits
  cardInternationalTransactionFee,
  cardInternationalTransactionFeeRebate,
  cardInternationalTransactionFeeReversal,
  cardInternationalTransactionFeeRebateReversal,
  wireFee, personalBankingSubscriptionFee,
  billingEngineSubscriptionFee                bank_fee: fees to / rebates from
                                              the bank, not a payee
  incomingDomesticWire, incomingInternationalWire,
  checkDeposit, interestPayment               incoming: money received
  currencyCloudReturn                         returned_payment: an
                                              international wire coming back;
                                              the original may already be
                                              counted, reviewer nets manually
  expenseReimbursement                        reimbursement: employee expense
                                              reimbursements are not 1099
                                              payments
  (any includable kind) status != sent        not_settled:<status>: pending,
                                              cancelled, failed, reversed,
                                              blocked never moved money to
                                              completion
  (any includable kind) amount >= 0           incoming: a credit, refund or
                                              return, not a payment made
  postedAt outside the requested year         outside_year: defensive; the API
                                              filter should already exclude

UNCLASSIFIED (listed one by one with a reason):
  other                  no method signal; kind means "anything else"
  <not in the enum>      schema drift: a kind this table has never seen
  amount missing         cannot total what has no amount

The classifier reads ``details`` to determine the payment method and
nothing else: no value from ``details`` is ever copied into a result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SETTLED_STATUSES = frozenset({"sent"})

# kind -> (method label, reason). Method labels are the keys of `by_method`.
INCLUDE_KINDS: dict[str, tuple[str | None, str]] = {
    "outgoingPayment": (None, "payment to a recipient; method read from details"),
    "externalTransfer": ("achPull", "counterparty-initiated ACH debit (ACH pull)"),
    "exogenousWireDrawdown": ("wirePull", "wire drawdown initiated by the counterparty (wire pull)"),
}

# kind -> (excluded category, reason)
EXCLUDE_KINDS: dict[str, tuple[str, str]] = {
    "internalTransfer": ("internal_transfer", "transfer between the organization's own accounts"),
    "treasuryTransfer": ("internal_transfer", "transfer to/from the organization's own treasury account"),
    "creditCardTransaction": ("card", "card payment; the card processor files 1099-K"),
    "debitCardTransaction": ("card", "card payment; the card processor files 1099-K"),
    "creditCardCredit": ("card", "card refund or credit"),
    "debitCardCredit": ("card", "card refund or credit"),
    "cardInternationalTransactionFee": ("bank_fee", "fee charged by the bank, not a payee"),
    "cardInternationalTransactionFeeRebate": ("bank_fee", "fee rebate from the bank"),
    "cardInternationalTransactionFeeReversal": ("bank_fee", "fee reversal by the bank"),
    "cardInternationalTransactionFeeRebateReversal": ("bank_fee", "fee rebate reversal by the bank"),
    "wireFee": ("bank_fee", "fee charged by the bank, not a payee"),
    "personalBankingSubscriptionFee": ("bank_fee", "subscription fee charged by the bank"),
    "billingEngineSubscriptionFee": ("bank_fee", "subscription fee charged by the bank"),
    "incomingDomesticWire": ("incoming", "money received"),
    "incomingInternationalWire": ("incoming", "money received"),
    "checkDeposit": ("incoming", "money received"),
    "interestPayment": ("incoming", "interest received"),
    "currencyCloudReturn": (
        "returned_payment",
        "international wire returned to the organization; the original payment may already be counted",
    ),
    "expenseReimbursement": ("reimbursement", "employee expense reimbursement, not a 1099 payment"),
}

UNCLASSIFIED_KINDS: dict[str, str] = {
    "other": "kind 'other' carries no payment-method signal",
}

KNOWN_KINDS = frozenset(INCLUDE_KINDS) | frozenset(EXCLUDE_KINDS) | frozenset(UNCLASSIFIED_KINDS)

# Method labels for outgoingPayment, in detection order (most specific first).
# DOCS: TransactionMethodData has no realTimePayment member; an RTP payment
# presumably carries electronicRoutingInfo and is therefore counted as "ach".
_DETAIL_METHODS = (
    ("internationalWireRoutingInfo", "internationalWire"),
    ("domesticWireRoutingInfo", "domesticWire"),
    ("electronicRoutingInfo", "ach"),
    ("address", "check"),
)

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Verdict:
    """Outcome for one transaction. Carries labels and a reason, never payload."""

    decision: str  # "include" | "exclude" | "unclassified"
    label: str  # include: method; exclude: category; unclassified: short code
    reason: str


def to_cents(value: Any) -> Decimal | None:
    """Exact decimal for an API amount (floats are 2-dp in the schema). None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def method_from_details(txn: dict[str, Any]) -> str:
    """Read the payment-method signal from ``details``. Returns a label only."""
    details = txn.get("details")
    if isinstance(details, dict):
        for key, label in _DETAIL_METHODS:
            if details.get(key):
                return label
    if txn.get("checkNumber"):
        return "check"
    return "unknown"


def classify(txn: dict[str, Any]) -> Verdict:
    """Apply the classification table to one raw transaction."""
    kind = txn.get("kind")
    if kind in EXCLUDE_KINDS:
        category, reason = EXCLUDE_KINDS[kind]
        return Verdict("exclude", category, reason)

    status = txn.get("status")
    if status not in SETTLED_STATUSES:
        return Verdict("exclude", f"not_settled:{status}", "only status 'sent' is completed money movement")

    amount = to_cents(txn.get("amount"))
    if amount is None:
        return Verdict("unclassified", "amount_missing", "transaction has no usable amount")
    if amount >= 0:
        return Verdict("exclude", "incoming", "non-negative amount: a credit, refund, or return, not a payment made")

    if kind in INCLUDE_KINDS:
        method, reason = INCLUDE_KINDS[kind]
        return Verdict("include", method or method_from_details(txn), reason)
    if kind in UNCLASSIFIED_KINDS:
        return Verdict("unclassified", "kind_other", UNCLASSIFIED_KINDS[kind])
    return Verdict("unclassified", "unknown_kind", f"kind {kind!r} is not in the classification table")


def _year_of(timestamp: Any) -> int | None:
    if isinstance(timestamp, str) and len(timestamp) >= 4 and timestamp[:4].isdigit():
        return int(timestamp[:4])
    return None


def _money(value: Decimal) -> float:
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def summarize(
    transactions: list[dict[str, Any]],
    *,
    year: int,
    threshold: float | int,
    recipients_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Group one year of transactions per recipient for a 1099 cross-check.

    ``recipients_by_id`` (from ``GET /recipients``) upgrades a group's
    confidence to ``high`` and supplies the canonical display name when the
    transaction's ``counterpartyId`` matches a known recipient.
    """
    recipients_by_id = recipients_by_id or {}
    threshold_cents = to_cents(threshold) or Decimal("0.00")

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    excluded: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": Decimal("0.00")})
    unclassified: list[dict[str, Any]] = []
    posted_at_missing = 0

    for txn in transactions:
        # Year attribution: postedAt (what the dashboard shows). createdAt is
        # only a fallback for the odd settled row that carries no postedAt.
        basis = txn.get("postedAt")
        if not basis:
            basis = txn.get("createdAt")
            posted_at_missing += 1
        if _year_of(basis) != year:
            bucket = excluded["outside_year"]
            bucket["count"] += 1
            bucket["amount"] += to_cents(txn.get("amount")) or Decimal("0.00")
            continue

        verdict = classify(txn)
        if verdict.decision == "exclude":
            bucket = excluded[verdict.label]
            bucket["count"] += 1
            bucket["amount"] += to_cents(txn.get("amount")) or Decimal("0.00")
            continue
        if verdict.decision == "unclassified":
            unclassified.append(
                {
                    "id": txn.get("id"),
                    "kind": txn.get("kind"),
                    "status": txn.get("status"),
                    "amount": txn.get("amount"),
                    "postedAt": txn.get("postedAt"),
                    "counterpartyName": txn.get("counterpartyName"),
                    "reason": verdict.reason,
                }
            )
            continue

        amount = -(to_cents(txn.get("amount")) or Decimal("0.00"))  # outgoing, stored positive
        counterparty_id = txn.get("counterpartyId")
        name = txn.get("counterpartyName")
        if isinstance(counterparty_id, str) and counterparty_id:
            key = ("counterparty_id", counterparty_id)
        elif isinstance(name, str) and name.strip():
            key = ("name", " ".join(name.split()).casefold())
        else:
            key = ("transaction", str(txn.get("id")))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "key": key,
                "counterparty_id": counterparty_id if key[0] == "counterparty_id" else None,
                "names": Counter(),
                "total": Decimal("0.00"),
                "count": 0,
                "by_method": defaultdict(lambda: {"count": 0, "total": Decimal("0.00")}),
            }
        if isinstance(name, str) and name.strip():
            group["names"][name.strip()] += 1
        group["total"] += amount
        group["count"] += 1
        group["by_method"][verdict.label]["count"] += 1
        group["by_method"][verdict.label]["total"] += amount

    recipients_out: list[dict[str, Any]] = []
    for group in groups.values():
        grouping, _ = group["key"]
        cid = group["counterparty_id"]
        recipient = recipients_by_id.get(cid) if cid else None
        if recipient is not None:
            confidence = "high"
            display = recipient.get("name") or (group["names"].most_common(1) or [("", 0)])[0][0]
        elif grouping == "counterparty_id":
            confidence = "medium"
            display = (group["names"].most_common(1) or [("", 0)])[0][0]
        else:
            confidence = "low"
            display = (group["names"].most_common(1) or [("", 0)])[0][0]
        recipients_out.append(
            {
                "display_name": display or "(unknown counterparty)",
                "recipient_id": cid if recipient is not None else None,
                "counterparty_id": cid,
                "grouping": grouping,
                "confidence": confidence,
                "total": _money(group["total"]),
                "payment_count": group["count"],
                "by_method": {
                    m: {"count": v["count"], "total": _money(v["total"])} for m, v in sorted(group["by_method"].items())
                },
                "flagged": group["total"] >= threshold_cents,
            }
        )
    recipients_out.sort(key=lambda r: (-r["total"], r["display_name"]))

    reportable_total = sum((g["total"] for g in groups.values()), Decimal("0.00"))
    return {
        "year": year,
        "threshold": _money(threshold_cents),
        "date_basis": {
            "field": "postedAt",
            "timezone": "UTC",
            "fallback_to_createdAt_count": posted_at_missing,
        },
        "status_basis": sorted(SETTLED_STATUSES),
        "totals": {
            "reportable_total": _money(reportable_total),
            "reportable_payment_count": sum(g["count"] for g in groups.values()),
            "recipient_count": len(recipients_out),
            "flagged_count": sum(1 for r in recipients_out if r["flagged"]),
            "unclassified_count": len(unclassified),
            "transactions_scanned": len(transactions),
        },
        "recipients": recipients_out,
        "unclassified": unclassified,
        "excluded_summary": {
            category: {"count": v["count"], "amount": _money(v["amount"])} for category, v in sorted(excluded.items())
        },
    }
