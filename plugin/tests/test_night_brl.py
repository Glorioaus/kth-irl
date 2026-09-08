"""夜间BRL候选：36条规则及商业证据边界。"""

from __future__ import annotations

import pytest

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.brl import RULE_REQUIREMENTS, evaluate_brl_dimension


SCOPE = "Company-A"
UNIT = {"scope_id": "UNIT-A", "subject_scope": SCOPE,
        "unit_kind": "material_business_unit", "unit_label": "Product-A"}
CRITERIA = build_catalog_from_wheel()["dimensions"]["BRL"]["registry"]["criteria"]


def _review(criterion, *, decision="supports", evidence_class=None,
            findings=None, suffix="1"):
    criterion_id = criterion["criterion_id"]
    default = {RULE_REQUIREMENTS[criterion_id]: True}
    if criterion_id == "BRL6-BM" and (evidence_class or criterion["eligible_evidence_classes"][0]) == "qualified_preorder":
        default.update({"amount_qualified": True, "refundability_visible": True,
                        "intended_price_bridge": True, "fulfillment_status_visible": True,
                        "buyer_status_qualified": True, "denominator_visible": True})
    if criterion_id == "BRL7-BM":
        default.update({"commercial_terms": True, "delivered": True,
                        "customer_ids": ["CUSTOMER-1", "CUSTOMER-2"]})
    if criterion_id in {"BRL8-BM", "BRL9-BM"}:
        default.update({"operating_period": "2026-Q2",
                        "metric_denominator": "all commercial orders"})
    return {
        "review_id": f"BRL-REV-{criterion_id}-{suffix}",
        "criterion_id": criterion_id,
        "claim_id": f"BRL-CLAIM-{criterion_id}-{suffix}",
        "decision": decision,
        "evidence_class": evidence_class or criterion["eligible_evidence_classes"][0],
        "findings": default if findings is None else findings,
        "scope_id": UNIT["scope_id"],
        "reviewer": "night-brl-test",
        "review_basis": "合成离线BRL复核",
        "support_scope": f"仅支持{criterion_id}商业推理",
    }


def _evaluate(reviews, unit=UNIT):
    return evaluate_brl_dimension(CRITERIA, reviews, scope=SCOPE,
                                  assessment_unit=unit)


def _row(result, criterion_id):
    return next(row for row in result["criteria"]
                if row["criterion_id"] == criterion_id)


@pytest.mark.parametrize("criterion", CRITERIA, ids=lambda row: row["criterion_id"])
def test_each_brl_rule_has_positive_and_missing_behavior(criterion):
    positive = _row(_evaluate([_review(criterion)]), criterion["criterion_id"])
    missing = _row(_evaluate([]), criterion["criterion_id"])
    assert positive["native_disposition"] == "met"
    assert missing["native_disposition"] == "insufficient"
    assert positive["requirements"] == [RULE_REQUIREMENTS[criterion["criterion_id"]]]


def test_brl_has_36_unique_criterion_requirements():
    assert len(CRITERIA) == len(RULE_REQUIREMENTS) == 36
    assert len(set(RULE_REQUIREMENTS.values())) == 36


def test_crl_level_or_wrong_evidence_class_is_not_brl_business_evidence():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "BRL1-BM")
    review = _review(criterion, evidence_class="crl_level")
    assert _row(_evaluate([review]), "BRL1-BM")["native_disposition"] \
        == "insufficient"


def test_refundable_or_unqualified_preorder_does_not_validate_business_model():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "BRL6-BM")
    incomplete = _review(criterion, evidence_class="qualified_preorder", findings={
        RULE_REQUIREMENTS["BRL6-BM"]: True,
        "amount_qualified": True,
        "refundability_visible": False,
        "intended_price_bridge": False,
        "fulfillment_status_visible": False,
        "buyer_status_qualified": False,
        "denominator_visible": False,
    })
    assert _row(_evaluate([incomplete]), "BRL6-BM")["native_disposition"] \
        == "insufficient"
    assert _row(_evaluate([_review(criterion, evidence_class="qualified_preorder",
                                  suffix="2")]), "BRL6-BM")["native_disposition"] == "met"


def test_level7_requires_delivered_commercial_sales_to_several_customers():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "BRL7-BM")
    one = _review(criterion, findings={
        RULE_REQUIREMENTS["BRL7-BM"]: True, "commercial_terms": True,
        "delivered": True, "customer_ids": ["CUSTOMER-1"],
    })
    assert _row(_evaluate([one]), "BRL7-BM")["native_disposition"] \
        == "insufficient"
    assert _row(_evaluate([_review(criterion, suffix="2")]), "BRL7-BM")[
        "native_disposition"] == "met"


def test_operating_metrics_require_period_and_denominator():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "BRL8-BM")
    vague = _review(criterion, findings={RULE_REQUIREMENTS["BRL8-BM"]: True})
    assert _row(_evaluate([vague]), "BRL8-BM")["native_disposition"] \
        == "insufficient"


def test_negative_conflict_and_cumulative_level_are_distinct():
    criterion = CRITERIA[0]
    negative = _review(criterion, decision="does_not_support")
    assert _row(_evaluate([negative]), criterion["criterion_id"])[
        "native_disposition"] == "not_met"
    assert _row(_evaluate([negative, _review(criterion, suffix="2")]),
                criterion["criterion_id"])["native_disposition"] == "partial"
    level1 = [_review(row) for row in CRITERIA if row["level"] == 1]
    result = _evaluate(level1)
    assert result["attained_level"] == 1
    assert result["first_unmet_level"] == 2


def test_brl_assessment_unit_is_exact_and_not_company_wide_merge():
    result = _evaluate([])
    assert result["assessment_unit"] == UNIT
    with pytest.raises(ValueError, match="评估单元"):
        _evaluate([], {**UNIT, "subject_scope": "Other-Company"})
