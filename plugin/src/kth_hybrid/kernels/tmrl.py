"""TMRL限定整改v2：逐条语义要求与证据类record-strength。"""
from __future__ import annotations

from collections import defaultdict

RULE_VERSION = "kth-hybrid.tmrl.night.v2"
RULE_REQUIREMENTS = {
    "TMRL1-C1": "key_competency_gap_state_recorded",
    "TMRL1-C2": "competency_resource_uncertainty_recorded",
    "TMRL2-C1": "limited_current_capability_snapshot",
    "TMRL2-C2": "initial_competency_need_identified",
    "TMRL2-C3": "initial_project_goal_defined",
    "TMRL3-C1": "partial_capability_to_begin",
    "TMRL3-C2": "competency_capacity_diversity_gaps_identified",
    "TMRL3-C3": "near_term_competency_access_plan",
    "TMRL4-C1": "execution_route_understood",
    "TMRL4-C2": "committed_champion_present",
    "TMRL4-C3": "several_competencies_present",
    "TMRL4-C4": "competency_plan_initiated",
    "TMRL4-C5": "role_commitment_ownership_discussions_started",
    "TMRL5-C1": "founder_team_operating_together",
    "TMRL5-C2": "team_alignment_and_role_commitment",
    "TMRL5-C3": "executed_ownership_alignment_agreement",
    "TMRL5-C4": "recruitment_execution_in_progress",
    "TMRL5-C5": "knowledge_sharing_system_started",
    "TMRL6-C1": "complementary_diverse_founding_team",
    "TMRL6-C2": "key_competencies_and_ceo_present",
    "TMRL6-C3": "team_accountability_demonstrated",
    "TMRL6-C4": "board_advisor_recruitment_started",
    "TMRL6-C5": "team_performance_risks_managed",
    "TMRL7-C1": "well_functioning_team_clear_roles",
    "TMRL7-C2": "organization_charter_documented",
    "TMRL7-C3": "two_year_organization_growth_plan",
    "TMRL7-C4": "learning_development_process_implemented",
    "TMRL7-C5": "board_advisors_operating",
    "TMRL8-C1": "professional_management_team_present",
    "TMRL8-C2": "competent_diverse_board_operating",
    "TMRL8-C3": "hr_policy_process_operating",
    "TMRL8-C4": "long_term_recruitment_ongoing",
    "TMRL8-C5": "organization_trained_and_motivated",
    "TMRL9-C1": "organization_high_performance",
    "TMRL9-C2": "organization_continuous_learning",
    "TMRL9-C3": "continuous_organization_improvement",
    "TMRL9-C4": "incentive_alignment_operating",
    "TMRL9-C5": "management_continuity_over_time",
}
_AGREEMENT_CLASSES = {"executed_ownership_agreement",
                      "executed_role_commitment_record"}
_OPERATING_CLASSES = {"accountability_operating_record",
                      "board_advisor_operating_record",
                      "founder_team_operating_record",
                      "learning_performance_record",
                      "management_continuity_record",
                      "organization_performance_record", "team_operating_record"}
_OPERATING_STATUSES = {"measured_operating_record", "operational_record",
                       "verified_operating_record"}


def _unit(value, scope):
    keys = ("scope_id", "subject_scope", "unit_kind", "unit_label")
    if not isinstance(value, dict) or value.get("subject_scope") != scope \
            or not all(isinstance(value.get(key), str) and value[key].strip()
                       for key in keys):
        raise ValueError("评估单元主体或结构不一致")
    return {key: value[key] for key in keys}


def _valid(review, criterion, scope_id):
    required = {"review_id", "criterion_id", "claim_id", "decision",
                "evidence_class", "findings", "scope_id", "reviewer",
                "review_basis", "support_scope"}
    return (isinstance(review, dict) and required <= set(review)
            and review["criterion_id"] == criterion["criterion_id"]
            and review["scope_id"] == scope_id
            and review["decision"] in {"supports", "does_not_support"}
            and review["evidence_class"] in criterion["eligible_evidence_classes"]
            and isinstance(review["findings"], dict))


def _texts(findings, key):
    values = findings.get(key)
    return isinstance(values, list) and any(
        isinstance(value, str) and value.strip() for value in values)


def _support(criterion, review):
    findings = review["findings"]
    criterion_id = criterion["criterion_id"]
    evidence_class = review["evidence_class"]
    subjects = findings.get("subject_ids")
    if findings.get(RULE_REQUIREMENTS[criterion_id]) is not True \
            or findings.get("team_specific") is not True \
            or not isinstance(subjects, list) or not subjects \
            or not isinstance(findings.get("current_period"), str) \
            or not findings["current_period"].strip():
        return False
    if findings.get("biography_only") is True \
            or findings.get("public_claim_only") is True \
            or findings.get("identity_only") is True:
        return False
    if findings.get("relationship_only") is True \
            and findings.get("causal_execution_effect") is not True:
        return False
    if evidence_class in _AGREEMENT_CLASSES:
        return findings.get("agreement_status") == "executed_agreement"
    if evidence_class in _OPERATING_CLASSES:
        return (findings.get("record_status") in _OPERATING_STATUSES
                and isinstance(findings.get("operating_period"), str)
                and bool(findings["operating_period"].strip())
                and _texts(findings, "actual_behaviors"))
    if evidence_class == "team_needs_hypothesis":
        return findings.get("hypothesis_status") == "documented"
    if evidence_class in {"team_capability_snapshot", "team_capability_matrix"}:
        valid = (findings.get("capability_status") == "verified_current"
                 and _texts(findings, "work_evidence_refs"))
        if criterion_id == "TMRL6-C2":
            valid = valid and isinstance(findings.get("ceo_subject_id"), str) \
                and bool(findings["ceo_subject_id"].strip())
        return valid
    if evidence_class == "competency_gap_record":
        return findings.get("gap_status") == "verified_current"
    if evidence_class == "project_goal_record":
        return findings.get("goal_status") == "documented"
    if evidence_class == "hiring_or_access_plan":
        accepted = {"documented", "initiated"}
        if criterion_id == "TMRL4-C4":
            accepted = {"initiated"}
        return findings.get("plan_status") in accepted
    if evidence_class == "execution_route_record":
        return findings.get("route_status") == "documented"
    if evidence_class == "champion_commitment_record":
        return (findings.get("commitment_status") == "committed"
                and findings.get("capacity_status") == "verified_current")
    if evidence_class == "team_alignment_record":
        return (findings.get("alignment_status") == "documented_current"
                and findings.get("commitment_status") == "committed")
    if evidence_class == "recruitment_execution_record":
        return findings.get("execution_status") == "active" \
            and _texts(findings, "actual_actions")
    if evidence_class == "knowledge_system_record":
        return findings.get("system_status") == "operating" \
            and _texts(findings, "actual_use_refs")
    if evidence_class == "executive_role_record":
        return findings.get("role_status") == "active" \
            and findings.get("executive_role") == "CEO"
    if evidence_class == "board_advisor_record":
        return findings.get("recruitment_status") == "active" \
            and _texts(findings, "actual_actions")
    if evidence_class == "team_risk_management_record":
        return findings.get("risk_process_status") == "operating" \
            and _texts(findings, "risk_refs")
    if evidence_class == "organization_charter":
        return findings.get("charter_status") == "approved_current"
    if evidence_class == "organization_growth_plan":
        return findings.get("plan_status") == "documented" \
            and findings.get("plan_horizon_months", 0) >= 24
    if evidence_class in {"learning_development_process",
                          "continuous_improvement_record"}:
        return findings.get("process_status") == "operating" \
            and _texts(findings, "actual_actions")
    if evidence_class == "management_team_record":
        roles = findings.get("executive_roles")
        return findings.get("team_status") == "active" \
            and isinstance(roles, list) and "CEO" in roles
    if evidence_class == "hr_policy_process":
        return findings.get("process_status") == "operating" \
            and isinstance(findings.get("responsible_subject"), str) \
            and bool(findings["responsible_subject"].strip())
    if evidence_class == "training_motivation_record":
        return findings.get("record_status") == "verified_current" \
            and isinstance(findings.get("measured_population"), str) \
            and bool(findings["measured_population"].strip())
    if evidence_class == "incentive_alignment_record":
        return findings.get("alignment_status") == "executed" \
            and isinstance(findings.get("covered_population"), str) \
            and bool(findings["covered_population"].strip())
    return False


def _criterion_result(criterion, reviews, scope_id):
    valid = [review for review in reviews if _valid(review, criterion, scope_id)]
    supports = [review for review in valid
                if review["decision"] == "supports" and _support(criterion, review)]
    negatives = [review for review in valid
                 if review["decision"] == "does_not_support"]
    if supports and negatives:
        native, product, rationale = "partial", "succeeded", "团队证据存在受控冲突。"
    elif negatives:
        native, product, rationale = "not_met", "succeeded", "受控复核明确不支持该团队准则。"
    elif supports:
        native, product, rationale = "met", "succeeded", "准则语义与对应团队记录强度均通过。"
    else:
        native, product, rationale = "insufficient", "insufficient", "缺少该准则要求的当前团队记录；身份、履历或意向不替代实际能力。"
    return {**criterion,
            "requirements": [RULE_REQUIREMENTS[criterion["criterion_id"]]],
            "native_disposition": native, "product_status": product,
            "review_refs": [review["review_id"] for review in valid],
            "claim_refs": sorted({review["claim_id"] for review in valid}),
            "rationale": rationale, "rule_version": RULE_VERSION}


def evaluate_tmrl_dimension(criteria, reviews, *, scope, assessment_unit):
    if {criterion.get("criterion_id") for criterion in criteria} != set(RULE_REQUIREMENTS):
        raise ValueError("TMRL准则集合不完整")
    unit = _unit(assessment_unit, scope)
    grouped = defaultdict(list)
    for review in reviews:
        if isinstance(review, dict) and review.get("criterion_id") in RULE_REQUIREMENTS:
            grouped[review["criterion_id"]].append(review)
    rows = [_criterion_result(criterion, grouped[criterion["criterion_id"]],
                              unit["scope_id"])
            for criterion in sorted(criteria, key=lambda item: (
                item["level"], item["criterion_id"]))]
    by_id = {row["criterion_id"]: row for row in rows}
    attained, first_unmet = 0, None
    for level in range(1, 10):
        if all(by_id[criterion["criterion_id"]]["native_disposition"] == "met"
               for criterion in criteria if criterion["level"] <= level):
            attained = level
        else:
            first_unmet = level
            break
    return {"dimension": "TMRL", "scope": scope, "assessment_unit": unit,
            "criteria": rows, "attained_level": attained,
            "first_unmet_level": first_unmet,
            "product_status": ("succeeded" if all(
                row["product_status"] == "succeeded" for row in rows)
                else "insufficient"), "rule_version": RULE_VERSION,
            "method_boundary": "身份overlay不得设定成熟度；协议与运行类必须达到canonical记录强度。"}
