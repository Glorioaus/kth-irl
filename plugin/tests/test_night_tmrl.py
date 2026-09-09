"""夜间TMRL候选：38条准则使用各自证据类真实记录强度。"""
import pytest
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.tmrl import RULE_REQUIREMENTS, evaluate_tmrl_dimension

SCOPE = "Company-A"
UNIT = {"scope_id": "UNIT-A", "subject_scope": SCOPE,
        "unit_kind": "material_team_unit", "unit_label": "Team-A"}
CRITERIA = build_catalog_from_wheel()["dimensions"]["TMRL"]["registry"]["criteria"]


def _class_fields(criterion, evidence_class):
    criterion_id = criterion["criterion_id"]
    fields = {}
    if evidence_class == "team_needs_hypothesis":
        fields["hypothesis_status"] = "documented"
    elif evidence_class in {"team_capability_snapshot", "team_capability_matrix"}:
        fields.update(capability_status="verified_current",
                      work_evidence_refs=["WORK-1"])
    elif evidence_class == "competency_gap_record":
        fields["gap_status"] = "verified_current"
    elif evidence_class == "project_goal_record":
        fields["goal_status"] = "documented"
    elif evidence_class == "hiring_or_access_plan":
        fields["plan_status"] = "initiated" if criterion_id == "TMRL4-C4" else "documented"
    elif evidence_class == "execution_route_record":
        fields["route_status"] = "documented"
    elif evidence_class == "champion_commitment_record":
        fields.update(commitment_status="committed", capacity_status="verified_current")
    elif evidence_class == "team_alignment_record":
        fields.update(alignment_status="documented_current",
                      commitment_status="committed")
    elif evidence_class in {"executed_role_commitment_record",
                            "executed_ownership_agreement"}:
        fields["agreement_status"] = "executed_agreement"
    elif evidence_class in {"founder_team_operating_record",
                            "accountability_operating_record",
                            "board_advisor_operating_record", "team_operating_record",
                            "organization_performance_record", "learning_performance_record",
                            "management_continuity_record"}:
        fields.update(record_status="verified_operating_record",
                      operating_period="2026-Q2 to Q3",
                      actual_behaviors=["BEHAVIOR-1"])
    elif evidence_class == "recruitment_execution_record":
        fields.update(execution_status="active", actual_actions=["RECRUIT-1"])
    elif evidence_class == "knowledge_system_record":
        fields.update(system_status="operating", actual_use_refs=["USE-1"])
    elif evidence_class == "executive_role_record":
        fields.update(role_status="active", executive_role="CEO")
    elif evidence_class == "board_advisor_record":
        fields.update(recruitment_status="active", actual_actions=["BOARD-1"])
    elif evidence_class == "team_risk_management_record":
        fields.update(risk_process_status="operating", risk_refs=["RISK-1"])
    elif evidence_class == "organization_charter":
        fields["charter_status"] = "approved_current"
    elif evidence_class == "organization_growth_plan":
        fields.update(plan_status="documented", plan_horizon_months=24)
    elif evidence_class == "learning_development_process":
        fields.update(process_status="operating", actual_actions=["LEARN-1"])
    elif evidence_class == "management_team_record":
        fields.update(team_status="active", executive_roles=["CEO", "CTO"])
    elif evidence_class == "hr_policy_process":
        fields.update(process_status="operating", responsible_subject="PERSON-1")
    elif evidence_class == "training_motivation_record":
        fields.update(record_status="verified_current", measured_population="all staff")
    elif evidence_class == "continuous_improvement_record":
        fields.update(process_status="operating", actual_actions=["IMPROVE-1"])
    elif evidence_class == "incentive_alignment_record":
        fields.update(alignment_status="executed", covered_population="all staff")
    if criterion_id == "TMRL6-C2":
        fields["ceo_subject_id"] = "PERSON-CEO"
    return fields


def review(criterion, findings=None, evidence_class=None, decision="supports", suffix="1"):
    evidence_class = evidence_class or criterion["eligible_evidence_classes"][0]
    base = {RULE_REQUIREMENTS[criterion["criterion_id"]]: True,
            "team_specific": True, "subject_ids": ["PERSON-1"],
            "current_period": "2026-Q3"}
    base.update(_class_fields(criterion, evidence_class))
    return {"review_id": f"REV-{criterion['criterion_id']}-{suffix}",
            "criterion_id": criterion["criterion_id"],
            "claim_id": f"C-{criterion['criterion_id']}-{suffix}",
            "decision": decision, "evidence_class": evidence_class,
            "findings": base if findings is None else findings,
            "scope_id": "UNIT-A", "reviewer": "night-tmrl",
            "review_basis": "合成团队复核", "support_scope": "仅支持当前团队准则"}


def evaluate(reviews):
    return evaluate_tmrl_dimension(CRITERIA, reviews, scope=SCOPE,
                                   assessment_unit=UNIT)


def row(result, criterion_id):
    return next(item for item in result["criteria"]
                if item["criterion_id"] == criterion_id)


@pytest.mark.parametrize("criterion", CRITERIA, ids=lambda item: item["criterion_id"])
def test_each_tmrl_rule_has_positive_and_missing_behavior(criterion):
    assert row(evaluate([review(criterion)]), criterion["criterion_id"])[
        "native_disposition"] == "met"
    assert row(evaluate([]), criterion["criterion_id"])[
        "native_disposition"] == "insufficient"


def test_tmrl_has_38_semantic_requirements():
    assert len(CRITERIA) == len(RULE_REQUIREMENTS) == 38
    assert len(set(RULE_REQUIREMENTS.values())) == 38
    assert all(not value.startswith("tmrl") for value in RULE_REQUIREMENTS.values())


def test_identity_biography_or_public_claim_does_not_prove_current_capability():
    criterion = next(item for item in CRITERIA if item["criterion_id"] == "TMRL3-C1")
    weak = {RULE_REQUIREMENTS["TMRL3-C1"]: True, "team_specific": True,
            "subject_ids": ["PERSON-1"], "current_period": "2026-Q3",
            "biography_only": True, "capability_status": "identity_only",
            "work_evidence_refs": []}
    assert row(evaluate([review(criterion, findings=weak)]), "TMRL3-C1")[
        "native_disposition"] == "insufficient"


def test_unsigned_agreement_and_non_operating_record_are_rejected():
    ownership = next(item for item in CRITERIA if item["criterion_id"] == "TMRL5-C3")
    draft = {RULE_REQUIREMENTS["TMRL5-C3"]: True, "team_specific": True,
             "subject_ids": ["PERSON-1"], "current_period": "2026-Q3",
             "agreement_status": "draft_unsigned"}
    assert row(evaluate([review(ownership, findings=draft)]), "TMRL5-C3")[
        "native_disposition"] == "insufficient"
    operating = next(item for item in CRITERIA if item["criterion_id"] == "TMRL5-C1")
    intent = {RULE_REQUIREMENTS["TMRL5-C1"]: True, "team_specific": True,
              "subject_ids": ["PERSON-1"], "current_period": "2026-Q3",
              "record_status": "biography", "operating_period": "2026-Q3",
              "actual_behaviors": []}
    assert row(evaluate([review(operating, findings=intent)]), "TMRL5-C1")[
        "native_disposition"] == "insufficient"


def test_relationship_has_no_readiness_effect_without_causal_evidence():
    criterion = next(item for item in CRITERIA if item["criterion_id"] == "TMRL4-C5")
    weak = {RULE_REQUIREMENTS["TMRL4-C5"]: True, "team_specific": True,
            "subject_ids": ["PERSON-1", "PERSON-2"], "current_period": "2026-Q3",
            "relationship_only": True, "alignment_status": "documented_current",
            "commitment_status": "committed"}
    assert row(evaluate([review(criterion, findings=weak)]), "TMRL4-C5")[
        "native_disposition"] == "insufficient"


def test_tmrl_cumulative_and_unit_scope():
    result = evaluate([review(item) for item in CRITERIA if item["level"] == 1])
    assert result["attained_level"] == 1
    with pytest.raises(ValueError, match="评估单元"):
        evaluate_tmrl_dimension(CRITERIA, [], scope=SCOPE,
                                assessment_unit={**UNIT, "subject_scope": "Other"})
