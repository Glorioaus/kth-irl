"""显式manifest驱动的六维离线候选索引；不选择latest或计算总分。"""
from __future__ import annotations

import json

from .audit import trace_crl_dimension, trace_dimension_result
from .contracts import sha256_hex

EXPECTED_DIMENSIONS = {"CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
MANIFEST_SCHEMA = "kth-hybrid.aggregation-manifest.v1"
VIEW_SCHEMA = "kth-hybrid.offline-six-dimension-view.v2"


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
    return {**body, "view_id": f"OFFLINE6::{digest}", "input_digest": digest}
