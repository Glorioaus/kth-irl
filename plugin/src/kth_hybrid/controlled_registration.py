"""受控登记：文档自识主体与收存时间证据的最小接入（当前评估口径试点）。

两项登记都只消费已封存对象，并在写入前后机械再解析上游：
- 主体：声明记录必须锚定已封存的页1投影（locator+text hash）、保留原文
  排版空格，规范化仅允许"全角右括号后单个空格"这一条，且不得声称
  Owner 背书（批准开发功能不是归属事实声明）。
- 时间：必须从封存上游记录的精确字段机械解析，登记为收存/复制事件；
  生成的新封存记录保留上游 blob/字段/原件关系，读取方可再解析。

登记是单次写入：主体仅在 NULL 时写入；时间走 append_time_evidence
追加修订。两者都经 Journal 封账，不提供任何"改成qualified"入口。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .qualification import (
    StoreIntegrityError,
    _registration_binding_error,
    _resolve_registration_proof,
    _verify_document_subject_binding,
    decode_document_subject_basis,
    parse_iso_datetime,
)

DECLARATION_SCHEMA = "kth-hybrid.document-subject-declaration.v1"
TIME_REGISTRATION_SCHEMA = "kth-hybrid.time-registration.v1"
DECLARATION_ORIGIN = "session:document-subject-declaration.json"
_DECLARATION_FIELDS = {
    "schema_version", "document_sha256", "document_subject",
    "document_subject_raw", "raw_locator", "raw_text_sha256",
    "normalization", "observed_by", "observed_at", "proof_scope",
}
_DECLARATION_OPTIONAL_FIELDS = {"declared_by"}
_ALLOWED_NORMALIZATION_KIND = "single_space_after_fullwidth_paren"
_ALLOWED_DECLARED_BY = {"executor-controlled-observation"}


class RegistrationRejected(RuntimeError):
    """受控登记合同不满足；不产生任何写入。"""


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _resolve_field(record: Any, field: str) -> tuple[Any, str | None]:
    node = record
    for token in re.split(r"\.|(\[|\])", field):
        if token in (None, "", "[", "]"):
            continue
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None, f"上游字段路径失败于 {token!r}"
        elif isinstance(node, dict):
            if token not in node:
                return None, f"上游记录无字段 {token!r}"
            node = node[token]
        else:
            return None, f"上游字段路径失败于 {token!r}"
    return node, None


def _apply_normalization(raw: str, normalization: Mapping[str, Any]) -> str:
    """仅允许去除全角右括号后的单个排版空格；其余差异一律拒绝。"""
    if not isinstance(normalization, Mapping) \
            or normalization.get("kind") != _ALLOWED_NORMALIZATION_KIND:
        raise RegistrationRejected(
            f"规范化依据不支持：{normalization.get('kind')!r}；"
            f"仅允许 {_ALLOWED_NORMALIZATION_KIND!r}")
    if normalization.get("removed") != " ":
        raise RegistrationRejected("规范化只能去除单个半角空格")
    if "） " not in raw:
        raise RegistrationRejected("原文不含全角右括号后空格，规范化无依据")
    return raw.replace("） ", "）", 1)


def import_provenance_document(workflow, *, name: str,
                               payload: Mapping[str, Any]) -> dict:
    """把声明记录作为 case_provenance 封存；同名单记不可重复导入。"""
    origin = f"session:{name}"
    existing = [row for row in workflow.store.fetch_all("import_records")
                if row["kind"] == "case_provenance"
                and row["origin_path"] == origin]
    if existing:
        raise RegistrationRejected(f"provenance记录已存在：{origin}")
    blob = workflow.blobs.put_bytes(_canonical(payload))
    task_key = f"provenance-import:{name}"
    workflow.journal.ensure_task(task_key, blob.sha256)
    claim = workflow.journal.claim(task_key, "controlled-registration",
                                   blob.sha256)
    try:
        import_id = workflow.store.add_import_record(
            "case_provenance", origin, blob.sha256,
            note="受控登记导入（本轮当前评估口径）")
    except Exception as exc:
        workflow.journal.record_failure(claim, str(exc))
        raise RegistrationRejected(f"provenance导入失败：{exc}") from exc
    workflow.journal.commit(claim, output_ref=blob.sha256)
    return {"origin": origin, "import_id": import_id,
            "blob_sha256": blob.sha256}


def declare_document_subject(workflow, *, source_id: str,
                             declaration: Mapping[str, Any]) -> dict:
    source = workflow.store.fetch_one("sources", "source_id", source_id)
    if source is None:
        raise RegistrationRejected(f"来源不存在：{source_id}")
    if source.get("document_subject") is not None:
        raise RegistrationRejected(
            "该来源已登记文档自识主体；单次登记，不接受改写")
    if not isinstance(declaration, Mapping) \
            or not _DECLARATION_FIELDS <= set(declaration) \
            or set(declaration) - _DECLARATION_FIELDS - \
            _DECLARATION_OPTIONAL_FIELDS \
            or declaration.get("schema_version") != DECLARATION_SCHEMA:
        raise RegistrationRejected("声明记录字段集合或schema非法")
    declared_by = declaration.get("declared_by")
    if declared_by is not None and declared_by not in _ALLOWED_DECLARED_BY:
        raise RegistrationRejected(
            f"declared_by={declared_by!r} 不被受控登记接受：批准开发功能"
            "不是Owner对文档归属的事实声明；只登记真实观察来源")
    if declaration["document_sha256"] != source["blob_sha256"]:
        raise RegistrationRejected(
            "声明绑定的原件hash与当前来源不一致，不能借用")
    basis = workflow.store.get_case_basis()
    if declaration["document_subject"] != basis["subject_legal_name"]:
        raise RegistrationRejected(
            "规范化后主体与当前CaseBasis主体不一致；登记无意义且易误导")

    normalized = _apply_normalization(
        declaration["document_subject_raw"],
        declaration["normalization"])
    if normalized != declaration["document_subject"]:
        raise RegistrationRejected("规范化结果与声明主体不一致")

    locator = declaration["raw_locator"]
    if not isinstance(locator, Mapping) or not locator:
        raise RegistrationRejected("raw_locator必须为非空定位对象")
    matched = None
    for row in workflow.store._conn.execute(
            "SELECT * FROM text_projections WHERE source_id=? AND "
            "status='projected'", (source_id,)).fetchall():
        row = dict(row)
        if json.loads(row["locator_json"]) == locator \
                and row["text_sha256"] == declaration["raw_text_sha256"]:
            matched = row
            break
    if matched is None:
        raise RegistrationRejected(
            "声明的页1投影（locator+text hash）在封存投影中不存在")
    text = workflow.blobs.read_bytes(
        matched["text_blob_sha256"]).decode("utf-8")
    if declaration["document_subject_raw"] not in text:
        raise RegistrationRejected(
            "原文自识字样未出现在声明的封存投影文本中")

    imported = import_provenance_document(
        workflow, name="document-subject-declaration.json",
        payload=declaration)
    subject_basis = {
        "kind": "case_field_reference",
        "path": "case:document-subject-declaration.json#/document_subject",
        "document_sha256_path":
            "case:document-subject-declaration.json#/document_sha256",
    }
    task_key = f"document-subject:{source_id}"
    workflow.journal.ensure_task(task_key, source["blob_sha256"])
    claim = workflow.journal.claim(task_key, "controlled-registration",
                                   source["blob_sha256"])
    try:
        with workflow.store._conn:
            cur = workflow.store._conn.execute(
                "UPDATE sources SET document_subject=?, "
                "document_subject_basis=? WHERE source_id=? AND "
                "document_subject IS NULL",
                (declaration["document_subject"],
                 json.dumps(subject_basis, ensure_ascii=False), source_id))
            if cur.rowcount != 1:
                raise RegistrationRejected("主体登记条件更新失败（并发或已登记）")
    except Exception as exc:
        workflow.journal.record_failure(claim, str(exc))
        raise RegistrationRejected(f"主体登记失败：{exc}") from exc
    workflow.journal.commit(claim, output_ref=imported["blob_sha256"])

    stored = workflow.store.fetch_one("sources", "source_id", source_id)
    proof, decode_error = decode_document_subject_basis(
        stored.get("document_subject_basis"))
    ok, basis_text, _bindings = _verify_document_subject_binding(
        proof, stored["document_subject"], stored, workflow.store,
        workflow.blobs)
    if decode_error or not ok:
        raise RegistrationRejected(
            f"登记后读回验证失败：{decode_error or basis_text}")
    return {
        "source_id": source_id,
        "document_subject": stored["document_subject"],
        "provenance_blob_sha256": imported["blob_sha256"],
        "readback_verification": basis_text,
        "raw_projection_id": matched["projection_id"],
    }


def register_time_evidence(workflow, *, source_id: str,
                           upstream_blob_sha256: str,
                           upstream_field: str,
                           upstream_document_field: str,
                           event_note: str) -> dict:
    """从封存上游记录机械解析收存/复制事件并登记 registered_at 证据。"""
    source = workflow.store.fetch_one("sources", "source_id", source_id)
    if source is None:
        raise RegistrationRejected(f"来源不存在：{source_id}")
    try:
        raw = workflow.blobs.read_bytes(upstream_blob_sha256)
    except (OSError, StoreIntegrityError, KeyError) as exc:
        raise RegistrationRejected(f"上游封存记录不可读：{exc}") from exc
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RegistrationRejected(f"上游封存记录解析失败：{exc}") from exc
    value, field_error = _resolve_field(record, upstream_field)
    if field_error or not isinstance(value, str):
        raise RegistrationRejected(
            f"上游时间字段不可解析：{field_error or '非字符串'}")
    dt = parse_iso_datetime(value)
    if dt is None:
        raise RegistrationRejected(f"上游时间字段值不可解析：{value!r}")
    doc_value, doc_error = _resolve_field(record, upstream_document_field)
    if doc_error or doc_value != source["blob_sha256"]:
        raise RegistrationRejected(
            "上游记录绑定的原件与当前来源不一致"
            f"（{doc_error or str(doc_value)[:12] + '…'}），不能借用")

    canonical_record = {
        "schema_version": TIME_REGISTRATION_SCHEMA,
        "event_kind": "copy_into_repository",
        "event_semantics": (
            "收存/复制事件时间；不是发布日期、不是首次接收时间、"
            "不是内容真实发生时间"),
        "document_sha256": doc_value,
        "recorded_value": value,
        "upstream": {
            "blob_sha256": upstream_blob_sha256,
            "field": upstream_field,
            "value": value,
            "document_field": upstream_document_field,
            "document_value": doc_value,
        },
        "event_note": event_note,
        "registered_by": "executor-controlled-observation",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    blob = workflow.blobs.put_bytes(_canonical(canonical_record))
    evidence = {
        "kind": "registered_at",
        "date": value,
        "basis": (
            f"封存记录载明的收存/复制事件（{event_note}）；上游 "
            f"{upstream_blob_sha256[:12]}…#{upstream_field} 机械再解析一致；"
            "为入池时点证明，非发布日期、非首次接收、非内容发生时间"),
        "registration_proof": {
            "blob_sha256": blob.sha256,
            "field": "recorded_value",
            "document_sha256": doc_value,
        },
    }
    task_key = f"time-registration:{source_id}"
    workflow.journal.ensure_task(task_key, blob.sha256)
    claim = workflow.journal.claim(task_key, "controlled-registration",
                                   blob.sha256)
    try:
        revision = workflow.store.append_time_evidence(source_id, evidence)
    except Exception as exc:
        workflow.journal.record_failure(claim, str(exc))
        raise RegistrationRejected(f"时间证据登记失败：{exc}") from exc
    workflow.journal.commit(claim, output_ref=blob.sha256)

    latest = workflow.store.latest_time_evidence(source_id)
    binding_error = _registration_binding_error(
        latest.get("registration_proof"), source, workflow.blobs)
    record_dt, proof_error = _resolve_registration_proof(
        latest.get("registration_proof"), workflow.blobs)
    if binding_error or proof_error or record_dt is None \
            or record_dt != dt:
        raise RegistrationRejected(
            f"登记后读回验证失败：{binding_error or proof_error}")
    return {
        "source_id": source_id,
        "revision": revision,
        "registered_date": value,
        "record_blob_sha256": blob.sha256,
        "upstream": canonical_record["upstream"],
        "readback_verification": "登记证明读取时再解析上游一致",
    }
