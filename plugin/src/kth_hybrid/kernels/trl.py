"""夜间TRL候选：批准G/2022 registry的项目特定技术求值。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


RULE_VERSION = "kth-hybrid.trl.night.v3"
RULE_REQUIREMENTS = {
    "TRL1-C1": "research_application_identified", "TRL1-C2": "initial_technology_idea",
    "TRL2-C1": "technology_concept_defined", "TRL2-C2": "applications_speculative",
    "TRL3-C1": "lab_parameters_show_feasibility", "TRL3-C2": "active_research_started",
    "TRL3-C3": "initial_user_requirements", "TRL4-C1": "components_integrated_in_lab",
    "TRL4-C2": "integrated_test_evidence", "TRL5-C1": "integrated_relevant_environment_test",
    "TRL5-C2": "relevant_environment_evidence", "TRL5-C3": "stressing_environment_simulated",
    "TRL5-C4": "requirements_from_user_feedback", "TRL6-C1": "representative_prototype_works",
    "TRL6-C2": "prototype_near_operational_spec", "TRL6-C3": "important_requirements_met",
    "TRL7-C1": "complete_prototype_operational", "TRL7-C2": "all_operational_requirements_addressed",
    "TRL7-C3": "complete_user_requirements", "TRL8-C1": "actual_operation_by_first_users",
    "TRL8-C2": "complete_functional_compatible_producible", "TRL8-C3": "all_performance_requirements_met",
    "TRL8-C4": "day_to_day_independent_use", "TRL9-C1": "scaled_operation_by_several_users",
    "TRL9-C2": "continuous_technology_improvement",
}


def _unit(value: dict, scope: str) -> dict:
    required = {"scope_id", "subject_scope", "unit_kind", "unit_label"}
    if not isinstance(value, dict) or not required <= set(value) \
            or value.get("subject_scope") != scope:
        raise ValueError("评估单元主体或结构不一致")
    if not all(isinstance(value[key], str) and value[key].strip()
               for key in required):
        raise ValueError("评估单元字段不能为空")
    return {key: value[key] for key in
            ("scope_id", "subject_scope", "unit_kind", "unit_label")}


def _valid(review: dict[str, Any], criterion: dict[str, Any],
           scope_id: str) -> bool:
    required = {"review_id", "criterion_id", "claim_id", "decision",
                "evidence_class", "findings", "scope_id", "reviewer",
                "review_basis", "support_scope"}
    return (isinstance(review, dict) and required <= set(review)
            and review.get("criterion_id") == criterion["criterion_id"]
            and review.get("scope_id") == scope_id
            and review.get("decision") in {"supports", "does_not_support"}
            and review.get("evidence_class") in criterion["eligible_evidence_classes"]
            and isinstance(review.get("findings"), dict)
            and all(isinstance(review.get(key), str) and review[key].strip()
                    for key in ("review_id", "claim_id", "evidence_class",
                                "reviewer", "review_basis", "support_scope")))


def _nonempty(findings: dict, *keys: str) -> bool:
    return all(isinstance(findings.get(key), str) and findings[key].strip()
               for key in keys)


def _support(criterion: dict[str, Any], review: dict[str, Any]) -> bool:
    findings = review["findings"]
    criterion_id = criterion["criterion_id"]
    if findings.get(RULE_REQUIREMENTS[criterion_id]) is not True \
            or findings.get("project_specific") is not True \
            or not _nonempty(findings, "configuration_id", "system_boundary"):
        return False
    evidence_class = review["evidence_class"]
    if evidence_class == "r_and_d_record":
        return findings.get("activity_status") == "active"
    if evidence_class == "requirements_record":
        if findings.get("requirements_status") not in {"defined", "documented"}:
            return False
        if criterion_id == "TRL5-C4" and findings.get("user_feedback_bound") is not True:
            return False
        if criterion_id == "TRL7-C3" and findings.get("complete_requirements") is not True:
            return False
        return True
    if evidence_class == "manufacturing_record":
        return _nonempty(findings, "manufacturing_scope", "manufacturing_results")
    if evidence_class == "continuous_improvement_record":
        actions = findings.get("improvement_actions")
        return (_nonempty(findings, "improvement_period")
                and isinstance(actions, list) and bool(actions))
    test_classes = {"lab_test", "test_record", "independent_test",
                    "relevant_environment_test", "operational_demonstration",
                    "actual_operation", "independent_operation_record",
                    "longitudinal_operation"}
    if evidence_class in test_classes and criterion["level"] >= 3:
        if not _nonempty(findings, "test_environment", "test_method",
                         "measured_results", "requirements_thresholds"):
            return False
        expected_environment = "laboratory" if criterion["level"] <= 4 else (
            "relevant" if criterion["level"] <= 6 else (
                "operational" if criterion["level"] == 7 else "actual_operation"))
        if findings.get("environment_kind") != expected_environment:
            return False
    if criterion["level"] >= 4 and findings.get("component_only") is True \
            and findings.get("complete_system_boundary") is not True:
        return False
    if evidence_class in {"actual_operation", "independent_operation_record",
                           "longitudinal_operation"}:
        users = findings.get("independent_user_ids")
        if not isinstance(users, list) or not any(
                isinstance(item, str) and item.strip() for item in users):
            return False
    if criterion_id == "TRL8-C4" \
            and findings.get("day_to_day_operation") is not True:
        return False
    if criterion_id == "TRL9-C1":
        users = {item for item in findings.get("independent_user_ids", [])
                 if isinstance(item, str) and item.strip()}
        if len(users) < 2 or not _nonempty(findings, "longitudinal_period"):
            return False
    return True


def _row(criterion: dict[str, Any], reviews: list[dict[str, Any]],
         scope_id: str) -> dict[str, Any]:
    valid = [review for review in reviews if _valid(review, criterion, scope_id)]
    supports = [review for review in valid
                if review["decision"] == "supports" and _support(criterion, review)]
    negatives = [review for review in valid
                 if review["decision"] == "does_not_support"]
    if supports and negatives:
        native, product, rationale = "partial", "succeeded", "技术证据存在受控冲突。"
    elif negatives:
        native, product, rationale = "not_met", "succeeded", "受控复核明确不支持该技术准则。"
    elif supports:
        native, product, rationale = "met", "succeeded", "项目配置、系统边界与准则技术证据均已绑定。"
    else:
        native, product, rationale = "insufficient", "insufficient", "没有项目特定技术证据；行业或竞品能力不能提升本项目TRL。"
    return {**criterion,
            "requirements": [RULE_REQUIREMENTS[criterion["criterion_id"]]],
            "native_disposition": native, "product_status": product,
            "review_refs": [review["review_id"] for review in valid],
            "claim_refs": sorted({review["claim_id"] for review in valid}),
            "rationale": rationale, "rule_version": RULE_VERSION}


def evaluate_trl_dimension(criteria: list[dict[str, Any]],
                           reviews: list[dict[str, Any]], *, scope: str,
                           assessment_unit: dict) -> dict[str, Any]:
    if {row.get("criterion_id") for row in criteria} != set(RULE_REQUIREMENTS):
        raise ValueError("TRL准则集合不完整或被替换")
    unit = _unit(assessment_unit, scope)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        if isinstance(review, dict) and review.get("criterion_id") in RULE_REQUIREMENTS:
            grouped[review["criterion_id"]].append(review)
    rows = [_row(item, grouped[item["criterion_id"]], unit["scope_id"])
            for item in sorted(criteria, key=lambda value: (value["level"],
                                                             value["criterion_id"]))]
    by_id = {row["criterion_id"]: row for row in rows}
    attained, first_unmet = 0, None
    for level in range(1, 10):
        if all(by_id[item["criterion_id"]]["native_disposition"] == "met"
               for item in criteria if item["level"] <= level):
            attained = level
        else:
            first_unmet = level
            break
    return {"dimension": "TRL", "scope": scope, "assessment_unit": unit,
            "criteria": rows, "attained_level": attained,
            "first_unmet_level": first_unmet,
            "product_status": ("succeeded" if all(
                row["product_status"] == "succeeded" for row in rows)
                else "insufficient"), "rule_version": RULE_VERSION,
            "method_boundary": "批准wheel G/2022内部shadow；类别级可行性不等于本项目技术验证。"}
