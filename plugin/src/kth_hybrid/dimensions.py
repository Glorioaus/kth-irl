"""判据与维度评估入口 v2（R1 垂直切片，R1.1 修复验收 R1-02）。

- 角色的 ``native_proposal`` 只作为**候选**记录在 notes，永不写入最终
  ``native_disposition``；没有真正调用原版求值时保持 null。
- 判据身份必须经 ``evidence_view['approved_criterion_ids']`` 校验；未登记
  判据 → ``method_unsupported``（未知判据），不得 succeeded。
- 仅 ``kernels.IMPLEMENTED_RULES`` 中的判据（带出处）可正向消费；已登记但
  R1.1 未实现规则的判据 → ``method_unsupported``（不是"证据不足"）。
- 产品状态与原生处置分列；不产生总分；角色候选没有写 Source/最终成熟度的
  权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .kernels import (
    IMPLEMENTED_RULES,
    RULE_VERSION,
    check_na_legality,
    check_native_disposition_legal,
    check_tmrl_identity_binding,
    implemented_criterion,
)


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
    """评估单条判据（v3）。

    ``judgment_candidate``：{"qualifications": [...], "claims": {id: row},
    "na_proposal": {"proposal","basis","case_flag_source"}|None,
    "native_proposal": str|None, "gap_refs": [...],
    "criterion_mapping": {quote,start,end,...}|None}。
    ``evidence_view``：{"case_flags", "dimension_levels_supported", "scope",
    "approved_criterion_ids", "catalog_criterion"（可信catalog中的规范判据）}。
    """
    dimension = criterion.get("dimension") or ""
    level = criterion.get("level") or 0
    criterion_id = criterion.get("criterion_id", "?")
    evaluation = CriterionEvaluation(criterion_id=criterion_id, dimension=dimension)
    evaluation.scope = evidence_view.get("scope", "")

    # 原生处置提案：只作候选记录，永不写入最终 native_disposition
    native_proposal = judgment_candidate.get("native_proposal")
    if native_proposal is not None:
        legal, basis = check_native_disposition_legal(dimension, native_proposal)
        evaluation.notes.append(
            f"角色原生提案 {native_proposal!r} 仅作候选记录"
            f"（{'与原生合同相容，但' if legal else basis + '，且'}R1 未调用原版"
            "求值，不写入最终原生处置）"
        )

    # 判据身份：未在批准 Registry 登记集合 → 拒绝
    approved = evidence_view.get("approved_criterion_ids")
    if approved is not None and criterion_id not in approved:
        evaluation.product_status = "method_unsupported"
        evaluation.rationale = (
            f"判据 {criterion_id} 不在批准 wheel Registry 登记集合中，"
            "拒绝评估（不产生业务值）"
        )
        return evaluation

    # 判据身份完整性（R1.2-B）：与可信 catalog 规范判据逐字段比对；
    # 同ID被改 dimension/level/text/na_policy → 拒绝（防调用方篡改）
    catalog_criterion = evidence_view.get("catalog_criterion")
    if catalog_criterion is not None:
        for field in ("dimension", "level", "text", "na_policy"):
            if criterion.get(field) != catalog_criterion.get(field) \
                    and not (criterion.get(field) is None
                             and catalog_criterion.get(field) is None):
                evaluation.product_status = "method_unsupported"
                evaluation.rationale = (
                    f"判据身份不符：{criterion_id} 的 {field} 与可信 catalog 不一致"
                    f"（输入 {criterion.get(field)!r} vs catalog "
                    f"{catalog_criterion.get(field)!r}），拒绝评估"
                )
                return evaluation

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

    # N/A 提案合法性（结构化+有源；na_policy 是 registry 机械字段）
    na_legal, na_basis = check_na_legality(
        criterion, judgment_candidate.get("na_proposal"),
        evidence_view.get("case_flags") or {},
    )
    if not na_legal:
        evaluation.notes.append(f"N/A 提案被拒：{na_basis}")
    elif isinstance(judgment_candidate.get("na_proposal"), dict) \
            and judgment_candidate["na_proposal"].get("proposal") == "not_applicable":
        evaluation.product_status = "succeeded"
        evaluation.rationale = (
            f"受限 N/A 合法成立：{na_basis}。产品状态为执行成功，原生处置仍为"
            " null（未调用原版）。"
        )
        proposal = judgment_candidate["na_proposal"]
        evaluation.notes.append(
            f"N/A 适用性依据：{proposal.get('basis')}（flag来源："
            f"{proposal.get('case_flag_source')}）"
        )
        return evaluation

    rule = implemented_criterion(criterion_id)
    if rule is None:
        evaluation.product_status = "method_unsupported"
        evaluation.rationale = (
            f"判据 {criterion_id} 已登记但 R1.1 未实现其规则"
            f"（已实现：{sorted(IMPLEMENTED_RULES)}）；"
            "规则未实现是方法范围事实，不是证据不足；逐判据规则对照属 R2/T07。"
        )
        return evaluation

    qualifications = judgment_candidate.get("qualifications") or []
    claims = judgment_candidate.get("claims") or {}

    # 正向通道（R1.2-B）：合格主张 × 用途交集 × **引文映射成立**（三层都要过；
    # 映射=引文逐字位于封存摘录+语义过滤命中，由 runner 预核后传入）
    mapping = judgment_candidate.get("criterion_mapping") or {}
    mapping_ok = mapping.get("status") == "mapped"
    supporting = []
    rejected_reasons = []
    for qual in qualifications:
        if isinstance(qual, dict):
            status = qual.get("status")
            allowed_uses = qual.get("allowed_uses") or []
            identity = qual.get("identity_judgment") or {}
            if not isinstance(identity, dict):
                identity = {"verdict": None}
            claim = claims.get(qual.get("claim_id"), {})
            claim_id = qual.get("claim_id")
        else:
            status = qual.status
            allowed_uses = qual.allowed_uses
            identity = {"verdict": qual.identity_judgment.verdict}
            claim = claims.get(qual.claim_id, {})
            claim_id = qual.claim_id
        if status != "qualified":
            rejected_reasons.append(f"{claim_id}:资格状态 {status}")
            continue
        tmrl_ok, tmrl_basis = check_tmrl_identity_binding(
            {"criterion_id": criterion_id, "dimension": dimension},
            identity, claim,
        )
        if not tmrl_ok:
            rejected_reasons.append(
                f"{claim.get('claim_id', claim_id)}:TMRL 身份约束不满足（{tmrl_basis}）"
            )
            continue
        if not set(allowed_uses) & set(rule["acceptable_uses"]):
            rejected_reasons.append(
                f"{claim_id}:用途 {allowed_uses} 不在已实现规则 {criterion_id} 的"
                f"可接受用途 {list(rule['acceptable_uses'])} 内"
            )
            continue
        if not mapping_ok:
            rejected_reasons.append(
                f"{claim_id}:判据映射不成立——{mapping.get('basis', '未提供映射')}；"
                f"{criterion_id} 需要'市场需求/问题/机会假设'类陈述的封存引文"
            )
            continue
        supporting.append((claim_id, claim, allowed_uses))

    if supporting:
        evaluation.product_status = "succeeded"
        evaluation.qual_refs = [cid for cid, _, _ in supporting]
        evaluation.evidence_refs = [c.get("source_id", "?") for _, c, _ in supporting]
        evaluation.rationale = (
            f"{len(supporting)} 条合格窄主张按已实现规则 {criterion_id}（出处："
            f"{rule['provenance']}）＋封存引文映射（{mapping.get('basis', '')}）"
            "支持该判据的证据可得性。**本结果不是原生 met 判定**：原生处置为 "
            "null（未调用原版 vertical），语义确认待人工/方法审查（R2）；不足与 "
            "met 的原生语义不得由本状态冒充。"
        )
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
                candidate = {"qualifications": [], "claims": {}, "gap_refs": []}
            result.criterion_results.append(
                evaluate_criterion(criterion, candidate, evidence_view)
            )
    except EvaluationError as exc:
        result.execution_status = "failed"
        result.error = str(exc)
    return result
