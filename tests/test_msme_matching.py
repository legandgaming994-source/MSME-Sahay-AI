"""Pure unit tests for the explainable matcher (no database)."""

from datetime import date
from decimal import Decimal as D

from msme.reconciliation.matching import (Doc, Txn, assign, classify, generate_candidates, name_similarity,
                                          reference_in_text, score_pair)


def doc(id="D1", direction="outward", number="INV-2026-0042", issued=date(2026, 6, 10), due=date(2026, 7, 10), total="1180.00", party="Kurnool Traders Pvt Ltd"):
    return Doc(id, direction, number, issued, due, D(total), party)


def txn(id="T1", when=date(2026, 6, 25), amount="1180.00", kind="deposit", text="NEFT KURNOOL TRADERS INV20260042", party="KURNOOL TRADERS"):
    return Txn(id, when, D(amount), kind, text, party)


def codes(c):
    return sorted(r["code"] for r in c.reasons)


def test_exact_amount_with_reference_and_name_is_strong_and_explained():
    c = score_pair(doc(), txn())
    assert c.classification == "STRONG" and c.score == 100
    assert codes(c) == ["AMOUNT_EXACT", "NAME", "REFERENCE", "TIMING"]
    assert all(r["detail"] for r in c.reasons) and sum(r["points"] for r in c.reasons) >= c.score


def test_exact_amount_alone_is_only_possible():
    c = score_pair(doc(party="Zeta Corp"), txn(text="NEFT 998877", party="Someone Else"))
    assert c.classification == "POSSIBLE" and codes(c) == ["AMOUNT_EXACT", "TIMING"]


def test_exact_amount_and_name_without_reference_is_probable():
    c = score_pair(doc(), txn(text="NEFT 998877"))
    assert c.classification == "PROBABLE" and "REFERENCE" not in codes(c)


def test_shortfall_is_flagged_as_possible_deduction_not_assumed():
    c = score_pair(doc(), txn(amount="1062.00"))     # 10% short
    assert "AMOUNT_SHORTFALL" in codes(c) and c.difference == D("-118.00")
    detail = [r for r in c.reasons if r["code"] == "AMOUNT_SHORTFALL"][0]["detail"]
    assert "possible" in detail and "no rate is assumed" in detail
    fifteen = score_pair(doc(), txn(amount="1003.00"))          # 15% short: beyond the deduction-at-source band
    assert "AMOUNT_SHORTFALL" not in codes(fifteen) and "AMOUNT_DIFFERS" in codes(fifteen)


def test_larger_shortfall_and_overpayment_earn_no_amount_points():
    far = score_pair(doc(), txn(amount="900.00"))           # >10% short but reference+name+timing still match
    assert far is not None and "AMOUNT_DIFFERS" in codes(far) and far.classification in ("PROBABLE", "POSSIBLE")
    over = score_pair(doc(), txn(amount="1300.00"))
    assert "AMOUNT_EXCEEDS" in codes(over) and [r for r in over.reasons if r["code"] == "AMOUNT_EXCEEDS"][0]["points"] == 0


def test_direction_must_match_the_money_flow():
    assert score_pair(doc(direction="outward"), txn(kind="withdrawal")) is None
    assert score_pair(doc(direction="inward"), txn(kind="deposit")) is None
    assert score_pair(doc(direction="inward"), txn(kind="withdrawal")) is not None


def test_date_window_is_enforced():
    assert score_pair(doc(), txn(when=date(2026, 5, 1))) is None            # more than 30 days before the invoice
    assert score_pair(doc(), txn(when=date(2026, 5, 15))) is not None       # advance within 30 days
    assert score_pair(doc(), txn(when=date(2027, 1, 20))) is None           # more than 180 days after


def test_nonpositive_amounts_never_match():
    assert score_pair(doc(total="0"), txn()) is None and score_pair(doc(), txn(amount="0")) is None


def test_reference_rules_avoid_short_and_numeric_false_positives():
    assert reference_in_text("INV-2026-0042", "paid INV20260042 thanks")
    assert not reference_in_text("42", "UTR 4200000042")             # too short
    assert not reference_in_text("1234", "ref 91234")                # purely numeric under 5 digits
    assert reference_in_text("12345", "ref 912345")
    assert not reference_in_text("", "anything")


def test_name_similarity_ignores_company_suffixes():
    assert name_similarity("Kurnool Traders Pvt Ltd", "KURNOOL TRADERS") == 1.0
    assert name_similarity("Acme Pvt Ltd", "Zeta Pvt Ltd") == 0.0
    assert name_similarity("", "x") == 0.0


def test_classify_thresholds():
    assert [classify(s) for s in (100, 85, 84, 60, 59, 40, 39)] == ["STRONG", "STRONG", "PROBABLE", "PROBABLE", "POSSIBLE", "POSSIBLE", None]


def test_assignment_is_one_to_one_greedy_and_reproducible():
    docs = [doc("D1"), doc("D2", number="INV-2026-0099", total="500.00", party="Beta Foods")]
    txns = [txn("T1"), txn("T2", amount="500.00", text="NEFT BETA FOODS INV20260099", party="BETA FOODS")]
    first = assign(generate_candidates(docs, txns))
    again = assign(generate_candidates(list(reversed(docs)), list(reversed(txns))))
    assert [(c.doc_id, c.txn_id) for c in first[0]] == [("D1", "T1"), ("D2", "T2")]
    assert [(c.doc_id, c.txn_id, c.score) for c in first[0]] == [(c.doc_id, c.txn_id, c.score) for c in again[0]]
    assert len({c.doc_id for c in first[0]}) == len({c.txn_id for c in first[0]}) == 2


def test_two_invoices_competing_for_one_payment_only_one_wins_and_is_ambiguous():
    docs = [doc("D1", number="INV-AAAA-1"), doc("D2", number="INV-BBBB-2")]
    only = [txn("T1", text="NEFT 55", party="Someone")]
    primary, alts = assign(generate_candidates(docs, only))
    assert len(primary) == 1 and primary[0].ambiguous            # identical scores: a person must choose
    assert len(alts) == 1 and alts[0].txn_also_suggested_for == (primary[0].doc_id,)


def test_duplicate_looking_payments_for_one_invoice_are_flagged_ambiguous():
    primary, alts = assign(generate_candidates([doc()], [txn("T1"), txn("T2")]))
    assert len(primary) == 1 and primary[0].ambiguous and len(alts) == 1
