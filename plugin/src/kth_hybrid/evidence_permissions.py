"""证据用途许可及其角色复核绑定；不从自由文本推断权限。"""

from __future__ import annotations

import copy
import json

from .contracts import sha256_hex


LICENSE_SCHEMA_V2 = "kth-hybrid.evidence-use-license.v2"
PERMISSION_BINDING_SCHEMA = \
    "kth-hybrid.dimension-review-permission-binding.v1"
LEGACY_LICENSE_FIELDS = {
    "license_id", "dimension_id", "result_id", "claim_id", "quote_sha256",
    "evidence_class", "subject_scope", "scope_id",
    "qualification_view_digest", "allowed_uses", "support_scope",
}
LICENSE_FIELDS_V2 = {
    "schema_version", "license_id", "dimension_id", "result_id",
    "source_review_id", "source_review", "criterion_id", "criterion",
    "claim_id", "quote_sha256", "evidence_class", "subject_scope",
    "scope_id", "qualification_view_digest", "qualification_view",
    "allowed_uses", "allowed_criterion_uses", "support_scope",
}
PERMISSION_TARGET_FIELDS = {
    "license_id", "requested_use", "dimension_id", "criterion_id",
    "claim_id", "quote_sha256", "evidence_class", "subject_scope",
    "scope_id", "support_scope", "findings",
}


def _digest(value) -> str:
    return sha256_hex(json.dumps(
        value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _role_candidate_digest(value: dict) -> str:
    """按角色合同的紧凑 JSON 规则重算候选摘要。"""
    body = {key: item for key, item in value.items()
            if key != "candidate_digest"}
    return sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"))


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 \
        and all(character in "0123456789abcdef" for character in value)


def _qualification_view_digest(view: dict) -> str:
    return _digest({key: value for key, value in view.items()
                    if key != "input_digest"})


def build_evidence_use_license(*, dimension_id: str, result_id: str,
                               binding: dict, criterion: dict,
                               scope_id) -> dict:
    """从维度冻结的review、criterion和资格视图生成v2许可。"""
    review = copy.deepcopy(binding.get("review") or {})
    qualification_view = copy.deepcopy(
        binding.get("qualification_view") or {})
    qualification_outcome = qualification_view.get("outcome") or {}
    allowed_uses = copy.deepcopy(qualification_outcome.get("allowed_uses"))
    criterion_id = review.get("criterion_id")
    claim = qualification_view.get("claim") or {}
    if not isinstance(criterion_id, str) or not criterion_id.strip() \
            or criterion.get("criterion_id") != criterion_id:
        raise ValueError("证据许可缺少精确criterion绑定")
    if not isinstance(allowed_uses, list) or not allowed_uses \
            or any(not isinstance(value, str) or not value.strip()
                   for value in allowed_uses) \
            or len(allowed_uses) != len(set(allowed_uses)):
        raise ValueError("证据许可缺少原资格allowed_uses")
    support_scope = review.get("support_scope")
    if not isinstance(support_scope, str) or not support_scope.strip():
        raise ValueError("证据许可support_scope不能为空")
    body = {
        "schema_version": LICENSE_SCHEMA_V2,
        "dimension_id": dimension_id,
        "result_id": result_id,
        "source_review_id": review.get("review_id"),
        "source_review": review,
        "criterion_id": criterion_id,
        "criterion": copy.deepcopy(criterion),
        "claim_id": review.get("claim_id"),
        "quote_sha256": review.get("quote_sha256"),
        "evidence_class": review.get("evidence_class"),
        "subject_scope": review.get("subject_scope") or claim.get("subject_scope"),
        "scope_id": scope_id,
        "qualification_view_digest": qualification_view.get("input_digest"),
        "qualification_view": qualification_view,
        "allowed_uses": allowed_uses,
        "allowed_criterion_uses": {criterion_id: copy.deepcopy(allowed_uses)},
        "support_scope": support_scope,
    }
    digest = _digest(body)
    return {**body, "license_id": f"EVIDUSE::{digest}"}


def validate_evidence_use_license(license_id: str, license_value: dict,
                                  *, view: dict | None = None) -> str:
    """核验许可正文；返回``v2``或``legacy``。"""
    if not isinstance(license_value, dict):
        raise ValueError("evidence license必须是完整对象")
    fields = set(license_value)
    if fields == LEGACY_LICENSE_FIELDS:
        body = {key: value for key, value in license_value.items()
                if key != "license_id"}
        if license_value.get("license_id") != license_id \
                or license_id != f"EVIDUSE::{_digest(body)}":
            raise ValueError("旧evidence license正文摘要或ID不一致")
        return "legacy"
    if fields != LICENSE_FIELDS_V2 \
            or license_value.get("schema_version") != LICENSE_SCHEMA_V2:
        raise ValueError("evidence license schema或字段集合非法")
    body = {key: value for key, value in license_value.items()
            if key != "license_id"}
    if license_value.get("license_id") != license_id \
            or license_id != f"EVIDUSE::{_digest(body)}":
        raise ValueError("evidence license正文摘要或ID不一致")

    review = license_value.get("source_review")
    criterion = license_value.get("criterion")
    qualification_view = license_value.get("qualification_view")
    allowed_uses = license_value.get("allowed_uses")
    criterion_id = license_value.get("criterion_id")
    if not isinstance(review, dict) \
            or review.get("review_id") != license_value.get("source_review_id") \
            or review.get("criterion_id") != criterion_id \
            or review.get("claim_id") != license_value.get("claim_id") \
            or review.get("quote_sha256") != license_value.get("quote_sha256") \
            or review.get("evidence_class") != license_value.get("evidence_class") \
            or review.get("support_scope") != license_value.get("support_scope"):
        raise ValueError("evidence license与原review绑定不一致")
    if not isinstance(criterion, dict) \
            or criterion.get("criterion_id") != criterion_id:
        raise ValueError("evidence license与criterion正文绑定不一致")
    if not isinstance(qualification_view, dict) \
            or qualification_view.get("input_digest") != \
            license_value.get("qualification_view_digest") \
            or _qualification_view_digest(qualification_view) != \
            qualification_view.get("input_digest"):
        raise ValueError("evidence license资格视图摘要不一致")
    claim = qualification_view.get("claim") or {}
    outcome = qualification_view.get("outcome") or {}
    if claim.get("claim_id") != license_value.get("claim_id") \
            or claim.get("excerpt_sha256") != license_value.get("quote_sha256") \
            or claim.get("subject_scope") != license_value.get("subject_scope"):
        raise ValueError("evidence license与资格视图Claim不一致")
    if not isinstance(allowed_uses, list) or not allowed_uses \
            or allowed_uses != outcome.get("allowed_uses") \
            or license_value.get("allowed_criterion_uses") != {
                criterion_id: allowed_uses}:
        raise ValueError("evidence license的allowed_criterion_uses非法")
    if not isinstance(license_value.get("support_scope"), str) \
            or not license_value["support_scope"].strip():
        raise ValueError("evidence license support_scope不能为空")
    if not _is_sha256(license_value.get("quote_sha256")) \
            or not _is_sha256(license_value.get("qualification_view_digest")):
        raise ValueError("evidence license hash字段非法")
    if view is not None:
        dimension = license_value.get("dimension_id")
        entry = (view.get("dimensions") or {}).get(dimension)
        if entry is None \
                or license_value.get("result_id") != entry.get("result_id") \
                or license_value.get("subject_scope") != view.get("scope") \
                or license_value.get("scope_id") != entry.get("scope_id"):
            raise ValueError("evidence license与六维view结果或scope不一致")
    return "v2"


def validate_permission_target(target: dict, licenses: dict,
                               evidence_refs: list[str]) -> dict:
    """只按目标点名的单一许可核对，不跨许可拼接字段。"""
    if not isinstance(target, dict) or set(target) != PERMISSION_TARGET_FIELDS:
        raise ValueError("角色候选review_target v3结构非法")
    license_id = target.get("license_id")
    if license_id not in evidence_refs or license_id not in licenses:
        raise ValueError("角色候选目标license_id不在证据引用中")
    license_value = licenses[license_id]
    if validate_evidence_use_license(license_id, license_value) != "v2":
        raise ValueError("legacy_restricted：旧许可不能用于review_target或confirmation")
    requested_use = target.get("requested_use")
    allowed = (license_value.get("allowed_criterion_uses") or {}).get(
        target.get("criterion_id"))
    if not isinstance(requested_use, str) or not requested_use.strip() \
            or not isinstance(allowed, list) or requested_use not in allowed:
        raise ValueError("requested_use不在该criterion的资格许可用途内")
    for field in (
            "dimension_id", "criterion_id", "claim_id", "quote_sha256",
            "evidence_class", "subject_scope", "scope_id", "support_scope"):
        if target.get(field) != license_value.get(field):
            raise ValueError(f"角色候选{field}与指定许可不一致")
    if not isinstance(target.get("findings"), dict):
        raise ValueError("角色候选findings非法")
    return copy.deepcopy(license_value)


def build_permission_binding(*, review_id: str, license_value: dict,
                             requested_use: str, candidate: dict,
                             confirmation: dict) -> dict:
    validate_evidence_use_license(
        license_value.get("license_id"), license_value)
    body = {
        "schema_version": PERMISSION_BINDING_SCHEMA,
        "review_id": review_id,
        "license_id": license_value["license_id"],
        "requested_use": requested_use,
        "candidate": copy.deepcopy(candidate),
        "candidate_digest": candidate.get("candidate_digest"),
        "confirmation": copy.deepcopy(confirmation),
        "confirmation_digest": _digest(confirmation),
        "license": copy.deepcopy(license_value),
    }
    return {**body, "binding_digest": _digest(body)}


def validate_permission_binding(binding: dict, *, review_id: str) -> dict:
    if not isinstance(binding, dict):
        raise ValueError("permission sidecar必须是完整对象")
    body = {key: value for key, value in binding.items()
            if key != "binding_digest"}
    if binding.get("schema_version") != PERMISSION_BINDING_SCHEMA \
            or binding.get("review_id") != review_id \
            or binding.get("binding_digest") != _digest(body):
        raise ValueError("permission sidecar身份或摘要不一致")
    candidate = binding.get("candidate") or {}
    confirmation = binding.get("confirmation") or {}
    if candidate.get("candidate_digest") != _role_candidate_digest(candidate) \
            or binding.get("candidate_digest") != candidate.get("candidate_digest"):
        raise ValueError("permission sidecar候选摘要不一致")
    if binding.get("confirmation_digest") != _digest(confirmation) \
            or confirmation.get("confirmation_id") != review_id:
        raise ValueError("permission sidecar confirmation摘要或身份不一致")
    license_value = binding.get("license") or {}
    if binding.get("license_id") != license_value.get("license_id") \
            or validate_evidence_use_license(
                binding.get("license_id"), license_value) != "v2":
        raise ValueError("permission sidecar许可不一致")
    target = candidate.get("review_target") or {}
    if target.get("license_id") != binding.get("license_id") \
            or target.get("requested_use") != binding.get("requested_use") \
            or any(confirmation.get(field) != target.get(field)
                   for field in PERMISSION_TARGET_FIELDS):
        raise ValueError("permission sidecar候选、确认与许可用途不一致")
    return copy.deepcopy(binding)
