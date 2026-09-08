"""R1.1 窄规则内核（kth-hybrid.kernels.r1-narrow.v2）。

**范围声明：**R1.1 只实现**一条**有明确原版/获批规则出处的真实判据
（CRL1-C1）；其余已登记判据一律 ``method_unsupported``，不冒充"证据不足"，
也不凭"用途类＋级别"通用路由替代具体规则（验收 R1-02）。完整 180 条逐判据
规则对照属 T07/R2。不重新发明 criterion、不篡改成熟度含义。

规则出处：
- 判据文本/级别/dispositions/na_policy：批准 wheel 六个 registry
  （``kth_hybrid.catalog`` 机械提取）；
- CRL1-C1 语义出处：registry 文本 "A possible market need, problem or
  opportunity hypothesis has been identified."——最低级信息性判据：存在
  已被识别/陈述的市场需求或机会假设；接受"主体自述/文档载明/第三方载明"
  的合格窄主张作为该假设已被识别的证据。
"""

from __future__ import annotations

RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v2"

# R1.1 已实现的判据规则（显式、逐条、带出处；未列入者 → method_unsupported）
IMPLEMENTED_RULES: dict[str, dict] = {
    "CRL1-C1": {
        "registry_id": "KTH-CRL-G-2022-in-Compiled-F-2025",
        "registry_version": "crl-g-2022.internal-shadow.v2",
        "criterion_text": "A possible market need, problem or opportunity "
                          "hypothesis has been identified.",
        "rule_kind": "specific",
        "provenance": (
            "批准 wheel kth_irl.v1.crl_vertical.get_crl_registry() CRL1-C1；"
            "语义=最低级信息性判据（存在已识别/陈述的市场需求/问题/机会假设），"
            "合格窄主张的'假设已被陈述'证据可支持；不判定更高成熟度"
        ),
        "acceptable_uses": (
            "company_self_statement",
            "document_dated_statement",
            "third_party_reported_fact",
        ),
    },
}

# 原生处置兼容表（用于拦截非法 native_disposition 提案）
CRL_NATIVE_DISPOSITIONS = ("met", "not_met", "partial", "not_applicable")
OTHER_NATIVE_DISPOSITIONS = ("met", "not_met", "partial", "not_applicable", "insufficient")


def implemented_criterion(criterion_id: str) -> dict | None:
    """返回该判据的已实现规则；未实现返回 None（调用方按 method_unsupported 处理）。"""
    return IMPLEMENTED_RULES.get(criterion_id)


def check_na_legality(criterion: dict, proposal: str | None,
                      case_flags: dict) -> tuple[bool, str]:
    """N/A 合法性：na_policy=never 一律拒绝；受限行需显式无外部融资策略。"""
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


def check_tmrl_identity_binding(criterion: dict, identity: dict,
                                claim: dict) -> tuple[bool, str]:
    """TMRL 身份约束（v2 修复结构：``identity`` 为调用方已取出的判断 dict，
    形如 ``{"verdict": "ok", "basis": "…"}``；不再二次嵌套取
    ``identity_judgment``）。

    身份不明/失败或主体范围含糊的共享证据不能证明 team-specific 判据
    （identity overlay 不得直接设定 readiness）。
    """
    dimension = criterion.get("dimension") or ""
    if dimension != "TMRL":
        return True, "非 TMRL 判据，不受身份叠加约束"
    if not isinstance(identity, dict):
        return False, f"身份判断结构非法：{type(identity).__name__}（期望 dict）"
    verdict = identity.get("verdict")
    if verdict is None and isinstance(identity.get("identity_judgment"), dict):
        # 兼容旧结构（ QualificationOutcome 整体传入）
        verdict = identity["identity_judgment"].get("verdict")
    if verdict != "ok":
        return False, (
            f"TMRL 判据 {criterion.get('criterion_id')} 需要明确的主体身份判断；"
            f"当前身份判定为 {verdict or '缺失'}，共享证据不能设定团队判据"
        )
    scope = (claim.get("subject_scope") or "").strip()
    if not scope or scope in ("全部", "all", "公司整体"):
        return False, "主体范围含糊（subject_scope 未限定具体团队/角色）"
    return True, f"身份判定 ok 且范围限定（{scope}）"
