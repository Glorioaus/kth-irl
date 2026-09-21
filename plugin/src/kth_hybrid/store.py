"""可靠原件仓与 Case 记录库。

设计依据（v3 计划 T03 + 2026-09-08 首次 I/O 复核实测）：
- Windows 上目录级 ``os.replace`` 到已存在目标确定性失败（WinError 5，重试无效），
  文件级 replace 正常——因此提交机制是 **文件先持久化 + os.replace（文件级）+
  SQLite 事务提交引用**，不依赖目录整体替换。
- 同内容去重：内容寻址（sha256）决定对象身份；元数据/引用存 SQLite。
- ``read_bytes`` 重核内容，截断/篡改可见失败。
- 原件落盘与 DB 提交之间崩溃产生"孤立工件"，保留不删，可被识别。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
import tempfile
import uuid
from pathlib import Path
from threading import RLock

from .contracts import (
    CASE_STAGES,
    BlobRef,
    is_sha256_hex,
    qualification_content_digest,
    sha256_hex,
)


class BudgetRejected(RuntimeError):
    """Provider 用量预留/结算违反上界或状态合同；事务已回滚。"""


_SCHEMA_VERSION = "kth-hybrid.store.v13"
WORKFLOW_REVIEW_MATERIALIZATION_SCHEMA = "workflow_review_materialization.v1"
_WORKFLOW_REVIEW_MATERIALIZATION_BODY_FIELDS = {
    "schema_version", "job_id", "request_id", "request_input_digest",
    "request_schema_version", "response_id", "response_digest",
    "response_blob_sha256", "response_schema_version", "producer",
    "source_mode", "authorization_id", "authorization_digest",
    "case_basis_version", "case_basis_digest", "case_basis_proof_digest",
    "evaluation_inputs", "evaluation_input_proof_bindings", "profile",
    "method_versions", "dimension_id", "criterion_id", "claim_id",
    "quote_sha256", "decision", "evidence_class", "findings",
    "subject_scope", "scope_id", "support_scope", "reviewer",
    "review_basis",
}
_WORKFLOW_REVIEW_MATERIALIZATION_FIELDS = (
    _WORKFLOW_REVIEW_MATERIALIZATION_BODY_FIELDS
    | {"materialization_digest", "materialization_id", "review_id"}
)


def _strict_json_dumps(value, *, label: str) -> str:
    """按标准JSON拒绝NaN/Infinity，避免非有限数进入冻结业务输入。"""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}含非标准JSON值或非有限数值：{exc}") from exc


def _workflow_materialization_digest(value: dict) -> str:
    return sha256_hex(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8"))


def validate_workflow_review_materialization(
        item: dict, *, review_id: str | None = None) -> dict:
    """校验物化sidecar的内容身份，不把调用方正文当作信任根。"""
    if not isinstance(item, dict) or set(item) != \
            _WORKFLOW_REVIEW_MATERIALIZATION_FIELDS:
        raise ValueError("workflow review物化sidecar字段集合非法")
    body = {key: item[key] for key in
            _WORKFLOW_REVIEW_MATERIALIZATION_BODY_FIELDS}
    digest = _workflow_materialization_digest(body)
    if item.get("schema_version") != WORKFLOW_REVIEW_MATERIALIZATION_SCHEMA \
            or item.get("materialization_digest") != digest \
            or item.get("materialization_id") != f"WFRMAT::{digest}" \
            or item.get("review_id") != f"DIMREVIEW::{digest}" \
            or (review_id is not None and item.get("review_id") != review_id):
        raise ValueError("workflow review物化sidecar内容身份无法重建")
    required_text = (
        "job_id", "request_id", "request_input_digest",
        "request_schema_version", "response_id", "response_digest",
        "response_blob_sha256", "response_schema_version", "source_mode",
        "authorization_id", "authorization_digest", "case_basis_digest",
        "case_basis_proof_digest", "dimension_id", "criterion_id", "claim_id",
        "quote_sha256", "decision", "evidence_class", "subject_scope",
        "scope_id", "support_scope", "reviewer", "review_basis",
    )
    if not all(isinstance(item.get(key), str) and item[key].strip()
               for key in required_text) \
            or not isinstance(item.get("case_basis_version"), int) \
            or item["case_basis_version"] <= 0 \
            or not isinstance(item.get("producer"), dict) \
            or not isinstance(item.get("evaluation_inputs"), dict) \
            or not isinstance(item.get("evaluation_input_proof_bindings"), dict) \
            or not isinstance(item.get("profile"), dict) \
            or not isinstance(item.get("method_versions"), dict) \
            or not isinstance(item.get("findings"), dict):
        raise ValueError("workflow review物化sidecar正文类型非法")
    return json.loads(_strict_json_dumps(item, label="workflow review物化sidecar"))


PRODUCT_RECORD_SCHEMA = "ag1.product-record.v1"
_PRODUCT_BODY_TYPES = {
    "submission": {
        "run_id": str, "contract_version": str, "case_id": str,
        "product_contract": str, "request": dict, "blockers": list, "inputs": list,
    },
    "task": {
        "contract_version": str, "run_id": str, "case_id": str, "role": str,
        "payload": dict, "allowed_materials": list, "host_session_id": str,
        "coverage_digest": str, "input_digest": str, "task_id": str,
    },
    "dispatch": {
        "task_id": str, "run_id": str, "input_digest": str, "role": str,
        "context_id": str, "source_mode": str, "token": int, "attempt_no": int,
        "started_at": str, "contract_version": str, "independence_status": str,
        "case_id": str, "dispatch_id": str,
    },
    "response": {"task_id": str, "blob_sha256": str, "result_digest": str},
    "consumption": {"task_id": str, "result_digest": str, "execution_status": str},
    "scope_candidate": {
        "coverage_digest": str, "units": list, "run_id": str, "task_id": str,
        "response_digest": str, "source_mode": str, "candidate_digest": str,
    },
    "scope": {
        "candidate_digest": str, "actor": str, "confirmed_at": str, "subject": dict,
        "evidence_cutoff": str, "units": list, "action": (dict, type(None)),
        "permissions": dict, "changes": dict, "run_id": str, "source_mode": str,
        "coverage_digest": str, "scope_digest": str,
    },
    "coverage_part": {
        "schema_version": str, "run_id": str, "inputs_digest": str,
        "coverage_digest": str, "collection": str, "part_index": int,
        "start": int, "count": int, "items": list, "part_digest": str,
    },
}
_PRODUCT_COVERAGE_TYPES = {
    "ag1.coverage.v1": {
        "schema_version": str, "run_id": str, "inputs_digest": str,
        "received_count": int, "entries": list, "segments": list,
        "unprocessed": list, "coverage_digest": str,
    },
    "ag1.coverage.index.v1": {
        "schema_version": str, "run_id": str, "inputs_digest": str,
        "received_count": int, "coverage_digest": str, "counts": dict,
        "parts": list, "index_digest": str,
    },
}


def _validate_product_body(object_id: str, run_id: str, kind: str, body: dict) -> None:
    """共同读写边界只校验记录类型和身份，业务资格仍由领域消费者重建。"""
    if not all(isinstance(value, str) and value.strip()
               for value in (object_id, run_id, kind)) or not isinstance(body, dict):
        raise ValueError("integrity: AG1产品对象身份或正文类型非法")
    if kind == "coverage":
        version = body.get("schema_version")
        schema = _PRODUCT_COVERAGE_TYPES.get(version) if isinstance(version, str) else None
    else:
        schema = _PRODUCT_BODY_TYPES.get(kind)
    if schema is None or set(body) != set(schema):
        raise ValueError(f"integrity: {kind}未登记或正文不符合封闭字段合同")
    for key, required in schema.items():
        choices = required if isinstance(required, tuple) else (required,)
        if type(body[key]) not in choices:
            raise ValueError(f"integrity: {kind}.{key}类型非法")
        if type(body[key]) is str and not body[key].strip():
            raise ValueError(f"integrity: {kind}.{key}身份或文本为空")
        if key.endswith(("_digest", "_sha256")) and not is_sha256_hex(body[key]):
            raise ValueError(f"integrity: {kind}.{key}摘要身份非法")
    if "run_id" in body and body["run_id"] != run_id:
        raise ValueError(f"integrity: {kind}正文与索引run归属不一致")
    if kind == "submission":
        expected_id = run_id
    elif kind == "task":
        expected_id = body["task_id"]
    elif kind in {"dispatch", "response", "consumption"}:
        prefix = {"dispatch": "DISPATCH", "response": "RESPONSE",
                  "consumption": "CONSUMED"}[kind]
        expected_id = f"{prefix}::{body['task_id']}"
    elif kind in {"scope_candidate", "scope", "coverage"}:
        prefix = {"scope_candidate": "SCOPE-CANDIDATE", "scope": "SCOPE",
                  "coverage": "COVERAGE"}[kind]
        expected_id = f"{prefix}::{run_id}"
    else:
        if body["schema_version"] != "ag1.coverage.part.v1":
            raise ValueError("integrity: coverage_part合同版本不支持")
        expected_id = (
            f"COVERAGE-PART::{run_id}::{body['coverage_digest']}::{body['part_index']}")
    if object_id != expected_id:
        raise ValueError(f"integrity: {kind}实际对象键与正文身份不一致")
    for key in ("received_count", "part_index", "start", "count", "token", "attempt_no"):
        if key in body and body[key] < (1 if key in {"count", "token", "attempt_no"} else 0):
            raise ValueError(f"integrity: {kind}.{key}超出有效范围")


def _decode_product_record(row) -> dict:
    if sha256_hex(row["body_json"].encode("utf-8")) != row["digest"]:
        raise ValueError("integrity: AG1产品对象摘要不一致")
    try:
        record = json.loads(row["body_json"])
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("integrity: AG1产品对象JSON损坏") from exc
    if not isinstance(record, dict):
        raise ValueError("integrity: AG1产品对象不是对象")
    if record.get("schema_version") == PRODUCT_RECORD_SCHEMA:
        if set(record) != {"schema_version", "object_id", "run_id", "kind", "body"} \
                or any(record[key] != row[key] for key in ("object_id", "run_id", "kind")):
            raise ValueError("integrity: AG1产品包络与实际索引身份不一致")
        body = record["body"]
    else:
        # 旧裸正文只经同一类型/身份规则读取，不自动迁移或重写原件。
        body = record
    _validate_product_body(row["object_id"], row["run_id"], row["kind"], body)
    return body


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_objects (
    object_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_product_objects_run
    ON product_objects(run_id, kind, object_id);
CREATE TABLE IF NOT EXISTS case_basis (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    subject_legal_name TEXT NOT NULL,
    subject_aliases TEXT NOT NULL,
    evidence_cutoff TEXT NOT NULL,
    subject_source_basis TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS case_basis_versions (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS claim_criterion_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    quote_start INTEGER NOT NULL,
    quote_end INTEGER NOT NULL,
    quote_sha256 TEXT NOT NULL,
    filter_hits TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS mapping_reviews (
    review_id TEXT PRIMARY KEY,
    case_basis_version INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    support_scope TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirmed','rejected')),
    reviewer TEXT NOT NULL,
    review_basis TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS crl_evidence_reviews (
    review_id TEXT PRIMARY KEY,
    case_basis_version INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('supports','does_not_support')),
    findings_json TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    support_scope TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    review_basis TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS crl_dimension_results (
    result_id TEXT PRIMARY KEY,
    input_digest TEXT NOT NULL UNIQUE,
    case_basis_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    product_status TEXT NOT NULL,
    result_blob_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS dimension_evidence_reviews (
    review_id TEXT PRIMARY KEY,
    dimension_id TEXT NOT NULL,
    case_basis_version INTEGER NOT NULL,
    claim_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('supports','does_not_support')),
    evidence_class TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    support_scope TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    review_basis TEXT NOT NULL,
    permission_mode TEXT NOT NULL DEFAULT 'legacy_unbound'
        CHECK (permission_mode IN
            ('legacy_unbound','license_v2','workflow_authorization_v1')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_dimension_reviews
    ON dimension_evidence_reviews(dimension_id, case_basis_version, scope_id);
CREATE TABLE IF NOT EXISTS dimension_review_permissions (
    review_id TEXT PRIMARY KEY REFERENCES dimension_evidence_reviews(review_id),
    license_id TEXT NOT NULL,
    requested_use TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    candidate_digest TEXT NOT NULL,
    confirmation_json TEXT NOT NULL,
    confirmation_digest TEXT NOT NULL,
    license_json TEXT NOT NULL,
    binding_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS workflow_review_materializations (
    materialization_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE
        REFERENCES dimension_evidence_reviews(review_id),
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
    request_id TEXT NOT NULL REFERENCES review_requests(request_id),
    response_id TEXT NOT NULL REFERENCES review_responses(response_id),
    authorization_id TEXT NOT NULL REFERENCES review_authorizations(authorization_id),
    materialization_digest TEXT NOT NULL UNIQUE,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS tmrl_identity_overlays (
    overlay_id TEXT PRIMARY KEY,
    case_basis_version INTEGER NOT NULL,
    scope_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN
        ('verified','probable','ok','unverified','ambiguous','same_name_only')),
    subject_ref_json TEXT NOT NULL,
    status_ref_json TEXT NOT NULL,
    scope_ref_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(case_basis_version, scope_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_tmrl_identity_overlays
    ON tmrl_identity_overlays(case_basis_version, scope_id, subject_id);
CREATE TABLE IF NOT EXISTS dimension_results (
    result_id TEXT PRIMARY KEY,
    dimension_id TEXT NOT NULL,
    input_digest TEXT NOT NULL UNIQUE,
    case_basis_version INTEGER NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    product_status TEXT NOT NULL,
    result_blob_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_dimension_results_dimension
    ON dimension_results(dimension_id, scope_id);
CREATE TABLE IF NOT EXISTS provider_usage_ledger (
    task_key TEXT PRIMARY KEY,
    authorization_digest TEXT NOT NULL,
    budget_scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('reserved','succeeded','failed','outcome_unknown')),
    input_reserved INTEGER NOT NULL,
    output_reserved INTEGER NOT NULL,
    input_actual INTEGER,
    output_actual INTEGER,
    usage_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (usage_status IN ('known','unknown')),
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_ledger_auth
    ON provider_usage_ledger(authorization_digest);
CREATE TABLE IF NOT EXISTS source_time_evidence (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS capture_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES import_records(import_id),
    dep_name TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capdep_import ON capture_dependencies(import_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    input_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS stages (
    stage TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN
        ('pending','running','succeeded','blocked','failed')),
    run_id INTEGER,
    detail TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS import_records (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    origin_path TEXT NOT NULL,
    origin_sha256 TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    blob_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    media_type TEXT,
    locator TEXT,
    retrieved_at TEXT,
    published_at TEXT,
    published_at_provenance TEXT,
    time_evidence TEXT,
    document_subject TEXT,
    document_subject_basis TEXT,
    source_family TEXT,
    capture_status TEXT NOT NULL,
    import_id INTEGER REFERENCES import_records(import_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    locator_kind TEXT NOT NULL CHECK (locator_kind IN ('byte_range','zip_member','pdf_page')),
    locator_start INTEGER,
    locator_end INTEGER,
    locator_ref TEXT,
    excerpt_sha256 TEXT NOT NULL,
    excerpt_text TEXT NOT NULL,
    interpretation TEXT NOT NULL,
    subject_scope TEXT NOT NULL,
    interpretation_attempt TEXT,
    input_digest TEXT,
    content_digest TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS qualifications (
    qual_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    source_judgment TEXT NOT NULL,
    identity_judgment TEXT NOT NULL,
    time_judgment TEXT NOT NULL,
    independence_judgment TEXT NOT NULL,
    allowed_uses TEXT NOT NULL,
    cannot_prove TEXT NOT NULL,
    review_attempt TEXT,
    status TEXT NOT NULL CHECK (status IN ('qualified','rejected','needs_review')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS gaps (
    gap_id TEXT PRIMARY KEY,
    gap_type TEXT NOT NULL,
    affected_criteria TEXT NOT NULL,
    pipeline_fault INTEGER NOT NULL DEFAULT 0,
    investigation TEXT NOT NULL,
    unconfirmed TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS criterion_results (
    result_id TEXT PRIMARY KEY,
    criterion_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    native_disposition TEXT,
    native_note TEXT,
    product_status TEXT NOT NULL CHECK (product_status IN
        ('succeeded','insufficient','method_unsupported','execution_failed')),
    evidence_refs TEXT NOT NULL,
    gap_refs TEXT NOT NULL,
    qual_refs TEXT NOT NULL,
    rationale TEXT NOT NULL,
    scope TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    input_digest TEXT,
    case_basis_version INTEGER,
    frozen_inputs TEXT,
    na_basis TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_id);
CREATE INDEX IF NOT EXISTS idx_quals_claim ON qualifications(claim_id);
CREATE TABLE IF NOT EXISTS attachment_imports (
    attachment_id TEXT PRIMARY KEY,
    origin_path TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved','unsupported','failed')),
    error TEXT,
    import_id INTEGER REFERENCES import_records(import_id),
    source_id TEXT REFERENCES sources(source_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(origin_path, blob_sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attachment_imports_content
    ON attachment_imports(blob_sha256);
CREATE TABLE IF NOT EXISTS attachment_aliases (
    alias_id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachment_imports(attachment_id),
    origin_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved','unsupported','failed')),
    error TEXT,
    import_id INTEGER NOT NULL REFERENCES import_records(import_id),
    source_id TEXT REFERENCES sources(source_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(attachment_id, origin_path)
);
CREATE TABLE IF NOT EXISTS text_projections (
    projection_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    source_blob_sha256 TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    text_sha256 TEXT,
    text_blob_sha256 TEXT,
    char_count INTEGER,
    tool TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('projected','unprocessed','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_text_projections_source
    ON text_projections(source_id, projection_id);
CREATE TABLE IF NOT EXISTS workflow_jobs (
    job_id TEXT PRIMARY KEY,
    input_digest TEXT NOT NULL UNIQUE,
    schema_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('saved','projected','awaiting_authorized_analysis','response_sealed',
         'consumed','failed','insufficient','awaiting_candidate_proposal',
         'proposal_response_sealed','reviews_consumed','evidence_materialized',
         'evaluating','evaluated','manifest_frozen','completed')),
    body_json TEXT NOT NULL,
    failure_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS review_requests (
    request_id TEXT PRIMARY KEY,
    request_input_digest TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
    status TEXT NOT NULL CHECK (status IN
        ('awaiting_authorized_analysis','response_sealed','consumed','failed')),
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(job_id, request_input_digest)
);
CREATE INDEX IF NOT EXISTS idx_review_requests_job
    ON review_requests(job_id, request_id);
CREATE TABLE IF NOT EXISTS workflow_job_dimension_outputs (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
    dimension_id TEXT NOT NULL CHECK (dimension_id IN
        ('CRL','BRL','TRL','IPRL','TMRL','FRL')),
    result_id TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    trace_digest TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (job_id, dimension_id),
    UNIQUE (job_id, result_id)
);
CREATE TABLE IF NOT EXISTS workflow_job_artifacts (
    job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id),
    manifest_id TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    manifest_blob_sha256 TEXT NOT NULL,
    view_id TEXT NOT NULL,
    view_digest TEXT NOT NULL,
    view_blob_sha256 TEXT NOT NULL,
    source_modes_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (manifest_id),
    UNIQUE (view_id)
);
CREATE TABLE IF NOT EXISTS review_responses (
    response_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES review_requests(request_id),
    response_digest TEXT NOT NULL UNIQUE,
    response_blob_sha256 TEXT NOT NULL,
    source_mode TEXT NOT NULL CHECK (source_mode IN
        ('manual_import','simulated','runtime_provider')),
    producer_json TEXT NOT NULL,
    output_schema TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('response_sealed','consumed','failed')),
    body_json TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS proposal_requests (
    request_id TEXT PRIMARY KEY,
    request_input_digest TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
    status TEXT NOT NULL CHECK (status IN
        ('awaiting_candidate_proposal','proposal_response_sealed',
         'consumed','failed')),
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(job_id, request_input_digest)
);
CREATE INDEX IF NOT EXISTS idx_proposal_requests_job
    ON proposal_requests(job_id, request_id);
CREATE TABLE IF NOT EXISTS proposal_responses (
    response_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES proposal_requests(request_id),
    response_digest TEXT NOT NULL UNIQUE,
    response_blob_sha256 TEXT NOT NULL,
    source_mode TEXT NOT NULL CHECK (source_mode IN ('manual_import','simulated','runtime_provider')),
    status TEXT NOT NULL CHECK (status IN ('proposal_response_sealed','consumed')),
    body_json TEXT NOT NULL,
    materialization_json TEXT,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS qualification_input_views (
    view_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    input_digest TEXT NOT NULL UNIQUE,
    view_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS review_authorizations (
    authorization_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    qualification_view_id TEXT NOT NULL
        REFERENCES qualification_input_views(view_id),
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""


class StoreIntegrityError(RuntimeError):
    """内容身份不符（截断/篡改/引用断裂）——必须可见失败。"""


class BlobStore:
    """内容寻址原件仓：同字节唯一身份，读时重核。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        self._lock = RLock()

    def _object_path(self, sha256: str) -> Path:
        if not is_sha256_hex(sha256):
            raise StoreIntegrityError(f"非法对象 id：{sha256!r}")
        path = (self.root / "objects" / sha256[:2] / sha256).resolve()
        if not path.is_relative_to((self.root / "objects").resolve()):
            raise StoreIntegrityError(f"目录逃逸：{sha256!r}")
        return path

    def put_bytes(self, data: bytes) -> BlobRef:
        digest = sha256_hex(data)
        ref = BlobRef(sha256=digest, byte_length=len(data))
        final = self._object_path(digest)
        if final.exists():
            existing = final.read_bytes()
            if existing != data:
                raise StoreIntegrityError(
                    f"对象 {digest} 已存在但内容不一致（疑似哈希碰撞或篡改）"
                )
            return ref
        final.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + fsync + 文件级 os.replace（实测可用；目录级替换不可用）
        fd, tmp_name = tempfile.mkstemp(dir=str(final.parent), suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, final)
        finally:
            if tmp.exists():
                tmp.unlink()
        # 提交后重核，防止替换过程静默损坏
        if final.read_bytes() != data:
            raise StoreIntegrityError(f"对象 {digest} 落盘后复核不一致")
        return ref

    def has(self, sha256: str) -> bool:
        try:
            return self._object_path(sha256).exists()
        except StoreIntegrityError:
            return False

    def read_bytes(self, ref: BlobRef | str) -> bytes:
        sha256 = ref.sha256 if isinstance(ref, BlobRef) else ref
        data = self._object_path(sha256).read_bytes()
        digest = sha256_hex(data)
        if digest != sha256 or len(data) != (
            ref.byte_length if isinstance(ref, BlobRef) else len(data)
        ):
            raise StoreIntegrityError(
                f"对象 {sha256} 复核失败：实际 sha256={digest}，字节长度={len(data)}"
            )
        return data

    def read_range(self, ref: BlobRef | str, start: int, end: int) -> bytes:
        data = self.read_bytes(ref)
        if not (0 <= start < end <= len(data)):
            raise StoreIntegrityError(f"越界定位 [{start},{end})，对象长度 {len(data)}")
        return data[start:end]

    def list_objects(self) -> set[str]:
        objects = self.root / "objects"
        found: set[str] = set()
        for shard in objects.iterdir():
            if shard.is_dir() and len(shard.name) == 2:
                for obj in shard.iterdir():
                    if obj.is_file() and is_sha256_hex(obj.name):
                        found.add(obj.name)
        return found


class CaseStore:
    """Case 记录库（SQLite 事务台账：阶段、来源、主张、资格、缺口、判据结果）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_SCHEMA_VERSION,),
        )
        self._conn.commit()

    @contextmanager
    def immediate_transaction(self):
        """串行化发布门：验证读取到结果写入必须处于同一写事务。"""
        if self._conn.in_transaction:
            raise RuntimeError("CaseStore 已有未完成事务，不能嵌套发布事务")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        else:
            if not self._conn.in_transaction:
                raise RuntimeError("发布事务在验证完成前被提前结束")
            self._conn.execute("COMMIT")

    @contextmanager
    def product_read_view(self):
        """为AG1读取建立同一连接、同一时点的只读视图。"""
        if self._conn.in_transaction:
            raise RuntimeError("AG1产品读取不能嵌套已有事务")
        self._conn.execute("BEGIN")
        try:
            yield self
        finally:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")

    def read_product_task_records(self) -> dict:
        """读取AG1任务及其执行关系，保留真实对象键作为完整性分母。"""
        rows = self._conn.execute(
            "SELECT object_id,run_id,kind,digest,body_json "
            "FROM product_objects ORDER BY object_id"
        ).fetchall()
        objects = []
        for row in rows:
            objects.append({
                "object_id": row["object_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "body": _decode_product_record(row),
            })
        journal_tasks = [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM tasks WHERE task_key LIKE 'TASK::%' "
                "ORDER BY task_key"
            ).fetchall()
        ]
        attempts = [
            dict(row) for row in self._conn.execute(
                "SELECT * FROM task_attempts WHERE task_key LIKE 'TASK::%' "
                "ORDER BY task_key,attempt_no"
            ).fetchall()
        ]
        return {
            "objects": objects,
            "journal_tasks": journal_tasks,
            "attempts": attempts,
        }

    def put_product_object(self, object_id: str, *, run_id: str,
                           kind: str, body: dict) -> dict:
        """AG1有类型的不可变对象；索引、正文、列表共同经过同一读写边界。"""
        _validate_product_body(object_id, run_id, kind, body)
        body_json = _strict_json_dumps(body, label="AG1产品正文")
        packed = _strict_json_dumps({
            "schema_version": PRODUCT_RECORD_SCHEMA, "object_id": object_id,
            "run_id": run_id, "kind": kind, "body": body,
        }, label="AG1产品包络")
        digest = sha256_hex(packed.encode("utf-8"))
        with self.immediate_transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO product_objects"
                "(object_id,run_id,kind,digest,body_json) VALUES (?,?,?,?,?)",
                (object_id, run_id, kind, digest, packed))
            row = self._conn.execute(
                "SELECT * FROM product_objects WHERE object_id=?", (object_id,)
            ).fetchone()
            existing = _decode_product_record(row)
            if row["run_id"] != run_id or row["kind"] != kind \
                    or _strict_json_dumps(existing, label="既有AG1正文") != body_json:
                raise ValueError("immutable: AG1对象身份冲突，禁止覆盖")
        return self.get_product_object(object_id, run_id=run_id, kind=kind)

    def product_case_id(self) -> str:
        """独立Case的派发身份不同；复制既有Case用于恢复时保持身份。"""
        with self.immediate_transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES ('ag1_case_id',?)",
                (str(uuid.uuid4()),))
            return self._conn.execute(
                "SELECT value FROM meta WHERE key='ag1_case_id'").fetchone()["value"]

    def get_product_object(self, object_id: str, *, run_id: str | None = None,
                           kind: str | None = None) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM product_objects WHERE object_id=?", (object_id,)
        ).fetchone()
        if row is None:
            return None
        if (run_id is not None and run_id != row["run_id"]) \
                or (kind is not None and kind != row["kind"]):
            raise ValueError("integrity: AG1对象正文或归属不一致")
        return _decode_product_record(row)

    def list_product_objects(self, run_id: str, kind: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT object_id FROM product_objects WHERE run_id=? AND kind=? "
            "ORDER BY object_id", (run_id, kind)).fetchall()
        return [self.get_product_object(row["object_id"], run_id=run_id, kind=kind)
                for row in rows]

    def assert_product_task_inventory(self, records: dict | None = None) -> None:
        """以任务及所有执行关系的并集作为分母，禁止孤儿记录逃逸。"""
        records = records or self.read_product_task_records()
        objects = records["objects"]
        by_id = {item["object_id"]: item for item in objects}
        tasks = {
            item["object_id"]: item for item in objects if item["kind"] == "task"
        }
        journal = {
            item["task_key"]: item for item in records["journal_tasks"]
        }
        attempts_by_task: dict[str, list[dict]] = {}
        for attempt in records["attempts"]:
            attempts_by_task.setdefault(attempt["task_key"], []).append(attempt)
        relation_kinds = {"dispatch", "response", "consumption", "scope_candidate"}
        relation_task_ids = set()
        for item in objects:
            if item["kind"] in relation_kinds:
                task_id = item["body"]["task_id"]
                relation_task_ids.add(task_id)
                if task_id not in tasks:
                    raise ValueError(
                        "integrity: AG1执行关系引用不存在的产品任务")
        for task_key in set(attempts_by_task) - set(tasks):
            raise ValueError("integrity: AG1 attempt没有产品任务父项")
        for task_key, row in journal.items():
            task = tasks.get(task_key)
            if task is None or task["body"]["input_digest"] != row["input_id"]:
                raise ValueError("integrity: Journal产品任务丢失对象或输入身份不符")
        for task_id, item in tasks.items():
            task = item["body"]
            body = {key: value for key, value in task.items()
                    if key not in {"task_id", "input_digest"}}
            if task_id != "TASK::" + sha256_hex(
                    json.dumps(body, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
            ) or task["input_digest"] != sha256_hex(
                    json.dumps(body, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
            ):
                raise ValueError("integrity: 产品任务正文摘要无法重建")
            run_item = by_id.get(task["run_id"])
            if run_item is None or run_item["kind"] != "submission":
                raise ValueError("integrity: Journal产品任务失去run归属")
            run = run_item["body"]
            if run["case_id"] != task["case_id"] \
                    or run["contract_version"] != task["contract_version"]:
                raise ValueError("integrity: Journal产品任务失去Case/合同归属")
            if task_id not in journal and (
                    attempts_by_task.get(task_id) or task_id in relation_task_ids):
                raise ValueError("integrity: 执行关系缺少Journal任务父项")

    def _migrate(self) -> None:
        """已建库增量迁移：保留R1数据并升级本地工作流至store.v6。"""
        version_row = self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        prior_version = version_row[0] if version_row else "kth-hybrid.store.v0"
        try:
            prior_generation = int(str(prior_version).rsplit(".v", 1)[1])
        except (IndexError, ValueError):
            prior_generation = 0
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS case_basis (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                subject_legal_name TEXT NOT NULL,
                subject_aliases TEXT NOT NULL,
                evidence_cutoff TEXT NOT NULL,
                subject_source_basis TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS case_basis_versions (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS source_time_evidence (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS capture_dependencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL REFERENCES import_records(import_id),
                dep_name TEXT NOT NULL,
                blob_sha256 TEXT NOT NULL,
                byte_length INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claim_criterion_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                quote_start INTEGER NOT NULL,
                quote_end INTEGER NOT NULL,
                quote_sha256 TEXT NOT NULL,
                filter_hits TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS mapping_reviews (
                review_id TEXT PRIMARY KEY,
                case_basis_version INTEGER NOT NULL,
                claim_id TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                quote_sha256 TEXT NOT NULL,
                support_scope TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('confirmed','rejected')),
                reviewer TEXT NOT NULL,
                review_basis TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS crl_evidence_reviews (
                review_id TEXT PRIMARY KEY,
                case_basis_version INTEGER NOT NULL,
                claim_id TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                quote_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('supports','does_not_support')),
                findings_json TEXT NOT NULL,
                subject_scope TEXT,
                support_scope TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                review_basis TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS crl_dimension_results (
                result_id TEXT PRIMARY KEY,
                input_digest TEXT NOT NULL UNIQUE,
                case_basis_version INTEGER NOT NULL,
                scope TEXT NOT NULL,
                product_status TEXT NOT NULL,
                result_blob_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS dimension_evidence_reviews (
                review_id TEXT PRIMARY KEY,
                dimension_id TEXT NOT NULL,
                case_basis_version INTEGER NOT NULL,
                claim_id TEXT NOT NULL,
                criterion_id TEXT NOT NULL,
                quote_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (decision IN ('supports','does_not_support')),
                evidence_class TEXT NOT NULL,
                findings_json TEXT NOT NULL,
                subject_scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                support_scope TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                review_basis TEXT NOT NULL,
                permission_mode TEXT NOT NULL DEFAULT 'legacy_unbound'
                    CHECK (permission_mode IN
                        ('legacy_unbound','license_v2',
                         'workflow_authorization_v1')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_dimension_reviews
                ON dimension_evidence_reviews(dimension_id, case_basis_version, scope_id);
            CREATE TABLE IF NOT EXISTS dimension_review_permissions (
                review_id TEXT PRIMARY KEY REFERENCES dimension_evidence_reviews(review_id),
                license_id TEXT NOT NULL,
                requested_use TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                candidate_digest TEXT NOT NULL,
                confirmation_json TEXT NOT NULL,
                confirmation_digest TEXT NOT NULL,
                license_json TEXT NOT NULL,
                binding_digest TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS workflow_review_materializations (
                materialization_id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL UNIQUE
                    REFERENCES dimension_evidence_reviews(review_id),
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                request_id TEXT NOT NULL REFERENCES review_requests(request_id),
                response_id TEXT NOT NULL REFERENCES review_responses(response_id),
                authorization_id TEXT NOT NULL
                    REFERENCES review_authorizations(authorization_id),
                materialization_digest TEXT NOT NULL UNIQUE,
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS tmrl_identity_overlays (
                overlay_id TEXT PRIMARY KEY,
                case_basis_version INTEGER NOT NULL,
                scope_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                resolution_status TEXT NOT NULL CHECK (resolution_status IN
                    ('verified','probable','ok','unverified','ambiguous','same_name_only')),
                subject_ref_json TEXT NOT NULL,
                status_ref_json TEXT NOT NULL,
                scope_ref_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(case_basis_version, scope_id, subject_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tmrl_identity_overlays
                ON tmrl_identity_overlays(case_basis_version, scope_id, subject_id);
            CREATE TABLE IF NOT EXISTS dimension_results (
                result_id TEXT PRIMARY KEY,
                dimension_id TEXT NOT NULL,
                input_digest TEXT NOT NULL UNIQUE,
                case_basis_version INTEGER NOT NULL,
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                product_status TEXT NOT NULL,
                result_blob_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_dimension_results_dimension
                ON dimension_results(dimension_id, scope_id);
            CREATE TABLE IF NOT EXISTS attachment_imports (
                attachment_id TEXT PRIMARY KEY,
                origin_path TEXT NOT NULL,
                blob_sha256 TEXT NOT NULL,
                byte_length INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('saved','unsupported','failed')),
                error TEXT,
                import_id INTEGER REFERENCES import_records(import_id),
                source_id TEXT REFERENCES sources(source_id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(origin_path, blob_sha256)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_attachment_imports_content
                ON attachment_imports(blob_sha256, media_type);
            CREATE TABLE IF NOT EXISTS attachment_aliases (
                alias_id TEXT PRIMARY KEY,
                attachment_id TEXT NOT NULL REFERENCES attachment_imports(attachment_id),
                origin_path TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('saved','unsupported','failed')),
                error TEXT,
                import_id INTEGER NOT NULL REFERENCES import_records(import_id),
                source_id TEXT REFERENCES sources(source_id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(attachment_id, origin_path)
            );
            CREATE TABLE IF NOT EXISTS text_projections (
                projection_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                source_blob_sha256 TEXT NOT NULL,
                locator_json TEXT NOT NULL,
                text_sha256 TEXT,
                text_blob_sha256 TEXT,
                char_count INTEGER,
                tool TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('projected','unprocessed','failed')),
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_text_projections_source
                ON text_projections(source_id, projection_id);
            CREATE TABLE IF NOT EXISTS workflow_jobs (
                job_id TEXT PRIMARY KEY,
                input_digest TEXT NOT NULL UNIQUE,
                schema_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                    ('saved','projected','awaiting_authorized_analysis',
                     'response_sealed','consumed','failed','insufficient',
                     'awaiting_candidate_proposal','proposal_response_sealed')),
                body_json TEXT NOT NULL,
                failure_json TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS review_requests (
                request_id TEXT PRIMARY KEY,
                request_input_digest TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                status TEXT NOT NULL CHECK (status IN
                    ('awaiting_authorized_analysis','response_sealed','consumed','failed')),
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(job_id, request_input_digest)
            );
            CREATE INDEX IF NOT EXISTS idx_review_requests_job
                ON review_requests(job_id, request_id);
            CREATE TABLE IF NOT EXISTS review_responses (
                response_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE REFERENCES review_requests(request_id),
                response_digest TEXT NOT NULL UNIQUE,
                response_blob_sha256 TEXT NOT NULL,
                source_mode TEXT NOT NULL CHECK (source_mode IN
                    ('manual_import','simulated','runtime_provider')),
                producer_json TEXT NOT NULL,
                output_schema TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN
                    ('response_sealed','consumed','failed')),
                body_json TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS proposal_requests (
                request_id TEXT PRIMARY KEY,
                request_input_digest TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                status TEXT NOT NULL CHECK (status IN
                    ('awaiting_candidate_proposal','proposal_response_sealed',
                     'consumed','failed')),
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(job_id, request_input_digest)
            );
            CREATE INDEX IF NOT EXISTS idx_proposal_requests_job
                ON proposal_requests(job_id, request_id);
            CREATE TABLE IF NOT EXISTS proposal_responses (
                response_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE REFERENCES proposal_requests(request_id),
                response_digest TEXT NOT NULL UNIQUE,
                response_blob_sha256 TEXT NOT NULL,
                source_mode TEXT NOT NULL CHECK (source_mode IN
                    ('manual_import','simulated','runtime_provider')),
                status TEXT NOT NULL CHECK (status IN
                    ('proposal_response_sealed','consumed')),
                body_json TEXT NOT NULL,
                materialization_json TEXT,
                consumed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS qualification_input_views (
                view_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                input_digest TEXT NOT NULL UNIQUE,
                view_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS review_authorizations (
                authorization_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                qualification_view_id TEXT NOT NULL
                    REFERENCES qualification_input_views(view_id),
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        """)
        columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(sources)").fetchall()}
        for column in ("time_evidence", "document_subject",
                       "document_subject_basis"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
        claim_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(claims)").fetchall()}
        for column in ("input_digest", "content_digest"):
            if column not in claim_columns:
                self._conn.execute(f"ALTER TABLE claims ADD COLUMN {column} TEXT")
        result_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(criterion_results)").fetchall()}
        for column in ("input_digest", "case_basis_version", "frozen_inputs",
                       "na_basis"):
            if column not in result_columns:
                self._conn.execute(
                    f"ALTER TABLE criterion_results ADD COLUMN {column} TEXT")
        crl_review_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(crl_evidence_reviews)").fetchall()}
        if "subject_scope" not in crl_review_columns:
            self._conn.execute("ALTER TABLE crl_evidence_reviews ADD COLUMN subject_scope TEXT")
        dimension_review_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(dimension_evidence_reviews)").fetchall()}
        if "permission_mode" not in dimension_review_columns:
            self._conn.execute(
                "ALTER TABLE dimension_evidence_reviews ADD COLUMN "
                "permission_mode TEXT NOT NULL DEFAULT 'legacy_unbound'")
        alias_columns = {row[1] for row in self._conn.execute(
            "PRAGMA table_info(attachment_aliases)").fetchall()}
        if "source_id" not in alias_columns:
            self._conn.execute(
                "ALTER TABLE attachment_aliases ADD COLUMN source_id TEXT")
        if prior_generation < 5:
            self._migrate_attachment_objects_v5()
        self._conn.commit()
        if prior_generation < 6:
            self._migrate_review_requests_v6()
        self._conn.commit()
        if prior_generation < 7:
            self._migrate_workflow_states_v7()
        self._conn.commit()
        if prior_generation < 8:
            self._migrate_dimension_reviews_v8()
        self._conn.commit()
        if prior_generation < 9:
            self._migrate_workflow_outputs_v9()
        self._conn.commit()
        if prior_generation < 10:
            self._migrate_proposal_source_mode_v10()
        self._conn.commit()
        if prior_generation < 11:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS provider_usage_ledger (
                    task_key TEXT PRIMARY KEY,
                    authorization_digest TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('reserved','succeeded','failed','outcome_unknown')),
                    input_reserved INTEGER NOT NULL,
                    output_reserved INTEGER NOT NULL,
                    input_actual INTEGER,
                    output_actual INTEGER,
                    usage_status TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (usage_status IN ('known','unknown')),
                    detail TEXT,
                    created_at TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_provider_usage_ledger_auth
                    ON provider_usage_ledger(authorization_digest);
            """)
        self._conn.commit()
        if prior_generation < 12:
            # v12：预算范围与授权文档摘要解耦，修订授权不再重置预算范围。
            columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(provider_usage_ledger)").fetchall()}
            if "budget_scope" not in columns:
                self._conn.executescript("""
                    ALTER TABLE provider_usage_ledger
                        ADD COLUMN budget_scope TEXT NOT NULL DEFAULT '';
                    UPDATE provider_usage_ledger
                        SET budget_scope = authorization_digest;
                    CREATE INDEX IF NOT EXISTS idx_provider_usage_ledger_scope
                        ON provider_usage_ledger(budget_scope);
                """)
        self._conn.commit()

    def _migrate_proposal_source_mode_v10(self) -> None:
        """扩展proposal_responses的source_mode约束以接受runtime_provider。"""
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='proposal_responses'").fetchone()
        if sql is not None and "runtime_provider" in (sql[0] or ""):
            return
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.executescript("""
                CREATE TABLE proposal_responses_v10 (
                    response_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE REFERENCES proposal_requests(request_id),
                    response_digest TEXT NOT NULL UNIQUE,
                    response_blob_sha256 TEXT NOT NULL,
                    source_mode TEXT NOT NULL CHECK (source_mode IN
                        ('manual_import','simulated','runtime_provider')),
                    status TEXT NOT NULL CHECK (status IN
                        ('proposal_response_sealed','consumed')),
                    body_json TEXT NOT NULL,
                    materialization_json TEXT,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                INSERT INTO proposal_responses_v10
                    SELECT * FROM proposal_responses;
                DROP TABLE proposal_responses;
                ALTER TABLE proposal_responses_v10 RENAME TO proposal_responses;
            """)
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_workflow_states_v7(self) -> None:
        """扩展候选阶段状态；保留既有v1 job与外键引用。"""
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='workflow_jobs'").fetchone()
        if sql is not None and "awaiting_candidate_proposal" in (sql[0] or ""):
            return
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.executescript("""
                CREATE TABLE workflow_jobs_v7 (
                    job_id TEXT PRIMARY KEY,
                    input_digest TEXT NOT NULL UNIQUE,
                    schema_version TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN
                        ('saved','projected','awaiting_authorized_analysis',
                         'response_sealed','consumed','failed','insufficient',
                         'awaiting_candidate_proposal',
                         'proposal_response_sealed')),
                    body_json TEXT NOT NULL,
                    failure_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO workflow_jobs_v7
                    SELECT * FROM workflow_jobs;
                DROP TABLE workflow_jobs;
                ALTER TABLE workflow_jobs_v7 RENAME TO workflow_jobs;
            """)
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_dimension_reviews_v8(self) -> None:
        """扩展非CRL review许可模式，同时保留历史许可sidecar。"""
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='dimension_evidence_reviews'").fetchone()
        if sql is not None and "workflow_authorization_v1" in (sql[0] or ""):
            return
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.executescript("""
                CREATE TABLE dimension_evidence_reviews_v8 (
                    review_id TEXT PRIMARY KEY,
                    dimension_id TEXT NOT NULL,
                    case_basis_version INTEGER NOT NULL,
                    claim_id TEXT NOT NULL,
                    criterion_id TEXT NOT NULL,
                    quote_sha256 TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN
                        ('supports','does_not_support')),
                    evidence_class TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    subject_scope TEXT NOT NULL,
                    scope_id TEXT NOT NULL,
                    support_scope TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    review_basis TEXT NOT NULL,
                    permission_mode TEXT NOT NULL DEFAULT 'legacy_unbound'
                        CHECK (permission_mode IN
                            ('legacy_unbound','license_v2',
                             'workflow_authorization_v1')),
                    created_at TEXT NOT NULL
                );
                INSERT INTO dimension_evidence_reviews_v8
                    SELECT review_id,dimension_id,case_basis_version,claim_id,
                           criterion_id,quote_sha256,decision,evidence_class,
                           findings_json,subject_scope,scope_id,support_scope,
                           reviewer,review_basis,permission_mode,created_at
                    FROM dimension_evidence_reviews;
                CREATE TABLE dimension_review_permissions_v8 (
                    review_id TEXT PRIMARY KEY
                        REFERENCES dimension_evidence_reviews_v8(review_id),
                    license_id TEXT NOT NULL,
                    requested_use TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    confirmation_json TEXT NOT NULL,
                    confirmation_digest TEXT NOT NULL,
                    license_json TEXT NOT NULL,
                    binding_digest TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                INSERT INTO dimension_review_permissions_v8
                    SELECT * FROM dimension_review_permissions;
                DROP TABLE dimension_review_permissions;
                DROP TABLE dimension_evidence_reviews;
                ALTER TABLE dimension_evidence_reviews_v8
                    RENAME TO dimension_evidence_reviews;
                ALTER TABLE dimension_review_permissions_v8
                    RENAME TO dimension_review_permissions;
                CREATE INDEX IF NOT EXISTS idx_dimension_reviews
                    ON dimension_evidence_reviews(
                        dimension_id, case_basis_version, scope_id);
            """)
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_workflow_outputs_v9(self) -> None:
        """扩展v2工作流的可恢复执行状态与精确输出登记表。"""
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='workflow_jobs'").fetchone()
        needs_states = sql is None or "manifest_frozen" not in (sql[0] or "")
        if needs_states:
            self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                self._conn.executescript("""
                    CREATE TABLE workflow_jobs_v9 (
                        job_id TEXT PRIMARY KEY,
                        input_digest TEXT NOT NULL UNIQUE,
                        schema_version TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN
                            ('saved','projected','awaiting_authorized_analysis',
                             'response_sealed','consumed','failed','insufficient',
                             'awaiting_candidate_proposal',
                             'proposal_response_sealed','reviews_consumed',
                             'evidence_materialized','evaluating','evaluated',
                             'manifest_frozen','completed')),
                        body_json TEXT NOT NULL,
                        failure_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO workflow_jobs_v9 SELECT * FROM workflow_jobs;
                    DROP TABLE workflow_jobs;
                    ALTER TABLE workflow_jobs_v9 RENAME TO workflow_jobs;
                """)
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS workflow_job_dimension_outputs (
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                dimension_id TEXT NOT NULL CHECK (dimension_id IN
                    ('CRL','BRL','TRL','IPRL','TMRL','FRL')),
                result_id TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                trace_digest TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY (job_id, dimension_id),
                UNIQUE (job_id, result_id)
            );
            CREATE TABLE IF NOT EXISTS workflow_job_artifacts (
                job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id),
                manifest_id TEXT NOT NULL,
                manifest_digest TEXT NOT NULL,
                manifest_blob_sha256 TEXT NOT NULL,
                view_id TEXT NOT NULL,
                view_digest TEXT NOT NULL,
                view_blob_sha256 TEXT NOT NULL,
                source_modes_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE (manifest_id),
                UNIQUE (view_id)
            );
        """)
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreIntegrityError(
                f"store.v9迁移后外键引用不完整：{len(violations)}项")

    def _migrate_attachment_objects_v5(self) -> None:
        """将v4按路径/媒体拆出的同blob行归并为单一内容对象。"""
        self._conn.execute("DROP INDEX IF EXISTS idx_attachment_imports_content")
        rows = self._conn.execute(
            "SELECT * FROM attachment_imports "
            "ORDER BY blob_sha256,origin_path,attachment_id").fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["blob_sha256"], []).append(dict(row))
        for blob_sha256, group in grouped.items():
            canonical_id = f"ATTACH::{blob_sha256}"
            old_ids = [row["attachment_id"] for row in group]
            placeholders = ",".join("?" for _ in old_ids)
            existing_aliases = [dict(row) for row in self._conn.execute(
                f"SELECT * FROM attachment_aliases WHERE attachment_id IN "
                f"({placeholders}) ORDER BY origin_path,alias_id", old_ids
            ).fetchall()]
            self._conn.execute(
                f"DELETE FROM attachment_aliases WHERE attachment_id IN "
                f"({placeholders})", old_ids)
            canonical = next(
                (row for row in group
                 if row["attachment_id"] == canonical_id), None)
            best = next(
                (row for row in group
                 if row["status"] == "saved" and row["source_id"]),
                canonical or group[0])
            if canonical is None:
                self._conn.execute(
                    "UPDATE attachment_imports SET attachment_id=? "
                    "WHERE attachment_id=?",
                    (canonical_id, best["attachment_id"]))
            self._conn.execute(
                "DELETE FROM attachment_imports WHERE blob_sha256=? "
                "AND attachment_id<>?", (blob_sha256, canonical_id))
            if canonical is not None and best["attachment_id"] != canonical_id:
                self._conn.execute(
                    "UPDATE attachment_imports SET origin_path=?,byte_length=?,"
                    "original_filename=?,media_type=?,status=?,error=?,"
                    "import_id=?,source_id=? WHERE attachment_id=?",
                    (best["origin_path"], best["byte_length"],
                     best["original_filename"], best["media_type"],
                     best["status"], best["error"], best["import_id"],
                     best["source_id"], canonical_id))
            aliases_by_path: dict[str, dict] = {}
            for row in group:
                aliases_by_path[row["origin_path"]] = {
                    "origin_path": row["origin_path"],
                    "original_filename": row["original_filename"],
                    "media_type": row["media_type"],
                    "status": row["status"],
                    "error": row["error"],
                    "import_id": row["import_id"],
                    "source_id": row["source_id"],
                    "created_at": row["created_at"],
                }
            for alias in existing_aliases:
                prior = aliases_by_path.get(alias["origin_path"], {})
                aliases_by_path[alias["origin_path"]] = {
                    "origin_path": alias["origin_path"],
                    "original_filename": alias["original_filename"],
                    "media_type": alias["media_type"],
                    "status": alias["status"],
                    "error": alias["error"],
                    "import_id": alias["import_id"],
                    "source_id": alias.get("source_id") or prior.get("source_id"),
                    "created_at": alias["created_at"],
                }
            for alias in aliases_by_path.values():
                identity = {
                    "attachment_id": canonical_id,
                    "origin_path": alias["origin_path"],
                    "original_filename": alias["original_filename"],
                    "media_type": alias["media_type"],
                    "status": alias["status"],
                    "error": alias["error"],
                }
                alias_digest = sha256_hex(json.dumps(
                    identity, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8"))
                self._conn.execute(
                    "INSERT INTO attachment_aliases("
                    "alias_id,attachment_id,origin_path,original_filename,"
                    "media_type,status,error,import_id,source_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"ATTALIAS::{alias_digest}", canonical_id,
                     alias["origin_path"], alias["original_filename"],
                     alias["media_type"], alias["status"], alias["error"],
                     alias["import_id"], alias["source_id"],
                     alias["created_at"]),
                )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attachment_imports_content "
            "ON attachment_imports(blob_sha256)")

    def _migrate_review_requests_v6(self) -> None:
        """移除v5跨job的request_input_digest全局唯一约束。"""
        global_digest_unique = False
        for index in self._conn.execute(
                "PRAGMA index_list(review_requests)").fetchall():
            if not index[2]:
                continue
            columns = [row[2] for row in self._conn.execute(
                f"PRAGMA index_info({index[1]})").fetchall()]
            if columns == ["request_input_digest"]:
                global_digest_unique = True
                break
        if not global_digest_unique:
            return
        self._conn.commit()
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("""
                CREATE TABLE review_requests_v6 (
                    request_id TEXT PRIMARY KEY,
                    request_input_digest TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                    status TEXT NOT NULL CHECK (status IN
                        ('awaiting_authorized_analysis','response_sealed',
                         'consumed','failed')),
                    body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(job_id, request_input_digest)
                )
            """)
            self._conn.execute(
                "INSERT INTO review_requests_v6 SELECT * FROM review_requests")
            self._conn.execute("DROP TABLE review_requests")
            self._conn.execute(
                "ALTER TABLE review_requests_v6 RENAME TO review_requests")
            self._conn.execute(
                "CREATE INDEX idx_review_requests_job "
                "ON review_requests(job_id,request_id)")
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreIntegrityError(
                f"store.v6迁移后外键引用不完整：{len(violations)}项")

    # ---- 阶段与运行 ----

    def new_run(self, input_digest: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs(input_digest) VALUES (?)", (input_digest,)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def init_stages(self, run_id: int) -> None:
        with self._conn:
            for stage in CASE_STAGES:
                self._conn.execute(
                    "INSERT OR IGNORE INTO stages(stage, state, run_id) VALUES (?, 'pending', ?)",
                    (stage, run_id),
                )

    def stage_state(self, stage: str) -> tuple[str, int | None]:
        row = self._conn.execute(
            "SELECT state, run_id FROM stages WHERE stage = ?", (stage,)
        ).fetchone()
        if row is None:
            raise KeyError(f"未知阶段 {stage}")
        return (row["state"], row["run_id"])

    def set_stage(self, stage: str, state: str, run_id: int | None = None,
                  detail: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE stages SET state=?, run_id=COALESCE(?, run_id), detail=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE stage=?",
                (state, run_id, detail, stage),
            )

    # ---- CaseBasis（有源主体与截止）----

    def set_case_basis(self, *, subject_legal_name: str,
                       subject_aliases: list[str], evidence_cutoff: str,
                       subject_source_basis: str, note: str | None = None) -> int:
        """登记本Case评估依据（**版本化追加**，旧版本不可变、不被抹去）。

        返回版本号。当前视图（id=1）指向最新版本；旧结果绑定的版本快照
        保持可读（R1.2-C）。
        """
        if not subject_source_basis or not str(subject_source_basis).strip():
            raise ValueError("case_basis 必须携带主体来源依据（subject_source_basis）")
        snapshot = {
            "subject_legal_name": subject_legal_name,
            "subject_aliases": subject_aliases,
            "evidence_cutoff": evidence_cutoff,
            "subject_source_basis": subject_source_basis,
            "note": note,
        }
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO case_basis_versions(snapshot_json) VALUES (?)",
                (json.dumps(snapshot, ensure_ascii=False),))
            version = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO case_basis(id, subject_legal_name, subject_aliases, "
                "evidence_cutoff, subject_source_basis, note) "
                "VALUES (1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET subject_legal_name=excluded.subject_legal_name, "
                "subject_aliases=excluded.subject_aliases, "
                "evidence_cutoff=excluded.evidence_cutoff, "
                "subject_source_basis=excluded.subject_source_basis, "
                "note=excluded.note",
                (subject_legal_name,
                 json.dumps(subject_aliases, ensure_ascii=False),
                 evidence_cutoff, subject_source_basis, note),
            )
        return version

    def get_case_basis(self) -> dict | None:
        row = self._conn.execute("SELECT * FROM case_basis WHERE id=1").fetchone()
        if row is None:
            return None
        basis = dict(row)
        basis["subject_aliases"] = json.loads(basis["subject_aliases"])
        versions = self._conn.execute(
            "SELECT MAX(version) FROM case_basis_versions").fetchone()
        basis["version"] = versions[0] if versions and versions[0] else None
        return basis

    def get_case_basis_version(self, version: int) -> dict | None:
        """读取指定版本快照（旧结果绑定版本的不可变输入）。"""
        row = self._conn.execute(
            "SELECT version, snapshot_json FROM case_basis_versions WHERE version=?",
            (version,),
        ).fetchone()
        if row is None:
            return None
        return {"version": row["version"],
                **json.loads(row["snapshot_json"])}

    def set_case_basis_if_changed(self, *, subject_legal_name: str,
                                   subject_aliases: list[str],
                                   evidence_cutoff: str,
                                   subject_source_basis: str,
                                   note: str | None = None) -> int:
        """内容不变时复用最新版本（重跑不是新事件）；变化才追加新版本。"""
        snapshot = {
            "subject_legal_name": subject_legal_name,
            "subject_aliases": subject_aliases,
            "evidence_cutoff": evidence_cutoff,
            "subject_source_basis": subject_source_basis,
            "note": note,
        }
        versions = self._conn.execute(
            "SELECT snapshot_json FROM case_basis_versions ORDER BY version "
            "DESC LIMIT 1").fetchall()
        if versions and json.loads(versions[0]["snapshot_json"]) == snapshot:
            current = self.get_case_basis()
            return current["version"]
        return self.set_case_basis(
            subject_legal_name=subject_legal_name,
            subject_aliases=subject_aliases,
            evidence_cutoff=evidence_cutoff,
            subject_source_basis=subject_source_basis, note=note)

    def get_case_basis_versions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT version, snapshot_json, created_at FROM case_basis_versions "
            "ORDER BY version").fetchall()
        out = []
        for row in rows:
            out.append({"version": row["version"],
                        "created_at": row["created_at"],
                        "snapshot": json.loads(row["snapshot_json"])})
        return out

    # ---- 时间证据（追加版本，不改写历史）----

    def append_time_evidence(self, source_id: str, evidence: dict) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO source_time_evidence(source_id, evidence_json) "
                "VALUES (?,?)",
                (source_id, json.dumps(evidence, ensure_ascii=False)),
            )
        return int(cur.lastrowid)

    def get_time_evidence_history(self, source_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT revision, evidence_json, created_at FROM source_time_evidence "
            "WHERE source_id=? ORDER BY revision", (source_id,),
        ).fetchall()
        history = []
        for row in rows:
            entry = {"revision": row["revision"],
                     "created_at": row["created_at"]}
            entry.update(json.loads(row["evidence_json"]))
            history.append(entry)
        return history

    def latest_time_evidence(self, source_id: str) -> dict | None:
        history = self.get_time_evidence_history(source_id)
        return history[-1] if history else None

    # ---- Provider 用量账本（派发前原子预留，结算不改写） ----

    def reserve_provider_usage(self, *, budget_scope: str,
                               task_key: str, purpose: str,
                               provider_id: str, input_reserved: int,
                               output_reserved: int,
                               historic_usage: list[dict] | None,
                               request_cap: int, input_cap: int,
                               output_cap: int,
                               authorization_digest: str | None = None,
                               lineage_scopes: list[str] | None = None) -> None:
        """单一写事务内以账本实况计算可用额度，校验后写入新预留。

        余额只在事务内重算：已结算且usage已知的行按实际值计消费；未结算
        reserved行与结算为unknown的行按其请求预留上界保守计占用。汇总范围
        为 budget_scope 及 lineage_scopes（同预算的历史范围标识，如 v11
        迁入行的 authorization_digest 或授权声明的 lineage），避免旧消费
        因范围标识演进而漏算；不在此范围内的行由调用方按孤儿行拒绝，不
        在本函数静默当作零。调用者不传入任何余额数值；historic_usage 仅
        承载账本外（前v11时代journal）mission，且逐项必须自证：known带
        实际值，unknown必须带 input_bound/output_bound与evidence_source，
        否则剩余额度不可证明，整体回滚拒绝。请求数、输入、输出三个上界
        同时成立；任一越限即零写入。
        """
        historic_usage = historic_usage or []
        scopes = [budget_scope, *(lineage_scopes or [])]
        placeholders = ",".join("?" * len(scopes))
        with self.immediate_transaction():
            row = self._conn.execute(
                "SELECT task_key FROM provider_usage_ledger WHERE task_key=?",
                (task_key,)).fetchone()
            if row is not None:
                raise BudgetRejected(f"用量预留已存在：{task_key}")
            rows = self._conn.execute(
                f"SELECT task_key, status, usage_status, input_actual, "
                f"output_actual, input_reserved, output_reserved FROM "
                f"provider_usage_ledger WHERE budget_scope IN "
                f"({placeholders})", scopes).fetchall()
            requests = len(rows)
            input_used = 0
            output_used = 0
            for item in rows:
                if item["usage_status"] == "known" \
                        and item["input_actual"] is not None:
                    input_used += item["input_actual"]
                    output_used += item["output_actual"] or 0
                else:
                    # 未结算或usage未知：按该次请求的预留上界保守占用。
                    input_used += item["input_reserved"]
                    output_used += item["output_reserved"]
            for item in historic_usage:
                if not isinstance(item, dict) or not item.get("task_key"):
                    raise BudgetRejected(
                        f"历史用量项缺少task_key：{item!r}")
                if item.get("usage_status") == "known":
                    actual_in, actual_out = item.get("input_actual"), \
                        item.get("output_actual")
                    if not (isinstance(actual_in, int) and actual_in >= 0
                            and isinstance(actual_out, int)
                            and actual_out >= 0):
                        raise BudgetRejected(
                            f"历史known用量缺少非负实际值：{item['task_key']}")
                    input_used += actual_in
                    output_used += actual_out
                else:
                    bound_in, bound_out = item.get("input_bound"), \
                        item.get("output_bound")
                    if not (isinstance(bound_in, int) and bound_in >= 0
                            and isinstance(bound_out, int) and bound_out >= 0
                            and isinstance(item.get("evidence_source"), str)
                            and item["evidence_source"].strip()):
                        raise BudgetRejected(
                            f"历史未知用量无有依据的逐项保守上界："
                            f"{item['task_key']}；剩余额度不可证明，拒绝预留")
                    input_used += bound_in
                    output_used += bound_out
                requests += 1
            if requests + 1 > request_cap:
                raise BudgetRejected(
                    f"预留后请求次数{requests + 1}超过上界{request_cap}")
            if input_used + input_reserved > input_cap:
                raise BudgetRejected(
                    f"预留后累计输入token超限：账本内实况{input_used}"
                    f"+本次预留{input_reserved}>{input_cap}")
            if output_used + output_reserved > output_cap:
                raise BudgetRejected(
                    f"预留后累计输出token超限：账本内实况{output_used}"
                    f"+本次预留{output_reserved}>{output_cap}")
            self._conn.execute(
                "INSERT INTO provider_usage_ledger(task_key, "
                "authorization_digest, budget_scope, purpose, provider_id, "
                "status, input_reserved, output_reserved) "
                "VALUES (?,?,?,?,?, 'reserved', ?,?)",
                (task_key, authorization_digest or "", budget_scope,
                 purpose, provider_id, input_reserved, output_reserved))

    def settle_provider_usage(self, task_key: str, *, status: str,
                              input_actual: int | None = None,
                              output_actual: int | None = None,
                              usage_status: str = "unknown",
                              detail: str | None = None) -> None:
        if status not in {"succeeded", "failed", "outcome_unknown"}:
            raise ValueError(f"结算状态非法：{status}")
        if usage_status not in {"known", "unknown"}:
            raise ValueError(f"usage状态非法：{usage_status}")
        with self.immediate_transaction():
            cur = self._conn.execute(
                "UPDATE provider_usage_ledger SET status=?, input_actual=?, "
                "output_actual=?, usage_status=?, detail=?, updated_at="
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE task_key=? AND status='reserved'",
                (status, input_actual, output_actual, usage_status, detail,
                 task_key))
            if cur.rowcount != 1:
                raise BudgetRejected(
                    f"用量结算条件更新失败（无预留或已结算）：{task_key}")

    def fetch_provider_usage_ledger(
            self, authorization_digest: str | None = None) -> list[dict]:
        if authorization_digest is None:
            rows = self._conn.execute(
                "SELECT * FROM provider_usage_ledger ORDER BY created_at, "
                "task_key").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM provider_usage_ledger WHERE "
                "authorization_digest=? ORDER BY created_at, task_key",
                (authorization_digest,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 采集依赖封存（真实blob引用，非文件名清单）----

    def add_capture_dependency(self, import_id: int, dep_name: str,
                               blob_sha256: str, byte_length: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO capture_dependencies(import_id, dep_name, "
                "blob_sha256, byte_length) VALUES (?,?,?,?)",
                (import_id, dep_name, blob_sha256, byte_length),
            )

    def get_capture_dependencies(self, import_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT dep_name, blob_sha256, byte_length FROM capture_dependencies "
            "WHERE import_id=? ORDER BY dep_name", (import_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_import(self, kind: str, origin_path: str) -> dict | None:
        """按种类+来源路径查已有导入记录（导入幂等：重跑不重复登记）。"""
        row = self._conn.execute(
            "SELECT * FROM import_records WHERE kind=? AND origin_path=? "
            "ORDER BY import_id LIMIT 1", (kind, origin_path),
        ).fetchone()
        return dict(row) if row else None

    def dependency_stats(self) -> dict:
        """依赖三口径分列：行数 / 逻辑依赖项（导入记录×文件名）/ 不同内容blob。"""
        rows = self._conn.execute(
            "SELECT i.origin_path, d.dep_name, d.blob_sha256 "
            "FROM capture_dependencies d JOIN import_records i "
            "ON d.import_id = i.import_id").fetchall()
        return {
            "dependency_rows": len(rows),
            "logical_dependencies": len({(r["origin_path"], r["dep_name"])
                                         for r in rows}),
            "distinct_dependency_blobs": len({r["blob_sha256"] for r in rows}),
        }

    def add_claim_mapping(self, claim_id: str, criterion_id: str, *,
                          quote_start: int, quote_end: int, quote_sha256: str,
                          filter_hits: list[str], status: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO claim_criterion_mappings(claim_id, criterion_id, "
                "quote_start, quote_end, quote_sha256, filter_hits, status) "
                "VALUES (?,?,?,?,?,?,?)",
                (claim_id, criterion_id, quote_start, quote_end, quote_sha256,
                 json.dumps(filter_hits, ensure_ascii=False), status),
            )

    def fetch_mappings(self, claim_id: str, criterion_id: str) -> list[dict]:
        """取该主张×判据的全部映射记录（按时间序；末行为最新）。"""
        rows = self._conn.execute(
            "SELECT * FROM claim_criterion_mappings WHERE claim_id=? AND "
            "criterion_id=? ORDER BY id", (claim_id, criterion_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_mapping_review(self, review_id: str, *, case_basis_version: int,
                           claim_id: str, criterion_id: str, quote_sha256: str,
                           support_scope: str, decision: str, reviewer: str,
                           review_basis: str) -> None:
        """登记一次实际离线/人工映射复核，运行期没有创建该记录的权限。"""
        if decision not in ("confirmed", "rejected"):
            raise ValueError(f"映射复核 decision 非法：{decision!r}")
        if not all(isinstance(value, str) and value.strip() for value in
                   (review_id, claim_id, criterion_id, quote_sha256,
                    support_scope, reviewer, review_basis)):
            raise ValueError("映射复核必须绑定非空的对象、范围、复核人和依据")
        with self._conn:
            self._conn.execute(
                "INSERT INTO mapping_reviews(review_id, case_basis_version, claim_id, "
                "criterion_id, quote_sha256, support_scope, decision, reviewer, "
                "review_basis) VALUES (?,?,?,?,?,?,?,?,?)",
                (review_id, case_basis_version, claim_id, criterion_id, quote_sha256,
                 support_scope, decision, reviewer, review_basis),
            )

    def get_mapping_review(self, review_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM mapping_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_crl_evidence_review(self, review_id: str, *, case_basis_version: int,
                                claim_id: str, criterion_id: str, quote_sha256: str,
                                decision: str, findings: dict, support_scope: str,
                                subject_scope: str, reviewer: str,
                                review_basis: str) -> None:
        if decision not in ("supports", "does_not_support") or not isinstance(findings, dict):
            raise ValueError("CRL复核decision或findings非法")
        with self._conn:
            self._conn.execute(
                "INSERT INTO crl_evidence_reviews(review_id,case_basis_version,claim_id,criterion_id,quote_sha256,decision,findings_json,subject_scope,support_scope,reviewer,review_basis) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, case_basis_version, claim_id, criterion_id, quote_sha256,
                 decision, json.dumps(findings, ensure_ascii=False, sort_keys=True),
                 subject_scope, support_scope, reviewer, review_basis),
            )

    def fetch_crl_evidence_reviews(self, case_basis_version: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM crl_evidence_reviews WHERE case_basis_version=? ORDER BY review_id",
            (case_basis_version,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["findings"] = json.loads(item.pop("findings_json"))
            out.append(item)
        return out

    def get_crl_evidence_review(self, review_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM crl_evidence_reviews WHERE review_id=?", (review_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["findings"] = json.loads(item.pop("findings_json"))
        return item

    def get_crl_dimension_result(self, input_digest: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM crl_dimension_results WHERE input_digest=?", (input_digest,)
        ).fetchone()
        return dict(row) if row else None

    def get_crl_dimension_result_by_id(self, result_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM crl_dimension_results WHERE result_id=?", (result_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_crl_dimension_result_in_transaction(
            self, result_id: str, *, input_digest: str, case_basis_version: int,
            scope: str, product_status: str, result_blob_sha256: str) -> None:
        if not self._conn.in_transaction:
            raise RuntimeError("CRL维度结果发布必须位于显式事务内")
        self._conn.execute(
            "INSERT INTO crl_dimension_results(result_id,input_digest,case_basis_version,scope,product_status,result_blob_sha256) VALUES (?,?,?,?,?,?)",
            (result_id, input_digest, case_basis_version, scope, product_status,
             result_blob_sha256),
        )

    def count_crl_dimension_results(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM crl_dimension_results").fetchone()[0])

    def add_dimension_evidence_review(
            self, review_id: str, *, dimension_id: str, case_basis_version: int,
            claim_id: str, criterion_id: str, quote_sha256: str, decision: str,
            evidence_class: str, findings: dict, subject_scope: str,
            scope_id: str, support_scope: str, reviewer: str,
            review_basis: str, permission_binding: dict | None = None,
            workflow_materialization: dict | None = None) -> None:
        """登记非CRL维度的受控、准则绑定复核记录。"""
        text_fields = (
            review_id, dimension_id, claim_id, criterion_id, quote_sha256,
            evidence_class, subject_scope, scope_id, support_scope, reviewer,
            review_basis,
        )
        if decision not in {"supports", "does_not_support"} \
                or not isinstance(findings, dict) \
                or not all(isinstance(value, str) and value.strip()
                           for value in text_fields):
            raise ValueError("维度复核记录的身份、decision、证据类别或findings非法")
        findings_json = _strict_json_dumps(findings, label="维度复核findings")
        if permission_binding is not None and workflow_materialization is not None:
            raise ValueError("维度review不能同时绑定旧用途许可与workflow物化sidecar")
        if permission_binding is not None:
            from .evidence_permissions import validate_permission_binding

            permission_binding = validate_permission_binding(
                permission_binding, review_id=review_id)
        if workflow_materialization is not None:
            workflow_materialization = validate_workflow_review_materialization(
                workflow_materialization, review_id=review_id)
            expected = {
                "dimension_id": dimension_id,
                "case_basis_version": case_basis_version,
                "claim_id": claim_id,
                "criterion_id": criterion_id,
                "quote_sha256": quote_sha256,
                "decision": decision,
                "evidence_class": evidence_class,
                "findings": findings,
                "subject_scope": subject_scope,
                "scope_id": scope_id,
                "support_scope": support_scope,
                "reviewer": reviewer,
                "review_basis": review_basis,
            }
            if any(workflow_materialization.get(key) != value
                   for key, value in expected.items()):
                raise ValueError("workflow物化sidecar与维度review正文不一致")
        permission_mode = (
            "license_v2" if permission_binding is not None
            else ("workflow_authorization_v1"
                  if workflow_materialization is not None else "legacy_unbound"))

        def write() -> None:
            self._conn.execute(
                "INSERT INTO dimension_evidence_reviews("
                "review_id,dimension_id,case_basis_version,claim_id,criterion_id,"
                "quote_sha256,decision,evidence_class,findings_json,subject_scope,"
                "scope_id,support_scope,reviewer,review_basis,permission_mode) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, dimension_id, case_basis_version, claim_id,
                 criterion_id, quote_sha256, decision, evidence_class,
                 findings_json,
                 subject_scope, scope_id, support_scope, reviewer, review_basis,
                 permission_mode),
            )
            if permission_binding is not None:
                self._conn.execute(
                    "INSERT INTO dimension_review_permissions("
                    "review_id,license_id,requested_use,candidate_json,"
                    "candidate_digest,confirmation_json,confirmation_digest,"
                    "license_json,binding_digest) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        review_id,
                        permission_binding["license_id"],
                        permission_binding["requested_use"],
                        _strict_json_dumps(
                            permission_binding["candidate"],
                            label="permission candidate"),
                        permission_binding["candidate_digest"],
                        _strict_json_dumps(
                            permission_binding["confirmation"],
                            label="permission confirmation"),
                        permission_binding["confirmation_digest"],
                        _strict_json_dumps(
                            permission_binding["license"],
                            label="permission license"),
                        permission_binding["binding_digest"],
                    ),
                )
            if workflow_materialization is not None:
                self._conn.execute(
                    "INSERT INTO workflow_review_materializations("
                    "materialization_id,review_id,job_id,request_id,response_id,"
                    "authorization_id,materialization_digest,body_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        workflow_materialization["materialization_id"],
                        review_id,
                        workflow_materialization["job_id"],
                        workflow_materialization["request_id"],
                        workflow_materialization["response_id"],
                        workflow_materialization["authorization_id"],
                        workflow_materialization["materialization_digest"],
                        _strict_json_dumps(
                            workflow_materialization,
                            label="workflow review物化sidecar"),
                    ),
                )

        if self._conn.in_transaction:
            write()
        else:
            with self._conn:
                write()

    def fetch_dimension_evidence_reviews(
            self, dimension_id: str, case_basis_version: int,
            scope_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dimension_evidence_reviews WHERE dimension_id=? "
            "AND case_basis_version=? AND scope_id=? ORDER BY review_id",
            (dimension_id, case_basis_version, scope_id),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["findings"] = json.loads(item.pop("findings_json"))
            out.append(item)
        return out

    def get_dimension_evidence_review(self, review_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dimension_evidence_reviews WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["findings"] = json.loads(item.pop("findings_json"))
        return item

    def get_dimension_review_permission(self, review_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dimension_review_permissions WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item.pop("created_at", None)
        item["schema_version"] = \
            "kth-hybrid.dimension-review-permission-binding.v1"
        item["candidate"] = json.loads(item.pop("candidate_json"))
        item["confirmation"] = json.loads(item.pop("confirmation_json"))
        item["license"] = json.loads(item.pop("license_json"))
        return item

    def get_workflow_review_materialization(self, review_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_review_materializations WHERE review_id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            return None
        item = json.loads(row["body_json"])
        for key in (
                "materialization_id", "review_id", "job_id", "request_id",
                "response_id", "authorization_id", "materialization_digest"):
            if item.get(key) != row[key]:
                raise StoreIntegrityError(
                    "workflow review物化sidecar索引与正文不一致")
        return item

    def add_tmrl_identity_overlay(
            self, overlay_id: str, *, case_basis_version: int, scope_id: str,
            subject_id: str, resolution_status: str, subject_ref: dict,
            status_ref: dict, scope_ref: dict) -> None:
        """登记逐人员/团队、内容绑定且不可覆盖的TMRL身份解析记录。"""
        text_fields = (overlay_id, scope_id, subject_id)
        allowed_statuses = {
            "verified", "probable", "ok", "unverified", "ambiguous",
            "same_name_only",
        }
        if not isinstance(case_basis_version, int) or case_basis_version <= 0 \
                or not all(isinstance(value, str) and value.strip()
                           for value in text_fields) \
                or resolution_status not in allowed_statuses \
                or not all(isinstance(value, dict)
                           for value in (subject_ref, status_ref, scope_ref)):
            raise ValueError("TMRL身份overlay身份、状态或证明引用非法")
        refs = {
            "subject_ref_json": _strict_json_dumps(
                subject_ref, label="TMRL身份subject_ref"),
            "status_ref_json": _strict_json_dumps(
                status_ref, label="TMRL身份status_ref"),
            "scope_ref_json": _strict_json_dumps(
                scope_ref, label="TMRL身份scope_ref"),
        }
        with self._conn:
            self._conn.execute(
                "INSERT INTO tmrl_identity_overlays("
                "overlay_id,case_basis_version,scope_id,subject_id,"
                "resolution_status,subject_ref_json,status_ref_json,scope_ref_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (overlay_id, case_basis_version, scope_id, subject_id,
                 resolution_status, refs["subject_ref_json"],
                 refs["status_ref_json"], refs["scope_ref_json"]),
            )

    @staticmethod
    def _decode_tmrl_identity_overlay(row) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        for key in ("subject_ref", "status_ref", "scope_ref"):
            item[key] = json.loads(item.pop(f"{key}_json"))
        return item

    def fetch_tmrl_identity_overlays(
            self, case_basis_version: int, scope_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM tmrl_identity_overlays WHERE case_basis_version=? "
            "AND scope_id=? ORDER BY subject_id",
            (case_basis_version, scope_id),
        ).fetchall()
        return [self._decode_tmrl_identity_overlay(row) for row in rows]

    def get_tmrl_identity_overlay(self, overlay_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tmrl_identity_overlays WHERE overlay_id=?",
            (overlay_id,),
        ).fetchone()
        return self._decode_tmrl_identity_overlay(row)

    def get_dimension_result(self, input_digest: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dimension_results WHERE input_digest=?",
            (input_digest,),
        ).fetchone()
        return dict(row) if row else None

    def get_dimension_result_by_id(self, result_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dimension_results WHERE result_id=?", (result_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_dimension_result_in_transaction(
            self, result_id: str, *, dimension_id: str, input_digest: str,
            case_basis_version: int, scope: str, scope_id: str,
            product_status: str, result_blob_sha256: str) -> None:
        if not self._conn.in_transaction:
            raise RuntimeError("维度结果发布必须位于显式事务内")
        self._conn.execute(
            "INSERT INTO dimension_results("
            "result_id,dimension_id,input_digest,case_basis_version,scope,scope_id,"
            "product_status,result_blob_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (result_id, dimension_id, input_digest, case_basis_version, scope,
             scope_id, product_status, result_blob_sha256),
        )

    def count_dimension_results(self, dimension_id: str | None = None) -> int:
        if dimension_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM dimension_results").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM dimension_results WHERE dimension_id=?",
                (dimension_id,),
            ).fetchone()
        return int(row[0])

    # ---- 本地产品工作流（追加式、内容寻址）----

    def get_attachment_import(self, *, origin_path: str,
                              blob_sha256: str,
                              media_type: str | None = None) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attachment_imports WHERE blob_sha256=? "
            "ORDER BY import_id LIMIT 1", (blob_sha256,)).fetchone()
        if row is not None:
            return dict(row)
        row = self._conn.execute(
            "SELECT * FROM attachment_imports WHERE origin_path=? "
            "AND blob_sha256=?", (origin_path, blob_sha256)).fetchone()
        return dict(row) if row else None

    def add_attachment_import(self, item: dict) -> dict:
        required = {
            "attachment_id", "origin_path", "blob_sha256", "byte_length",
            "original_filename", "media_type", "status", "error",
            "import_id", "source_id",
        }
        if set(item) != required:
            raise ValueError("附件导入记录字段不完整或含额外字段")
        if item["status"] not in {"saved", "unsupported", "failed"}:
            raise ValueError("附件导入状态非法")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO attachment_imports("
                "attachment_id,origin_path,blob_sha256,byte_length,"
                "original_filename,media_type,status,error,import_id,source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(item[key] for key in (
                    "attachment_id", "origin_path", "blob_sha256",
                    "byte_length", "original_filename", "media_type", "status",
                    "error", "import_id", "source_id")),
            )
        stored = self.get_attachment_import(
            origin_path=item["origin_path"], blob_sha256=item["blob_sha256"],
            media_type=item["media_type"])
        if stored is None:
            raise RuntimeError("附件导入记录持久化失败")
        return stored

    def count_attachment_imports(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM attachment_imports").fetchone()[0])

    def get_attachment_alias(self, attachment_id: str,
                             origin_path: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM attachment_aliases WHERE attachment_id=? "
            "AND origin_path=?", (attachment_id, origin_path)).fetchone()
        return dict(row) if row else None

    def add_attachment_alias(self, item: dict) -> dict:
        required = {
            "alias_id", "attachment_id", "origin_path", "original_filename",
            "media_type", "status", "error", "import_id", "source_id",
        }
        if set(item) != required:
            raise ValueError("附件来源别名字段不完整或含额外字段")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO attachment_aliases("
                "alias_id,attachment_id,origin_path,original_filename,"
                "media_type,status,error,import_id,source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(item[key] for key in (
                    "alias_id", "attachment_id", "origin_path",
                    "original_filename", "media_type", "status", "error",
                    "import_id", "source_id")),
            )
        stored = self.get_attachment_alias(
            item["attachment_id"], item["origin_path"])
        if stored is None:
            raise RuntimeError("附件来源别名持久化失败")
        return stored

    def count_attachment_aliases(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM attachment_aliases").fetchone()[0])

    def promote_attachment_source(self, attachment_id: str, *, source_id: str,
                                  media_type: str, import_id: int) -> None:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE attachment_imports SET source_id=?,media_type=?,"
                "status='saved',error=NULL,import_id=? WHERE attachment_id=? "
                "AND source_id IS NULL",
                (source_id, media_type, import_id, attachment_id))
        if cur.rowcount not in {0, 1}:
            raise RuntimeError("附件业务对象来源提升失败")

    @staticmethod
    def _decode_projection(row) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["locator"] = json.loads(item.pop("locator_json"))
        return item

    def add_text_projection(self, item: dict) -> dict:
        required = {
            "projection_id", "source_id", "source_blob_sha256", "locator",
            "text_sha256", "text_blob_sha256", "char_count", "tool",
            "status", "error",
        }
        if set(item) != required:
            raise ValueError("文本投影记录字段不完整或含额外字段")
        if item["status"] not in {"projected", "unprocessed", "failed"}:
            raise ValueError("文本投影状态非法")
        locator_json = _strict_json_dumps(item["locator"], label="文本投影定位")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO text_projections("
                "projection_id,source_id,source_blob_sha256,locator_json,"
                "text_sha256,text_blob_sha256,char_count,tool,status,error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item["projection_id"], item["source_id"],
                 item["source_blob_sha256"], locator_json,
                 item["text_sha256"], item["text_blob_sha256"],
                 item["char_count"], item["tool"], item["status"],
                 item["error"]),
            )
        stored = self.get_text_projection(item["projection_id"])
        if stored is None:
            raise RuntimeError("文本投影记录持久化失败")
        return stored

    def replace_text_projections_atomic(self, source_id: str,
                                        items: list[dict]) -> list[dict]:
        """单事务替换一个Source的完整投影集合；任一行失败则全部回滚。"""
        if not isinstance(items, list) or not items \
                or any(item.get("source_id") != source_id for item in items) \
                or len({item.get("projection_id") for item in items}) != len(items):
            raise ValueError("完整投影集合非法、为空或身份重复")
        required = {
            "projection_id", "source_id", "source_blob_sha256", "locator",
            "text_sha256", "text_blob_sha256", "char_count", "tool",
            "status", "error",
        }
        with self._conn:
            self._conn.execute(
                "DELETE FROM text_projections WHERE source_id=?", (source_id,))
            for item in items:
                if set(item) != required \
                        or item["status"] not in {
                            "projected", "unprocessed", "failed"}:
                    raise ValueError("完整投影集合中的记录非法")
                self._conn.execute(
                    "INSERT INTO text_projections("
                    "projection_id,source_id,source_blob_sha256,locator_json,"
                    "text_sha256,text_blob_sha256,char_count,tool,status,error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (item["projection_id"], item["source_id"],
                     item["source_blob_sha256"],
                     _strict_json_dumps(item["locator"], label="文本投影定位"),
                     item["text_sha256"], item["text_blob_sha256"],
                     item["char_count"], item["tool"], item["status"],
                     item["error"]),
                )
        return self.fetch_text_projections(source_id)

    def get_text_projection(self, projection_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM text_projections WHERE projection_id=?",
            (projection_id,)).fetchone()
        return self._decode_projection(row)

    def fetch_text_projections(self, source_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM text_projections WHERE source_id=? "
            "ORDER BY projection_id", (source_id,)).fetchall()
        return [self._decode_projection(row) for row in rows]

    @staticmethod
    def _decode_workflow_job(row) -> dict | None:
        if row is None:
            return None
        stored = dict(row)
        body = json.loads(stored.pop("body_json"))
        failure_json = stored.pop("failure_json")
        body.update({
            "state": stored["state"],
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
        })
        if failure_json is not None:
            body["failure"] = json.loads(failure_json)
        return body

    def add_workflow_job(self, item: dict) -> dict:
        required = {"schema_version", "job_id", "input_digest", "state"}
        if not required <= set(item):
            raise ValueError("工作流job缺少身份或状态字段")
        body = {key: value for key, value in item.items()
                if key not in {"state", "failure", "created_at", "updated_at"}}
        failure = item.get("failure")
        body_json = _strict_json_dumps(body, label="工作流job")
        failure_json = (_strict_json_dumps(failure, label="工作流失败")
                        if failure is not None else None)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO workflow_jobs("
                "job_id,input_digest,schema_version,state,body_json,failure_json) "
                "VALUES (?,?,?,?,?,?)",
                (item["job_id"], item["input_digest"],
                 item["schema_version"], item["state"], body_json,
                 failure_json),
            )
        stored = self.get_workflow_job(item["job_id"])
        if stored is None:
            raise RuntimeError("工作流job持久化失败")
        return stored

    def add_workflow_bundle(self, item: dict,
                            requests: list[dict]) -> dict:
        """将一个新job及其全部review request置于同一SQLite事务。"""
        required = {"schema_version", "job_id", "input_digest", "state"}
        if not required <= set(item) or not isinstance(requests, list) \
                or not requests:
            raise ValueError("工作流job/request bundle不完整")
        body = {key: value for key, value in item.items()
                if key not in {"state", "failure", "created_at", "updated_at"}}
        body_json = _strict_json_dumps(body, label="工作流job")
        failure = item.get("failure")
        failure_json = (_strict_json_dumps(failure, label="工作流失败")
                        if failure is not None else None)
        with self._conn:
            self._conn.execute(
                "INSERT INTO workflow_jobs("
                "job_id,input_digest,schema_version,state,body_json,failure_json) "
                "VALUES (?,?,?,?,?,?)",
                (item["job_id"], item["input_digest"], item["schema_version"],
                 item["state"], body_json, failure_json))
            for request in requests:
                request_body = _strict_json_dumps(
                    {key: value for key, value in request.items()
                     if key != "status"}, label="专业复核请求")
                self._conn.execute(
                    "INSERT INTO review_requests("
                    "request_id,request_input_digest,job_id,status,body_json) "
                    "VALUES (?,?,?,?,?)",
                    (request["request_id"], request["request_input_digest"],
                     request["job_id"], request["status"], request_body))
        stored = self.get_workflow_job(item["job_id"])
        if stored is None or len(self.fetch_review_requests(
                item["job_id"])) != len(requests):
            raise RuntimeError("工作流job/request bundle持久化不完整")
        return stored

    def add_candidate_workflow_bundle(self, item: dict,
                                      requests: list[dict]) -> dict:
        """将v2 job与全部候选提出请求置于同一SQLite事务。"""
        required = {"schema_version", "job_id", "input_digest", "state"}
        if not required <= set(item) or not isinstance(requests, list) \
                or not requests:
            raise ValueError("候选job/request bundle不完整")
        body = {key: value for key, value in item.items()
                if key not in {"state", "failure", "created_at", "updated_at"}}
        body_json = _strict_json_dumps(body, label="候选工作流job")
        with self._conn:
            self._conn.execute(
                "INSERT INTO workflow_jobs("
                "job_id,input_digest,schema_version,state,body_json,failure_json) "
                "VALUES (?,?,?,?,?,NULL)",
                (item["job_id"], item["input_digest"], item["schema_version"],
                 item["state"], body_json))
            for request in requests:
                request_body = _strict_json_dumps(
                    {key: value for key, value in request.items()
                     if key != "status"}, label="候选提出请求")
                self._conn.execute(
                    "INSERT INTO proposal_requests("
                    "request_id,request_input_digest,job_id,status,body_json) "
                    "VALUES (?,?,?,?,?)",
                    (request["request_id"], request["request_input_digest"],
                     request["job_id"], request["status"], request_body))
        stored = self.get_workflow_job(item["job_id"])
        if stored is None or len(self.fetch_proposal_requests(
                item["job_id"])) != len(requests):
            raise RuntimeError("候选job/request bundle持久化不完整")
        return stored

    def get_workflow_job(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._decode_workflow_job(row)

    def count_workflow_jobs(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM workflow_jobs").fetchone()[0])

    def set_workflow_job_state(self, job_id: str, state: str, *,
                               failure: dict | None = None) -> None:
        failure_json = (_strict_json_dumps(failure, label="工作流失败")
                        if failure is not None else None)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE workflow_jobs SET state=?, failure_json=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE job_id=?", (state, failure_json, job_id))
        if cur.rowcount != 1:
            raise KeyError(f"工作流job {job_id} 不存在")

    @staticmethod
    def _decode_workflow_dimension_output(row) -> dict | None:
        return dict(row) if row is not None else None

    def fetch_workflow_job_dimension_outputs(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT job_id,dimension_id,result_id,input_digest,trace_digest,created_at "
            "FROM workflow_job_dimension_outputs WHERE job_id=? "
            "ORDER BY dimension_id", (job_id,)).fetchall()
        return [self._decode_workflow_dimension_output(row) for row in rows]

    def get_workflow_job_dimension_output(self, job_id: str,
                                          dimension_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT job_id,dimension_id,result_id,input_digest,trace_digest,created_at "
            "FROM workflow_job_dimension_outputs WHERE job_id=? AND dimension_id=?",
            (job_id, dimension_id)).fetchone()
        return self._decode_workflow_dimension_output(row)

    def add_workflow_job_dimension_output(
            self, *, job_id: str, dimension_id: str, result_id: str,
            input_digest: str, trace_digest: str) -> dict:
        """登记已实际重核trace的精确维度结果；不提供latest替换语义。"""
        values = (job_id, dimension_id, result_id, input_digest, trace_digest)
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("workflow维度输出身份字段必须为非空字符串")
        with self.immediate_transaction():
            existing = self.get_workflow_job_dimension_output(job_id, dimension_id)
            expected = {
                "job_id": job_id,
                "dimension_id": dimension_id,
                "result_id": result_id,
                "input_digest": input_digest,
                "trace_digest": trace_digest,
            }
            if existing is None:
                self._conn.execute(
                    "INSERT INTO workflow_job_dimension_outputs("
                    "job_id,dimension_id,result_id,input_digest,trace_digest) "
                    "VALUES (?,?,?,?,?)", values)
            elif any(existing[key] != value for key, value in expected.items()):
                raise StoreIntegrityError("workflow维度输出已登记为不同精确结果")
        stored = self.get_workflow_job_dimension_output(job_id, dimension_id)
        if stored is None:
            raise StoreIntegrityError("workflow维度输出持久化失败")
        return stored

    def get_workflow_job_artifacts(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_job_artifacts WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["source_modes"] = json.loads(value.pop("source_modes_json"))
        return value

    def get_workflow_job_artifacts_by_object(self, object_id: str) -> dict | None:
        if not isinstance(object_id, str) or not object_id.strip():
            raise ValueError("workflow工件对象ID必须为非空字符串")
        column = ("manifest_id" if object_id.startswith("AGGMAN::")
                  else "view_id" if object_id.startswith("OFFLINE6::")
                  else None)
        if column is None:
            return None
        row = self._conn.execute(
            f"SELECT * FROM workflow_job_artifacts WHERE {column}=?", (object_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["source_modes"] = json.loads(value.pop("source_modes_json"))
        return value

    def add_workflow_job_artifacts(
            self, *, job_id: str, manifest_id: str, manifest_digest: str,
            manifest_blob_sha256: str, view_id: str, view_digest: str,
            view_blob_sha256: str, source_modes: list[str]) -> dict:
        """原子冻结manifest/view blob引用及实际使用的来源模式。"""
        values = {
            "job_id": job_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "manifest_blob_sha256": manifest_blob_sha256,
            "view_id": view_id,
            "view_digest": view_digest,
            "view_blob_sha256": view_blob_sha256,
        }
        if any(not isinstance(value, str) or not value.strip()
               for value in values.values()):
            raise ValueError("workflow工件身份字段必须为非空字符串")
        if not isinstance(source_modes, list) or not source_modes or any(
                not isinstance(value, str) or not value.strip()
                for value in source_modes):
            raise ValueError("workflow来源模式必须为非空字符串列表")
        source_modes = sorted(set(source_modes))
        with self.immediate_transaction():
            existing = self.get_workflow_job_artifacts(job_id)
            expected = {**values, "source_modes": source_modes}
            if existing is None:
                self._conn.execute(
                    "INSERT INTO workflow_job_artifacts("
                    "job_id,manifest_id,manifest_digest,manifest_blob_sha256,"
                    "view_id,view_digest,view_blob_sha256,source_modes_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (*values.values(), _strict_json_dumps(
                        source_modes, label="workflow来源模式")))
            elif any(existing[key] != value for key, value in expected.items()):
                raise StoreIntegrityError("workflow工件已登记为不同冻结对象")
        stored = self.get_workflow_job_artifacts(job_id)
        if stored is None:
            raise StoreIntegrityError("workflow工件持久化失败")
        return stored

    def freeze_workflow_job_artifacts(
            self, *, job_id: str, manifest_id: str, manifest_digest: str,
            manifest_blob_sha256: str, view_id: str, view_digest: str,
            view_blob_sha256: str, source_modes: list[str]) -> dict:
        """同一立即事务登记工件并进入manifest_frozen，禁止两者出现裂缝。"""
        values = {
            "job_id": job_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "manifest_blob_sha256": manifest_blob_sha256,
            "view_id": view_id,
            "view_digest": view_digest,
            "view_blob_sha256": view_blob_sha256,
        }
        if any(not isinstance(value, str) or not value.strip()
               for value in values.values()):
            raise ValueError("workflow冻结工件身份字段必须为非空字符串")
        if not isinstance(source_modes, list) or not source_modes or any(
                not isinstance(value, str) or not value.strip()
                for value in source_modes):
            raise ValueError("workflow冻结来源模式必须为非空字符串列表")
        source_modes = sorted(set(source_modes))
        with self.immediate_transaction():
            job = self._conn.execute(
                "SELECT state FROM workflow_jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"工作流job {job_id} 不存在")
            existing = self.get_workflow_job_artifacts(job_id)
            expected = {**values, "source_modes": source_modes}
            if existing is None:
                if job["state"] != "evaluated":
                    raise StoreIntegrityError(
                        "workflow工件只能从evaluated状态原子冻结")
                self._conn.execute(
                    "INSERT INTO workflow_job_artifacts("
                    "job_id,manifest_id,manifest_digest,manifest_blob_sha256,"
                    "view_id,view_digest,view_blob_sha256,source_modes_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (*values.values(), _strict_json_dumps(
                        source_modes, label="workflow冻结来源模式")))
                self._conn.execute(
                    "UPDATE workflow_jobs SET state='manifest_frozen',"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE job_id=? AND state='evaluated'", (job_id,))
            elif any(existing[key] != value for key, value in expected.items()):
                raise StoreIntegrityError("workflow冻结工件已登记为不同对象")
            elif job["state"] == "evaluated":
                self._conn.execute(
                    "UPDATE workflow_jobs SET state='manifest_frozen',"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE job_id=? AND state='evaluated'", (job_id,))
            elif job["state"] not in {"manifest_frozen", "completed"}:
                raise StoreIntegrityError("workflow既有工件对应的job状态非法")
        stored = self.get_workflow_job_artifacts(job_id)
        if stored is None:
            raise StoreIntegrityError("workflow冻结工件持久化失败")
        return stored

    def refresh_workflow_job_state(self, job_id: str, *,
                                   resume_failed: bool = False) -> str:
        """同一BEGIN IMMEDIATE内读取request并单调更新job状态。"""
        ranks = {
            "awaiting_authorized_analysis": 1,
            "response_sealed": 2,
            "consumed": 3,
        }
        with self.immediate_transaction():
            job = self._conn.execute(
                "SELECT state FROM workflow_jobs WHERE job_id=?",
                (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"工作流job {job_id} 不存在")
            current = job["state"]
            rows = self._conn.execute(
                "SELECT status FROM review_requests WHERE job_id=? "
                "ORDER BY request_id", (job_id,)).fetchall()
            states = [row["status"] for row in rows]
            if not states:
                raise ValueError("workflow job没有review request")
            if "failed" in states:
                desired = "failed"
            elif "awaiting_authorized_analysis" in states:
                desired = "awaiting_authorized_analysis"
            elif "response_sealed" in states:
                desired = "response_sealed"
            elif all(state == "consumed" for state in states):
                desired = "consumed"
            else:
                raise ValueError("review request状态组合非法")
            if current == "failed" and not resume_failed:
                return current
            if current in ranks and desired in ranks \
                    and ranks[desired] < ranks[current]:
                return current
            clear_failure = resume_failed and desired != "failed"
            cur = self._conn.execute(
                "UPDATE workflow_jobs SET state=?,"
                "failure_json=CASE WHEN ? THEN NULL ELSE failure_json END,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE job_id=? AND state=?",
                (desired, int(clear_failure), job_id, current))
            if cur.rowcount != 1:
                raise RuntimeError("workflow job条件状态转换失败")
            return desired

    @staticmethod
    def _decode_review_request(row) -> dict | None:
        if row is None:
            return None
        stored = dict(row)
        body = json.loads(stored.pop("body_json"))
        body["status"] = stored["status"]
        body["created_at"] = stored["created_at"]
        body["updated_at"] = stored["updated_at"]
        return body

    def add_review_request(self, item: dict) -> dict:
        body_json = _strict_json_dumps(
            {key: value for key, value in item.items() if key != "status"},
            label="专业复核请求")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO review_requests("
                "request_id,request_input_digest,job_id,status,body_json) "
                "VALUES (?,?,?,?,?)",
                (item["request_id"], item["request_input_digest"],
                 item["job_id"], item["status"], body_json),
            )
        stored = self.get_review_request(item["request_id"])
        if stored is None:
            raise RuntimeError("专业复核请求持久化失败")
        return stored

    def get_review_request(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM review_requests WHERE request_id=?",
            (request_id,)).fetchone()
        return self._decode_review_request(row)

    def fetch_review_requests(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_requests WHERE job_id=? ORDER BY request_id",
            (job_id,)).fetchall()
        return [self._decode_review_request(row) for row in rows]

    @staticmethod
    def _decode_proposal_request(row) -> dict | None:
        if row is None:
            return None
        stored = dict(row)
        body = json.loads(stored.pop("body_json"))
        body.update({
            "status": stored["status"],
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
        })
        return body

    def get_proposal_request(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM proposal_requests WHERE request_id=?",
            (request_id,)).fetchone()
        return self._decode_proposal_request(row)

    def fetch_proposal_requests(self, job_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM proposal_requests WHERE job_id=? ORDER BY request_id",
            (job_id,)).fetchall()
        return [self._decode_proposal_request(row) for row in rows]

    @staticmethod
    def _decode_proposal_response(row) -> dict | None:
        if row is None:
            return None
        stored = dict(row)
        body = json.loads(stored.pop("body_json"))
        materialization = stored.pop("materialization_json")
        body.update({
            "response_blob_sha256": stored["response_blob_sha256"],
            "source_mode": stored["source_mode"],
            "status": stored["status"],
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
            "consumed_at": stored["consumed_at"],
        })
        if materialization is not None:
            body["materialization"] = json.loads(materialization)
        return body

    def add_proposal_response(self, item: dict) -> dict:
        body_json = _strict_json_dumps(
            {key: value for key, value in item.items()
             if key not in {"status", "response_blob_sha256", "source_mode"}},
            label="候选提出返回")
        with self.immediate_transaction():
            request = self._conn.execute(
                "SELECT job_id,status FROM proposal_requests WHERE request_id=?",
                (item["request_id"],)).fetchone()
            if request is None or request["status"] != "awaiting_candidate_proposal":
                raise ValueError("候选请求不存在或不处于待执行状态")
            self._conn.execute(
                "INSERT INTO proposal_responses("
                "response_id,request_id,response_digest,response_blob_sha256,"
                "source_mode,status,body_json) VALUES (?,?,?,?,?,?,?)",
                (item["response_id"], item["request_id"],
                 item["response_digest"], item["response_blob_sha256"],
                 item["source_mode"], item["status"], body_json))
            self._conn.execute(
                "UPDATE proposal_requests SET status='proposal_response_sealed',"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE request_id=?", (item["request_id"],))
            remaining = self._conn.execute(
                "SELECT COUNT(*) FROM proposal_requests WHERE job_id=? "
                "AND status='awaiting_candidate_proposal'",
                (request["job_id"],)).fetchone()[0]
            if not remaining:
                self._conn.execute(
                    "UPDATE workflow_jobs SET state='proposal_response_sealed',"
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE job_id=?", (request["job_id"],))
        stored = self.get_proposal_response(item["response_id"])
        if stored is None:
            raise RuntimeError("候选提出返回持久化失败")
        return stored

    def get_proposal_response(self, response_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM proposal_responses WHERE response_id=?",
            (response_id,)).fetchone()
        return self._decode_proposal_response(row)

    def get_proposal_response_for_request(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM proposal_responses WHERE request_id=?",
            (request_id,)).fetchone()
        return self._decode_proposal_response(row)

    def get_qualification_input_view(self, view_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM qualification_input_views WHERE view_id=?",
            (view_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["view"] = json.loads(value.pop("view_json"))
        return value

    def get_review_authorization(self, authorization_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT body_json FROM review_authorizations "
            "WHERE authorization_id=?", (authorization_id,)).fetchone()
        if row is None:
            return None
        return json.loads(row["body_json"])

    def materialize_candidate_response_atomic(
            self, *, response_id: str, request_id: str, job_id: str,
            plans: list[dict], candidate_statuses: list[dict],
            review_request_builder) -> dict:
        """原子登记Claim、资格视图、授权及v2专业请求。"""
        with self.immediate_transaction():
            response = self._conn.execute(
                "SELECT status,materialization_json FROM proposal_responses "
                "WHERE response_id=? AND request_id=?",
                (response_id, request_id)).fetchone()
            if response is None:
                raise KeyError("候选返回不存在")
            if response["status"] == "consumed":
                if response["materialization_json"] is None:
                    raise StoreIntegrityError("已消费候选缺少物化清单")
                return json.loads(response["materialization_json"])
            if response["status"] != "proposal_response_sealed":
                raise ValueError("候选返回状态不可消费")
            claim_ids: list[str] = []
            view_ids: list[str] = []
            authorization_ids: list[str] = []
            review_request_ids: list[str] = []
            gap_ids: list[str] = []
            for plan in plans:
                claim = plan["claim"]
                claim_ids.append(claim["claim_id"])
                existing = self._conn.execute(
                    "SELECT * FROM claims WHERE claim_id=?",
                    (claim["claim_id"],)).fetchone()
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO claims(claim_id,source_id,locator_kind,"
                        "locator_start,locator_end,locator_ref,excerpt_sha256,"
                        "excerpt_text,interpretation,subject_scope,"
                        "interpretation_attempt,input_digest,content_digest,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(claim[key] for key in (
                            "claim_id", "source_id", "locator_kind",
                            "locator_start", "locator_end", "locator_ref",
                            "excerpt_sha256", "excerpt_text", "interpretation",
                            "subject_scope", "interpretation_attempt",
                            "input_digest", "content_digest", "created_at")))
                elif dict(existing).get("content_digest") != claim["content_digest"]:
                    raise StoreIntegrityError("同claim_id已登记不同候选内容")
                qualification = plan.get("qualification")
                if qualification is not None:
                    existing_qual = self._conn.execute(
                        "SELECT * FROM qualifications WHERE qual_id=?",
                        (qualification["qual_id"],)).fetchone()
                    if existing_qual is None:
                        self._conn.execute(
                            "INSERT INTO qualifications(qual_id,claim_id,"
                            "source_judgment,identity_judgment,time_judgment,"
                            "independence_judgment,allowed_uses,cannot_prove,"
                            "review_attempt,status,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (qualification["qual_id"], qualification["claim_id"],
                             qualification["source_judgment"],
                             qualification["identity_judgment"],
                             qualification["time_judgment"],
                             qualification["independence_judgment"],
                             qualification["allowed_uses"],
                             qualification["cannot_prove"],
                             qualification["review_attempt"],
                             qualification["status"],
                             qualification["created_at"]))
                    else:
                        actual = dict(existing_qual)
                        if qualification_content_digest(actual) != \
                                qualification_content_digest(qualification) \
                                or actual["status"] != qualification["status"] \
                                or actual["allowed_uses"] != \
                                qualification["allowed_uses"]:
                            raise StoreIntegrityError(
                                "既有Qualification内容、状态或allowed_uses冲突")
                gap = plan.get("gap")
                if gap is not None:
                    gap_ids.append(gap["gap_id"])
                    self._conn.execute(
                        "INSERT OR IGNORE INTO gaps(gap_id,gap_type,"
                        "affected_criteria,pipeline_fault,investigation,unconfirmed) "
                        "VALUES (?,?,?,?,?,?)",
                        (gap["gap_id"], gap["gap_type"],
                         _strict_json_dumps(gap["affected_criteria"],
                                            label="候选缺口准则"),
                         int(gap["pipeline_fault"]), gap["investigation"],
                         _strict_json_dumps(gap["unconfirmed"],
                                            label="候选缺口未确认项")))
                view_id = plan["qualification_view_id"]
                view_ids.append(view_id)
                view_json = _strict_json_dumps(
                    plan["qualification_view"], label="资格输入视图")
                self._conn.execute(
                    "INSERT OR IGNORE INTO qualification_input_views("
                    "view_id,claim_id,input_digest,view_json) VALUES (?,?,?,?)",
                    (view_id, claim["claim_id"],
                     plan["qualification_view"]["input_digest"], view_json))
                stored_view = self._conn.execute(
                    "SELECT view_json FROM qualification_input_views WHERE view_id=?",
                    (view_id,)).fetchone()
                if stored_view is None or stored_view["view_json"] != view_json:
                    raise StoreIntegrityError("资格输入视图内容身份冲突")
                authorization = plan.get("authorization")
                if authorization is not None:
                    authorization_ids.append(authorization["authorization_id"])
                    authorization_json = _strict_json_dumps(
                        authorization, label="首次复核授权")
                    self._conn.execute(
                        "INSERT OR IGNORE INTO review_authorizations("
                        "authorization_id,claim_id,qualification_view_id,body_json) "
                        "VALUES (?,?,?,?)",
                        (authorization["authorization_id"], claim["claim_id"],
                         view_id, authorization_json))
                    stored_auth = self._conn.execute(
                        "SELECT body_json FROM review_authorizations "
                        "WHERE authorization_id=?",
                        (authorization["authorization_id"],)).fetchone()
                    if stored_auth is None or stored_auth["body_json"] != authorization_json:
                        raise StoreIntegrityError("首次复核授权内容身份冲突")
                    review_request = review_request_builder(
                        job_id=job_id, authorization=authorization)
                    review_request_ids.append(review_request["request_id"])
                    request_json = _strict_json_dumps(
                        {key: value for key, value in review_request.items()
                         if key != "status"}, label="v2专业复核请求")
                    self._conn.execute(
                        "INSERT OR IGNORE INTO review_requests("
                        "request_id,request_input_digest,job_id,status,body_json) "
                        "VALUES (?,?,?,?,?)",
                        (review_request["request_id"],
                         review_request["request_input_digest"], job_id,
                         review_request["status"], request_json))
            materialization = {
                "claim_ids": claim_ids,
                "qualification_view_ids": view_ids,
                "authorization_ids": authorization_ids,
                "review_request_ids": review_request_ids,
                "gap_ids": gap_ids,
                "candidate_statuses": candidate_statuses,
            }
            materialization_json = _strict_json_dumps(
                materialization, label="候选物化清单")
            self._conn.execute(
                "UPDATE proposal_responses SET status='consumed',"
                "materialization_json=?,consumed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE response_id=? AND status='proposal_response_sealed'",
                (materialization_json, response_id))
            self._conn.execute(
                "UPDATE proposal_requests SET status='consumed',"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE request_id=? AND status='proposal_response_sealed'",
                (request_id,))
            remaining = self._conn.execute(
                "SELECT status FROM proposal_requests WHERE job_id=? "
                "AND status!='consumed' ORDER BY request_id", (job_id,)).fetchall()
            if any(row["status"] == "awaiting_candidate_proposal" for row in remaining):
                state = "awaiting_candidate_proposal"
            elif remaining:
                state = "proposal_response_sealed"
            elif review_request_ids:
                state = "awaiting_authorized_analysis"
            else:
                state = "insufficient"
            self._conn.execute(
                "UPDATE workflow_jobs SET state=?,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE job_id=?",
                (state, job_id))
            return materialization

    @staticmethod
    def _decode_review_response(row) -> dict | None:
        if row is None:
            return None
        stored = dict(row)
        body = json.loads(stored.pop("body_json"))
        body.update({
            "response_blob_sha256": stored["response_blob_sha256"],
            "source_mode": stored["source_mode"],
            "status": stored["status"],
            "created_at": stored["created_at"],
            "updated_at": stored["updated_at"],
            "consumed_at": stored["consumed_at"],
        })
        return body

    def add_review_response(self, item: dict) -> dict:
        body_json = _strict_json_dumps(
            {key: value for key, value in item.items()
             if key not in {"status", "response_blob_sha256", "source_mode"}},
            label="专业复核返回")
        producer_json = _strict_json_dumps(item["producer"], label="复核生产者")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO review_responses("
                "response_id,request_id,response_digest,response_blob_sha256,"
                "source_mode,producer_json,output_schema,status,body_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (item["response_id"], item["request_id"],
                 item["response_digest"], item["response_blob_sha256"],
                 item["source_mode"], producer_json, item["output_schema"],
                 item["status"], body_json),
            )
            self._conn.execute(
                "UPDATE review_requests SET status='response_sealed', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE request_id=? AND status='awaiting_authorized_analysis'",
                (item["request_id"],),
            )
        stored = self.get_review_response(item["response_id"])
        if stored is None:
            raise RuntimeError("专业复核返回持久化失败")
        return stored

    def get_review_response(self, response_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM review_responses WHERE response_id=?",
            (response_id,)).fetchone()
        return self._decode_review_response(row)

    def get_review_response_for_request(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM review_responses WHERE request_id=?",
            (request_id,)).fetchone()
        return self._decode_review_response(row)

    def mark_review_response_consumed(self, response_id: str) -> dict:
        with self._conn:
            row = self._conn.execute(
                "SELECT request_id,status FROM review_responses WHERE response_id=?",
                (response_id,)).fetchone()
            if row is None:
                raise KeyError(f"专业复核返回 {response_id} 不存在")
            if row["status"] == "response_sealed":
                self._conn.execute(
                    "UPDATE review_responses SET status='consumed', "
                    "consumed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE response_id=? AND status='response_sealed'",
                    (response_id,))
                self._conn.execute(
                    "UPDATE review_requests SET status='consumed', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE request_id=? AND status='response_sealed'",
                    (row["request_id"],))
        return self.get_review_response(response_id)

    # ---- 记录写入（事务）----

    def add_import_record(self, kind: str, origin_path: str, origin_sha256: str | None,
                          note: str | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO import_records(kind, origin_path, origin_sha256, note) "
                "VALUES (?,?,?,?)",
                (kind, origin_path, origin_sha256, note),
            )
        return int(cur.lastrowid)

    def ensure_import_record(self, kind: str, origin_path: str,
                             origin_sha256: str | None,
                             note: str | None = None) -> int:
        """按完整来源/内容/审计正文复用同一次导入记录，避免恢复时双写。"""
        with self._conn:
            row = self._conn.execute(
                "SELECT import_id FROM import_records WHERE kind=? "
                "AND origin_path=? AND origin_sha256 IS ? AND note IS ? "
                "ORDER BY import_id LIMIT 1",
                (kind, origin_path, origin_sha256, note),
            ).fetchone()
            if row is not None:
                return int(row["import_id"])
            cur = self._conn.execute(
                "INSERT INTO import_records(kind, origin_path, origin_sha256, note) "
                "VALUES (?,?,?,?)",
                (kind, origin_path, origin_sha256, note),
            )
            return int(cur.lastrowid)

    def add_source(self, source_id: str, blob_sha256: str, byte_length: int, *,
                   media_type: str | None = None, locator: str | None = None,
                   retrieved_at: str | None = None, published_at: str | None = None,
                   published_at_provenance: str | None = None,
                   time_evidence: dict | None = None,
                   document_subject: str | None = None,
                   document_subject_basis: str | None = None,
                   source_family: str | None = None, capture_status: str = "imported",
                   import_id: int | None = None) -> None:
        if not is_sha256_hex(blob_sha256):
            raise StoreIntegrityError(f"非法 blob id：{blob_sha256!r}")
        with self._conn:
            self._conn.execute(
                "INSERT INTO sources(source_id, blob_sha256, byte_length, media_type, "
                "locator, retrieved_at, published_at, published_at_provenance, "
                "time_evidence, document_subject, document_subject_basis, "
                "source_family, capture_status, import_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, blob_sha256, byte_length, media_type, locator, retrieved_at,
                 published_at, published_at_provenance,
                 json.dumps(time_evidence, ensure_ascii=False) if time_evidence else None,
                 document_subject, document_subject_basis,
                 source_family, capture_status, import_id),
            )

    def set_source_time_evidence(self, source_id: str, time_evidence: dict) -> None:
        """[已废弃于v2] 原地改写会被静默UPDATE；新代码用 append_time_evidence。

        保留仅为读取兼容：现实现改为追加一个新修订版本，不再覆盖旧值。
        """
        self.append_time_evidence(source_id, time_evidence)

    def add_claim(self, claim_id: str, source_id: str, *, locator_kind: str,
                  excerpt_start: int, excerpt_end: int, excerpt_sha256: str,
                  excerpt_text: str, interpretation: str, subject_scope: str,
                  interpretation_attempt: str | None = None,
                  locator_ref: str | None = None,
                  input_digest: str | None = None,
                  content_digest: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO claims(claim_id, source_id, locator_kind, locator_start, "
                "locator_end, locator_ref, excerpt_sha256, excerpt_text, interpretation, "
                "subject_scope, interpretation_attempt, input_digest, content_digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, source_id, locator_kind, excerpt_start, excerpt_end,
                 locator_ref, excerpt_sha256, excerpt_text, interpretation,
                 subject_scope, interpretation_attempt, input_digest,
                 content_digest),
            )

    def add_qualification(self, qual_id: str, claim_id: str, *, source_judgment: str,
                          identity_judgment: str, time_judgment: str,
                          independence_judgment: str, allowed_uses: list[str],
                          cannot_prove: list[str], review_attempt: str | None,
                          status: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO qualifications(qual_id, claim_id, source_judgment, "
                "identity_judgment, time_judgment, independence_judgment, allowed_uses, "
                "cannot_prove, review_attempt, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (qual_id, claim_id, source_judgment, identity_judgment, time_judgment,
                 independence_judgment, json.dumps(allowed_uses, ensure_ascii=False),
                 json.dumps(cannot_prove, ensure_ascii=False), review_attempt, status),
            )

    def add_gap(self, gap_id: str, gap_type: str, affected_criteria: list[str], *,
                pipeline_fault: bool = False, investigation: str = "",
                unconfirmed: list[str] | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO gaps(gap_id, gap_type, affected_criteria, pipeline_fault, "
                "investigation, unconfirmed) VALUES (?,?,?,?,?,?)",
                (gap_id, gap_type, json.dumps(affected_criteria, ensure_ascii=False),
                 int(pipeline_fault), investigation,
                 json.dumps(unconfirmed or [], ensure_ascii=False)),
            )

    def add_criterion_result(self, result_id: str, criterion_id: str, dimension: str, *,
                             native_disposition: str | None, native_note: str | None,
                             product_status: str, evidence_refs: list[str],
                             gap_refs: list[str], qual_refs: list[str], rationale: str,
                             scope: str, rule_version: str,
                             input_digest: str | None = None,
                             case_basis_version: int | None = None,
                             frozen_inputs: dict | None = None,
                             na_basis: dict | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO criterion_results(result_id, criterion_id, dimension, "
                "native_disposition, native_note, product_status, evidence_refs, "
                "gap_refs, qual_refs, rationale, scope, rule_version, input_digest, "
                "case_basis_version, frozen_inputs, na_basis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (result_id, criterion_id, dimension, native_disposition, native_note,
                 product_status, json.dumps(evidence_refs, ensure_ascii=False),
                 json.dumps(gap_refs, ensure_ascii=False),
                 json.dumps(qual_refs, ensure_ascii=False), rationale, scope,
                 rule_version, input_digest, case_basis_version,
                 json.dumps(frozen_inputs, ensure_ascii=False,
                            sort_keys=True) if frozen_inputs else None,
                 json.dumps(na_basis, ensure_ascii=False) if na_basis else None),
            )

    def add_criterion_result_in_transaction(
            self, result_id: str, criterion_id: str, dimension: str, *,
            native_disposition: str | None, native_note: str | None,
            product_status: str, evidence_refs: list[str], gap_refs: list[str],
            qual_refs: list[str], rationale: str, scope: str, rule_version: str,
            input_digest: str | None = None, case_basis_version: int | None = None,
            frozen_inputs: dict | None = None, na_basis: dict | None = None) -> None:
        """在 :meth:`immediate_transaction` 内插入已核验候选，禁止隐式提交。"""
        if not self._conn.in_transaction:
            raise RuntimeError("判据结果发布必须位于显式事务内")
        self._conn.execute(
            "INSERT INTO criterion_results(result_id, criterion_id, dimension, "
            "native_disposition, native_note, product_status, evidence_refs, "
            "gap_refs, qual_refs, rationale, scope, rule_version, input_digest, "
            "case_basis_version, frozen_inputs, na_basis) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, criterion_id, dimension, native_disposition, native_note,
             product_status, json.dumps(evidence_refs, ensure_ascii=False),
             json.dumps(gap_refs, ensure_ascii=False),
             json.dumps(qual_refs, ensure_ascii=False), rationale, scope,
             rule_version, input_digest, case_basis_version,
             json.dumps(frozen_inputs, ensure_ascii=False,
                        sort_keys=True) if frozen_inputs else None,
             json.dumps(na_basis, ensure_ascii=False) if na_basis else None),
        )

    # ---- 读取 ----

    def fetch_one(self, table: str, key_field: str, key_value: str) -> dict | None:
        allowed = {"sources", "claims", "qualifications", "gaps", "criterion_results",
                   "import_records"}
        if table not in allowed:
            raise ValueError(f"禁止查询表 {table}")
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_field} = ?", (key_value,)
        ).fetchone()
        return dict(row) if row else None

    def fetch_all(self, table: str) -> list[dict]:
        allowed = {"sources", "claims", "qualifications", "gaps", "criterion_results",
                   "import_records", "stages", "runs"}
        if table not in allowed:
            raise ValueError(f"禁止查询表 {table}")
        rows = self._conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CaseStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def find_uncommitted_blobs(store: BlobStore, case: CaseStore) -> set[str]:
    """识别孤立工件：对象仓中存在、但没有任何 Source 引用的对象。

    这些是"落盘后未提交"的失败材料——保留不删，不得当作成功结果。
    """
    referenced = {row["blob_sha256"] for row in case.fetch_all("sources")}
    return store.list_objects() - referenced
