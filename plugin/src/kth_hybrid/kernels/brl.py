"""夜间BRL候选：批准D/2025 registry的离线受控求值。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


RULE_VERSION = "kth-hybrid.brl.night.v2"
RULE_REQUIREMENTS: dict[str, str] = {
    "BRL1-BM": "business_idea_or_model_stated",
    "BRL1-MO": "market_hypothesis_stated",
    "BRL1-CO": "competition_hypothesis_stated",
    "BRL1-SU": "sustainability_awareness_documented",
    "BRL2-BM": "structured_business_concept",
    "BRL2-MO": "initial_market_size_overview",
    "BRL2-CO": "competitors_or_alternatives_listed",
    "BRL2-SU": "sustainability_business_effect_understood",
    "BRL3-BM": "business_model_draft_described",
    "BRL3-MO": "target_markets_and_tam_sam_estimated",
    "BRL3-CO": "competitive_landscape_described",
    "BRL3-SU": "sustainability_outcomes_described",
    "BRL4-BM": "cost_revenue_pricing_viability_calculated",
    "BRL4-MO": "market_value_chain_geography_defined",
    "BRL4-CO": "proposed_competitive_position_documented",
    "BRL4-SU": "positive_negative_impact_assessed",
    "BRL5-BM": "pricing_and_wtp_validated_by_market",
    "BRL5-MO": "market_and_value_chain_updated_from_feedback",
    "BRL5-CO": "competitive_position_updated_from_feedback",
    "BRL5-SU": "sustainability_integration_considered",
    "BRL6-BM": "business_model_validated_realistic_scenario",
    "BRL6-MO": "target_market_and_geography_selected",
    "BRL6-CO": "differentiators_used_in_pitch",
    "BRL6-SU": "sustainability_metrics_proposed",
    "BRL7-BM": "commercial_sales_to_several_customers",
    "BRL7-MO": "market_and_sales_estimates_validated",
    "BRL7-CO": "differentiators_validated_and_communicated",
    "BRL7-SU": "sustainability_monitoring_defined",
    "BRL8-BM": "operating_metrics_show_viability",
    "BRL8-MO": "additional_markets_described",
    "BRL8-CO": "competitor_monitoring_implemented",
    "BRL8-SU": "sustainability_creates_business_value",
    "BRL9-BM": "operations_meet_profit_growth_scale_expectations",
    "BRL9-MO": "additional_markets_actively_pursued",
    "BRL9-CO": "monitoring_covers_future_markets",
    "BRL9-SU": "growth_and_sustainability_metric_balance",
}


def _assessment_unit(value: dict, scope: str) -> dict:
    required = {"scope_id", "subject_scope", "unit_kind", "unit_label"}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("评估单元结构不完整")
    if value["subject_scope"] != scope:
        raise ValueError("评估单元主体与维度scope不一致")
    if not all(isinstance(value[key], str) and value[key].strip()
               for key in required):
        raise ValueError("评估单元身份字段不能为空")
    return {key: value[key] for key in
            ("scope_id", "subject_scope", "unit_kind", "unit_label")}


def _valid_review(review: dict[str, Any], criterion: dict[str, Any],
                  scope_id: str) -> tuple[bool, str]:
    required = {"review_id", "criterion_id", "claim_id", "decision",
                "evidence_class", "findings", "scope_id", "reviewer",
                "review_basis", "support_scope"}
    if not isinstance(review, dict) or not required <= set(review):
        return False, "BRL复核结构不完整"
    if review["criterion_id"] != criterion["criterion_id"]:
        return False, "BRL复核不属于当前准则"
    if review["scope_id"] != scope_id:
        return False, "BRL复核评估单元不一致"
    if review["decision"] not in {"supports", "does_not_support"}:
        return False, "BRL复核decision非法"
    if review["evidence_class"] not in criterion["eligible_evidence_classes"]:
        return False, "证据类别不能支持当前BRL准则"
    if not isinstance(review["findings"], dict):
        return False, "BRL findings必须为对象"
    if not all(isinstance(review[key], str) and review[key].strip()
               for key in ("review_id", "claim_id", "evidence_class",
                           "reviewer", "review_basis", "support_scope")):
        return False, "BRL复核身份或范围为空"
    return True, "ok"


def _support_satisfies(criterion_id: str, review: dict[str, Any]) -> bool:
    findings = review["findings"]
    if findings.get(RULE_REQUIREMENTS[criterion_id]) is not True:
        return False
    criterion_level = int(criterion_id[3])
    transaction_classes = {
        "qualified_pricing_test", "qualified_preorder",
        "pilot_or_test_sale", "commercial_sale"}
    if criterion_id.endswith("-BM") and criterion_level >= 5 \
            and review["evidence_class"] in transaction_classes:
        allowed = {
            "incentive_compatible_price_test", "non_refundable_deposit",
            "paid_preorder", "delivered_sale", "repeat_sale"}
        if criterion_level >= 6:
            allowed = {"non_refundable_deposit", "paid_preorder",
                       "delivered_sale", "repeat_sale"}
        if criterion_level >= 7:
            allowed = {"delivered_sale", "repeat_sale"}
        if findings.get("transaction_commitment") not in allowed:
            return False
    if criterion_id == "BRL6-BM" \
            and review["evidence_class"] == "qualified_preorder":
        required = ("amount_qualified", "refundability_visible",
                    "intended_price_bridge", "fulfillment_status_visible",
                    "buyer_status_qualified", "denominator_visible")
        if not all(findings.get(key) is True for key in required) \
                or findings.get("refundable") is not False:
            return False
    if criterion_id == "BRL6-BM" \
            and review["evidence_class"] in {"pilot_or_test_sale", "commercial_sale"} \
            and findings.get("fulfilled") is not True:
        return False
    if criterion_id == "BRL7-BM":
        customers = findings.get("customer_ids")
        if findings.get("commercial_terms") is not True \
                or findings.get("delivered") is not True \
                or findings.get("fulfilled") is not True \
                or not isinstance(customers, list) \
                or len({item for item in customers
                        if isinstance(item, str) and item.strip()}) < 2:
            return False
    if criterion_id in {"BRL8-BM", "BRL9-BM"}:
        if not all(isinstance(findings.get(key), str)
                   and findings[key].strip()
                   for key in ("operating_period", "metric_denominator")):
            return False
        actual = findings.get("actual_metrics")
        target = findings.get("target_metrics")
        metrics = ("profit", "growth") if criterion_id == "BRL8-BM" \
            else ("profit", "growth", "scalability")
        if not isinstance(actual, dict) or not isinstance(target, dict) \
                or any(not isinstance(actual.get(key), (int, float))
                       or isinstance(actual.get(key), bool)
                       or not isinstance(target.get(key), (int, float))
                       or isinstance(target.get(key), bool)
                       or actual[key] < target[key] for key in metrics):
            return False
    return True


def _criterion_result(criterion: dict[str, Any], reviews: list[dict[str, Any]],
                      scope_id: str) -> dict[str, Any]:
    valid = []
    invalid = []
    for review in reviews:
        ok, reason = _valid_review(review, criterion, scope_id)
        if ok:
            valid.append(review)
        else:
            invalid.append(reason)
    supports = [row for row in valid if row["decision"] == "supports"
                and _support_satisfies(criterion["criterion_id"], row)]
    negatives = [row for row in valid if row["decision"] == "does_not_support"]
    if supports and negatives:
        native, product = "partial", "succeeded"
        rationale = "商业准则同时存在受控支持与反对复核，保留partial。"
    elif negatives:
        native, product = "not_met", "succeeded"
        rationale = "受控商业复核明确不支持该准则。"
    elif supports:
        native, product = "met", "succeeded"
        rationale = "准则专属商业finding与允许证据类别均成立。"
    else:
        native, product = "insufficient", "insufficient"
        rationale = "没有足够的商业准则证据；不从CRL等级、叙事或资料缺失推断BRL。"
    if invalid:
        rationale += " 无效复核未消费：" + "；".join(sorted(set(invalid)))
    return {**criterion,
            "requirements": [RULE_REQUIREMENTS[criterion["criterion_id"]]],
            "native_disposition": native, "product_status": product,
            "review_refs": [row["review_id"] for row in valid],
            "claim_refs": sorted({row["claim_id"] for row in valid}),
            "rationale": rationale, "rule_version": RULE_VERSION}


def evaluate_brl_dimension(criteria: list[dict[str, Any]],
                           reviews: list[dict[str, Any]], *, scope: str,
                           assessment_unit: dict) -> dict[str, Any]:
    expected = set(RULE_REQUIREMENTS)
    received = {row.get("criterion_id") for row in criteria
                if isinstance(row, dict)}
    if received != expected:
        raise ValueError("BRL准则集合不完整或被替换")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("BRL scope不能为空")
    unit = _assessment_unit(assessment_unit, scope)
    by_criterion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        if isinstance(review, dict) and review.get("criterion_id") in expected:
            by_criterion[review["criterion_id"]].append(review)
    rows = [_criterion_result(row, by_criterion[row["criterion_id"]],
                              unit["scope_id"])
            for row in sorted(criteria, key=lambda item: (item["level"],
                                                           item["aspect"]))]
    by_id = {row["criterion_id"]: row for row in rows}
    attained = 0
    first_unmet = None
    for level in range(1, 10):
        cumulative = [row for row in criteria if row["level"] <= level]
        if all(by_id[row["criterion_id"]]["native_disposition"] == "met"
               for row in cumulative):
            attained = level
        else:
            first_unmet = level
            break
    return {"dimension": "BRL", "scope": scope,
            "assessment_unit": unit, "criteria": rows,
            "attained_level": attained, "first_unmet_level": first_unmet,
            "product_status": ("succeeded" if all(
                row["product_status"] == "succeeded" for row in rows)
                else "insufficient"),
            "rule_version": RULE_VERSION,
            "method_boundary": "批准wheel D/2025内部shadow；不重判客户需求或生成投资决定。"}
