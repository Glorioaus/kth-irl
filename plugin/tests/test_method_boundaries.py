"""T06/R1.1：方法硬边界——CRL 范围/无insufficient、FRL 受限N/A、TMRL 身份。

v2（R1.1）：通用"用途类→级别"路由已删除，改为 IMPLEMENTED_RULES 单判据规则；
未登记/未实现判据一律 method_unsupported，不得当作证据不足。
不得把只跑通 FRL 低级正例作为六维可行证明；每个边界独立验证。
"""

from __future__ import annotations

import pytest

from kth_hybrid.dimensions import evaluate_criterion
from kth_hybrid.kernels import (
    IMPLEMENTED_RULES,
    check_na_legality,
    check_native_disposition_legal,
    check_tmrl_identity_binding,
    implemented_criterion,
)

CRL_VIEW = {"dimension_levels_supported": [1, 2, 3, 4], "case_flags": {},
            "scope": "微玖",
            # CRL5-C1 不存在于真实 registry；加入登记集仅用于验证"已登记但级别
            # 超范围"纵深防御分支（未登记分支由反例测试覆盖）
            "approved_criterion_ids": {"CRL1-C1", "CRL2-C1", "CRL1-C2",
                                       "CRL5-C1"}}
FRL_VIEW = {"dimension_levels_supported": list(range(1, 10)), "case_flags": {},
            "scope": "微玖（融资主体）",
            "approved_criterion_ids": {"FRL1-NEED", "FRL4-PITCH"}}
TMRL_VIEW = {"dimension_levels_supported": list(range(1, 10)), "case_flags": {},
             "scope": "微玖", "approved_criterion_ids": {"TMRL2-C1"}}


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
    assert any("仅作候选记录" in n for n in result.notes)
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
    assert any("N/A 提案被拒" in n for n in result.notes)
    assert result.product_status != "succeeded"
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


def test_tmrl_identity_binding_paired():
    # 合格输入（已取出身份dict）必须通过；不合格必须拒绝——成对验证
    criterion = {"criterion_id": "TMRL2-C1", "dimension": "TMRL"}
    ok, ok_basis = check_tmrl_identity_binding(
        criterion, {"verdict": "ok", "basis": "已核验"},
        {"subject_scope": "微玖创始团队（具体角色）"})
    assert ok, ok_basis
    bad, bad_basis = check_tmrl_identity_binding(
        criterion, {"verdict": "unknown"}, {"subject_scope": "微玖创始团队"})
    assert not bad and "身份判定" in bad_basis
    generic, generic_basis = check_tmrl_identity_binding(
        criterion, {"verdict": "ok"}, {"subject_scope": "公司整体"})
    assert not generic and "主体范围含糊" in generic_basis
    legacy, _ = check_tmrl_identity_binding(
        criterion, {"identity_judgment": {"verdict": "ok"}},
        {"subject_scope": "微玖创始团队（具体角色）"})
    assert legacy  # 兼容旧嵌套结构


def test_tmrl_criteria_are_method_unsupported_and_identity_guard_paired():
    # R1.1 未实现 TMRL 规则：合格输入也只得到 method_unsupported（不冒充消费）
    tmrl2 = {"criterion_id": "TMRL2-C1", "dimension": "TMRL", "level": 2}
    result = evaluate_criterion(tmrl2, _qualified_candidate(), TMRL_VIEW)
    assert result.product_status == "method_unsupported"
    # 身份约束的结构修复由成对 kernel 测试验证（合格通过/不合格拒绝）
    from kth_hybrid.kernels import check_tmrl_identity_binding

    criterion = {"criterion_id": "TMRL2-C1", "dimension": "TMRL"}
    ok, _ = check_tmrl_identity_binding(
        criterion, {"verdict": "ok"}, {"subject_scope": "微玖创始团队（角色）"})
    assert ok  # v1 结构缺陷（合格输入被读成身份缺失）已修复
    bad, _ = check_tmrl_identity_binding(
        criterion, {"verdict": "unknown"}, {"subject_scope": "微玖创始团队（角色）"})
    assert not bad


def test_only_implemented_rule_consumes_positive_channel():
    crl1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1}
    rule = implemented_criterion("CRL1-C1")
    assert rule and rule["rule_kind"] == "specific" and rule["provenance"]
    result = evaluate_criterion(crl1, _qualified_candidate(), CRL_VIEW)
    assert result.product_status == "succeeded"
    assert "不是原生 met" in result.rationale


def test_registered_but_unimplemented_is_method_unsupported():
    # 已登记但 R1.1 未实现规则 → method_unsupported（不是证据不足）
    crl2 = {"criterion_id": "CRL2-C1", "dimension": "CRL", "level": 2}
    result = evaluate_criterion(crl2, _qualified_candidate(), CRL_VIEW)
    assert result.product_status == "method_unsupported"
    assert "未实现其规则" in result.rationale
    crl1c2 = {"criterion_id": "CRL1-C2", "dimension": "CRL", "level": 1}
    result2 = evaluate_criterion(crl1c2, _qualified_candidate(), CRL_VIEW)
    assert result2.product_status == "method_unsupported"


def test_implemented_rules_are_explicit_and_few():
    assert set(IMPLEMENTED_RULES) == {"CRL1-C1"}, \
        "R1.1 只实现有出处的单判据；扩充需逐条带出处并经批准"


def test_no_judgment_candidate_is_honest_insufficient():
    crl1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1}
    result = evaluate_criterion(
        crl1, {"qualifications": [], "claims": {}, "gap_refs": []}, CRL_VIEW)
    assert result.product_status == "insufficient"
    assert "无候选主张" in result.rationale


def test_dimensions_do_not_aggregate_into_single_score():
    from kth_hybrid.dimensions import evaluate_dimension

    criteria = [
        {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1},
        {"criterion_id": "CRL5-C1", "dimension": "CRL", "level": 5},
        {"criterion_id": "CRL2-C1", "dimension": "CRL", "level": 2},
    ]
    dimension = evaluate_dimension(
        "CRL", "微玖", CRL_VIEW, criteria,
        {"CRL1-C1": _qualified_candidate()},
    )
    statuses = [c.product_status for c in dimension.criterion_results]
    assert statuses == ["succeeded", "method_unsupported", "method_unsupported"]
    assert not hasattr(dimension, "total_score")  # 无总分
