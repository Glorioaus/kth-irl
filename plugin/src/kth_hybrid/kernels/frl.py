"""R2-B候选：批准FRL F/2025 registry的离线受控求值。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


RULE_VERSION = "kth-hybrid.frl.r2b.v1"

# 每条字段对应批准registry的一条详细状态描述。低级状态也是需要明确复核的
# 发展状态，不因资料缺失自动成立。
RULE_REQUIREMENTS: dict[str, str] = {
    "FRL1-NEED": "limited_validation_cost_insight",
    "FRL1-OPTIONS": "limited_funding_option_insight",
    "FRL1-STATUS": "no_funding_obtained",
    "FRL1-PITCH": "funding_potential_not_expressed",
    "FRL2-NEED": "initial_validation_costs_understood",
    "FRL2-OPTIONS": "initial_funding_sources_identified",
    "FRL2-STATUS": "fundraising_efforts_started",
    "FRL2-PITCH": "funding_description_exists",
    "FRL3-NEED": "six_to_twelve_month_budget_draft",
    "FRL3-OPTIONS": "instrument_requirements_understood",
    "FRL3-STATUS": "usable_validation_funding_available",
    "FRL3-PITCH": "first_funding_presentation_exists",
    "FRL4-NEED": "milestone_cost_risk_plan_defined",
    "FRL4-OPTIONS": "next_stage_funding_sources_identified",
    "FRL4-STATUS": "first_project_steps_funded",
    "FRL4-PITCH": "funding_pitch_or_application_exists",
    "FRL5-NEED": "time_phased_need_with_forecast",
    "FRL5-OPTIONS": "roadmap_and_cap_table_effects_considered",
    "FRL5-STATUS": "next_step_funding_with_following_gap",
    "FRL5-PITCH": "pitch_tested_with_relevant_audience",
    "FRL6-NEED": "cash_flow_projection_exists",
    "FRL6-OPTIONS": "later_stage_funding_roadmap_updated",
    "FRL6-STATUS": "funding_terms_discussed",
    "FRL6-PITCH": "pitch_revised_from_feedback",
    "FRL7-NEED": "accounting_and_scenarios_exist",
    "FRL7-OPTIONS": "target_and_backup_financing_identified",
    "FRL7-STATUS": "near_term_sheet_discussions_exist",
    "FRL7-PITCH": "diligence_material_complete",
    "FRL8-NEED": "financial_monitoring_and_controls_operate",
    "FRL8-OPTIONS": "roadmap_continuously_updated",
    "FRL8-STATUS": "twelve_month_runway_available",
    "FRL8-PITCH": "complete_business_plan_in_materials",
    "FRL9-NEED": "scaleup_need_reflects_plan_and_forecast",
    "FRL9-OPTIONS": "long_term_scaleup_financing_strategy",
    "FRL9-STATUS": "next_stage_sources_show_concrete_interest",
    "FRL9-PITCH": "funding_materials_continuously_updated",
}

_STATUS_BUCKETS: dict[str, set[str]] = {
    "FRL3-STATUS": {"cash_now", "closed_or_drawable"},
    "FRL4-STATUS": {"cash_now", "closed_or_drawable"},
    "FRL5-STATUS": {"binding_conditional", "closed_or_drawable"},
    "FRL6-STATUS": {"soft_or_nonbinding", "binding_conditional"},
    "FRL7-STATUS": {"soft_or_nonbinding"},
    "FRL8-STATUS": {"cash_now", "closed_or_drawable"},
    "FRL9-STATUS": {"soft_or_nonbinding", "binding_conditional"},
}


def _financing_entity(value: dict, scope: str) -> dict:
    required = {"financing_entity_id", "subject_scope", "assessment_unit_refs"}
    if not isinstance(value, dict) or not required <= set(value):
        raise ValueError("融资主体结构不完整")
    if value["subject_scope"] != scope:
        raise ValueError("融资主体subject_scope与维度scope不一致")
    entity_id = value["financing_entity_id"]
    units = value["assessment_unit_refs"]
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("融资主体身份不能为空")
    if not isinstance(units, list) or not units \
            or any(not isinstance(item, str) or not item.strip() for item in units) \
            or len(units) != len(set(units)):
        raise ValueError("评估单元引用必须为非空、去重的字符串列表")
    return {
        "financing_entity_id": entity_id,
        "subject_scope": scope,
        "assessment_unit_refs": sorted(units),
    }


def _legal_no_external_policy(applicability: dict | None, entity: dict,
                              scope: str) -> bool:
    if not isinstance(applicability, dict):
        return False
    binding = applicability.get("policy_binding")
    return (
        applicability.get("external_financing_planned") is False
        and applicability.get("financing_entity_id")
        == entity["financing_entity_id"]
        and applicability.get("subject_scope") == scope
        and isinstance(binding, dict)
        and binding.get("value") is False
        and isinstance(binding.get("origin_path"), str)
        and bool(binding["origin_path"].strip())
        and isinstance(binding.get("origin_sha256"), str)
        and len(binding["origin_sha256"]) == 64
        and isinstance(binding.get("field_path"), str)
        and bool(binding["field_path"].strip())
    )


def _review_valid(review: dict[str, Any], criterion: dict[str, Any],
                  entity_id: str) -> tuple[bool, str]:
    required = {
        "review_id", "criterion_id", "claim_id", "decision", "evidence_class",
        "findings", "financing_entity_id", "reviewer", "review_basis",
        "support_scope",
    }
    if not isinstance(review, dict) or not required <= set(review):
        return False, "FRL复核记录结构不完整"
    if review["criterion_id"] != criterion["criterion_id"]:
        return False, "FRL复核记录不属于当前准则"
    if review["financing_entity_id"] != entity_id:
        return False, "FRL复核融资主体不一致"
    if review["decision"] not in {"supports", "does_not_support"}:
        return False, "FRL复核decision非法"
    if not isinstance(review["findings"], dict):
        return False, "FRL复核findings必须为对象"
    if not all(isinstance(review[field], str) and review[field].strip()
               for field in ("review_id", "claim_id", "evidence_class",
                             "reviewer", "review_basis", "support_scope")):
        return False, "FRL复核缺少绑定身份、证据类别或范围"
    if review["evidence_class"] not in criterion["eligible_evidence_classes"]:
        return False, "FRL复核证据类别不适用于当前准则"
    return True, "ok"


def _support_satisfies(criterion_id: str, review: dict[str, Any]) -> bool:
    findings = review["findings"]
    if findings.get(RULE_REQUIREMENTS[criterion_id]) is not True:
        return False
    allowed_buckets = _STATUS_BUCKETS.get(criterion_id)
    if allowed_buckets is not None \
            and findings.get("funding_bucket") not in allowed_buckets:
        return False
    if findings.get("preorder_cash") is True \
            and findings.get("delivery_refund_obligations_visible") is not True:
        return False
    return True


def _criterion_result(criterion: dict[str, Any], reviews: list[dict[str, Any]],
                      *, entity_id: str, legal_na: bool) -> dict[str, Any]:
    criterion_id = criterion["criterion_id"]
    valid: list[dict[str, Any]] = []
    invalid: list[str] = []
    for review in reviews:
        ok, reason = _review_valid(review, criterion, entity_id)
        if ok:
            valid.append(review)
        else:
            invalid.append(reason)
    supports = [row for row in valid
                if row["decision"] == "supports" and _support_satisfies(
                    criterion_id, row)]
    negatives = [row for row in valid if row["decision"] == "does_not_support"]
    if legal_na and criterion["na_policy"] == "explicit_no_external_financing_only":
        native = "not_applicable"
        product = "succeeded"
        rationale = "当前融资主体有源政策明确不计划外部融资；按registry受限N/A处理。"
    elif supports and negatives:
        native = "partial"
        product = "succeeded"
        rationale = "存在合格支持与反对复核，保留原生partial。"
    elif negatives:
        native = "not_met"
        product = "succeeded"
        rationale = "受控复核明确不支持该FRL准则。"
    elif supports:
        native = "met"
        product = "succeeded"
        rationale = "准则专属finding与registry允许证据类别均通过受控复核。"
    else:
        native = "insufficient"
        product = "insufficient"
        rationale = "没有足够的准则绑定合格证据；不以资料缺失证明低级状态或资金事实。"
    if invalid:
        rationale += " 无效复核未消费：" + "；".join(sorted(set(invalid)))
    return {
        **criterion,
        "requirements": [RULE_REQUIREMENTS[criterion_id]],
        "native_disposition": native,
        "product_status": product,
        "review_refs": [row["review_id"] for row in valid],
        "claim_refs": sorted({row["claim_id"] for row in valid}),
        "rationale": rationale,
        "rule_version": RULE_VERSION,
    }


def evaluate_frl_dimension(criteria: list[dict[str, Any]],
                           reviews: list[dict[str, Any]], *, scope: str,
                           financing_entity: dict,
                           applicability: dict | None = None) -> dict[str, Any]:
    """对一个融资主体求值FRL 36条，并按原版累计门导出0至9级。"""
    expected = set(RULE_REQUIREMENTS)
    received = {row.get("criterion_id") for row in criteria
                if isinstance(row, dict)}
    if received != expected:
        raise ValueError(
            f"FRL准则集合不完整/不一致：缺少={sorted(expected - received)}，"
            f"额外={sorted(item for item in received - expected if item)}")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("FRL维度scope不能为空")
    entity = _financing_entity(financing_entity, scope)
    legal_na = _legal_no_external_policy(applicability, entity, scope)
    by_criterion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        if isinstance(review, dict) and review.get("criterion_id") in expected:
            by_criterion[review["criterion_id"]].append(review)
    rows = [_criterion_result(
        criterion, by_criterion[criterion["criterion_id"]],
        entity_id=entity["financing_entity_id"], legal_na=legal_na)
        for criterion in sorted(
            criteria, key=lambda row: (row["level"], row["aspect_id"]))]
    by_id = {row["criterion_id"]: row for row in rows}
    attained = 0
    first_unmet: int | None = None
    for level in range(1, 10):
        cumulative = [criterion for criterion in criteria
                      if criterion["level"] <= level]
        if all(by_id[criterion["criterion_id"]]["native_disposition"]
               in {"met", "not_applicable"} for criterion in cumulative):
            attained = level
        else:
            first_unmet = level
            break
    return {
        "dimension": "FRL",
        "scope": scope,
        "financing_entity": entity,
        "criteria": rows,
        "attained_level": attained,
        "first_unmet_level": first_unmet,
        "product_status": (
            "succeeded" if all(row["product_status"] == "succeeded" for row in rows)
            else "insufficient"
        ),
        "applicability": applicability,
        "rule_version": RULE_VERSION,
        "method_boundary": (
            "批准wheel F/2025内部shadow；非官方KTH评估、估值或投资决定。"
        ),
    }
