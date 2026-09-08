"""R1.1 窄规则内核（kth-hybrid.kernels.r1-narrow.v4）。

**范围声明：**R1 只实现**一条**有明确原版/获批规则出处的真实判据
（CRL1-C1）；其余已登记判据一律 ``method_unsupported``。完整 180 条逐判据
规则对照属 T07/R2。不重新发明 criterion、不篡改成熟度含义。

R1.3-B 变化：
- 关键词只做**候选发现**（candidate）；确认需 ``confirm_mapping`` 的留痕
  语义确认记录，且经**否定门控**（引文/解释含"尚未识别"类否定表述时
  拒绝确认，即使调用方提交确认）。
- N/A 合法性 v4：适用性依据与 case flag 必须由 runner 预解析为封存对象
  字段（``applicability_resolved``/``flag_resolved`` 带 ``_resolved``），
  不接受非空字符串。

规则出处：
- 判据文本/级别/dispositions/na_policy：批准 wheel 六个 registry
  （``kth_hybrid.catalog`` 机械提取）；
- CRL1-C1 语义出处：registry 文本 "A possible market need, problem or
  opportunity hypothesis has been identified."——最低级信息性判据：存在
  已被识别/陈述的市场需求或机会假设；支持范围仅为"**来源陈述该假设**"，
  不扩大为市场规模/产能/成熟度已被独立证实。
"""

from __future__ import annotations

RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v4"

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
            "支持范围仅为'来源陈述该假设'；不判定更高成熟度、不证明市场规模/"
            "产能/成熟度已被独立证实"
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

# 保守语义过滤词族（仅候选发现，R1.3-B：不能单独批准关系）
_MARKET_HYPOTHESIS_TERMS = (
    "市场", "需求", "机会", "客户", "应用场景", "应用领域", "应用", "量产", "出货",
    "market", "need", "opportunity", "customer",
)

# 否定标识（R1.3-B：明确否定已识别假设的表述 → 不可确认）
_NEGATION_MARKERS = (
    "尚未识别", "未识别", "没有识别", "暂无任何", "不存在任何", "并无任何",
    "没有任何", "尚未发现", "未发现", "尚不存在", "并未识别", "未提出任何",
    "没有明确的", "尚未形成",
)


def implemented_criterion(criterion_id: str) -> dict | None:
    """返回该判据的已实现规则；未实现返回 None（调用方按 method_unsupported 处理）。"""
    return IMPLEMENTED_RULES.get(criterion_id)


def check_na_legality(criterion: dict, proposal, case_flags: dict) -> tuple[bool, str]:
    """N/A 合法性 v4（R1.3-B）：预解析封存证据合同。

    - proposal 为 ``{"proposal", "applicability_ref", "flag_ref"}``，两个引用
      必须由 runner **预解析**为封存对象字段（``applicability_resolved`` /
      ``flag_resolved`` 带 ``_resolved`` 与实际值）；非空字符串不接受；
    - na_policy=never 一律拒绝；受限行要求解析后的 flag 值为真；
    - N/A 仍须先通过判据身份校验（由 dimensions 在身份检查后调用）。
    """
    if proposal is None:
        return True, "无 N/A 提案"
    if not isinstance(proposal, dict) or proposal.get("proposal") != "not_applicable":
        return False, f"N/A 提案结构非法：{proposal!r}（期望结构化对象）"
    applicability = proposal.get("applicability_resolved")
    flag = proposal.get("flag_resolved")
    if not isinstance(applicability, dict) or not applicability.get("_resolved"):
        return False, "N/A 适用性依据未解析到封存对象字段（applicability_ref）"
    if not isinstance(flag, dict) or not flag.get("_resolved"):
        return False, "N/A case flag 来源未解析到封存对象字段（flag_ref）"
    policy = criterion.get("na_policy")
    if policy is None or policy == "never":
        return False, (
            f"判据 {criterion.get('criterion_id')} 的 na_policy="
            f"{policy or '（无，按 never 处理）'}，不允许 N/A"
        )
    if policy == "explicit_no_external_financing_only":
        flag_value = flag.get("value")
        if flag_value is True or (isinstance(flag_value, str)
                                  and flag_value.strip()):
            return True, (
                f"判据 {criterion.get('criterion_id')} 为受限 N/A 行；适用性依据"
                f"已解析（{applicability.get('path')}："
                f"{str(applicability.get('value'))[:60]}）；"
                f"flag 已解析（{flag.get('path')}：{flag_value!r}），N/A 合法"
            )
        return False, (
            f"判据 {criterion.get('criterion_id')} 为受限 N/A 行，但已解析的"
            f"flag 值非真（{flag_value!r}），N/A 非法"
        )
    return False, f"未知 na_policy：{policy}"


# ---- 判据主张映射（R1.2-B 引文定位；R1.3-B 候选/确认分离）----

def _negation_present(*texts):
    """否定标识：任一文本含否定标记即返回该标记。"""
    for text in texts:
        if not text:
            continue
        for marker in _NEGATION_MARKERS:
            if marker in text:
                return marker
    return None


def interpret_claim_for_criterion(quote_text, criterion_id, sealed_excerpt,
                                  quote_start, quote_end):
    """受控映射步骤 v4：关键词只做**候选发现**，不单独批准关系。

    返回 status ∈ unmapped / candidate（经 ``confirm_mapping`` 后 confirmed）。
    引文字节必须与封存摘录的 [quote_start, quote_end) 完全一致（防编造引文）。
    """
    from ..contracts import sha256_hex as _sha

    if not (isinstance(quote_start, int) and isinstance(quote_end, int)
            and 0 <= quote_start < quote_end <= len(sealed_excerpt)):
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": f"映射引文区间非法 [{quote_start},{quote_end})"
                         f"（摘录长度 {len(sealed_excerpt)}）"}
    sealed_slice = sealed_excerpt[quote_start:quote_end]
    try:
        quote_bytes = quote_text.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": "映射引文不是有效文本"}
    if quote_bytes != sealed_slice:
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": "映射引文与封存摘录区间不一致（引文必须逐字来自封存原文）"}
    quote_sha = _sha(quote_bytes)
    negation = _negation_present(quote_text)
    if negation:
        return {"status": "unmapped", "filter_hits": [],
                "quote_sha256": quote_sha,
                "quote_start": quote_start, "quote_end": quote_end,
                "negation_marker": negation,
                "basis": f"引文含明确否定表述（{negation}）：该来源陈述的是"
                         "『尚未识别假设』，不能作为已识别假设的支持关系"}
    hits = [t for t in _MARKET_HYPOTHESIS_TERMS if t in (quote_text or "")]
    if not hits:
        return {"status": "unmapped", "filter_hits": [],
                "quote_sha256": quote_sha,
                "quote_start": quote_start, "quote_end": quote_end,
                "basis": "引文逐字核验通过，但未命中判据语义词族（"
                         f"{criterion_id} 需要'市场需求/问题/机会假设'类陈述）"}
    return {"status": "candidate", "filter_hits": hits,
            "quote_sha256": quote_sha,
            "quote_start": quote_start, "quote_end": quote_end,
            "basis": f"引文逐字位于封存摘录[{quote_start},{quote_end})，语义词族"
                     f"命中 {hits}（仅候选发现；需留痕语义确认方可成为支持关系）"}


def confirm_mapping(candidate, confirmation, interpretation=""):
    """语义确认步骤（R1.3-B）：留痕确认 + 否定门控。

    - 无确认/无效确认 → 保持 candidate（不伪装已自动判定）；
    - 引文或解释含否定表述 → 拒绝确认（unmapped_negated），即使调用方确认；
    - 有效确认 {confirmed: True, confirmator, review_basis} → confirmed。
    """
    if candidate.get("status") != "candidate":
        return candidate
    negation = _negation_present(interpretation, str(confirmation or ""))
    if candidate.get("negation_marker") or negation:
        marker = candidate.get("negation_marker") or negation
        out = dict(candidate)
        out["status"] = "unmapped_negated"
        out["basis"] += f"；语义确认被否定门控拒绝（{marker}）"
        return out
    if not isinstance(confirmation, dict) \
            or confirmation.get("confirmed") is not True \
            or not str(confirmation.get("confirmator") or "").strip() \
            or not str(confirmation.get("review_basis") or "").strip():
        return candidate
    out = dict(candidate)
    out["status"] = "confirmed"
    out["confirmation"] = confirmation
    out["basis"] += (f"；语义确认：{confirmation['confirmator']}"
                     f"（{confirmation['review_basis']}）")
    return out


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
    """TMRL 身份约束（v2 修复结构：``identity`` 为调用方已取出的判断 dict）。

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
