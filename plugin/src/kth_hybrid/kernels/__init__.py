"""R1 窄规则内核（kth-hybrid.kernels.r1-narrow.v1）。

**范围声明：**这里只包含 R1 垂直切片实际需要的最小业务规则，均以保守口径
写成显式表；完整六维逐判据规则对照属 T07/R2，不得把本文件当作完整 KTH 规则
实现。不重新发明 criterion、不篡改成熟度含义。

规则来源：
- 判据/级别/dispositions/na_policy：批准 wheel 六个 registry（catalog 机械提取）；
- CRL 1–4 级范围、CRL 无原生 insufficient：method-scope-v1 §2；
- FRL 受限 N/A（explicit_no_external_financing_only / never）：
  wheel FRL registry na_policy 字段；
- TMRL 身份叠加不得直接设定 readiness：wheel TMRL registry claim_boundary
  （identity_overlay_may_set_readiness=false）。
"""

from __future__ import annotations

RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v1"

# R1 允许的资格用途类 → 可支持的最高判据级别（保守：仅级别1的
# "假设/自述/第三方载明存在"类信息性判据；更高级别留给 T07 逐条规则对照）。
R1_MAX_LEVEL_BY_USE = {
    "company_self_statement": 1,
    "document_dated_statement": 1,
    "third_party_reported_fact": 1,
}

# 原生处置兼容表（用于拦截非法 native_disposition 提案）
CRL_NATIVE_DISPOSITIONS = ("met", "not_met", "partial", "not_applicable")
OTHER_NATIVE_DISPOSITIONS = ("met", "not_met", "partial", "not_applicable", "insufficient")


def use_class_supports_criterion(allowed_use: str, dimension: str,
                                 level: int) -> bool:
    """R1 窄规则：某资格用途类是否可支持该判据。"""
    return R1_MAX_LEVEL_BY_USE.get(allowed_use, 0) >= level


def check_na_legality(criterion: dict, proposal: str | None,
                      case_flags: dict) -> tuple[bool, str]:
    """N/A 合法性：na_policy=never 一律拒绝；受限行需显式无外部融资策略。

    返回 (是否合法, 依据)。criterion 取自 wheel registry 的判据行；
    proposal 为 'not_applicable' 或 None。
    """
    if proposal != "not_applicable":
        return True, "无 N/A 提案"
    policy = criterion.get("na_policy")
    if policy is None or policy == "never":
        return False, (
            f"判据 {criterion.get('criterion_id')} 的 na_policy="
            f"{policy or '（无，按 never 处理）'}，不允许 N/A"
        )
    if policy == "explicit_no_external_financing_only":
        if case_flags.get("explicit_no_external_financing") is True:
            return True, (
                f"判据 {criterion.get('criterion_id')} 为受限 N/A 行，且 Case 显式"
                "声明不计划外部融资（explicit_no_external_financing=true），N/A 合法"
            )
        return False, (
            f"判据 {criterion.get('criterion_id')} 为受限 N/A 行"
            "（explicit_no_external_financing_only），但 Case 未显式声明"
            "不计划外部融资，N/A 非法"
        )
    return False, f"未知 na_policy：{policy}"


def check_native_disposition_legal(dimension: str, disposition: str) -> tuple[bool, str]:
    """拦截与原生合同不符的 native_disposition（如给 CRL 填 insufficient）。"""
    allowed = CRL_NATIVE_DISPOSITIONS if dimension == "CRL" else OTHER_NATIVE_DISPOSITIONS
    if disposition in allowed:
        return True, "合法"
    return False, (
        f"维度 {dimension} 原生 dispositions 不含 {disposition}"
        f"（CRL 无原生 insufficient，见 method-scope-v1）"
    )


def check_tmrl_identity_binding(criterion: dict, qualification_like: dict,
                                claim: dict) -> tuple[bool, str]:
    """TMRL 身份约束：身份不明/失败或主体范围含糊的共享证据不能证明
    team-specific 判据（identity overlay 不得直接设定 readiness）。"""
    dimension = (criterion.get("dimension") or "")
    if dimension != "TMRL":
        return True, "非 TMRL 判据，不受身份叠加约束"
    identity = qualification_like.get("identity_judgment", {})
    verdict = identity.get("verdict") if isinstance(identity, dict) else identity
    if verdict != "ok":
        return False, (
            f"TMRL 判据 {criterion.get('criterion_id')} 需要明确的主体身份判断；"
            f"当前身份判定为 {verdict or '缺失'}，共享证据不能设定团队判据"
        )
    scope = claim.get("subject_scope") or ""
    if not scope or scope in ("全部", "all", "公司整体"):
        return False, "主体范围含糊（subject_scope 未限定具体团队/角色）"
    return True, f"身份判定 ok 且范围限定（{scope}）"
