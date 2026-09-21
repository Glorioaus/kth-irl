"""R2-A：批准 registry 中 CRL 1–4 级的离线、受控复核求值。

本模块不调用旧 session、模型或 Provider。规则文字来自批准 wheel registry；
原 wheel 的 CRL 编译器由宿主提供 adjudication，因此这里的受控 findings 合同是
新运行层的明确离线实现，不宣称已证明与宿主 adjudication 的全部行为等价。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


RULE_VERSION = "kth-hybrid.crl.r2a.v1"

# 每项 requirements 都是从对应 registry 原文直接拆出的最小可审阅判断，不共享
# "关键词命中即通过" 路径。联系人的计数按去重后的受控身份处理。
RULE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "CRL1-C1": ("market_need_hypothesis",),
    "CRL1-C2": ("customer_problem_hypotheses",),
    "CRL1-C3": ("initial_market_customer_knowledge",),
    "CRL2-C1": ("secondary_market_research",),
    "CRL2-C2": ("market_customer_problem_alternative_familiarity",),
    "CRL2-C3": ("clear_problem_need_description",),
    "CRL3-C1": ("primary_feedback_contacts",),
    "CRL3-C2": ("customer_segments",),
    "CRL3-C3": ("hypothesis_updated_after_feedback",),
    "CRL4-C1": ("primary_feedback_contacts", "importance_confirmed"),
    "CRL4-C2": ("customer_profiles",),
    "CRL4-C3": ("user_payer_decider_roles",),
    "CRL4-C4": ("positioning_against_alternatives", "primary_feedback_contacts"),
}


def _controlled_review(review: dict[str, Any], criterion_id: str) -> tuple[bool, str]:
    required = {"review_id", "criterion_id", "claim_id", "decision", "findings",
                "reviewer", "review_basis", "support_scope"}
    if not isinstance(review, dict) or not required <= set(review):
        return False, "CRL复核记录结构不完整"
    if review["criterion_id"] != criterion_id:
        return False, "CRL复核记录不属于当前准则"
    if review["decision"] not in ("supports", "does_not_support"):
        return False, "CRL复核decision非法"
    if not isinstance(review["findings"], dict):
        return False, "CRL复核findings必须为对象"
    if not all(isinstance(review[field], str) and review[field].strip()
               for field in ("review_id", "claim_id", "reviewer", "review_basis",
                             "support_scope")):
        return False, "CRL复核缺少绑定身份或范围"
    return True, "ok"


def _contacts(findings_rows: list[dict[str, Any]]) -> set[str]:
    contacts: set[str] = set()
    for findings in findings_rows:
        values = findings.get("primary_feedback_contacts")
        if not isinstance(values, list):
            continue
        contacts.update(value for value in values if isinstance(value, str) and value.strip())
    return contacts


def _requirements_met(criterion_id: str, findings_rows: list[dict[str, Any]]) -> bool:
    if not findings_rows:
        return False
    merged_true = {
        key for findings in findings_rows
        for key, value in findings.items() if value is True
    }
    requirements = RULE_REQUIREMENTS[criterion_id]
    if "primary_feedback_contacts" in requirements:
        minimum = 2 if criterion_id == "CRL4-C1" else 1
        if len(_contacts(findings_rows)) < minimum:
            return False
    return all(
        requirement == "primary_feedback_contacts" or requirement in merged_true
        for requirement in requirements
    )


def _criterion_result(criterion: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    criterion_id = criterion["criterion_id"]
    valid: list[dict[str, Any]] = []
    invalid_reasons: list[str] = []
    for review in reviews:
        ok, reason = _controlled_review(review, criterion_id)
        if ok:
            valid.append(review)
        else:
            invalid_reasons.append(reason)
    supports = [review for review in valid if review["decision"] == "supports"]
    negatives = [review for review in valid if review["decision"] == "does_not_support"]
    review_refs = [review["review_id"] for review in valid]
    claim_refs = sorted({review["claim_id"] for review in valid})

    if supports and negatives:
        native = "partial"
        product = "succeeded"
        rationale = "存在受控支持与反对复核，保留原生 partial，不以计数裁决。"
    elif negatives:
        native = "not_met"
        product = "succeeded"
        rationale = "受控复核明确不支持该准则，产生原生 not_met。"
    elif _requirements_met(criterion_id, [review["findings"] for review in supports]):
        native = "met"
        product = "succeeded"
        rationale = "受控复核 findings 满足该准则的独立最小要求。"
    else:
        native = None
        product = "insufficient"
        rationale = "没有足够的受控、准则绑定 findings；保持产品 insufficient，不推定不存在。"
    if invalid_reasons:
        rationale += " 无效复核未消费：" + "；".join(sorted(set(invalid_reasons)))
    return {
        "criterion_id": criterion_id,
        "level": criterion["level"],
        "text": criterion["text"],
        "requirements": list(RULE_REQUIREMENTS[criterion_id]),
        "native_disposition": native,
        "product_status": product,
        "review_refs": review_refs,
        "claim_refs": claim_refs,
        "rationale": rationale,
        "rule_version": RULE_VERSION,
    }


def evaluate_crl_dimension(criteria: list[dict[str, Any]],
                           reviews: list[dict[str, Any]], *, scope: str) -> dict[str, Any]:
    """离线求值 CRL registry 的全部 13 条并执行逐级累计。

    ``reviews`` 必须来自受控 Case 流程；本纯函数仍会拒绝结构不完整、错准则的
    记录。输出把原生 disposition 与产品状态分离，且不会产生评分或业务决定。
    """
    expected = set(RULE_REQUIREMENTS)
    received = {row.get("criterion_id") for row in criteria if isinstance(row, dict)}
    if received != expected:
        missing = sorted(expected - received)
        extra = sorted(item for item in received - expected if item)
        raise ValueError(f"CRL R2-A 准则集合不完整/不一致：缺少={missing}，额外={extra}")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("CRL维度scope不能为空")
    by_criterion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for review in reviews:
        if isinstance(review, dict) and review.get("criterion_id") in expected:
            by_criterion[review["criterion_id"]].append(review)
    rows = [_criterion_result(row, by_criterion[row["criterion_id"]])
            for row in sorted(criteria, key=lambda item: (item["level"], item["criterion_id"]))]
    by_level: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_level[row["level"]].append(row)
    attained: int | None = None
    first_unmet: int | None = None
    for level in sorted(by_level):
        if all(row["native_disposition"] == "met" for row in by_level[level]):
            attained = level
            continue
        first_unmet = level
        break
    return {
        "dimension": "CRL",
        "scope": scope,
        "criteria": rows,
        "attained_level": attained,
        "first_unmet_level": first_unmet,
        "product_status": "succeeded" if first_unmet is None else "insufficient",
        "rule_version": RULE_VERSION,
        "method_boundary": "批准 wheel 当前仅支持CRL1–4；非官方KTH评估，不生成业务决定。",
    }
