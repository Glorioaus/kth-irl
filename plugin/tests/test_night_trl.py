"""夜间TRL候选：25条项目特定技术证据规则。"""

from __future__ import annotations

import pytest

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.trl import RULE_REQUIREMENTS, evaluate_trl_dimension


SCOPE = "Company-A"
UNIT = {"scope_id": "UNIT-A", "subject_scope": SCOPE,
        "unit_kind": "material_technology_unit", "unit_label": "System-A"}
CRITERIA = build_catalog_from_wheel()["dimensions"]["TRL"]["registry"]["criteria"]


def _review(criterion, *, findings=None, evidence_class=None, decision="supports",
            suffix="1"):
    level = criterion["level"]
    evidence_class = evidence_class or criterion["eligible_evidence_classes"][0]
    default = {RULE_REQUIREMENTS[criterion["criterion_id"]]: True,
               "project_specific": True, "configuration_id": "CFG-A",
               "system_boundary": "complete System-A"}
    if evidence_class == "r_and_d_record":
        default["activity_status"] = "active"
    if evidence_class == "requirements_record":
        default["requirements_status"] = "documented"
        if criterion["criterion_id"] == "TRL5-C4":
            default["user_feedback_bound"] = True
        if criterion["criterion_id"] == "TRL7-C3":
            default["complete_requirements"] = True
    if evidence_class in {"lab_test", "test_record", "independent_test",
                          "relevant_environment_test", "operational_demonstration",
                          "actual_operation", "independent_operation_record",
                          "longitudinal_operation"} and level >= 3:
        environment = "laboratory" if level <= 4 else (
            "relevant" if level <= 6 else (
                "operational" if level == 7 else "actual_operation"))
        default.update({"test_environment": "declared environment",
                        "test_method": "repeatable protocol",
                        "measured_results": "bounded measurements",
                        "requirements_thresholds": "declared thresholds",
                        "environment_kind": environment})
    if evidence_class == "manufacturing_record":
        default.update({"manufacturing_scope": "configured system",
                        "manufacturing_results": "producibility record"})
    if evidence_class == "continuous_improvement_record":
        default.update({"improvement_period": "2026-Q2 to Q3",
                        "improvement_actions": ["ACTION-1"]})
    if level >= 4:
        default["complete_system_boundary"] = True
    if level >= 8:
        default.update({"independent_user_ids": ["USER-1"],
                        "day_to_day_operation": True})
    if level >= 9:
        default.update({"independent_user_ids": ["USER-1", "USER-2"],
                        "longitudinal_period": "six months"})
    return {"review_id": f"TRL-REV-{criterion['criterion_id']}-{suffix}",
            "criterion_id": criterion["criterion_id"],
            "claim_id": f"TRL-CLAIM-{criterion['criterion_id']}-{suffix}",
            "decision": decision,
            "evidence_class": evidence_class,
            "findings": default if findings is None else findings,
            "scope_id": UNIT["scope_id"], "reviewer": "night-trl",
            "review_basis": "合成离线TRL复核",
            "support_scope": f"仅支持{criterion['criterion_id']}项目技术推理"}


def _evaluate(reviews, unit=UNIT):
    return evaluate_trl_dimension(CRITERIA, reviews, scope=SCOPE,
                                  assessment_unit=unit)


def _row(result, criterion_id):
    return next(row for row in result["criteria"]
                if row["criterion_id"] == criterion_id)


@pytest.mark.parametrize("criterion", CRITERIA, ids=lambda row: row["criterion_id"])
def test_each_trl_rule_has_positive_and_missing_behavior(criterion):
    assert _row(_evaluate([_review(criterion)]), criterion["criterion_id"])[
        "native_disposition"] == "met"
    assert _row(_evaluate([]), criterion["criterion_id"])[
        "native_disposition"] == "insufficient"


def test_trl_has_25_unique_requirements():
    assert len(CRITERIA) == len(RULE_REQUIREMENTS) == 25
    assert len(set(RULE_REQUIREMENTS.values())) == 25


def test_industry_or_competitor_capability_cannot_prove_project_trl():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "TRL3-C1")
    findings = {RULE_REQUIREMENTS["TRL3-C1"]: True,
                "project_specific": False, "competitor_system": True,
                "configuration_id": "OTHER", "system_boundary": "other system",
                "test_environment": "lab", "test_method": "paper",
                "measured_results": "competitor result",
                "requirements_thresholds": "unknown"}
    assert _row(_evaluate([_review(criterion, findings=findings)]), "TRL3-C1")[
        "native_disposition"] == "insufficient"


def test_component_test_does_not_become_complete_system_readiness():
    criterion = next(row for row in CRITERIA if row["criterion_id"] == "TRL4-C1")
    findings = dict(_review(criterion)["findings"])
    findings.update({"component_only": True, "complete_system_boundary": False})
    assert _row(_evaluate([_review(criterion, findings=findings)]), "TRL4-C1")[
        "native_disposition"] == "insufficient"


def test_actual_operation_and_scale_require_user_and_time_identity():
    trl8 = next(row for row in CRITERIA if row["criterion_id"] == "TRL8-C1")
    weak8 = dict(_review(trl8)["findings"])
    weak8["independent_user_ids"] = []
    assert _row(_evaluate([_review(trl8, findings=weak8)]), "TRL8-C1")[
        "native_disposition"] == "insufficient"
    trl9 = next(row for row in CRITERIA if row["criterion_id"] == "TRL9-C1")
    weak9 = dict(_review(trl9)["findings"])
    weak9.update({"independent_user_ids": ["USER-1"],
                  "longitudinal_period": ""})
    assert _row(_evaluate([_review(trl9, findings=weak9)]), "TRL9-C1")[
        "native_disposition"] == "insufficient"


def test_trl_cumulative_and_assessment_unit_boundaries():
    level1 = [_review(row) for row in CRITERIA if row["level"] == 1]
    result = _evaluate(level1)
    assert result["attained_level"] == 1
    assert result["first_unmet_level"] == 2
    with pytest.raises(ValueError, match="评估单元"):
        _evaluate([], {**UNIT, "subject_scope": "Other"})
