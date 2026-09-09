"""冻结六维视图上的离线角色v2合同；角色没有成熟度或决定权限。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata

EXPECTED_DIMENSIONS = {"CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
ATTEMPT_SCHEMA = "kth-hybrid.offline-role-attempt.v2"
CONFIRMATION_SCHEMA = "kth-hybrid.role-confirmation.v2"
FORBIDDEN_OUTPUT_KEYS = {
    "attained_level", "current_level", "readiness_level", "frl_level",
    "native_disposition", "final_decision", "investment_recommendation",
    "yes_no", "total_score", "weighted_score", "aggregate_score",
}
_BASE_ATTEMPT_FIELDS = {
    "schema_version", "role", "producer_id", "context_id", "simulated",
    "input_view_id", "input_digest", "scope", "dimension_result_refs",
    "candidate_id", "candidate_digest", "statement", "evidence_refs",
    "limitations",
}
_REVIEW_TARGET_FIELDS = {
    "dimension_id", "criterion_id", "claim_id", "quote_sha256",
    "evidence_class", "subject_scope", "scope_id", "support_scope", "findings",
}
_CONFIRMATION_FIELDS = {
    "schema_version", "confirmation_id", "candidate_id", "candidate_digest",
    "input_view_id", "input_digest", "producer_id", "role",
    "case_basis_version", "decision", "reviewer", "review_basis",
    "evidence_refs", *_REVIEW_TARGET_FIELDS,
}
_MAX_AUTHORITY_TEXT_LENGTH = 65536
_MAX_AUTHORITY_STRUCTURE_DEPTH = 16
_MAX_JSON_DECODE_LAYERS = 16
_FORBIDDEN_DECISION_KEY_TOKENS = {
    "finaldecision", "investmentrecommendation",
}
_FORBIDDEN_COUNSEL_AUTHORITY = re.compile(
    r"(?:\b(?:CRL|TRL|BRL|IPRL|TMRL|FRL)\s*(?:为|达到|=|:|：)\s*[1-9]\b|"
    r"(?:达到|评为|定为)\s*\b(?:CRL|TRL|BRL|IPRL|TMRL|FRL)\s*[1-9]\b|"
    r"(?:成熟度|当前等级|定级|评级).{0,10}[一二三四五六七八九1-9]\s*级|"
    r"(?:达到|评为|定为)[一二三四五六七八九1-9]\s*级|"
    r"值得投资|不值得投资|批准立项|拒绝立项|可投资|(?-i:\bYES\b|\bNO\b))",
    re.IGNORECASE,
)
_FORBIDDEN_DECISION_ASSIGNMENT = re.compile(
    r"(?:[\"']?\b(?:final[\s_-]*decision|investment[\s_-]*recommendation)"
    r"\b[\"']?)\s*(?::|=)\s*(?:[\"'][^\"']*[\"']|[^\s,;}\]]+)",
    re.IGNORECASE,
)


def _json_digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def role_candidate_digest(attempt: dict) -> str:
    return _json_digest({key: value for key, value in attempt.items()
                         if key != "candidate_digest"})


def _canonical_authority_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s_-]+", "", normalized)


def _scan_authority(value, path="root"):
    stack = [(value, path, 1, 0)]
    while stack:
        item, item_path, depth, json_layers = stack.pop()
        if isinstance(item, dict):
            if depth > _MAX_AUTHORITY_STRUCTURE_DEPTH:
                raise ValueError(
                    "角色验证结构深度超过16层，拒绝继续扫描")
            forbidden = FORBIDDEN_OUTPUT_KEYS & set(item)
            canonical_forbidden = {
                key for key in item if isinstance(key, str)
                and _canonical_authority_key(key)
                in _FORBIDDEN_DECISION_KEY_TOKENS
            }
            forbidden.update(canonical_forbidden)
            if forbidden:
                raise ValueError(
                    f"角色越权字段 {item_path}: {sorted(forbidden)}")
            for key, nested in item.items():
                stack.append((nested, f"{item_path}.{key}", depth + 1,
                              json_layers))
        elif isinstance(item, (list, tuple)):
            if depth > _MAX_AUTHORITY_STRUCTURE_DEPTH:
                raise ValueError(
                    "角色验证结构深度超过16层，拒绝继续扫描")
            for index, nested in enumerate(item):
                stack.append((nested, f"{item_path}[{index}]", depth + 1,
                              json_layers))
        elif isinstance(item, str):
            if len(item) > _MAX_AUTHORITY_TEXT_LENGTH:
                raise ValueError(
                    "角色验证文本长度超过65536字符，拒绝继续扫描")
            normalized = unicodedata.normalize("NFKC", item)
            if _FORBIDDEN_COUNSEL_AUTHORITY.search(normalized) \
                    or _FORBIDDEN_DECISION_ASSIGNMENT.search(normalized):
                raise ValueError(
                    f"角色越权文本 {item_path}：不得赋值成熟度或投资决定")
            stripped = normalized.strip()
            is_json_container = (
                stripped.startswith("{") and stripped.endswith("}")) \
                or (stripped.startswith("[") and stripped.endswith("]"))
            is_json_string = stripped.startswith('"') and stripped.endswith('"')
            if is_json_container or is_json_string:
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                except RecursionError as exc:
                    raise ValueError(
                        "角色验证结构深度超过16层，拒绝继续扫描") from exc
                if json_layers >= _MAX_JSON_DECODE_LAYERS:
                    raise ValueError(
                        "角色验证JSON解码层数超过16层，拒绝继续扫描")
                if isinstance(decoded, (dict, list)):
                    stack.append((decoded, f"{item_path}<json>", depth,
                                  json_layers + 1))
                elif isinstance(decoded, str):
                    if depth >= _MAX_AUTHORITY_STRUCTURE_DEPTH:
                        raise ValueError(
                            "角色验证结构深度超过16层，拒绝继续扫描")
                    stack.append((decoded, f"{item_path}<json-string>",
                                  depth + 1, json_layers + 1))


def _view_licenses(view):
    from .aggregate import validate_offline_dimension_view

    validate_offline_dimension_view(view)
    licenses = view.get("evidence_licenses")
    if not isinstance(licenses, dict):
        raise ValueError("冻结视图缺少内容绑定evidence-use licenses")
    for license_id, value in licenses.items():
        if not isinstance(value, dict) or value.get("license_id") != license_id:
            raise ValueError("冻结视图evidence license身份非法")
    return licenses


def _validate_review_target(target, licenses, evidence_refs, view):
    if not isinstance(target, dict) or set(target) != _REVIEW_TARGET_FIELDS:
        raise ValueError("角色候选review_target结构非法")
    if target["dimension_id"] not in EXPECTED_DIMENSIONS - {"CRL"} \
            or target["subject_scope"] != view.get("scope") \
            or not isinstance(target["findings"], dict):
        raise ValueError("角色候选review_target维度、主体或findings非法")
    matching = [licenses[ref] for ref in evidence_refs if ref in licenses and
                licenses[ref].get("dimension_id") == target["dimension_id"] and
                licenses[ref].get("claim_id") == target["claim_id"] and
                licenses[ref].get("quote_sha256") == target["quote_sha256"] and
                licenses[ref].get("evidence_class") == target["evidence_class"] and
                licenses[ref].get("subject_scope") == target["subject_scope"] and
                licenses[ref].get("scope_id") == target["scope_id"]]
    if not matching:
        raise ValueError("角色候选目标没有匹配的内容绑定证据许可")


def validate_role_attempt(attempt: dict, view: dict) -> dict:
    from .aggregate import validate_offline_dimension_view

    validate_offline_dimension_view(view)
    if not isinstance(attempt, dict):
        raise ValueError("角色候选必须是完整对象")
    _scan_authority(attempt)
    allowed = _BASE_ATTEMPT_FIELDS | ({"review_target"} if "review_target" in attempt else set())
    if set(attempt) != allowed:
        raise ValueError("角色候选schema字段不精确或含额外字段")
    if attempt["schema_version"] != ATTEMPT_SCHEMA \
            or attempt["role"] not in {"PRO", "CON", "CHAIR"} \
            or attempt["simulated"] is not True:
        raise ValueError("仅接受v2且明确标记的离线模拟角色候选")
    if attempt["input_view_id"] != view.get("view_id") \
            or attempt["input_digest"] != view.get("input_digest") \
            or attempt["scope"] != view.get("scope"):
        raise ValueError("角色候选与冻结六维视图身份不一致")
    expected_refs = {dimension: row["result_id"]
                     for dimension, row in (view.get("dimensions") or {}).items()}
    if set(expected_refs) != EXPECTED_DIMENSIONS \
            or attempt["dimension_result_refs"] != expected_refs:
        raise ValueError("角色候选遗漏或替换维度结果引用")
    if attempt.get("candidate_digest") != role_candidate_digest(attempt):
        raise ValueError("角色候选完整摘要不一致")
    if not all(isinstance(attempt[key], str) and attempt[key].strip()
               for key in ("producer_id", "context_id", "candidate_id", "statement")):
        raise ValueError("角色身份或候选文本为空")
    if not isinstance(attempt["limitations"], list) or not attempt["limitations"]:
        raise ValueError("角色limitations必须为非空列表")
    evidence_refs = attempt["evidence_refs"]
    licenses = _view_licenses(view)
    if not isinstance(evidence_refs, list) or not evidence_refs \
            or len(evidence_refs) != len(set(evidence_refs)) \
            or any(ref not in licenses for ref in evidence_refs):
        raise ValueError("角色证据引用不在冻结视图许可中")
    if "review_target" in attempt:
        _validate_review_target(attempt["review_target"], licenses,
                                evidence_refs, view)
    return copy.deepcopy(attempt)


def assemble_offline_deliberation(view: dict, pro: dict, con: dict,
                                  rounds: list[dict], chair: dict, *,
                                  owner_selected_round_count: int) -> dict:
    pro = validate_role_attempt(pro, view)
    con = validate_role_attempt(con, view)
    chair = validate_role_attempt(chair, view)
    if (pro["role"], con["role"], chair["role"]) != ("PRO", "CON", "CHAIR"):
        raise ValueError("角色职责不匹配")
    if len({pro["producer_id"], con["producer_id"], chair["producer_id"]}) != 3 \
            or len({pro["context_id"], con["context_id"], chair["context_id"]}) != 3:
        raise ValueError("PRO/CON/CHAIR必须保持生产者与上下文独立")
    if owner_selected_round_count not in {1, 2, 3} \
            or not isinstance(rounds, list) \
            or len(rounds) != owner_selected_round_count:
        raise ValueError("离线辩论轮次与预选数量不一致")
    parsed_rounds = []
    for number, row in enumerate(rounds, 1):
        if not isinstance(row, dict) or set(row) != {
                "round_number", "pro_response", "con_response"} \
                or row.get("round_number") != number:
            raise ValueError("离线辩论轮次结构或顺序错误")
        _scan_authority(row, f"rounds[{number - 1}]")
        parsed = {"round_number": number}
        for key, actor, opponent in (("pro_response", pro, con),
                                     ("con_response", con, pro)):
            response = row[key]
            if not isinstance(response, dict) or set(response) != {
                    "producer_id", "observed_candidate_id", "statement"} \
                    or response.get("producer_id") != actor["producer_id"] \
                    or response.get("observed_candidate_id") != opponent["candidate_id"] \
                    or not isinstance(response.get("statement"), str) \
                    or not response["statement"].strip():
                raise ValueError("离线辩论回应未绑定正确对手或生产者")
            parsed[key] = copy.deepcopy(response)
        parsed_rounds.append(parsed)
    return {"schema_version": "kth-hybrid.offline-deliberation.v2",
            "status": "simulated_offline_candidate",
            "input_view_id": view["view_id"], "input_digest": view["input_digest"],
            "scope": view["scope"],
            "owner_selected_round_count": owner_selected_round_count,
            "pro": pro, "con": con, "rounds": parsed_rounds, "chair": chair,
            "limitations": ["离线模拟不等于真实模型角色链",
                            "角色不能写成熟度、原生处置或投资决定"]}


def confirm_role_candidate(candidate: dict, confirmation: dict, *,
                           dimension_id: str, view: dict | None = None) -> dict:
    if view is None:
        raise ValueError("角色候选确认缺少冻结view，无法重核候选")
    validated = validate_role_attempt(candidate, view)
    if "review_target" not in validated:
        raise ValueError("角色候选没有可确认的准则目标")
    if not isinstance(confirmation, dict) or set(confirmation) != _CONFIRMATION_FIELDS \
            or confirmation.get("schema_version") != CONFIRMATION_SCHEMA:
        raise ValueError("角色confirmation schema不精确")
    _scan_authority(confirmation)
    target = validated["review_target"]
    identity_matches = (
        confirmation["candidate_id"] == validated["candidate_id"]
        and confirmation["candidate_digest"] == validated["candidate_digest"]
        and confirmation["input_view_id"] == view["view_id"]
        and confirmation["input_digest"] == view["input_digest"]
        and confirmation["producer_id"] == validated["producer_id"]
        and confirmation["role"] == validated["role"]
        and confirmation["case_basis_version"] == view["case_basis_version"]
        and confirmation["evidence_refs"] == validated["evidence_refs"]
        and dimension_id == target["dimension_id"]
        and all(confirmation[field] == target[field]
                for field in _REVIEW_TARGET_FIELDS)
    )
    if not identity_matches:
        raise ValueError("角色候选、view、生产者或确认目标绑定不一致")
    if confirmation["decision"] == "rejected":
        raise ValueError("人工复核拒绝该角色候选，不生成review")
    if confirmation["decision"] not in {"supports", "does_not_support"}:
        raise ValueError("人工确认decision非法")
    return {"review_id": confirmation["confirmation_id"],
            "dimension_id": dimension_id,
            "case_basis_version": view["case_basis_version"],
            "claim_id": confirmation["claim_id"],
            "criterion_id": confirmation["criterion_id"],
            "quote_sha256": confirmation["quote_sha256"],
            "decision": confirmation["decision"],
            "evidence_class": confirmation["evidence_class"],
            "findings": copy.deepcopy(confirmation["findings"]),
            "subject_scope": confirmation["subject_scope"],
            "scope_id": confirmation["scope_id"],
            "support_scope": confirmation["support_scope"],
            "reviewer": confirmation["reviewer"],
            "review_basis": confirmation["review_basis"],
            "candidate_ref": validated["candidate_id"],
            "candidate_digest": validated["candidate_digest"],
            "evidence_refs": copy.deepcopy(validated["evidence_refs"])}
