"""判据与维度评估入口（R1 垂直切片）。

``evaluate_criterion`` 消费资格判断结果，产出 CriterionResult（产品状态与
原生处置分列）；``evaluate_dimension`` 聚合为 DimensionResult 或执行错误。
角色候选没有写 Source / 最终成熟度的权限：本模块只读取冻结视图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .kernels import (
    RULE_VERSION,
    check_na_legality,
    check_native_disposition_legal,
    check_tmrl_identity_binding,
    use_class_supports_criterion,
)

_PRODUCT_STATUSES = ("succeeded", "insufficient", "method_unsupported", "execution_failed")


class EvaluationError(RuntimeError):
    """执行失败：输入残缺/非法。不产生业务值，不得映射为业务 NO。"""


@dataclass
class CriterionEvaluation:
    criterion_id: str
    dimension: str
    native_disposition: str | None = None
    native_note: str = ""
    product_status: str = "insufficient"
    evidence_refs: list[str] = field(default_factory=list)
    gap_refs: list[str] = field(default_factory=list)
    qual_refs: list[str] = field(default_factory=list)
    rationale: str = ""
    scope: str = ""
    rule_version: str = RULE_VERSION
    notes: list[str] = field(default_factory=list)


def evaluate_criterion(criterion: dict, judgment_candidate: dict,
                       evidence_view: dict) -> CriterionEvaluation:
    """评估单条判据。

    ``criterion``：catalog 判据行（含 dimension/level/na_policy）。
    ``judgment_candidate``：{"qualifications": [QualificationOutcome 或已存行],
    "claims": {claim_id: claim 行}, "na_proposal": str|None,
    "native_proposal": str|None}。
    ``evidence_view``：{"case_flags": {...}, "dimension_levels_supported": [..],
    "scope": str}。
    """
    dimension = criterion.get("dimension") or ""
    level = criterion.get("level") or 0
    evaluation = CriterionEvaluation(
        criterion_id=criterion.get("criterion_id", "?"), dimension=dimension
    )
    evaluation.scope = evidence_view.get("scope", "")

    # 原生处置提案合法性（如给 CRL 提 insufficient → 拒绝该提案并登记）
    native_proposal = judgment_candidate.get("native_proposal")
    if native_proposal is not None:
        legal, basis = check_native_disposition_legal(dimension, native_proposal)
        if legal:
            evaluation.native_disposition = native_proposal
            evaluation.native_note = "R1 记录提案值；未调用原版 vertical 复算"
        else:
            evaluation.notes.append(f"原生处置提案被拒：{basis}")

    # 方法范围：级别超出 registry 支持范围（CRL 5–9 等）
    levels_supported = evidence_view.get("dimension_levels_supported") or []
    if levels_supported and level not in levels_supported:
        evaluation.product_status = "method_unsupported"
        evaluation.rationale = (
            f"判据级别 {level} 超出批准 wheel {dimension} registry 支持范围 "
            f"{levels_supported}（CRL 仅 1–4；范围裁定包属 R2）。不伪报完整"
            f"{dimension}，也不因此给业务 NO。"
        )
        return evaluation

    qualifications = judgment_candidate.get("qualifications") or []
    claims = judgment_candidate.get("claims") or {}

    # N/A 提案合法性
    na_legal, na_basis = check_na_legality(
        criterion, judgment_candidate.get("na_proposal"),
        evidence_view.get("case_flags") or {},
    )
    if not na_legal:
        evaluation.notes.append(f"N/A 提案被拒：{na_basis}")
    elif judgment_candidate.get("na_proposal") == "not_applicable":
        evaluation.product_status = "succeeded"
        evaluation.rationale = f"受限 N/A 合法成立：{na_basis}。产品状态为执行成功，" \
                               "原生处置仍为 null（未调用原版）。"
        return evaluation

    # 正向通道：合格窄主张 × R1 用途类规则
    supporting: list[Any] = []
    rejected_reasons: list[str] = []
    for qual in qualifications:
        if isinstance(qual, dict):
            status = qual.get("status")
            allowed_uses = qual.get("allowed_uses") or []
            claim = claims.get(qual.get("claim_id"), {})
            identity = qual.get("identity_judgment", {})
        else:
            status = qual.status
            allowed_uses = qual.allowed_uses
            claim = claims.get(qual.claim_id, {})
            identity = {"verdict": qual.identity_judgment.verdict}
        if status != "qualified":
            rejected_reasons.append(
                f"{qual.get('claim_id') if isinstance(qual, dict) else qual.claim_id}:"
                f"资格状态 {status}"
            )
            continue
        # TMRL 身份叠加约束
        tmrl_ok, tmrl_basis = check_tmrl_identity_binding(
            {**criterion, "dimension": dimension}, identity, claim
        )
        if not tmrl_ok:
            rejected_reasons.append(
                f"{claim.get('claim_id', '?')}:TMRL 身份约束不满足（{tmrl_basis}）"
            )
            continue
        if any(use_class_supports_criterion(u, dimension, level)
               for u in allowed_uses):
            supporting.append(qual)
        else:
            rejected_reasons.append(
                f"{claim.get('claim_id', '?')}:用途类 {allowed_uses} 不覆盖级别 "
                f"{level}（R1 窄规则上限：级别 1 信息性判据）"
            )

    if supporting:
        evaluation.product_status = "succeeded"
        evaluation.qual_refs = [
            q.get("claim_id") if isinstance(q, dict) else q.claim_id for q in supporting
        ]
        evaluation.evidence_refs = [
            claims.get(
                q.get("claim_id") if isinstance(q, dict) else q.claim_id, {}
            ).get("source_id", "?") for q in supporting
        ]
        evaluation.rationale = (
            f"{len(supporting)} 条合格窄主张按 R1 窄规则支持该级别 {level} 信息性"
            "判据的证据可得性。**本结果不是原生 met 判定**：原生处置为 null"
            "（未调用原版 vertical），完整判据规则对照属 R2；不足与 met 的原生"
            "语义不得由本状态冒充。"
        )
        if rejected_reasons:
            evaluation.notes.extend(rejected_reasons)
        return evaluation

    evaluation.product_status = "insufficient"
    evaluation.gap_refs = judgment_candidate.get("gap_refs") or []
    detail = "；".join(rejected_reasons[:3]) if rejected_reasons else "无候选主张"
    gap_note = "；缺口记录见 gap 引用" if evaluation.gap_refs else ""
    evaluation.rationale = (
        f"证据不足（如实）：{detail}。R1 不为演示填 met{gap_note}。"
    )
    evaluation.notes.extend(rejected_reasons)
    return evaluation


@dataclass
class DimensionEvaluation:
    dimension: str
    scope: str
    criterion_results: list[CriterionEvaluation] = field(default_factory=list)
    execution_status: str = "succeeded"
    supported_levels: list[int] = field(default_factory=list)
    error: str | None = None


def evaluate_dimension(dimension: str, scope: str, evidence_view: dict,
                       criteria: list[dict],
                       judgment_candidates_by_criterion: dict[str, dict]) -> DimensionEvaluation:
    """聚合单维度：全部必需判据有结果或明确阻断；不产生总分。"""
    result = DimensionEvaluation(dimension=dimension, scope=scope)
    result.supported_levels = list(evidence_view.get("dimension_levels_supported") or [])
    try:
        for criterion in criteria:
            candidate = judgment_candidates_by_criterion.get(criterion["criterion_id"])
            if candidate is None:
                # 无候选也必须先过方法范围边界，再落"证据不足"
                candidate = {"qualifications": [], "claims": {}, "gap_refs": []}
            result.criterion_results.append(
                evaluate_criterion(criterion, candidate, evidence_view)
            )
    except EvaluationError as exc:
        result.execution_status = "failed"
        result.error = str(exc)
    return result
