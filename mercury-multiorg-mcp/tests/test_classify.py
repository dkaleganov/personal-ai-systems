"""Unit tests for the pure 1099 classifier against the synthetic 2026 dataset."""

import json
from decimal import Decimal

import pytest

from mercury_multiorg_mcp.classify import (
    EXCLUDE_KINDS,
    INCLUDE_KINDS,
    KNOWN_KINDS,
    UNCLASSIFIED_KINDS,
    Verdict,
    classify,
    method_from_details,
    summarize,
    to_cents,
)

from .conftest import load_fixture

# The live TransactionKind enum (docs.mercury.com/reference/listtransactions, 2026-09-12).
LIVE_KINDS = {
    "externalTransfer",
    "internalTransfer",
    "outgoingPayment",
    "creditCardCredit",
    "creditCardTransaction",
    "debitCardCredit",
    "debitCardTransaction",
    "cardInternationalTransactionFee",
    "cardInternationalTransactionFeeRebate",
    "cardInternationalTransactionFeeReversal",
    "cardInternationalTransactionFeeRebateReversal",
    "incomingDomesticWire",
    "checkDeposit",
    "incomingInternationalWire",
    "treasuryTransfer",
    "currencyCloudReturn",
    "wireFee",
    "personalBankingSubscriptionFee",
    "billingEngineSubscriptionFee",
    "expenseReimbursement",
    "exogenousWireDrawdown",
    "interestPayment",
    "other",
}
LIVE_STATUSES = {"pending", "sent", "cancelled", "failed", "reversed", "blocked"}

ROUTING = "999999999"
ACCOUNT_NUMBERS = ("999988887777", "999977776666", "999966665555")
IBAN = "DE00000000000000000000"


@pytest.fixture
def dataset() -> list[dict]:
    return load_fixture("transactions_1099_2026.json")["transactions"]


@pytest.fixture
def recipients_by_id() -> dict[str, dict]:
    rows = load_fixture("recipients_page1.json")["recipients"] + load_fixture("recipients_page2.json")["recipients"]
    return {r["id"]: r for r in rows}


def _by_name(report: dict) -> dict[str, dict]:
    return {r["display_name"]: r for r in report["recipients"]}


def test_table_covers_every_live_kind_exactly_once():
    assert KNOWN_KINDS == LIVE_KINDS
    assert not (set(INCLUDE_KINDS) & set(EXCLUDE_KINDS))
    assert not (set(INCLUDE_KINDS) & set(UNCLASSIFIED_KINDS))
    assert not (set(EXCLUDE_KINDS) & set(UNCLASSIFIED_KINDS))


def test_fixture_exercises_every_kind_and_status(dataset):
    kinds = {t["kind"] for t in dataset}
    assert LIVE_KINDS <= kinds  # plus one deliberate unknown kind
    assert {t["status"] for t in dataset} == LIVE_STATUSES


def _sent(kind, amount, **extra):
    return {"id": "x", "kind": kind, "status": "sent", "amount": amount, "postedAt": "2026-06-01T00:00:00Z", **extra}


@pytest.mark.parametrize("kind", sorted(EXCLUDE_KINDS))
def test_excluded_kinds_are_excluded_regardless_of_status_and_sign(kind):
    category, _ = EXCLUDE_KINDS[kind]
    for status in LIVE_STATUSES:
        for amount in (-10.0, 10.0):
            v = classify({"kind": kind, "status": status, "amount": amount})
            assert v == Verdict("exclude", category, v.reason)


@pytest.mark.parametrize("kind", sorted(INCLUDE_KINDS))
def test_includable_kinds_require_sent_and_negative(kind):
    assert classify(_sent(kind, -10.0)).decision == "include"
    assert classify(_sent(kind, 10.0)) .label == "incoming"
    assert classify(_sent(kind, 0)).label == "incoming"
    for status in LIVE_STATUSES - {"sent"}:
        v = classify({"kind": kind, "status": status, "amount": -10.0})
        assert v.decision == "exclude" and v.label == f"not_settled:{status}"


def test_include_method_labels():
    assert classify(_sent("externalTransfer", -1)).label == "achPull"
    assert classify(_sent("exogenousWireDrawdown", -1)).label == "wirePull"
    ach = {"electronicRoutingInfo": {"accountNumber": "1", "routingNumber": "2", "electronicAccountType": "x"}}
    dom = {"domesticWireRoutingInfo": {"accountNumber": "1", "routingNumber": "2"}}
    intl = {"internationalWireRoutingInfo": {"iban": "x", "swiftCode": "y", "countrySpecific": {}}}
    chk = {"address": {"address1": "1 Example Way", "city": "S", "postalCode": "0"}}
    assert classify(_sent("outgoingPayment", -1, details=ach)).label == "ach"
    assert classify(_sent("outgoingPayment", -1, details=dom)).label == "domesticWire"
    assert classify(_sent("outgoingPayment", -1, details=intl)).label == "internationalWire"
    assert classify(_sent("outgoingPayment", -1, details=chk)).label == "check"
    assert classify(_sent("outgoingPayment", -1, details=None, checkNumber="55")).label == "check"
    assert classify(_sent("outgoingPayment", -1, details=None)).label == "unknown"
    assert classify(_sent("outgoingPayment", -1, details={})).label == "unknown"
    # most specific signal wins when several are present
    assert method_from_details({"details": {**ach, **intl}}) == "internationalWire"


def test_unclassified_cases():
    assert classify(_sent("other", -5)).label == "kind_other"
    assert classify(_sent("neverSeenKind", -5)).label == "unknown_kind"
    assert classify(_sent("outgoingPayment", None)).label == "amount_missing"
    assert classify(_sent("outgoingPayment", "not a number")).label == "amount_missing"
    assert classify({"kind": None, "status": "sent", "amount": -1}).label == "unknown_kind"
    # non-sent unknown kinds are a status exclusion, not an unclassified row
    assert classify({"kind": "other", "status": "pending", "amount": -5}).label == "not_settled:pending"


def test_to_cents_is_exact():
    assert to_cents(-1999.99) == Decimal("-1999.99")
    assert to_cents("0.1") + to_cents("0.2") == Decimal("0.30")
    assert to_cents(True) is None and to_cents(None) is None


def test_summary_totals_grouping_and_flags(dataset, recipients_by_id):
    report = summarize(dataset, year=2026, threshold=2000, recipients_by_id=recipients_by_id)
    by = _by_name(report)

    nw = by["Northwind Consulting LLC"]
    assert nw["total"] == 2100.0 and nw["payment_count"] == 3 and nw["flagged"] is True
    assert nw["confidence"] == "high" and nw["grouping"] == "counterparty_id"
    assert nw["recipient_id"] == nw["counterparty_id"] == "cccccccc-0001-4ccc-8ccc-cccccccccccc"
    assert nw["by_method"] == {"ach": {"count": 3, "total": 2100.0}}

    fab = by["Fabrikam Design Studio"]  # exactly at threshold flags
    assert fab["total"] == 2000.0 and fab["flagged"] is True
    assert fab["by_method"] == {"domesticWire": {"count": 1, "total": 2000.0}}

    tail = by["Tailspin Toys GmbH"]  # one cent under does not
    assert tail["total"] == 1999.99 and tail["flagged"] is False
    assert tail["by_method"] == {"internationalWire": {"count": 1, "total": 1999.99}}

    wing = by["Wingtip Cleaning Co"]
    assert wing["total"] == 1500.0 and wing["by_method"] == {"check": {"count": 2, "total": 1500.0}}

    mystery = by["Mystery Payee"]  # counterpartyId present but not a known recipient
    assert mystery["confidence"] == "medium" and mystery["recipient_id"] is None
    assert mystery["counterparty_id"] == "cccccccc-0077-4ccc-8ccc-cccccccccccc"
    assert mystery["by_method"] == {"unknown": {"count": 1, "total": 250.0}}

    lit = by["Litware Utilities"]  # name-only grouping normalises case and whitespace
    assert lit["confidence"] == "low" and lit["grouping"] == "name"
    assert lit["counterparty_id"] is None and lit["recipient_id"] is None
    assert lit["total"] == 2050.0 and lit["flagged"] is True
    assert lit["by_method"] == {"achPull": {"count": 2, "total": 2050.0}}

    draw = by["Drawdown Lender LLC"]
    assert draw["by_method"] == {"wirePull": {"count": 1, "total": 800.0}}

    assert set(by) == {
        "Northwind Consulting LLC",
        "Fabrikam Design Studio",
        "Tailspin Toys GmbH",
        "Wingtip Cleaning Co",
        "Mystery Payee",
        "Litware Utilities",
        "Drawdown Lender LLC",
    }
    # sorted by total desc
    assert [r["total"] for r in report["recipients"]] == sorted((r["total"] for r in report["recipients"]), reverse=True)

    t = report["totals"]
    assert t["reportable_total"] == 2100 + 2000 + 1999.99 + 1500 + 250 + 2050 + 800
    assert t["reportable_payment_count"] == 11
    assert t["recipient_count"] == 7
    assert t["flagged_count"] == 3
    assert t["transactions_scanned"] == len(dataset)
    assert report["threshold"] == 2000.0
    assert report["status_basis"] == ["sent"]


def test_summary_unclassified_bucket(dataset, recipients_by_id):
    report = summarize(dataset, year=2026, threshold=2000, recipients_by_id=recipients_by_id)
    unc = {u["kind"]: u for u in report["unclassified"]}
    assert set(unc) == {"other", "futureKindFromSchemaDrift"}
    assert unc["other"]["amount"] == -99.0 and unc["other"]["counterpartyName"] == "Unknown Thing"
    assert "no payment-method signal" in unc["other"]["reason"]
    assert "not in the classification table" in unc["futureKindFromSchemaDrift"]["reason"]
    assert set(unc["other"]) == {"id", "kind", "status", "amount", "postedAt", "counterpartyName", "reason"}
    assert report["totals"]["unclassified_count"] == 2


def test_summary_excluded_summary(dataset, recipients_by_id):
    report = summarize(dataset, year=2026, threshold=2000, recipients_by_id=recipients_by_id)
    ex = report["excluded_summary"]
    assert ex["internal_transfer"] == {"count": 2, "amount": -12000.0}
    assert ex["card"] == {"count": 4, "amount": -47.09}
    assert ex["bank_fee"] == {"count": 7, "amount": -50.0}
    assert ex["incoming"]["count"] == 4 + 1 + 1  # 4 incoming kinds + positive externalTransfer + positive outgoingPayment
    assert ex["incoming"]["amount"] == pytest.approx(12000 + 8000 + 450 + 12.34 + 3000 + 700)
    assert ex["returned_payment"] == {"count": 1, "amount": 1999.99}
    assert ex["reimbursement"] == {"count": 1, "amount": -320.0}
    assert ex["not_settled:pending"] == {"count": 2, "amount": -799.0}
    assert ex["not_settled:failed"] == {"count": 1, "amount": -700.0}
    assert ex["not_settled:cancelled"] == {"count": 1, "amount": -500.0}
    assert ex["not_settled:reversed"] == {"count": 1, "amount": -500.0}
    assert ex["not_settled:blocked"] == {"count": 1, "amount": -500.0}
    assert ex["outside_year"] == {"count": 2, "amount": -19998.0}
    # every scanned transaction landed in exactly one bucket
    counted = sum(v["count"] for v in ex.values()) + len(report["unclassified"]) + report["totals"]["reportable_payment_count"]
    assert counted == len(dataset)


def test_year_boundaries_by_posted_date(dataset):
    """Jan 1 and Dec 31 (UTC, by postedAt) are inside; Dec 31 prior year and Jan 1 next year are outside."""
    by_id = {t["id"]: t for t in dataset}
    jan1 = next(t for t in dataset if t["postedAt"] == "2026-01-01T00:00:00Z")
    assert jan1["createdAt"].startswith("2025-12-31")  # created in the prior year, posted in 2026 -> counts in 2026
    dec31 = next(t for t in dataset if t["postedAt"] == "2026-12-31T23:59:59Z")
    prior = next(t for t in dataset if t["postedAt"] == "2025-12-31T23:59:59Z")
    following = next(t for t in dataset if t["postedAt"] == "2027-01-01T00:00:00Z")
    assert following["createdAt"].startswith("2026-12-31")  # created in 2026, posted in 2027 -> not in 2026

    r2026 = summarize([jan1, dec31, prior, following], year=2026, threshold=0)
    assert r2026["totals"]["reportable_payment_count"] == 2
    assert r2026["recipients"][0]["total"] == 1400.0
    assert r2026["excluded_summary"]["outside_year"]["count"] == 2

    r2027 = summarize([jan1, dec31, prior, following], year=2027, threshold=0)
    assert r2027["totals"]["reportable_payment_count"] == 1 and r2027["recipients"][0]["total"] == 9999.0
    assert by_id[following["id"]] is following


def test_posted_at_missing_falls_back_to_created_at():
    row = {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -5, "postedAt": None, "createdAt": "2026-03-03T00:00:00Z", "counterpartyId": "c", "counterpartyName": "N"}
    report = summarize([row], year=2026, threshold=0)
    assert report["totals"]["reportable_payment_count"] == 1
    assert report["date_basis"]["fallback_to_createdAt_count"] == 1
    assert report["date_basis"]["field"] == "postedAt" and report["date_basis"]["timezone"] == "UTC"


def test_threshold_is_compared_in_cents():
    rows = [
        {"id": str(i), "kind": "outgoingPayment", "status": "sent", "amount": -0.1, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "c", "counterpartyName": "N"}
        for i in range(3)
    ]
    report = summarize(rows, year=2026, threshold=0.3)  # 0.1+0.1+0.1 == 0.3 exactly in cents, not in floats
    assert report["recipients"][0]["total"] == 0.3 and report["recipients"][0]["flagged"] is True


def test_missing_counterparty_entirely_groups_per_transaction():
    rows = [
        {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -1, "postedAt": "2026-01-01T00:00:00Z"},
        {"id": "b", "kind": "outgoingPayment", "status": "sent", "amount": -1, "postedAt": "2026-01-01T00:00:00Z", "counterpartyName": "   "},
    ]
    report = summarize(rows, year=2026, threshold=0)
    assert len(report["recipients"]) == 2
    assert {r["display_name"] for r in report["recipients"]} == {"(unknown counterparty)"}
    assert {r["grouping"] for r in report["recipients"]} == {"transaction"}
    assert {r["confidence"] for r in report["recipients"]} == {"low"}


def test_display_name_prefers_recipient_record_over_transaction_text(recipients_by_id):
    row = {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -1, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "cccccccc-0001-4ccc-8ccc-cccccccccccc", "counterpartyName": "NORTHWIND CONSULT (ACH)"}
    report = summarize([row], year=2026, threshold=0, recipients_by_id=recipients_by_id)
    assert report["recipients"][0]["display_name"] == "Northwind Consulting LLC"


def test_classifier_output_never_carries_bank_coordinates(dataset, recipients_by_id):
    """Fixtures carry routing/account numbers and an IBAN inside `details`; none may reach the report."""
    assert any(t.get("details") for t in dataset)
    report = summarize(dataset, year=2026, threshold=2000, recipients_by_id=recipients_by_id)
    dumped = json.dumps(report)
    assert ROUTING not in dumped
    assert IBAN not in dumped
    for number in ACCOUNT_NUMBERS:
        assert number not in dumped
    assert "details" not in dumped
    assert "RoutingInfo" not in dumped
    assert "address1" not in dumped
