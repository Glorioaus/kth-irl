"""冻结六维视图上的离线角色合同，不调用模型或授予决定权限。"""
from __future__ import annotations
import copy

EXPECTED_DIMENSIONS = {"CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
FORBIDDEN_OUTPUT_KEYS = {
    "attained_level", "native_disposition", "final_decision",
    "investment_recommendation", "yes_no", "total_score",
}


def validate_role_attempt(attempt: dict, view: dict) -> dict:
    required = {
        "schema_version", "role", "producer_id", "context_id", "simulated",
        "input_view_id", "input_digest", "scope", "dimension_result_refs",
        "candidate_id", "statement", "evidence_refs", "limitations",
    }
    if not isinstance(attempt, dict) or not required <= set(attempt):
        raise ValueError("角色attempt结构不完整")
    forbidden = FORBIDDEN_OUTPUT_KEYS & set(attempt)
    if forbidden:
        raise ValueError(f"角色越权字段：{sorted(forbidden)}")
    if attempt["schema_version"] != "kth-hybrid.offline-role-attempt.v1" \
            or attempt["role"] not in {"PRO", "CON", "CHAIR"} \
            or attempt["simulated"] is not True:
        raise ValueError("仅接受明确标记的离线模拟角色attempt")
    if attempt["input_view_id"] != view.get("view_id") \
            or attempt["input_digest"] != view.get("input_digest") \
            or attempt["scope"] != view.get("scope"):
        raise ValueError("角色attempt与冻结六维视图身份不一致")
    expected_refs = {
        dimension: row["result_id"]
        for dimension, row in (view.get("dimensions") or {}).items()
    }
    if set(expected_refs) != EXPECTED_DIMENSIONS \
            or attempt["dimension_result_refs"] != expected_refs:
        raise ValueError("角色attempt遗漏或替换维度结果引用")
    if not all(isinstance(attempt[key], str) and attempt[key].strip()
               for key in ("producer_id", "context_id", "candidate_id", "statement")):
        raise ValueError("角色身份或候选文本为空")
    for key in ("evidence_refs", "limitations"):
        if not isinstance(attempt[key], list) or not attempt[key] \
                or any(not isinstance(item, str) or not item.strip()
                       for item in attempt[key]):
            raise ValueError(f"角色{key}必须为非空文本列表")
    return copy.deepcopy(attempt)


def assemble_offline_deliberation(view: dict, pro: dict, con: dict,
                                  rounds: list[dict], chair: dict, *,
                                  owner_selected_round_count: int) -> dict:
    pro = validate_role_attempt(pro, view)
    con = validate_role_attempt(con, view)
    chair = validate_role_attempt(chair, view)
    if pro["role"] != "PRO" or con["role"] != "CON" or chair["role"] != "CHAIR":
        raise ValueError("角色职责不匹配")
    if len({pro["producer_id"], con["producer_id"], chair["producer_id"]}) != 3 \
            or len({pro["context_id"], con["context_id"], chair["context_id"]}) != 3:
        raise ValueError("PRO/CON/CHAIR必须保持生产者与上下文独立")
    if owner_selected_round_count not in {1, 2, 3} \
            or not isinstance(rounds, list) \
            or len(rounds) != owner_selected_round_count:
        raise ValueError("离线辩论轮次与预选数量不一致")
    for number, row in enumerate(rounds, 1):
        if not isinstance(row, dict) or row.get("round_number") != number:
            raise ValueError("离线辩论轮次顺序错误")
        expected = (("pro_response", pro, con), ("con_response", con, pro))
        for key, actor, opponent in expected:
            response = row.get(key)
            if not isinstance(response, dict) \
                    or response.get("producer_id") != actor["producer_id"] \
                    or response.get("observed_candidate_id") != opponent["candidate_id"] \
                    or not isinstance(response.get("statement"), str) \
                    or not response["statement"].strip():
                raise ValueError("离线辩论回应未绑定正确对手或生产者")
    return {
        "schema_version": "kth-hybrid.offline-deliberation.v1",
        "status": "simulated_offline_candidate",
        "input_view_id": view["view_id"],
        "input_digest": view["input_digest"],
        "scope": view["scope"],
        "owner_selected_round_count": owner_selected_round_count,
        "pro": pro,
        "con": con,
        "rounds": copy.deepcopy(rounds),
        "chair": chair,
        "limitations": [
            "离线模拟不等于真实模型角色链",
            "角色不能写成熟度、原生处置或投资决定",
        ],
    }


def confirm_role_candidate(candidate: dict, confirmation: dict, *,
                           dimension_id: str) -> dict:
    """将角色候选适配为待落库review；确认本身不能生成准则处置。"""
    required = {
        "confirmation_id", "candidate_id", "decision", "reviewer",
        "review_basis", "criterion_id", "claim_id", "quote_sha256",
        "evidence_class", "scope_id", "subject_scope", "support_scope",
        "findings",
    }
    if not isinstance(confirmation, dict) or not required <= set(confirmation) \
            or confirmation.get("candidate_id") != candidate.get("candidate_id") \
            or confirmation.get("decision") != "confirmed" \
            or dimension_id not in EXPECTED_DIMENSIONS - {"CRL"}:
        raise ValueError("角色候选缺少匹配的受控确认")
    if not isinstance(confirmation["findings"], dict):
        raise ValueError("受控确认findings非法")
    return {
        "review_id": confirmation["confirmation_id"],
        "dimension_id": dimension_id,
        "case_basis_version": None,
        "claim_id": confirmation["claim_id"],
        "criterion_id": confirmation["criterion_id"],
        "quote_sha256": confirmation["quote_sha256"],
        "decision": "supports",
        "evidence_class": confirmation["evidence_class"],
        "findings": copy.deepcopy(confirmation["findings"]),
        "subject_scope": confirmation["subject_scope"],
        "scope_id": confirmation["scope_id"],
        "support_scope": confirmation["support_scope"],
        "reviewer": confirmation["reviewer"],
        "review_basis": confirmation["review_basis"],
        "candidate_ref": candidate["candidate_id"],
    }
