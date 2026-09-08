"""R2-A：CRL 13 条既有准则与 1–4 级离线累计行为。"""

from __future__ import annotations

import kth_hybrid.kernels as kernels
from kth_hybrid.catalog import build_catalog_from_wheel
import pytest


def test_r2a_exposes_a_dedicated_crl_dimension_evaluator():
    assert callable(getattr(kernels, "evaluate_crl_dimension", None)), \
        "R2-A 必须提供13条CRL规则的专用维度求值入口"


def _criteria():
    return build_catalog_from_wheel()["dimensions"]["CRL"]["registry"]["criteria"]


def _review(criterion_id: str, findings: dict, *, decision="supports") -> dict:
    return {
        "review_id": f"REV::{criterion_id}",
        "criterion_id": criterion_id,
        "claim_id": f"CLM::{criterion_id}",
        "decision": decision,
        "findings": findings,
        "reviewer": "r2a-synthetic-offline-review",
        "review_basis": "可追溯合成离线复核",
        "support_scope": "仅支持对应CRL准则",
    }


def _all_findings() -> dict:
    return {
        "market_need_hypothesis": True,
        "customer_problem_hypotheses": True,
        "initial_market_customer_knowledge": True,
        "secondary_market_research": True,
        "market_customer_problem_alternative_familiarity": True,
        "clear_problem_need_description": True,
        "primary_feedback_contacts": ["customer-1", "customer-2"],
        "customer_segments": True,
        "hypothesis_updated_after_feedback": True,
        "importance_confirmed": True,
        "customer_profiles": True,
        "user_payer_decider_roles": True,
        "positioning_against_alternatives": True,
    }


@pytest.mark.parametrize("criterion_id", sorted([
    "CRL1-C1", "CRL1-C2", "CRL1-C3", "CRL2-C1", "CRL2-C2", "CRL2-C3",
    "CRL3-C1", "CRL3-C2", "CRL3-C3", "CRL4-C1", "CRL4-C2", "CRL4-C3",
    "CRL4-C4",
]))
def test_each_rule_has_a_minimal_positive_and_missing_finding_negative(criterion_id):
    evaluator = kernels.evaluate_crl_dimension
    criteria = _criteria()
    requirements = kernels.RULE_REQUIREMENTS[criterion_id]
    positive = {}
    for requirement in requirements:
        positive[requirement] = (
            ["customer-1", "customer-2"] if requirement == "primary_feedback_contacts"
            and criterion_id == "CRL4-C1"
            else ["customer-1"] if requirement == "primary_feedback_contacts" else True)
    met = evaluator(criteria, [_review(criterion_id, positive)], scope="合成产品单元")
    row = next(item for item in met["criteria"] if item["criterion_id"] == criterion_id)
    assert row["native_disposition"] == "met"
    missing = dict(positive)
    missing.pop(requirements[0])
    insufficient = evaluator(criteria, [_review(criterion_id, missing)], scope="合成产品单元")
    row = next(item for item in insufficient["criteria"] if item["criterion_id"] == criterion_id)
    assert row["native_disposition"] is None
    assert row["product_status"] == "insufficient"


def test_all_13_rules_can_be_individually_met_with_criterion_specific_reviews():
    evaluator = getattr(kernels, "evaluate_crl_dimension", None)
    assert callable(evaluator)
    criteria = _criteria()
    reviews = [_review(row["criterion_id"], _all_findings()) for row in criteria]

    result = evaluator(criteria, reviews, scope="合成产品单元")

    assert {row["criterion_id"] for row in result["criteria"]} == {
        row["criterion_id"] for row in criteria
    }
    assert all(row["native_disposition"] == "met" for row in result["criteria"])
    assert result["attained_level"] == 4
    assert result["first_unmet_level"] is None


def test_missing_low_level_evidence_blocks_cumulative_level_even_with_later_reviews():
    evaluator = getattr(kernels, "evaluate_crl_dimension", None)
    assert callable(evaluator)
    criteria = _criteria()
    reviews = [
        _review(row["criterion_id"], _all_findings())
        for row in criteria if row["criterion_id"] != "CRL1-C2"
    ]

    result = evaluator(criteria, reviews, scope="合成产品单元")

    c2 = next(row for row in result["criteria"] if row["criterion_id"] == "CRL1-C2")
    assert c2["product_status"] == "insufficient"
    assert c2["native_disposition"] is None
    assert result["attained_level"] is None
    assert result["first_unmet_level"] == 1


def test_reviewed_negative_is_native_not_met_not_product_insufficient():
    evaluator = getattr(kernels, "evaluate_crl_dimension", None)
    assert callable(evaluator)
    criteria = _criteria()
    reviews = [_review("CRL1-C1", _all_findings(), decision="does_not_support")]

    result = evaluator(criteria, reviews, scope="合成产品单元")
    row = next(item for item in result["criteria"] if item["criterion_id"] == "CRL1-C1")

    assert row["native_disposition"] == "not_met"
    assert row["product_status"] == "succeeded"


def test_conflicting_controlled_reviews_are_partial():
    result = kernels.evaluate_crl_dimension(
        _criteria(), [_review("CRL1-C1", {"market_need_hypothesis": True}),
                      _review("CRL1-C1", {}, decision="does_not_support")],
        scope="合成产品单元")
    row = next(item for item in result["criteria"] if item["criterion_id"] == "CRL1-C1")
    assert row["native_disposition"] == "partial"


def test_multiple_reviews_merge_distinct_contacts_without_double_counting():
    reviews = [
        _review("CRL4-C1", {"primary_feedback_contacts": ["c1"],
                            "importance_confirmed": True}),
        {**_review("CRL4-C1", {"primary_feedback_contacts": ["c2"]}),
         "review_id": "REV::CRL4-C1::2", "claim_id": "CLM::CRL4-C1::2"},
    ]
    result = kernels.evaluate_crl_dimension(_criteria(), reviews, scope="合成产品单元")
    row = next(item for item in result["criteria"] if item["criterion_id"] == "CRL4-C1")
    assert row["native_disposition"] == "met"


def test_crl4_multiple_contacts_are_deduplicated_before_becoming_met():
    evaluator = getattr(kernels, "evaluate_crl_dimension", None)
    assert callable(evaluator)
    criteria = _criteria()
    findings = _all_findings()
    findings["primary_feedback_contacts"] = ["customer-1", "customer-1"]
    reviews = [_review("CRL4-C1", findings)]

    result = evaluator(criteria, reviews, scope="合成产品单元")
    row = next(item for item in result["criteria"] if item["criterion_id"] == "CRL4-C1")

    assert row["product_status"] == "insufficient"
    assert row["native_disposition"] is None
