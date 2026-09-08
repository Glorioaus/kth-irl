# -*- coding: utf-8 -*-
"""kernels v4 补丁：候选/确认/否定门控 + N/A 预解析合同。"""
import io

path = "src/kth_hybrid/kernels/__init__.py"
src = io.open(path, encoding="utf-8").read()

src = src.replace('RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v3"',
                  'RULE_VERSION = "kth-hybrid.kernels.r1-narrow.v4"')

marker_old = 'def interpret_claim_for_criterion('
start = src.index(marker_old)
import re
m = re.search(r"\n(?=def check_na_legality)", src[start:])
end = start + m.start()
old_block = src[start:end]

new_block = '''def _negation_present(*texts):
    """R1.3-B：否定标识——引文/解释含明确否定已识别假设的表述时不得确认。"""
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

    返回 status ∈ unmapped / candidate（经 confirm_mapping 后 confirmed）。
    引文字节必须与封存摘录的 [quote_start, quote_end) 完全一致（防编造引文）。
    """
    from ..contracts import sha256_hex as _sha

    if not (isinstance(quote_start, int) and isinstance(quote_end, int)
            and 0 <= quote_start < quote_end <= len(sealed_excerpt)):
        return {"status": "unmapped", "filter_hits": [], "quote_sha256": None,
                "basis": "映射引文区间非法 [%s,%s)（摘录长度 %s）"
                         % (quote_start, quote_end, len(sealed_excerpt))}
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
                "basis": "引文含明确否定表述（%s）：该来源陈述的是『尚未识别"
                         "假设』，不能作为已识别假设的支持关系" % negation}
    hits = [t for t in _MARKET_HYPOTHESIS_TERMS if t in (quote_text or "")]
    if not hits:
        return {"status": "unmapped", "filter_hits": [],
                "quote_sha256": quote_sha,
                "quote_start": quote_start, "quote_end": quote_end,
                "basis": "引文逐字核验通过，但未命中判据语义词族（%s 需要"
                         "'市场需求/问题/机会假设'类陈述）" % criterion_id}
    return {"status": "candidate", "filter_hits": hits,
            "quote_sha256": quote_sha,
            "quote_start": quote_start, "quote_end": quote_end,
            "basis": "引文逐字位于封存摘录[%s,%s)，语义词族命中 %s（仅候选发现；"
                     "需留痕语义确认方可成为支持关系）"
                     % (quote_start, quote_end, hits)}


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
        out["basis"] += "；语义确认被否定门控拒绝（%s）" % marker
        return out
    if not isinstance(confirmation, dict) \\
            or confirmation.get("confirmed") is not True \\
            or not str(confirmation.get("confirmator") or "").strip() \\
            or not str(confirmation.get("review_basis") or "").strip():
        return candidate
    out = dict(candidate)
    out["status"] = "confirmed"
    out["confirmation"] = confirmation
    out["basis"] += "；语义确认：%s（%s）" % (confirmation["confirmator"],
                                             confirmation["review_basis"])
    return out

'''

src = src[:start] + new_block + src[end:]

# 否定词族挂在语义词族后
src = src.replace(
    '''_MARKET_HYPOTHESIS_TERMS = (''',
    '''_NEGATION_MARKERS = (
    "尚未识别", "未识别", "没有识别", "暂无任何", "不存在任何", "并无任何",
    "没有任何", "尚未发现", "未发现", "尚不存在", "并未识别", "未提出任何",
    "没有明确的", "尚未形成",
)

_MARKET_HYPOTHESIS_TERMS = (''', 1)

# check_na_legality v4：预解析证据
na_start = src.index("def check_na_legality(")
m2 = re.search(r"\n(?=# ---- 判据主张映射|\n\ndef interpret_claim)", src[na_start:])
if m2:
    na_end = na_start + m2.start()
else:
    na_end = len(src)
new_na = '''def check_na_legality(criterion, proposal, case_flags):
    """N/A 合法性 v4（R1.3-B）：预解析封存证据合同。

    - proposal 为 ``{"proposal", "applicability_ref", "flag_ref"}``，两个引用
      必须由 runner **预解析**为封存对象字段（applicability_resolved /
      flag_resolved 带 ``_resolved`` 与实际值）；非空字符串不接受；
    - na_policy=never 一律拒绝；受限行要求解析后的 flag 值为真；
    - N/A 仍须先通过判据身份校验（由 dimensions 在身份检查后调用）。
    """
    if proposal is None:
        return True, "无 N/A 提案"
    if not isinstance(proposal, dict) or proposal.get("proposal") != "not_applicable":
        return False, "N/A 提案结构非法：%r（期望结构化对象）" % (proposal,)
    applicability = proposal.get("applicability_resolved")
    flag = proposal.get("flag_resolved")
    if not isinstance(applicability, dict) or not applicability.get("_resolved"):
        return False, "N/A 适用性依据未解析到封存对象字段（applicability_ref）"
    if not isinstance(flag, dict) or not flag.get("_resolved"):
        return False, "N/A case flag 来源未解析到封存对象字段（flag_ref）"
    policy = criterion.get("na_policy")
    if policy is None or policy == "never":
        return False, ("判据 %s 的 na_policy=%s，不允许 N/A"
                       % (criterion.get("criterion_id"),
                          policy or "（无，按 never 处理）"))
    if policy == "explicit_no_external_financing_only":
        flag_value = flag.get("value")
        if flag_value is True or (isinstance(flag_value, str)
                                  and flag_value.strip()):
            return True, ("判据 %s 为受限 N/A 行；适用性依据已解析（%s：%s）；"
                          "flag 已解析（%s：%r），N/A 合法"
                          % (criterion.get("criterion_id"),
                             applicability.get("path"),
                             str(applicability.get("value"))[:60],
                             flag.get("path"), flag_value))
        return False, ("判据 %s 为受限 N/A 行，但已解析的 flag 值非真（%r），"
                       "N/A 非法" % (criterion.get("criterion_id"), flag_value))
    return False, "未知 na_policy：%s" % policy
'''
src = src[:na_start] + new_na + src[na_end:]

io.open(path, "w", encoding="utf-8", newline="\n").write(src)
print("kernels v4 done")
