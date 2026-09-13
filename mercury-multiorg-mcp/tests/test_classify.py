"""Unit tests for the pure 1099 classifier against the synthetic 2026 dataset."""

import json
from decimal import Decimal

import pytest

from mercury_multiorg_mcp.classify import (
    BUCKET_HINTS,
    EXCLUDE_KINDS,
    INCLUDE_KINDS,
    KNOWN_KINDS,
    MERCURY_HINT,
    NEEDS_REVIEW_KINDS,
    Verdict,
    classify,
    default_threshold,
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

# Expected 2026 figures for the fixture (see the fixture generator comments).
REPORTABLE_TOTAL = 9249.99  # 2100 + 2000 + 1999.99 + 1500 + 600 + 250 + 800
REPORTABLE_COUNT = 10
NEEDS_REVIEW_TOTAL = 6650.0  # 3000 + 2050 + 1500 + 100
NEEDS_REVIEW_COUNT = 6
UPPER_BOUND = 15899.99


@pytest.fixture
def dataset() -> list[dict]:
    return load_fixture("transactions_1099_2026.json")["transactions"]


@pytest.fixture
def recipients_by_id() -> dict[str, dict]:
    rows = load_fixture("recipients_page1.json")["recipients"] + load_fixture("recipients_page2.json")["recipients"]
    return {r["id"]: r for r in rows}


@pytest.fixture
def report(dataset, recipients_by_id) -> dict:
    return summarize(dataset, year=2026, threshold=2000, recipients_by_id=recipients_by_id)


def _by_name(report: dict) -> dict[str, dict]:
    return {r["display_name"]: r for r in report["recipients"]}


def _by_key(report: dict) -> dict[str, dict]:
    return {r["counterparty_id"] or r["display_name"]: r for r in report["recipients"]}


# -- table shape ---------------------------------------------------------


def test_table_covers_every_live_kind_exactly_once():
    assert KNOWN_KINDS == LIVE_KINDS
    tables = (set(INCLUDE_KINDS), set(NEEDS_REVIEW_KINDS), set(EXCLUDE_KINDS))
    for i, a in enumerate(tables):
        for b in tables[i + 1 :]:
            assert not (a & b)
    assert set(INCLUDE_KINDS) == {"outgoingPayment", "exogenousWireDrawdown"}
    assert set(NEEDS_REVIEW_KINDS) == {"externalTransfer", "other"}
    assert {b for b, _ in NEEDS_REVIEW_KINDS.values()} == set(BUCKET_HINTS) == {"linked_account_transfers", "unlabeled_debits"}


def test_no_unhedged_pull_claim_in_table():
    """The docs define no kind semantics; the code must not assert 'ACH pull' or 'wire pull'."""
    import inspect

    import mercury_multiorg_mcp.classify as mod

    src = inspect.getsource(mod).lower()
    assert "achpull" not in src and "wirepull" not in src and "ach pull" not in src and "wire pull" not in src


def test_fixture_exercises_every_kind_and_status(dataset):
    kinds = {t["kind"] for t in dataset}
    assert LIVE_KINDS <= kinds  # plus one deliberate unknown kind
    assert {t["status"] for t in dataset} == LIVE_STATUSES


def test_default_threshold_is_year_aware():
    assert default_threshold(2024) == 600.0
    assert default_threshold(2025) == 600.0
    assert default_threshold(2026) == 2000.0
    assert default_threshold(2030) == 2000.0


# -- classify() ----------------------------------------------------------


def _sent(kind, amount, **extra):
    return {"id": "x", "kind": kind, "status": "sent", "amount": amount, "postedAt": "2026-06-01T00:00:00Z", **extra}


@pytest.mark.parametrize("kind", sorted(EXCLUDE_KINDS))
def test_excluded_kinds_are_excluded_regardless_of_status_and_sign(kind):
    category, _ = EXCLUDE_KINDS[kind]
    for status in LIVE_STATUSES:
        for amount in (-10.0, 10.0):
            v = classify({"kind": kind, "status": status, "amount": amount})
            assert v == Verdict("exclude", category, v.reason)


@pytest.mark.parametrize("kind", sorted(INCLUDE_KINDS) + sorted(NEEDS_REVIEW_KINDS) + ["neverSeenKind"])
def test_non_excluded_kinds_require_sent_and_negative(kind):
    assert classify(_sent(kind, 10.0)).label == "incoming"
    assert classify(_sent(kind, 0)).label == "incoming"
    for status in LIVE_STATUSES - {"sent"}:
        v = classify({"kind": kind, "status": status, "amount": -10.0})
        assert v.decision == "exclude" and v.label == f"not_settled:{status}"


def test_include_and_needs_review_labels():
    assert classify(_sent("exogenousWireDrawdown", -1)) == Verdict(
        "include", "wireDrawdown", "wire drawdown, presumed counterparty-initiated; undocumented"
    )
    v = classify(_sent("externalTransfer", -1))
    assert v.decision == "needs_review" and v.label == "linked_account_transfers"
    v = classify(_sent("other", -1))
    assert v.decision == "needs_review" and v.label == "unlabeled_debits"
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
    assert classify(_sent("neverSeenKind", -5)).label == "unknown_kind"
    assert classify(_sent("outgoingPayment", None)).label == "amount_missing"
    assert classify(_sent("outgoingPayment", "not a number")).label == "amount_missing"
    assert classify(_sent("other", None)).label == "amount_missing"
    assert classify({"kind": None, "status": "sent", "amount": -1}).label == "unknown_kind"
    # `other` is no longer unclassified: it is a needs-review bucket
    assert classify(_sent("other", -5)).decision == "needs_review"


def test_to_cents_is_exact():
    assert to_cents(-1999.99) == Decimal("-1999.99")
    assert to_cents("0.1") + to_cents("0.2") == Decimal("0.30")
    assert to_cents(True) is None and to_cents(None) is None


# -- summarize(): recipients ---------------------------------------------


def test_summary_totals_grouping_and_flags(dataset, report):
    by = _by_key(report)

    nw = by["cccccccc-0001-4ccc-8ccc-cccccccccccc"]
    assert nw["display_name"] == "Northwind Consulting LLC"
    assert nw["total"] == 2100.0 and nw["payment_count"] == 3 and nw["flagged"] is True
    assert nw["confidence"] == "high" and nw["grouping"] == "counterparty_id"
    assert nw["recipient_id"] == nw["counterparty_id"]
    assert nw["by_method"] == {"ach": {"count": 3, "total": 2100.0}}
    assert nw["possible_same_payee"] == [] and nw["name_merged_total"] == 2100.0 and nw["flagged_for_review"] is False

    fab = by["cccccccc-0003-4ccc-8ccc-cccccccccccc"]  # exactly at threshold flags
    assert fab["total"] == 2000.0 and fab["flagged"] is True
    assert fab["by_method"] == {"domesticWire": {"count": 1, "total": 2000.0}}

    tail = by["cccccccc-0004-4ccc-8ccc-cccccccccccc"]  # one cent under does not
    assert tail["total"] == 1999.99 and tail["flagged"] is False
    assert tail["by_method"] == {"internationalWire": {"count": 1, "total": 1999.99}}

    mystery = by["cccccccc-0077-4ccc-8ccc-cccccccccccc"]  # counterpartyId present but not a known recipient
    assert mystery["display_name"] == "Mystery Payee"
    assert mystery["confidence"] == "medium" and mystery["recipient_id"] is None
    assert mystery["by_method"] == {"unknown": {"count": 1, "total": 250.0}}

    draw = by["cccccccc-0088-4ccc-8ccc-cccccccccccc"]
    assert draw["by_method"] == {"wireDrawdown": {"count": 1, "total": 800.0}}

    # externalTransfer and other rows are no longer recipients
    names = {r["display_name"] for r in report["recipients"]}
    assert "Litware Utilities" not in names and "Unknown Thing" not in names and "Mercury Credit" not in names
    assert len(report["recipients"]) == 7
    # sorted by total desc
    assert [r["total"] for r in report["recipients"]] == sorted((r["total"] for r in report["recipients"]), reverse=True)

    t = report["totals"]
    assert t["reportable_total"] == REPORTABLE_TOTAL
    assert t["reportable_payment_count"] == REPORTABLE_COUNT
    assert t["recipient_count"] == 7
    assert t["flagged_count"] == 2
    assert t["needs_review_total"] == NEEDS_REVIEW_TOTAL
    assert t["needs_review_count"] == NEEDS_REVIEW_COUNT
    assert t["reportable_total_upper_bound"] == UPPER_BOUND
    assert t["reportable_total_upper_bound"] == pytest.approx(t["reportable_total"] + t["needs_review_total"])
    assert t["unclassified_count"] == 1
    assert t["transactions_scanned"] == len(dataset)
    assert report["threshold"] == 2000.0
    assert report["status_basis"] == ["sent"]


def test_same_payee_under_two_counterparty_ids(report):
    by = _by_key(report)
    wing = by["cccccccc-0005-4ccc-8ccc-cccccccccccc"]
    wing2 = by["cccccccc-0055-4ccc-8ccc-cccccccccccc"]
    assert wing["display_name"] == wing2["display_name"] == "Wingtip Cleaning Co"
    assert wing["confidence"] == "high" and wing2["confidence"] == "medium"
    assert wing["total"] == 1500.0 and wing2["total"] == 600.0
    assert wing["flagged"] is False and wing2["flagged"] is False  # neither crosses alone
    assert wing["possible_same_payee"] == ["cccccccc-0055-4ccc-8ccc-cccccccccccc"]
    assert wing2["possible_same_payee"] == ["cccccccc-0005-4ccc-8ccc-cccccccccccc"]
    assert wing["name_merged_total"] == wing2["name_merged_total"] == 2100.0
    assert wing["flagged_for_review"] is True and wing2["flagged_for_review"] is True
    # merged total is informational: flagged_count still counts individual groups only
    assert report["totals"]["flagged_count"] == 2


def test_same_payee_merge_below_threshold_is_not_flagged_for_review():
    rows = [
        {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -100, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "id-1", "counterpartyName": "Same Co"},
        {"id": "b", "kind": "outgoingPayment", "status": "sent", "amount": -100, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "id-2", "counterpartyName": "SAME   co"},
        {"id": "c", "kind": "outgoingPayment", "status": "sent", "amount": -100, "postedAt": "2026-01-01T00:00:00Z", "counterpartyName": "Same Co"},
    ]
    report = summarize(rows, year=2026, threshold=500)
    by = _by_key(report)
    assert by["id-1"]["possible_same_payee"] == ["id-2"] and by["id-2"]["possible_same_payee"] == ["id-1"]
    assert by["id-1"]["name_merged_total"] == 200.0 and by["id-1"]["flagged_for_review"] is False
    # the name-only group is not part of the id merge (it is already grouped by name)
    assert by["Same Co"]["grouping"] == "name" and by["Same Co"]["possible_same_payee"] == []


# -- summarize(): needs_review -------------------------------------------


def test_needs_review_buckets(report):
    nr = report["needs_review"]
    assert set(nr) == {"linked_account_transfers", "unlabeled_debits"}

    linked = nr["linked_account_transfers"]
    assert [e["display_name"] for e in linked] == ["Acme Holdings External Checking", "Litware Utilities"]  # total desc
    acme, lit = linked
    assert acme == {
        "display_name": "Acme Holdings External Checking",
        "counterparty_id": "cccccccc-0090-4ccc-8ccc-cccccccccccc",
        "count": 1,
        "total": 3000.0,
        "by_kind": {"externalTransfer": {"count": 1, "total": 3000.0}},
        "would_flag": True,
        "sample_transaction_ids": ["bbbbbbbb-0044-4bbb-8bbb-bbbbbbbbbbbb"],
        "hint": BUCKET_HINTS["linked_account_transfers"],
        "possible_same_payee": [],
        "name_merged_total": 3000.0,
        "would_flag_merged": False,
    }
    # name-only aggregation normalises case and whitespace across rows
    assert lit["counterparty_id"] is None and lit["count"] == 2 and lit["total"] == 2050.0
    assert lit["would_flag"] is True and len(lit["sample_transaction_ids"]) == 2
    assert lit["by_kind"] == {"externalTransfer": {"count": 2, "total": 2050.0}}
    assert "confirm before adding to filing" in lit["hint"] and MERCURY_HINT not in lit["hint"]

    unlabeled = nr["unlabeled_debits"]
    assert [e["display_name"] for e in unlabeled] == ["Mercury Credit", "Unknown Thing"]
    mercury, unknown = unlabeled
    assert mercury["total"] == 1500.0 and mercury["would_flag"] is False
    assert mercury["by_kind"] == {"other": {"count": 1, "total": 1500.0}}
    assert mercury["hint"] == f"{BUCKET_HINTS['unlabeled_debits']}. {MERCURY_HINT}"
    assert unknown["count"] == 2 and unknown["total"] == 100.0 and unknown["would_flag"] is False
    assert unknown["hint"] == BUCKET_HINTS["unlabeled_debits"]
    assert MERCURY_HINT not in unknown["hint"]

    # the pending `other` row is a status exclusion, not a needs-review row
    assert report["excluded_summary"]["not_settled:pending"]["count"] == 2


def test_needs_review_sample_ids_capped_at_three_and_would_flag_uses_threshold():
    rows = [
        {"id": f"t{i}", "kind": "other", "status": "sent", "amount": -500, "postedAt": "2026-01-01T00:00:00Z", "counterpartyName": "Some Vendor"}
        for i in range(5)
    ]
    report = summarize(rows, year=2026, threshold=2500)
    entry = report["needs_review"]["unlabeled_debits"][0]
    assert entry["count"] == 5 and entry["total"] == 2500.0
    assert entry["sample_transaction_ids"] == ["t0", "t1", "t2"]
    assert entry["would_flag"] is True  # exactly at threshold
    assert report["needs_review"]["linked_account_transfers"] == []
    assert report["recipients"] == [] and report["totals"]["reportable_total"] == 0.0
    assert report["totals"]["needs_review_total"] == 2500.0 and report["totals"]["reportable_total_upper_bound"] == 2500.0


def test_mercury_hint_is_by_prefix_only():
    def one(name):
        rows = [{"id": "a", "kind": "other", "status": "sent", "amount": -1, "postedAt": "2026-01-01T00:00:00Z", "counterpartyName": name}]
        return summarize(rows, year=2026, threshold=0)["needs_review"]["unlabeled_debits"][0]["hint"]

    assert MERCURY_HINT in one("Mercury Credit")
    assert MERCURY_HINT in one("MERCURY TREASURY")
    assert MERCURY_HINT in one("  mercury io")
    assert MERCURY_HINT not in one("Mercuryville Plumbing")  # no trailing space after the word
    assert MERCURY_HINT not in one("Not Mercury Inc")
    assert MERCURY_HINT not in one("Mercury")


# -- summarize(): unclassified and excluded -------------------------------


def test_summary_unclassified_bucket(report):
    unc = report["unclassified"]
    assert len(unc) == 1
    assert unc[0]["kind"] == "futureKindFromSchemaDrift" and unc[0]["amount"] == -1.0
    assert "not in the classification table" in unc[0]["reason"]
    assert set(unc[0]) == {"id", "kind", "status", "amount", "postedAt", "counterpartyName", "reason"}


def test_amount_missing_row_reaches_unclassified():
    rows = [
        {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": None, "postedAt": "2026-05-05T00:00:00Z", "counterpartyId": "c", "counterpartyName": "No Amount Co"},
        {"id": "b", "kind": "outgoingPayment", "status": "sent", "postedAt": "2026-05-05T00:00:00Z", "counterpartyName": "No Amount Co"},
    ]
    report = summarize(rows, year=2026, threshold=0)
    assert [u["reason"] for u in report["unclassified"]] == ["transaction has no usable amount"] * 2
    assert report["unclassified"][0] == {
        "id": "a",
        "kind": "outgoingPayment",
        "status": "sent",
        "amount": None,
        "postedAt": "2026-05-05T00:00:00Z",
        "counterpartyName": "No Amount Co",
        "reason": "transaction has no usable amount",
    }
    assert report["recipients"] == [] and report["totals"]["unclassified_count"] == 2


def test_summary_excluded_summary(dataset, report):
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
    # every scanned transaction landed in exactly one place
    counted = (
        sum(v["count"] for v in ex.values())
        + len(report["unclassified"])
        + report["totals"]["reportable_payment_count"]
        + report["totals"]["needs_review_count"]
    )
    assert counted == len(dataset)


# -- summarize(): dates -------------------------------------------------


def test_year_boundaries_by_posted_date(dataset):
    """Jan 1 and Dec 31 (UTC, by postedAt) are inside; Dec 31 prior year and Jan 1 next year are outside."""
    jan1 = next(t for t in dataset if t["postedAt"] == "2026-01-01T00:00:00Z")
    assert jan1["createdAt"].startswith("2025-12-31")  # created in the prior year, posted in 2026 -> counts in 2026
    dec31 = next(t for t in dataset if t["postedAt"] == "2026-12-31T23:59:59Z")
    prior = next(t for t in dataset if t["postedAt"] == "2025-12-31T23:59:59Z")
    following = next(t for t in dataset if t["postedAt"] == "2027-01-01T00:00:00Z")
    assert following["createdAt"].startswith("2026-12-31")  # created in 2026, posted in 2027 -> not in 2026

    r2026 = summarize([jan1, dec31, prior, following], year=2026, threshold=0)
    assert r2026["totals"]["reportable_payment_count"] == 2
    assert r2026["recipients"][0]["total"] == 1400.0
    assert r2026["excluded_summary"]["outside_year"] == {"count": 2, "amount": -19998.0}

    r2027 = summarize([jan1, dec31, prior, following], year=2027, threshold=0)
    assert r2027["totals"]["reportable_payment_count"] == 1 and r2027["recipients"][0]["total"] == 9999.0


def test_posted_at_missing_falls_back_to_created_at_and_counts_included_rows_only():
    included = {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -5, "postedAt": None, "createdAt": "2026-03-03T00:00:00Z", "counterpartyId": "c", "counterpartyName": "N"}
    pending = {"id": "b", "kind": "outgoingPayment", "status": "pending", "amount": -5, "postedAt": None, "createdAt": "2026-03-04T00:00:00Z", "counterpartyId": "c", "counterpartyName": "N"}
    reviewed = {"id": "c", "kind": "other", "status": "sent", "amount": -5, "postedAt": None, "createdAt": "2026-03-05T00:00:00Z", "counterpartyName": "M"}
    report = summarize([included, pending, reviewed], year=2026, threshold=0)
    assert report["totals"]["reportable_payment_count"] == 1
    assert report["excluded_summary"]["not_settled:pending"]["count"] == 1
    assert report["totals"]["needs_review_count"] == 1
    assert report["date_basis"] == {"field": "postedAt", "timezone": "UTC", "fallback_to_createdAt_count": 1}


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
    assert all(r["possible_same_payee"] == [] for r in report["recipients"])


def test_unknown_counterparty_id_groups_never_merge_as_same_payee():
    """Two id-groups with no usable name both display '(unknown counterparty)'; that must not read as one payee."""
    rows = [
        {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -900, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "id-1"},
        {"id": "b", "kind": "outgoingPayment", "status": "sent", "amount": -900, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "id-2", "counterpartyName": "  "},
    ]
    report = summarize(rows, year=2026, threshold=1000)
    by = _by_key(report)
    assert by["id-1"]["display_name"] == by["id-2"]["display_name"] == "(unknown counterparty)"
    assert by["id-1"]["grouping"] == by["id-2"]["grouping"] == "counterparty_id"
    for entry in (by["id-1"], by["id-2"]):
        assert entry["possible_same_payee"] == []
        assert entry["name_merged_total"] == 900.0
        assert entry["flagged_for_review"] is False


def test_display_name_prefers_recipient_record_over_transaction_text(recipients_by_id):
    row = {"id": "a", "kind": "outgoingPayment", "status": "sent", "amount": -1, "postedAt": "2026-01-01T00:00:00Z", "counterpartyId": "cccccccc-0001-4ccc-8ccc-cccccccccccc", "counterpartyName": "NORTHWIND CONSULT (ACH)"}
    report = summarize([row], year=2026, threshold=0, recipients_by_id=recipients_by_id)
    assert report["recipients"][0]["display_name"] == "Northwind Consulting LLC"


def test_classifier_output_never_carries_bank_coordinates(dataset, report):
    """Fixtures carry routing/account numbers and an IBAN inside `details`; none may reach the report."""
    assert any(t.get("details") for t in dataset)
    dumped = json.dumps(report)
    assert ROUTING not in dumped
    assert IBAN not in dumped
    for number in ACCOUNT_NUMBERS:
        assert number not in dumped
    assert "details" not in dumped
    assert "RoutingInfo" not in dumped
    assert "address1" not in dumped


def test_needs_review_same_payee_under_two_ids_is_merged_for_review():
    """'ACME LLC' and 'Acme Llc' under two counterparty ids: each below the threshold, together above it."""
    rows = [
        {"id": "a", "kind": "other", "status": "sent", "amount": -1200, "postedAt": "2026-02-01T00:00:00Z", "counterpartyId": "id-1", "counterpartyName": "ACME LLC"},
        {"id": "b", "kind": "other", "status": "sent", "amount": -900, "postedAt": "2026-03-01T00:00:00Z", "counterpartyId": "id-2", "counterpartyName": "Acme  Llc"},
        {"id": "c", "kind": "other", "status": "sent", "amount": -50, "postedAt": "2026-03-02T00:00:00Z", "counterpartyName": "acme llc"},  # name-only: not part of the id merge
        {"id": "d", "kind": "externalTransfer", "status": "sent", "amount": -10, "postedAt": "2026-03-03T00:00:00Z", "counterpartyId": "id-3", "counterpartyName": "ACME LLC"},  # other bucket: separate
    ]
    report = summarize(rows, year=2026, threshold=2000)
    unlabeled = {e["counterparty_id"] or e["display_name"]: e for e in report["needs_review"]["unlabeled_debits"]}
    one, two = unlabeled["id-1"], unlabeled["id-2"]
    assert one["would_flag"] is False and two["would_flag"] is False
    assert one["possible_same_payee"] == ["id-2"] and two["possible_same_payee"] == ["id-1"]
    assert one["name_merged_total"] == two["name_merged_total"] == 2100.0
    assert one["would_flag_merged"] is True and two["would_flag_merged"] is True
    name_only = unlabeled["ACME LLC"] if "ACME LLC" in unlabeled else unlabeled["acme llc"]
    assert name_only["counterparty_id"] is None and name_only["possible_same_payee"] == [] and name_only["would_flag_merged"] is False
    linked = report["needs_review"]["linked_account_transfers"][0]
    assert linked["counterparty_id"] == "id-3" and linked["possible_same_payee"] == [] and linked["name_merged_total"] == 10.0
    # totals are unaffected by the cross-reference
    assert report["totals"]["needs_review_total"] == 2160.0 and report["totals"]["reportable_total"] == 0.0


def test_needs_review_merge_below_threshold_is_not_flagged():
    rows = [
        {"id": "a", "kind": "other", "status": "sent", "amount": -100, "postedAt": "2026-02-01T00:00:00Z", "counterpartyId": "id-1", "counterpartyName": "Small Co"},
        {"id": "b", "kind": "other", "status": "sent", "amount": -100, "postedAt": "2026-03-01T00:00:00Z", "counterpartyId": "id-2", "counterpartyName": "SMALL CO"},
    ]
    entries = summarize(rows, year=2026, threshold=500)["needs_review"]["unlabeled_debits"]
    assert all(e["name_merged_total"] == 200.0 and e["would_flag_merged"] is False and len(e["possible_same_payee"]) == 1 for e in entries)
