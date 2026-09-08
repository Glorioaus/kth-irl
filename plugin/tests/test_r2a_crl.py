"""R2-A：CRL 13 条既有准则与 1–4 级离线累计行为。"""

from __future__ import annotations

import kth_hybrid.kernels as kernels
from kth_hybrid.catalog import build_catalog_from_wheel


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
