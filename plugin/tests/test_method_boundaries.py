"""T06：方法硬边界——CRL 范围/无insufficient、FRL 受限N/A、TMRL 身份。

不得把只跑通 FRL 低级正例作为六维可行证明；每个边界独立验证。
"""

from __future__ import annotations

import pytest

from kth_hybrid.dimensions import evaluate_criterion
from kth_hybrid.kernels import (
    check_na_legality,
    check_native_disposition_legal,
    check_tmrl_identity_binding,
    use_class_supports_criterion,
)

CRL_VIEW = {"dimension_levels_supported": [1, 2, 3, 4], "case_flags": {},
            "scope": "微玖"}
FRL_VIEW = {"dimension_levels_supported": list(range(1, 10)), "case_flags": {},
            "scope": "微玖（融资主体）"}
TMRL_VIEW = {"dimension_levels_supported": list(range(1, 10)), "case_flags": {},
             "scope": "微玖"}


def _qualified_candidate(claim_id="CLM-1", uses=("company_self_statement",),
                         identity_verdict="ok", scope="具体团队/角色"):
    return {
        "qualifications": [{
            "claim_id": claim_id, "status": "qualified",
            "allowed_uses": list(uses),
            "identity_judgment": {"verdict": identity_verdict, "basis": "测试"},
        }],
        "claims": {claim_id: {"claim_id": claim_id, "subject_scope": scope}},
        "gap_refs": [],
    }


def test_crl_level5_is_method_unsupported_not_fake_crl4():
    synthetic_crl5 = {"criterion_id": "CRL5-C1", "dimension": "CRL", "level": 5,
                      "text": "（KTH 材料 CRL5 行；wheel registry 未实现）"}
    result = evaluate_criterion(synthetic_crl5, _qualified_candidate(), CRL_VIEW)
    assert result.product_status == "method_unsupported"
    assert "超出批准 wheel CRL registry 支持范围" in result.rationale
    assert result.native_disposition is None  # 不伪报，也不给业务NO


def test_crl_native_insufficient_proposal_is_rejected():
    crl1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1}
    candidate = _qualified_candidate()
    candidate["native_proposal"] = "insufficient"
    result = evaluate_criterion(crl1, candidate, CRL_VIEW)
    assert result.native_disposition is None
    assert any("原生处置提案被拒" in n for n in result.notes)
    legal, basis = check_native_disposition_legal("CRL", "insufficient")
    assert not legal and "CRL 无原生 insufficient" in basis


def test_crl_native_legal_dispositions_accepted():
    for disposition in ("met", "not_met", "partial", "not_applicable"):
        legal, _ = check_native_disposition_legal("CRL", disposition)
        assert legal
    legal, _ = check_native_disposition_legal("BRL", "insufficient")
    assert legal  # 其他五维原生含 insufficient


def test_frl_na_never_policy_rejects_na():
    frl1 = {"criterion_id": "FRL1-NEED", "dimension": "FRL", "level": 1,
            "na_policy": "never"}
    candidate = _qualified_candidate()
    candidate["na_proposal"] = "not_applicable"
    result = evaluate_criterion(frl1, candidate, FRL_VIEW)
    assert result.product_status != "succeeded" or "N/A" not in result.rationale
    assert any("N/A 提案被拒" in n for n in result.notes)
    legal, basis = check_na_legality(frl1, "not_applicable", {})
    assert not legal and "不允许 N/A" in basis


def test_frl_restricted_na_requires_explicit_no_external_financing():
    frl4_pitch = {"criterion_id": "FRL4-PITCH", "dimension": "FRL", "level": 4,
                  "na_policy": "explicit_no_external_financing_only"}
    candidate = _qualified_candidate()
    candidate["na_proposal"] = "not_applicable"
    # 未声明 → 非法
    result = evaluate_criterion(frl4_pitch, candidate, FRL_VIEW)
    assert any("N/A 提案被拒" in n for n in result.notes)
    assert result.product_status != "succeeded"
    # 显式声明不计划外部融资 → 合法 N/A
    view = {**FRL_VIEW, "case_flags": {"explicit_no_external_financing": True}}
    result2 = evaluate_criterion(frl4_pitch, candidate, view)
    assert result2.product_status == "succeeded"
    assert "受限 N/A 合法成立" in result2.rationale
    assert result2.native_disposition is None


def test_tmrl_ambiguous_identity_cannot_set_team_criterion():
    tmrl2 = {"criterion_id": "TMRL2-C1", "dimension": "TMRL", "level": 2}
    candidate = _qualified_candidate(identity_verdict="unknown")
    result = evaluate_criterion(tmrl2, candidate, TMRL_VIEW)
    assert result.product_status == "insufficient"
    assert any("TMRL 身份约束不满足" in n for n in result.notes)


def test_tmrl_generic_scope_cannot_set_team_criterion():
    ok, basis = check_tmrl_identity_binding(
        {"criterion_id": "TMRL2-C1", "dimension": "TMRL"},
        {"identity_judgment": {"verdict": "ok"}}, {"subject_scope": "公司整体"})
    assert not ok and "主体范围含糊" in basis
    ok2, _ = check_tmrl_identity_binding(
        {"criterion_id": "TMRL2-C1", "dimension": "TMRL"},
        {"identity_judgment": {"verdict": "ok"}},
        {"subject_scope": "微玖创始团队（具体角色）"})
    assert ok2


def test_r1_use_class_caps_at_level1():
    assert use_class_supports_criterion("company_self_statement", "CRL", 1)
    assert use_class_supports_criterion("third_party_reported_fact", "BRL", 1)
    assert not use_class_supports_criterion("company_self_statement", "CRL", 2)
    assert not use_class_supports_criterion("third_party_reported_fact", "TRL", 3)
    assert not use_class_supports_criterion("unknown_use", "CRL", 1)


def test_higher_level_needs_full_rules_not_r1_narrow_path():
    crl2 = {"criterion_id": "CRL2-C1", "dimension": "CRL", "level": 2}
    result = evaluate_criterion(crl2, _qualified_candidate(
        uses=("company_self_statement",)), CRL_VIEW)
    assert result.product_status == "insufficient"
    assert "R1 窄规则上限" in result.rationale  # 明确说不足，不冒充met


def test_no_judgment_candidate_is_honest_insufficient():
    crl1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1}
    result = evaluate_criterion(crl1, {"qualifications": [], "claims": {},
                                       "gap_refs": []}, CRL_VIEW)
    assert result.product_status == "insufficient"
    assert "无候选主张" in result.rationale


def test_dimensions_do_not_aggregate_into_single_score():
    from kth_hybrid.dimensions import evaluate_dimension

    criteria = [
        {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1},
        {"criterion_id": "CRL5-C1", "dimension": "CRL", "level": 5},
    ]
    dimension = evaluate_dimension(
        "CRL", "微玖", CRL_VIEW, criteria,
        {"CRL1-C1": _qualified_candidate()},
    )
    statuses = [c.product_status for c in dimension.criterion_results]
    assert statuses == ["succeeded", "method_unsupported"]
    assert not hasattr(dimension, "total_score")  # 无总分
