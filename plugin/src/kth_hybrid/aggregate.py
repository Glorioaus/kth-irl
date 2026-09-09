"""显式manifest驱动的六维离线候选索引；不选择latest或计算总分。"""
from __future__ import annotations

import json

from .audit import trace_crl_dimension, trace_dimension_result
from .contracts import sha256_hex

EXPECTED_DIMENSIONS = {"CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
MANIFEST_SCHEMA = "kth-hybrid.aggregation-manifest.v1"
VIEW_SCHEMA = "kth-hybrid.offline-six-dimension-view.v2"
_VIEW_FIELDS = {
    "schema_version", "status", "manifest_id", "manifest_digest",
    "case_basis_version", "scope", "dimensions", "evidence_licenses",
    "limitations", "view_id", "input_digest",
}
_DIMENSION_ENTRY_FIELDS = {
    "dimension_id", "result_id", "input_digest", "case_basis_version",
    "scope", "scope_id", "product_status", "attained_level",
    "catalog_sha256", "rule_version", "result_schema_version",
    "assessment_scope", "financing_entity", "trace_ok",
}
_LICENSE_FIELDS = {
    "license_id", "dimension_id", "result_id", "claim_id", "quote_sha256",
    "evidence_class", "subject_scope", "scope_id",
    "qualification_view_digest", "allowed_uses", "support_scope",
}


def _payload(blobs, row):
    return json.loads(blobs.read_bytes(row["result_blob_sha256"]).decode("utf-8"))


def _result_entry(case, blobs, dimension_id: str, result_id: str) -> tuple[dict, dict]:
    if dimension_id == "CRL":
        row = case.get_crl_dimension_result_by_id(result_id)
        trace = trace_crl_dimension(case, blobs, result_id)
        if row is None:
            raise ValueError(f"manifest指定CRL结果不存在：{result_id}")
        payload = _payload(blobs, row)
        frozen = payload.get("frozen_inputs") or {}
        entry = {
            "dimension_id": "CRL",
            "result_id": result_id,
            "input_digest": row["input_digest"],
            "case_basis_version": row["case_basis_version"],
            "scope": row["scope"],
            "scope_id": None,
            "product_status": payload.get("dimension", {}).get("product_status"),
            "attained_level": payload.get("dimension", {}).get("attained_level"),
            "catalog_sha256": frozen.get("catalog_sha256"),
            "rule_version": frozen.get("rule_version"),
            "result_schema_version": payload.get("schema_version"),
            "assessment_scope": None,
            "financing_entity": None,
            "trace_ok": trace.get("ok") is True,
        }
        return entry, payload
    row = case.get_dimension_result_by_id(result_id)
    if row is None or row.get("dimension_id") != dimension_id:
        raise ValueError(f"manifest指定{dimension_id}结果不存在或维度不符：{result_id}")
    trace = trace_dimension_result(case, blobs, result_id)
    payload = _payload(blobs, row)
    frozen = payload.get("frozen_inputs") or {}
    entry = {
        "dimension_id": dimension_id,
        "result_id": result_id,
        "input_digest": row["input_digest"],
        "case_basis_version": row["case_basis_version"],
        "scope": row["scope"],
        "scope_id": row["scope_id"],
        "product_status": payload.get("dimension", {}).get("product_status"),
        "attained_level": payload.get("dimension", {}).get("attained_level"),
        "catalog_sha256": frozen.get("catalog_sha256"),
        "rule_version": frozen.get("rule_version"),
        "result_schema_version": payload.get("schema_version"),
        "assessment_scope": frozen.get("assessment_scope"),
        "financing_entity": frozen.get("financing_entity"),
        "trace_ok": trace.get("ok") is True,
    }
    return entry, payload


def validate_dimension_index(rows, *, scope, case_basis_version,
                             expected_rule_versions=None):
    by_dimension = {}
    for row in rows:
        dimension = row.get("dimension_id") if isinstance(row, dict) else None
        if dimension in by_dimension:
            raise ValueError(f"维度重复：{dimension}")
        by_dimension[dimension] = row
    missing = EXPECTED_DIMENSIONS - set(by_dimension)
    extra = set(by_dimension) - EXPECTED_DIMENSIONS
    if missing or extra:
        raise ValueError(f"六维缺失或额外：缺失={sorted(missing)}，额外={sorted(extra)}")
    versions = {row.get("rule_version") for row in by_dimension.values()}
    if expected_rule_versions is None and len(versions) != 1:
        raise ValueError("六维方法版本混用且未提供逐维预期版本")
    for dimension, row in by_dimension.items():
        if row.get("scope") != scope:
            raise ValueError(f"{dimension} scope不一致")
        if row.get("case_basis_version") != case_basis_version:
            raise ValueError(f"{dimension} CaseBasis版本不一致")
        if row.get("trace_ok") is not True:
            raise ValueError(f"{dimension} trace未通过")
        for field in ("result_id", "input_digest", "catalog_sha256",
                      "rule_version", "result_schema_version"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"{dimension} manifest缺少{field}")
        if dimension in {"BRL", "TRL", "IPRL", "TMRL"}:
            unit = row.get("assessment_scope") or {}
            if unit.get("scope_id") != row.get("scope_id") \
                    or unit.get("subject_scope") != scope:
                raise ValueError(f"{dimension}评估单元与manifest scope_id不一致")
        if dimension == "FRL":
            entity = row.get("financing_entity") or {}
            if entity.get("financing_entity_id") != row.get("scope_id") \
                    or entity.get("subject_scope") != scope \
                    or not entity.get("assessment_unit_refs"):
                raise ValueError("FRL融资主体或共享评估单元与manifest不一致")
    if expected_rule_versions is not None:
        if set(expected_rule_versions) != EXPECTED_DIMENSIONS:
            raise ValueError("逐维预期方法版本集合不完整")
        for dimension, version in expected_rule_versions.items():
            if by_dimension[dimension]["rule_version"] != version:
                raise ValueError(f"{dimension}方法版本与manifest profile不一致")
    return by_dimension


def freeze_aggregation_manifest(case, blobs, result_ids):
    """从调用方显式给出的六个result_id冻结聚合manifest。"""
    if not isinstance(result_ids, dict) or set(result_ids) != EXPECTED_DIMENSIONS:
        raise ValueError("聚合manifest必须显式给出六个精确result_id")
    basis = case.get_case_basis()
    entries = {}
    payloads = {}
    for dimension in sorted(EXPECTED_DIMENSIONS):
        entry, payload = _result_entry(case, blobs, dimension, result_ids[dimension])
        entries[dimension] = entry
        payloads[dimension] = payload
    expected_versions = {dimension: entry["rule_version"]
                         for dimension, entry in entries.items()}
    validate_dimension_index(
        list(entries.values()), scope=basis["subject_legal_name"],
        case_basis_version=basis["version"],
        expected_rule_versions=expected_versions)
    body = {
        "schema_version": MANIFEST_SCHEMA,
        "case_basis_version": basis["version"],
        "scope": basis["subject_legal_name"],
        "expected_rule_versions": expected_versions,
        "dimensions": entries,
    }
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {**body, "manifest_id": f"AGGMAN::{digest}",
            "manifest_digest": digest}


def _validate_manifest_identity(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("aggregation manifest结构或schema非法")
    body = {key: value for key, value in manifest.items()
            if key not in {"manifest_id", "manifest_digest"}}
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if manifest.get("manifest_digest") != digest \
            or manifest.get("manifest_id") != f"AGGMAN::{digest}":
        raise ValueError("aggregation manifest身份摘要不一致")


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 \
        and all(character in "0123456789abcdef" for character in value)


def validate_offline_dimension_view(view):
    """严格重核view正文、六维结果引用及每条内容寻址证据许可。"""
    if not isinstance(view, dict) or set(view) != _VIEW_FIELDS \
            or view.get("schema_version") != VIEW_SCHEMA \
            or view.get("status") != "offline_candidate":
        raise ValueError("六维视图schema或字段集合非法")
    body = {key: value for key, value in view.items()
            if key not in {"view_id", "input_digest"}}
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if view.get("input_digest") != digest \
            or view.get("view_id") != f"OFFLINE6::{digest}":
        raise ValueError("六维视图正文摘要或view_id不一致")
    manifest_digest = view.get("manifest_digest")
    if not _is_sha256(manifest_digest) \
            or view.get("manifest_id") != f"AGGMAN::{manifest_digest}":
        raise ValueError("六维视图manifest ID/digest绑定非法")
    dimensions = view.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != EXPECTED_DIMENSIONS:
        raise ValueError("六维视图结果集合不完整")
    for dimension, entry in dimensions.items():
        if not isinstance(entry, dict) or set(entry) != _DIMENSION_ENTRY_FIELDS \
                or entry.get("dimension_id") != dimension \
                or entry.get("case_basis_version") != view.get("case_basis_version") \
                or entry.get("scope") != view.get("scope") \
                or not isinstance(entry.get("result_id"), str) \
                or not isinstance(entry.get("input_digest"), str) \
                or entry.get("trace_ok") is not True:
            raise ValueError(f"六维视图{dimension}结果引用结构非法")
    licenses = view.get("evidence_licenses")
    if not isinstance(licenses, dict):
        raise ValueError("六维视图evidence licenses结构非法")
    for license_id, license_value in licenses.items():
        if not isinstance(license_value, dict) \
                or set(license_value) != _LICENSE_FIELDS:
            raise ValueError("六维视图evidence license字段集合非法")
        license_body = {key: value for key, value in license_value.items()
                        if key != "license_id"}
        license_digest = sha256_hex(json.dumps(
            license_body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if license_value.get("license_id") != license_id \
                or license_id != f"EVIDUSE::{license_digest}":
            raise ValueError("六维视图evidence license正文摘要或ID不一致")
        dimension = license_value.get("dimension_id")
        entry = dimensions.get(dimension)
        if entry is None \
                or license_value.get("result_id") != entry.get("result_id") \
                or license_value.get("subject_scope") != view.get("scope") \
                or license_value.get("scope_id") != entry.get("scope_id") \
                or not isinstance(license_value.get("claim_id"), str) \
                or not license_value["claim_id"].strip() \
                or not _is_sha256(license_value.get("quote_sha256")) \
                or not _is_sha256(license_value.get("qualification_view_digest")) \
                or not isinstance(license_value.get("allowed_uses"), list) \
                or not isinstance(license_value.get("support_scope"), str) \
                or not license_value["support_scope"].strip():
            raise ValueError("六维视图evidence license结果、主张或scope绑定非法")
        evidence_class = license_value.get("evidence_class")
        if dimension == "CRL":
            if evidence_class is not None:
                raise ValueError("CRL evidence license不应伪造非CRL证据类别")
        elif not isinstance(evidence_class, str) or not evidence_class.strip():
            raise ValueError("非CRL evidence license缺少证据类别")
    return view


def _evidence_licenses(payloads):
    licenses = {}
    for dimension, payload in payloads.items():
        frozen = payload.get("frozen_inputs") or {}
        for binding in frozen.get("evidence_bindings") or []:
            review = binding.get("review") or {}
            view = binding.get("qualification_view") or {}
            claim = view.get("claim") or binding.get("claim") or {}
            body = {
                "dimension_id": dimension,
                "result_id": payload.get("result_id"),
                "claim_id": review.get("claim_id") or claim.get("claim_id"),
                "quote_sha256": review.get("quote_sha256") or claim.get("excerpt_sha256"),
                "evidence_class": review.get("evidence_class"),
                "subject_scope": claim.get("subject_scope"),
                "scope_id": frozen.get("scope_id"),
                "qualification_view_digest": view.get("input_digest"),
                "allowed_uses": (view.get("outcome") or {}).get("allowed_uses") or [],
                "support_scope": review.get("support_scope"),
            }
            digest = sha256_hex(json.dumps(
                body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            license_id = f"EVIDUSE::{digest}"
            licenses[license_id] = {"license_id": license_id, **body}
    return licenses


def build_offline_dimension_view(case, blobs, manifest):
    """只消费manifest指定结果；新增latest结果不改变旧manifest重放。"""
    _validate_manifest_identity(manifest)
    basis = case.get_case_basis_version(manifest["case_basis_version"])
    if basis is None or basis["subject_legal_name"] != manifest.get("scope"):
        raise ValueError("manifest CaseBasis版本或主体不存在")
    expected_versions = manifest.get("expected_rule_versions") or {}
    indexed = validate_dimension_index(
        list((manifest.get("dimensions") or {}).values()),
        scope=manifest["scope"], case_basis_version=manifest["case_basis_version"],
        expected_rule_versions=expected_versions)
    current_entries = {}
    payloads = {}
    for dimension, saved in indexed.items():
        current, payload = _result_entry(case, blobs, dimension, saved["result_id"])
        if current != saved:
            raise ValueError(f"{dimension}结果、评估单元或方法版本与manifest不一致")
        current_entries[dimension] = current
        payloads[dimension] = payload
    body = {
        "schema_version": VIEW_SCHEMA,
        "status": "offline_candidate",
        "manifest_id": manifest["manifest_id"],
        "manifest_digest": manifest["manifest_digest"],
        "case_basis_version": manifest["case_basis_version"],
        "scope": manifest["scope"],
        "dimensions": current_entries,
        "evidence_licenses": _evidence_licenses(payloads),
        "limitations": ["非正式KTH评估", "非投资决定", "无跨维总分",
                        "材料不足不等于业务NO"],
    }
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    view = {**body, "view_id": f"OFFLINE6::{digest}", "input_digest": digest}
    validate_offline_dimension_view(view)
    return view
