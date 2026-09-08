"""R2-B候选：FRL 36条离线规则、受限N/A与融资主体共享。"""

from __future__ import annotations

import pytest

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.frl import (
    RULE_REQUIREMENTS,
    evaluate_frl_dimension,
)


SCOPE = "Company-A"
ENTITY = {
    "financing_entity_id": "FIN-COMPANY-A",
    "subject_scope": SCOPE,
    "assessment_unit_refs": ["UNIT-PRODUCT", "UNIT-PLATFORM"],
}
FRL_CRITERIA = build_catalog_from_wheel()["dimensions"]["FRL"]["registry"]["criteria"]


def _criteria():
    return FRL_CRITERIA


def _review(criterion, *, decision="supports", evidence_class=None,
            findings=None, suffix="1"):
    criterion_id = criterion["criterion_id"]
    required = RULE_REQUIREMENTS[criterion_id]
    default_findings = {required: True}
    if criterion_id in {"FRL3-STATUS", "FRL4-STATUS", "FRL8-STATUS"}:
        default_findings["funding_bucket"] = "cash_now"
    elif criterion_id == "FRL5-STATUS":
        default_findings["funding_bucket"] = "binding_conditional"
    elif criterion_id in {"FRL6-STATUS", "FRL7-STATUS", "FRL9-STATUS"}:
        default_findings["funding_bucket"] = "soft_or_nonbinding"
    return {
        "review_id": f"FRL-REV-{criterion_id}-{suffix}",
        "criterion_id": criterion_id,
        "claim_id": f"CLAIM-{criterion_id}-{suffix}",
        "decision": decision,
        "evidence_class": evidence_class or criterion["eligible_evidence_classes"][0],
        "findings": findings if findings is not None else default_findings,
        "financing_entity_id": ENTITY["financing_entity_id"],
        "reviewer": "r2b-test",
        "review_basis": "合成离线FRL受控复核",
        "support_scope": f"仅支持{criterion_id}",
    }


def _evaluate(reviews, *, applicability=None, entity=ENTITY):
    return evaluate_frl_dimension(
        _criteria(), reviews, scope=SCOPE,
        financing_entity=entity, applicability=applicability)


def _row(result, criterion_id):
    return next(row for row in result["criteria"]
                if row["criterion_id"] == criterion_id)


@pytest.mark.parametrize("criterion", _criteria(),
                         ids=lambda row: row["criterion_id"])
def test_each_frl_rule_has_distinct_positive_and_missing_evidence_behavior(criterion):
    positive = _row(_evaluate([_review(criterion)]), criterion["criterion_id"])
    missing = _row(_evaluate([]), criterion["criterion_id"])

    assert positive["native_disposition"] == "met"
    assert positive["product_status"] == "succeeded"
    assert positive["requirements"] == [RULE_REQUIREMENTS[criterion["criterion_id"]]]
    assert missing["native_disposition"] == "insufficient"
    assert missing["product_status"] == "insufficient"


def test_frl_has_36_unique_requirements_and_exact_27_never_9_restricted_na():
    criteria = _criteria()
    assert len(criteria) == len(RULE_REQUIREMENTS) == 36
    assert len(set(RULE_REQUIREMENTS.values())) == 36
    assert sum(row["na_policy"] == "never" for row in criteria) == 27
    assert sum(row["na_policy"] == "explicit_no_external_financing_only"
               for row in criteria) == 9


def test_wrong_evidence_class_or_missing_specific_finding_cannot_meet_rule():
    criterion = next(row for row in _criteria()
                     if row["criterion_id"] == "FRL3-STATUS")
    wrong_class = _review(
        criterion, evidence_class="soft_or_nonbinding_interest",
        findings={RULE_REQUIREMENTS[criterion["criterion_id"]]: True,
                  "funding_bucket": "soft_or_nonbinding"})
    missing_finding = _review(
        criterion, findings={"funding_bucket": "closed_or_drawable"})

    assert _row(_evaluate([wrong_class]), "FRL3-STATUS")["native_disposition"] \
        == "insufficient"
    assert _row(_evaluate([missing_finding]), "FRL3-STATUS")["native_disposition"] \
        == "insufficient"


@pytest.mark.parametrize("criterion_id", ["FRL3-STATUS", "FRL4-STATUS", "FRL8-STATUS"])
def test_soft_or_conditional_interest_is_not_usable_cash(criterion_id):
    criterion = next(row for row in _criteria()
                     if row["criterion_id"] == criterion_id)
    key = RULE_REQUIREMENTS[criterion_id]
    for bucket in ("soft_or_nonbinding", "binding_conditional"):
        review = _review(
            criterion,
            findings={key: True, "funding_bucket": bucket},
        )
        assert _row(_evaluate([review]), criterion_id)["native_disposition"] \
            == "insufficient"


def test_binding_conditional_is_valid_for_level5_status_but_not_cash_now():
    criterion = next(row for row in _criteria()
                     if row["criterion_id"] == "FRL5-STATUS")
    review = _review(criterion, findings={
        RULE_REQUIREMENTS[criterion["criterion_id"]]: True,
        "funding_bucket": "binding_conditional",
    })
    assert _row(_evaluate([review]), "FRL5-STATUS")["native_disposition"] == "met"


def test_preorder_cash_must_keep_delivery_and_refund_obligations_visible():
    criterion = next(row for row in _criteria()
                     if row["criterion_id"] == "FRL3-STATUS")
    key = RULE_REQUIREMENTS[criterion["criterion_id"]]
    hidden = _review(criterion, findings={
        key: True, "funding_bucket": "cash_now", "preorder_cash": True,
        "delivery_refund_obligations_visible": False,
    })
    visible = _review(criterion, findings={
        key: True, "funding_bucket": "cash_now", "preorder_cash": True,
        "delivery_refund_obligations_visible": True,
    }, suffix="2")
    assert _row(_evaluate([hidden]), "FRL3-STATUS")["native_disposition"] \
        == "insufficient"
    assert _row(_evaluate([visible]), "FRL3-STATUS")["native_disposition"] == "met"


def test_low_level_negative_description_requires_explicit_review_not_absence():
    criterion = next(row for row in _criteria()
                     if row["criterion_id"] == "FRL1-STATUS")
    assert _row(_evaluate([]), "FRL1-STATUS")["native_disposition"] \
        == "insufficient"
    reviewed = _review(criterion, findings={
        RULE_REQUIREMENTS[criterion["criterion_id"]]: True,
        "funding_state": "no_funding_obtained",
    })
    assert _row(_evaluate([reviewed]), "FRL1-STATUS")["native_disposition"] == "met"


def test_negative_and_conflicting_reviews_remain_distinct():
    criterion = _criteria()[0]
    negative = _review(criterion, decision="does_not_support")
    assert _row(_evaluate([negative]), criterion["criterion_id"])["native_disposition"] \
        == "not_met"
    conflict = _evaluate([negative, _review(criterion, suffix="2")])
    assert _row(conflict, criterion["criterion_id"])["native_disposition"] == "partial"


def _no_external_policy(**changes):
    value = {
        "external_financing_planned": False,
        "financing_entity_id": ENTITY["financing_entity_id"],
        "subject_scope": SCOPE,
        "policy_binding": {
            "origin_path": "case:financing-policy.json",
            "origin_sha256": "a" * 64,
            "field_path": "/entities/0/external_financing_planned",
            "value": False,
        },
    }
    value.update(changes)
    return value


def test_restricted_na_requires_bound_no_external_financing_policy():
    result = _evaluate([], applicability=_no_external_policy())
    restricted = [row for row in result["criteria"]
                  if row["na_policy"] == "explicit_no_external_financing_only"]
    never = [row for row in result["criteria"] if row["na_policy"] == "never"]
    assert len(restricted) == 9
    assert all(row["native_disposition"] == "not_applicable" for row in restricted)
    assert all(row["native_disposition"] == "insufficient" for row in never)

    for invalid in (
        _no_external_policy(external_financing_planned=True),
        _no_external_policy(financing_entity_id="OTHER"),
        _no_external_policy(subject_scope="Other-Company"),
        _no_external_policy(policy_binding=None),
    ):
        invalid_result = _evaluate([], applicability=invalid)
        assert all(row["native_disposition"] != "not_applicable"
                   for row in invalid_result["criteria"])


def test_cumulative_level_accepts_only_met_or_legal_na_and_stops_at_first_gap():
    criteria = _criteria()
    level1 = [_review(row) for row in criteria if row["level"] == 1]
    first = _evaluate(level1)
    assert first["attained_level"] == 1
    assert first["first_unmet_level"] == 2

    through4 = [_review(row) for row in criteria
                if row["level"] <= 4 and row["na_policy"] == "never"]
    fourth = _evaluate(through4, applicability=_no_external_policy())
    assert fourth["attained_level"] == 4
    assert fourth["first_unmet_level"] == 5


def test_one_financing_entity_covers_multiple_units_without_product_unit_grades():
    result = _evaluate([])
    assert result["financing_entity"]["financing_entity_id"] == "FIN-COMPANY-A"
    assert result["financing_entity"]["assessment_unit_refs"] == [
        "UNIT-PLATFORM", "UNIT-PRODUCT"]
    assert "unit_levels" not in result

    with pytest.raises(ValueError, match="融资主体"):
        _evaluate([], entity={**ENTITY, "subject_scope": "Other-Company"})
    with pytest.raises(ValueError, match="评估单元"):
        _evaluate([], entity={**ENTITY, "assessment_unit_refs": ["UNIT-X", "UNIT-X"]})
