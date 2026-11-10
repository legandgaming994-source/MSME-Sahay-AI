"""Tests for the effective-dated MSME rule engine (classification, GST calendar, schemes).

Pure Python: runs under pytest or `python -m tests.test_msme_rules`.
Expected GST dates are cross-checked against published FY2026-27 tables
(e.g. June 2026 -> GSTR-1 11 Jul, GSTR-3B 20 Jul; QRMP Q2 -> 13 Oct, 22/24 Oct).
"""

from datetime import date

import pytest

from msme.rules import (
    Fact, RuleError, classify_msme, evaluate_scheme, facts_from_classification, gst_deadlines, list_schemes,
)

CR = 10_000_000
NOW = date(2026, 10, 17)


# --- classification ---------------------------------------------------------

def test_micro_boundaries_inclusive_current_rules():
    r = classify_msme(2.5 * CR, 10 * CR, NOW)
    assert r.status == "CLASSIFIED" and r.category == "Micro"


def test_either_metric_pushes_category_up():
    assert classify_msme(2 * CR, 15 * CR, NOW).category == "Small"
    assert classify_msme(5 * CR, 8 * CR, NOW).category == "Small"
    assert classify_msme(30 * CR, 50 * CR, NOW).category == "Medium"


def test_not_msme_above_medium():
    assert classify_msme(130 * CR, 10 * CR, NOW).status == "NOT_MSME"
    assert classify_msme(10 * CR, 600 * CR, NOW).status == "NOT_MSME"


def test_effective_dating_old_limits_apply_before_april_2025():
    old = classify_msme(2 * CR, 8 * CR, date(2024, 1, 1))
    assert old.category == "Small" and "2119" in old.rule_version
    assert classify_msme(2 * CR, 8 * CR, date(2025, 4, 1)).category == "Micro"
    assert classify_msme(2 * CR, 8 * CR, date(2025, 3, 31)).category == "Small"


def test_no_rule_before_earliest_version():
    with pytest.raises(RuleError):
        classify_msme(CR, CR, date(2019, 1, 1))


def test_partial_evidence_is_insufficient_with_lower_bound():
    r = classify_msme(2 * CR, None, NOW)
    assert r.status == "INSUFFICIENT_EVIDENCE" and r.category is None and r.minimum_category == "Micro"
    r = classify_msme(None, 15 * CR, NOW)
    assert r.status == "INSUFFICIENT_EVIDENCE" and r.minimum_category == "Small"
    assert classify_msme(None, None, NOW).minimum_category is None


def test_partial_evidence_already_above_medium_is_not_msme():
    assert classify_msme(None, 600 * CR, NOW).status == "NOT_MSME"


def test_negative_and_garbage_rejected():
    with pytest.raises(RuleError):
        classify_msme(-1, 1, NOW)
    with pytest.raises(RuleError):
        classify_msme("abc", 1, NOW)


# --- GST calendar -----------------------------------------------------------

def _pick(ds, form, period):
    m = [d for d in ds if d.form == form and d.period == period]
    assert len(m) == 1, (form, period, m)
    return m[0]


def test_regular_monthly_calendar():
    ds = gst_deadlines("regular_monthly", 2026)
    assert len(ds) == 25  # 12 x GSTR-1 + 12 x GSTR-3B + GSTR-9
    assert _pick(ds, "GSTR-1", "2026-06").due_date == date(2026, 7, 11)
    assert _pick(ds, "GSTR-3B", "2026-06").due_date == date(2026, 7, 20)
    assert _pick(ds, "GSTR-1", "2026-12").due_date == date(2027, 1, 11)
    assert _pick(ds, "GSTR-3B", "2027-03").due_date == date(2027, 4, 20)  # crosses year boundary
    assert _pick(ds, "GSTR-9", "FY2026-27").due_date == date(2027, 12, 31)
    assert all(d.basis == "STATUTORY_DEFAULT" and "not a filing" in d.note for d in ds)


def test_qrmp_calendar_categories_and_payments():
    x = gst_deadlines("qrmp", 2026, "X")
    y = gst_deadlines("qrmp", 2026, "Y")
    assert _pick(x, "GSTR-1", "2026-07..2026-09").due_date == date(2026, 10, 13)
    assert _pick(x, "GSTR-3B", "2026-07..2026-09").due_date == date(2026, 10, 22)
    assert _pick(y, "GSTR-3B", "2026-07..2026-09").due_date == date(2026, 10, 24)
    assert _pick(x, "GSTR-3B", "2027-01..2027-03").due_date == date(2027, 4, 22)
    pmt = sorted(d.due_date for d in x if d.form == "PMT-06")
    assert len(pmt) == 8 and pmt[0] == date(2026, 5, 25) and pmt[1] == date(2026, 6, 25)
    # month 3 of a quarter has no PMT-06 (paid via the quarterly GSTR-3B)
    assert not [d for d in x if d.form == "PMT-06" and d.period == "2026-06"]
    assert all(d.optional for d in x if d.form == "IFF")


def test_qrmp_requires_explicit_state_category():
    with pytest.raises(RuleError):
        gst_deadlines("qrmp", 2026)
    with pytest.raises(RuleError):
        gst_deadlines("qrmp", 2026, "Z")


def test_composition_calendar():
    ds = gst_deadlines("composition", 2026)
    assert _pick(ds, "CMP-08", "2026-04..2026-06").due_date == date(2026, 7, 18)
    assert _pick(ds, "GSTR-4", "FY2026-27").due_date == date(2027, 4, 30)


def test_extension_override_is_marked_and_scoped():
    ds = gst_deadlines("regular_monthly", 2026, overrides=[
        {"form": "GSTR-3B", "period": "2026-06", "due_date": "2026-07-31", "source": "notification (test)"}])
    o = _pick(ds, "GSTR-3B", "2026-06")
    assert o.basis == "EXTENSION_OVERRIDE" and o.due_date == date(2026, 7, 31)
    assert _pick(ds, "GSTR-3B", "2026-07").basis == "STATUTORY_DEFAULT"


def test_unknown_filer_and_pre_rule_year():
    with pytest.raises(RuleError):
        gst_deadlines("bogus", 2026)
    with pytest.raises(RuleError):
        gst_deadlines("regular_monthly", 2019)


# --- schemes ----------------------------------------------------------------

def F(**kw):
    return [Fact(k, v, "user_declared") for k, v in kw.items()]


def test_list_schemes():
    assert {s["id"] for s in list_schemes()} == {"pmmy-mudra", "cgtmse"}


def test_mudra_appears_met_never_says_eligible():
    a = evaluate_scheme("pmmy-mudra", F(non_corporate_non_farm=True, credit_facility_amount=400_000), NOW)
    assert a.status == "CRITERIA_APPEARS_MET" and a.lender_decision_required is True
    assert "NOT lender decisions" in a.disclaimer
    assert all(c.evidence is not None for c in a.criteria)


def test_mudra_tarun_plus_needs_prior_repayment():
    base = dict(non_corporate_non_farm=True, credit_facility_amount=1_500_000)
    assert evaluate_scheme("pmmy-mudra", F(**base, prior_tarun_loan_repaid=False), NOW).status == "CRITERIA_NOT_MET"
    assert evaluate_scheme("pmmy-mudra", F(**base, prior_tarun_loan_repaid=True), NOW).status == "CRITERIA_APPEARS_MET"
    assert evaluate_scheme("pmmy-mudra", F(**base), NOW).status == "INSUFFICIENT_EVIDENCE"
    assert evaluate_scheme("pmmy-mudra", F(non_corporate_non_farm=True, credit_facility_amount=2_500_000,
                                           prior_tarun_loan_repaid=True), NOW).status == "CRITERIA_NOT_MET"


def test_mudra_effective_dating_limit_was_10_lakh_before_oct_2024():
    facts = F(non_corporate_non_farm=True, credit_facility_amount=1_500_000, prior_tarun_loan_repaid=True)
    assert evaluate_scheme("pmmy-mudra", facts, date(2024, 10, 23)).status == "CRITERIA_NOT_MET"
    assert evaluate_scheme("pmmy-mudra", facts, date(2024, 10, 24)).status == "CRITERIA_APPEARS_MET"


def test_missing_evidence_is_unknown_not_met():
    a = evaluate_scheme("pmmy-mudra", [], NOW)
    assert a.status == "INSUFFICIENT_EVIDENCE"
    assert {c.status for c in a.criteria} == {"UNKNOWN"}


def test_cgtmse_flow_with_classification_facts():
    cls = classify_msme(3 * CR, 20 * CR, NOW)  # Small
    facts = facts_from_classification(cls) + F(udyam_registered=True, sector="service", collateral_free_requested=True)
    a = evaluate_scheme("cgtmse", facts, NOW)
    assert a.status == "CRITERIA_APPEARS_MET"
    assert a.criteria[1].evidence.source.startswith("derived:msme-classification@")
    assert any("percentages" in u for u in a.unverified_details)


def test_cgtmse_medium_not_met_and_unregistered_not_met():
    f = F(udyam_registered=True, msme_category="Medium", sector="manufacturing", collateral_free_requested=True)
    assert evaluate_scheme("cgtmse", f, NOW).status == "CRITERIA_NOT_MET"
    f = F(udyam_registered=False, msme_category="Micro", sector="manufacturing", collateral_free_requested=True)
    assert evaluate_scheme("cgtmse", f, NOW).status == "CRITERIA_NOT_MET"


def test_cgtmse_not_available_before_revamp_and_unknown_scheme():
    with pytest.raises(RuleError):
        evaluate_scheme("cgtmse", [], date(2023, 3, 31))
    with pytest.raises(RuleError):
        evaluate_scheme("nope", [], NOW)


def test_unclassified_enterprise_yields_no_category_fact():
    assert facts_from_classification(classify_msme(None, None, NOW)) == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
