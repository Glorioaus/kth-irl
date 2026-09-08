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

RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v3"

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


def check_na_legality(criterion: dict, proposal, case_flags: dict) -> tuple[bool, str]:
    """N/A 合法性 v3（R1.2-B）：

    - proposal 为结构化对象 ``{"proposal","basis","case_flag_source"}``，basis
      与 case_flag_source 必须非空（有源适用性依据，不接受布尔/文字放行）；
    - na_policy=never 一律拒绝；受限行要求对应 case flag 值为真**且该 flag 自身
      带来源**（``{"value": True, "source": 非空}``）；
    - N/A 同样必须先通过判据身份校验（由 dimensions 在身份检查后调用）。
    """
    if proposal is None:
        return True, "无 N/A 提案"
    if not isinstance(proposal, dict) or proposal.get("proposal") != "not_applicable":
        return False, f"N/A 提案结构非法：{proposal!r}（期望结构化对象）"
    if not str(proposal.get("basis") or "").strip():
        return False, "N/A 提案缺少适用性依据（basis）"
    if not str(proposal.get("case_flag_source") or "").strip():
        return False, "N/A 提案缺少 case flag 来源（case_flag_source）"
    policy = criterion.get("na_policy")
    if policy is None or policy == "never":
        return False, (
            f"判据 {criterion.get('criterion_id')} 的 na_policy="
            f"{policy or '（无，按 never 处理）'}，不允许 N/A"
        )
    if policy == "explicit_no_external_financing_only":
        flag = case_flags.get("explicit_no_external_financing")
        if isinstance(flag, dict) and flag.get("value") is True \
                and str(flag.get("source") or "").strip():
            return True, (
                f"判据 {criterion.get('criterion_id')} 为受限 N/A 行；Case 显式声明"
                f"不计划外部融资（flag 来源：{flag['source']}；适用性依据："
                f"{proposal['basis']}），N/A 合法"
            )
        return False, (
            f"判据 {criterion.get('criterion_id')} 为受限 N/A 行"
            "（explicit_no_external_financing_only），但 case flag 缺失/为假/无来源，"
            "N/A 非法"
        )
    return False, f"未知 na_policy：{policy}"


# ---- 判据主张映射（R1.2-B）----

# 保守语义过滤词族（透明、可复审；命中数与命中文段全部留痕。语义确认仍待
# 人工/方法审查，本过滤只是最低限度的实质检查，不是关键词替代业务判断）
_MARKET_HYPOTHESIS_TERMS = (
    "市场", "需求", "机会", "客户", "应用场景", "应用领域", "应用", "量产", "出货",
    "market", "need", "opportunity", "customer",
)


def interpret_claim_for_criterion(quote_text: str, criterion_id: str,
                                  sealed_excerpt: bytes,
                                  quote_start: int, quote_end: int) -> dict:
    """受控映射步骤：引文必须逐字位于封存摘录区间，语义过滤命中留痕。

    返回 {"status": mapped/unmapped, "filter_hits": [...], "quote_sha256",
    "basis"}。引文字节必须与封存摘录的 [quote_start, quote_end) 完全一致
    （防编造引文）；语义过滤是对引文（而非解释文字）的保守实质检查。
    """
    if not (isinstance(quote_start, int) and isinstance(quote_end, int)
            and 0 <= quote_start < quote_end <= len(sealed_excerpt)):
        return {"status": "unmapped",
                "filter_hits": [],
                "quote_sha256": None,
                "basis": f"映射引文区间非法 [{quote_start},{quote_end})（摘录长度 "
                         f"{len(sealed_excerpt)}）"}
    sealed_slice = sealed_excerpt[quote_start:quote_end]
    try:
        quote_bytes = quote_text.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": "映射引文不是有效文本"}
    if quote_bytes != sealed_slice:
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": "映射引文与封存摘录区间不一致（引文必须逐字来自封存原文）"}
    hits = [t for t in _MARKET_HYPOTHESIS_TERMS if t in (quote_text or "")]
    if not hits:
        return {"status": "unmapped", "filter_hits": [],
                "quote_sha256": None,
                "basis": "引文逐字核验通过，但未命中判据语义词族（"
                         f"{criterion_id} 需要'市场需求/问题/机会假设'类陈述；"
                         "语义确认待人工/方法审查）"}
    from ..contracts import sha256_hex as _sha

    return {"status": "mapped", "filter_hits": hits,
            "quote_sha256": _sha(quote_bytes),
            "quote_start": quote_start, "quote_end": quote_end,
            "basis": f"引文逐字位于封存摘录[{quote_start},{quote_end})，保守语义"
                     f"过滤命中 {hits}（留痕可复审；语义确认待方法审查）"}


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
