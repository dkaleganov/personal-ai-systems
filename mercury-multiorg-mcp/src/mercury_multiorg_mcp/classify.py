"""1099 classification of Mercury transactions. Pure functions, no I/O.

Given one calendar year of an organization's transactions, decide which
ones are payments the organization *made* to a payee that belong on a
1099 cross-check, group them per recipient, set aside what needs a human
decision, and expose everything that was left out so a reviewer can audit
the result.

Classification table (live ``TransactionKind`` enum, docs.mercury.com
``/reference/listtransactions``, fetched 2026-09-12). The live docs define
no semantics for the enum values; the table below only asserts what the
kind name itself supports. The same table is reproduced in CLAUDE.md, in
the ``reportable_totals`` tool docstring, and in README.md; keep the four
in sync.

INCLUDE (settled, outgoing amount only; counted in ``reportable_total``):
  outgoingPayment        payment to a recipient. Method from ``details``:
                         internationalWireRoutingInfo -> internationalWire,
                         domesticWireRoutingInfo -> domesticWire,
                         electronicRoutingInfo -> ach, address / checkNumber
                         -> check, none -> unknown
  exogenousWireDrawdown  wire drawdown, presumed counterparty-initiated;
                         undocumented (method label ``wireDrawdown``)

NEEDS REVIEW (settled, outgoing; aggregated per counterparty under
``needs_review`` and counted in ``reportable_total_upper_bound`` only):
  externalTransfer       linked_account_transfers. Real-organization data
                         (2026-09) showed negative rows here were the
                         organization's own linked external bank accounts
                         and cross-organization Mercury transfers, not
                         vendor debits; a vendor-initiated ACH debit could
                         still appear here, so a human confirms.
  other                  unlabeled_debits. The same data showed genuine
                         vendor-initiated ACH debits arriving as ``other``,
                         alongside Mercury product payments (card bill,
                         treasury). No method signal; a human confirms.

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
  (any includable, needs-review, or unclassified kind) status != sent
                                              not_settled:<status>: pending,
                                              cancelled, failed, reversed,
                                              blocked never moved money to
                                              completion
  (any includable, needs-review, or unclassified kind) amount >= 0
                                              incoming: a credit, refund or
                                              return, not a payment made
  postedAt outside the requested year         outside_year: the API window is
                                              padded by a day each side, so
                                              this bucket normally holds the
                                              padding rows

UNCLASSIFIED (genuinely anomalous rows, listed one by one with a reason):
  <not in the enum>      unknown_kind: schema drift
  amount missing         amount_missing: cannot total what has no amount

The classifier reads ``details`` to determine the payment method and
nothing else: no value from ``details`` is ever copied into a result.
Hints on needs-review entries are fixed strings chosen by kind and by a
name prefix test; counterparty text itself remains data.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SETTLED_STATUSES = frozenset({"sent"})

# Federal 1099-NEC/MISC reporting threshold by tax year: 600 through 2025,
# 2000 from 2026, inflation-indexed from 2027 (pass the current figure).
def default_threshold(year: int) -> float:
    """Default flag threshold for a tax year."""
    return 600.0 if year <= 2025 else 2000.0


# kind -> (method label or None to read details, reason). Labels key `by_method`.
INCLUDE_KINDS: dict[str, tuple[str | None, str]] = {
    "outgoingPayment": (None, "payment to a recipient; method read from details"),
    "exogenousWireDrawdown": ("wireDrawdown", "wire drawdown, presumed counterparty-initiated; undocumented"),
}

# kind -> (needs_review bucket, reason)
NEEDS_REVIEW_KINDS: dict[str, tuple[str, str]] = {
    "externalTransfer": (
        "linked_account_transfers",
        "real-organization data showed the organization's own linked/external accounts and cross-org transfers here",
    ),
    "other": ("unlabeled_debits", "kind 'other' carries no payment-method signal"),
}

BUCKET_HINTS: dict[str, str] = {
    "linked_account_transfers": (
        "Mercury books transfers to your own linked/external accounts and cross-org transfers here; "
        "a vendor-initiated ACH debit could also appear — confirm before adding to filing"
    ),
    "unlabeled_debits": (
        "kind 'other' with no method signal: typically vendor-initiated ACH debits or Mercury product payments — confirm"
    ),
}
MERCURY_HINT = "Mercury product payment (card bill, treasury); usually not a vendor"
_MERCURY_PREFIX = "mercury "
SAMPLE_IDS = 3

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

KNOWN_KINDS = frozenset(INCLUDE_KINDS) | frozenset(NEEDS_REVIEW_KINDS) | frozenset(EXCLUDE_KINDS)

# Method labels for outgoingPayment, in detection order (most specific first).
# DOCS: TransactionMethodData has no realTimePayment member. A real-time
# payment therefore shows up under "ach" (if electronicRoutingInfo is
# returned for it) or "unknown" (if no routing details are returned).
_DETAIL_METHODS = (
    ("internationalWireRoutingInfo", "internationalWire"),
    ("domesticWireRoutingInfo", "domesticWire"),
    ("electronicRoutingInfo", "ach"),
    ("address", "check"),
)

_CENTS = Decimal("0.01")
_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Verdict:
    """Outcome for one transaction. Carries labels and a reason, never payload."""

    decision: str  # "include" | "needs_review" | "exclude" | "unclassified"
    label: str  # include: method; needs_review: bucket; exclude: category; unclassified: short code
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
    if kind in NEEDS_REVIEW_KINDS:
        bucket, reason = NEEDS_REVIEW_KINDS[kind]
        return Verdict("needs_review", bucket, reason)
    return Verdict("unclassified", "unknown_kind", f"kind {kind!r} is not in the classification table")


def _year_of(timestamp: Any) -> int | None:
    if isinstance(timestamp, str) and len(timestamp) >= 4 and timestamp[:4].isdigit():
        return int(timestamp[:4])
    return None


def _money(value: Decimal) -> float:
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _normalize_name(name: Any) -> str:
    if isinstance(name, str):
        return " ".join(name.split()).casefold()
    return ""


def _group_key(txn: dict[str, Any]) -> tuple[str, str]:
    """(grouping, key): counterpartyId first, normalised name second, the row itself last."""
    counterparty_id = txn.get("counterpartyId")
    if isinstance(counterparty_id, str) and counterparty_id:
        return ("counterparty_id", counterparty_id)
    normalized = _normalize_name(txn.get("counterpartyName"))
    if normalized:
        return ("name", normalized)
    return ("transaction", str(txn.get("id")))


def _new_group(key: tuple[str, str]) -> dict[str, Any]:
    return {
        "key": key,
        "counterparty_id": key[1] if key[0] == "counterparty_id" else None,
        "names": Counter(),
        "total": _ZERO,
        "count": 0,
        "by_label": defaultdict(lambda: {"count": 0, "total": _ZERO}),
        "sample_ids": [],
    }


def _add_to_group(group: dict[str, Any], txn: dict[str, Any], label: str, amount: Decimal) -> None:
    name = txn.get("counterpartyName")
    if isinstance(name, str) and name.strip():
        group["names"][name.strip()] += 1
    group["total"] += amount
    group["count"] += 1
    group["by_label"][label]["count"] += 1
    group["by_label"][label]["total"] += amount
    if len(group["sample_ids"]) < SAMPLE_IDS and txn.get("id") is not None:
        group["sample_ids"].append(txn.get("id"))


def _display_name(group: dict[str, Any]) -> str:
    return (group["names"].most_common(1) or [("", 0)])[0][0]


def _hint(bucket: str, display_name: str) -> str:
    hint = BUCKET_HINTS[bucket]
    if display_name.strip().casefold().startswith(_MERCURY_PREFIX):
        hint = f"{hint}. {MERCURY_HINT}"
    return hint


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
    threshold_cents = to_cents(threshold) or _ZERO

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    review: dict[str, dict[tuple[str, str], dict[str, Any]]] = {bucket: {} for bucket in BUCKET_HINTS}
    excluded: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "amount": _ZERO})
    unclassified: list[dict[str, Any]] = []
    fallback_included = 0

    for txn in transactions:
        # Year attribution: postedAt (what the dashboard shows). createdAt is
        # only a fallback for the odd settled row that carries no postedAt;
        # the API's posted-date filter cannot return such a row, so the
        # fallback counter is normally 0.
        basis = txn.get("postedAt")
        used_fallback = not basis
        if used_fallback:
            basis = txn.get("createdAt")
        if _year_of(basis) != year:
            bucket = excluded["outside_year"]
            bucket["count"] += 1
            bucket["amount"] += to_cents(txn.get("amount")) or _ZERO
            continue

        verdict = classify(txn)
        if verdict.decision == "exclude":
            bucket = excluded[verdict.label]
            bucket["count"] += 1
            bucket["amount"] += to_cents(txn.get("amount")) or _ZERO
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

        amount = -(to_cents(txn.get("amount")) or _ZERO)  # outgoing, stored positive
        key = _group_key(txn)
        if verdict.decision == "needs_review":
            table = review[verdict.label]
            group = table.get(key)
            if group is None:
                group = table[key] = _new_group(key)
            _add_to_group(group, txn, str(txn.get("kind")), amount)
            continue

        group = groups.get(key)
        if group is None:
            group = groups[key] = _new_group(key)
        _add_to_group(group, txn, verdict.label, amount)
        if used_fallback:
            fallback_included += 1

    recipients_out: list[dict[str, Any]] = []
    for group in groups.values():
        grouping, _ = group["key"]
        cid = group["counterparty_id"]
        recipient = recipients_by_id.get(cid) if cid else None
        if recipient is not None:
            confidence = "high"
            display = recipient.get("name") or _display_name(group)
        elif grouping == "counterparty_id":
            confidence = "medium"
            display = _display_name(group)
        else:
            confidence = "low"
            display = _display_name(group)
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
                    m: {"count": v["count"], "total": _money(v["total"])} for m, v in sorted(group["by_label"].items())
                },
                "flagged": group["total"] >= threshold_cents,
                "possible_same_payee": [],
                "name_merged_total": _money(group["total"]),
                "flagged_for_review": False,
                "_total": group["total"],
            }
        )

    # Post-pass: the same payee under several counterparty ids (a recipient
    # re-created, a name-matched external id) shows up as separate id-groups
    # with the same normalised display name. Surface the merge so a reviewer
    # sees when the combined total crosses the threshold.
    by_norm_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in recipients_out:
        if entry["counterparty_id"] and entry["display_name"] != "(unknown counterparty)":
            by_norm_name[_normalize_name(entry["display_name"])].append(entry)
    for siblings in by_norm_name.values():
        if len(siblings) < 2:
            continue
        merged = sum((e["_total"] for e in siblings), _ZERO)
        for entry in siblings:
            entry["possible_same_payee"] = [e["counterparty_id"] for e in siblings if e is not entry]
            entry["name_merged_total"] = _money(merged)
            entry["flagged_for_review"] = merged >= threshold_cents
    for entry in recipients_out:
        del entry["_total"]
    recipients_out.sort(key=lambda r: (-r["total"], r["display_name"]))

    needs_review_out: dict[str, list[dict[str, Any]]] = {}
    needs_review_total = _ZERO
    needs_review_count = 0
    for bucket, table in review.items():
        entries = []
        for group in table.values():
            display = _display_name(group) or "(unknown counterparty)"
            entries.append(
                {
                    "display_name": display,
                    "counterparty_id": group["counterparty_id"],
                    "count": group["count"],
                    "total": _money(group["total"]),
                    "by_kind": {
                        k: {"count": v["count"], "total": _money(v["total"])} for k, v in sorted(group["by_label"].items())
                    },
                    "would_flag": group["total"] >= threshold_cents,
                    "sample_transaction_ids": list(group["sample_ids"]),
                    "hint": _hint(bucket, display),
                    "possible_same_payee": [],
                    "name_merged_total": _money(group["total"]),
                    "would_flag_merged": False,
                    "_total": group["total"],
                }
            )
            needs_review_total += group["total"]
            needs_review_count += group["count"]
        # Same post-pass as for recipients: the same payee under several
        # counterparty ids ("ACME LLC" / "Acme Llc") must not hide below the
        # threshold as two small rows.
        siblings_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for entry in entries:
            if entry["counterparty_id"] and entry["display_name"] != "(unknown counterparty)":
                siblings_by_name[_normalize_name(entry["display_name"])].append(entry)
        for siblings in siblings_by_name.values():
            if len(siblings) < 2:
                continue
            merged = sum((e["_total"] for e in siblings), _ZERO)
            for entry in siblings:
                entry["possible_same_payee"] = [e["counterparty_id"] for e in siblings if e is not entry]
                entry["name_merged_total"] = _money(merged)
                entry["would_flag_merged"] = merged >= threshold_cents
        for entry in entries:
            del entry["_total"]
        entries.sort(key=lambda r: (-r["total"], r["display_name"]))
        needs_review_out[bucket] = entries

    reportable_total = sum((g["total"] for g in groups.values()), _ZERO)
    return {
        "year": year,
        "threshold": _money(threshold_cents),
        "date_basis": {
            "field": "postedAt",
            "timezone": "UTC",
            "fallback_to_createdAt_count": fallback_included,
        },
        "status_basis": sorted(SETTLED_STATUSES),
        "totals": {
            "reportable_total": _money(reportable_total),
            "reportable_payment_count": sum(g["count"] for g in groups.values()),
            "recipient_count": len(recipients_out),
            "flagged_count": sum(1 for r in recipients_out if r["flagged"]),
            "needs_review_total": _money(needs_review_total),
            "needs_review_count": needs_review_count,
            "reportable_total_upper_bound": _money(reportable_total + needs_review_total),
            "unclassified_count": len(unclassified),
            "transactions_scanned": len(transactions),
        },
        "recipients": recipients_out,
        "needs_review": needs_review_out,
        "unclassified": unclassified,
        "excluded_summary": {
            category: {"count": v["count"], "amount": _money(v["amount"])} for category, v in sorted(excluded.items())
        },
    }
