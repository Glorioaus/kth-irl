"""KTH 本地持久工作流核心；CLI/Plugin 仅应调用本模块。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Iterable

from .aggregation_profiles import get_aggregation_profile
from .contracts import sha256_hex
from .intake import inspect_attachment
from .journal import CommitRejected, Journal
from .review_queue import (
    AWAITING,
    ReviewQueue,
    ReviewQueueRejected,
    build_request,
    request_input_digest,
    request_input_payload,
)
from .store import BlobStore, CaseStore


JOB_SCHEMA = "kth-local.workflow-job.v1"
JOB_INPUT_SCHEMA = "kth-local.workflow-job-input.v1"
MAX_ATTACHMENT_FILES = 256
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENT_BATCH_BYTES = 256 * 1024 * 1024
MAX_REVIEW_SPECS = 512
MAX_REVIEW_SPECS_BYTES = 2 * 1024 * 1024
MAX_REVIEW_SPEC_DEPTH = 24


class WorkflowRejected(RuntimeError):
    """工作流输入、状态或持久化关系不满足受控合同。"""


def _canonical_bytes(value, *, label: str) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkflowRejected(f"{label}不是规范JSON：{exc}") from exc


def _digest(value, *, label: str) -> str:
    return sha256_hex(_canonical_bytes(value, label=label))


def _require_bounded_json(value, *, label: str, max_bytes: int,
                          max_depth: int) -> None:
    stack = [(value, 1, frozenset())]
    while stack:
        item, depth, ancestors = stack.pop()
        if depth > max_depth:
            raise WorkflowRejected(f"{label}深度超过上限{max_depth}")
        if isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in ancestors:
                raise WorkflowRejected(f"{label}包含循环引用")
            nested = ancestors | {identity}
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1, nested) for child in children)
    if len(_canonical_bytes(value, label=label)) > max_bytes:
        raise WorkflowRejected(f"{label}序列化字节超过上限{max_bytes}")


class LocalWorkflow:
    """单Case本地工作流；所有持久记录共用 ``records.sqlite3``。"""

    def __init__(self, case_dir: Path | str):
        self.case_dir = Path(case_dir)
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.case_dir / "blobs")
        self.db_path = self.case_dir / "records.sqlite3"
        self.store = CaseStore(self.db_path)
        self.journal = Journal(self.db_path)
        self.reviews = ReviewQueue(self.store, self.blobs, self.journal)

    def initialize_case(self, *, subject_legal_name: str,
                        subject_aliases: list[str], evidence_cutoff: str,
                        subject_source_basis: str,
                        note: str | None = None) -> dict:
        values = (subject_legal_name, evidence_cutoff, subject_source_basis)
        if not all(isinstance(value, str) and value.strip() for value in values) \
                or not isinstance(subject_aliases, list) \
                or any(not isinstance(value, str) or not value.strip()
                       for value in subject_aliases):
            raise WorkflowRejected("CaseBasis主体、别名、截止或来源依据非法")
        version = self.store.set_case_basis_if_changed(
            subject_legal_name=subject_legal_name,
            subject_aliases=copy.deepcopy(subject_aliases),
            evidence_cutoff=evidence_cutoff,
            subject_source_basis=subject_source_basis,
            note=note,
        )
        return self.store.get_case_basis_version(version)

    @staticmethod
    def _attachment_result(row: dict, alias: dict | None = None) -> dict:
        alias = alias or {}
        return {
            "attachment_id": row.get("attachment_id"),
            "alias_id": alias.get("alias_id"),
            "origin_path": alias.get("origin_path", row.get("origin_path")),
            "blob_sha256": row.get("blob_sha256"),
            "byte_length": row.get("byte_length"),
            "original_filename": alias.get(
                "original_filename", row.get("original_filename")),
            "media_type": alias.get("media_type", row.get("media_type")),
            "status": alias.get("status", row.get("status")),
            "error": alias.get("error", row.get("error")),
            "import_id": alias.get("import_id", row.get("import_id")),
            "source_id": row.get("source_id"),
            "created_at": alias.get("created_at", row.get("created_at")),
        }

    def import_attachments(self, paths: list[Path | str]) -> list[dict]:
        if not isinstance(paths, (list, tuple)) or not paths \
                or len(paths) > MAX_ATTACHMENT_FILES:
            raise WorkflowRejected(
                f"附件必须是1至{MAX_ATTACHMENT_FILES}项的明确有限文件列表")
        prepared = []
        declared_total = 0
        for raw_path in paths:
            path = Path(raw_path)
            try:
                resolved = str(path.resolve(strict=True))
                if not path.is_file():
                    raise OSError("不是普通文件")
                size = path.stat().st_size
            except (OSError, ValueError) as exc:
                raise WorkflowRejected(f"附件路径读取失败：{path}：{exc}") from exc
            if size > MAX_ATTACHMENT_BYTES:
                raise WorkflowRejected(
                    f"附件单文件字节超过上限{MAX_ATTACHMENT_BYTES}：{path}")
            declared_total += size
            if declared_total > MAX_ATTACHMENT_BATCH_BYTES:
                raise WorkflowRejected(
                    "附件批次总字节超过上限"
                    f"{MAX_ATTACHMENT_BATCH_BYTES}")
            prepared.append((path, resolved, size))
        inspected = []
        actual_total = 0
        for path, resolved, declared_size in prepared:
            data = path.read_bytes()
            if len(data) != declared_size or len(data) > MAX_ATTACHMENT_BYTES:
                raise WorkflowRejected(f"附件读取期间大小变化或超过单文件上限：{path}")
            actual_total += len(data)
            if actual_total > MAX_ATTACHMENT_BATCH_BYTES:
                raise WorkflowRejected(
                    "附件批次读取后总字节超过上限"
                    f"{MAX_ATTACHMENT_BATCH_BYTES}")
            inspected.append((path, resolved, data, inspect_attachment(path.name, data)))
        results = []
        for path, resolved, data, inspection in inspected:
            blob_sha256 = sha256_hex(data)
            existing = self.store.get_attachment_import(
                origin_path=resolved, blob_sha256=blob_sha256,
                media_type=inspection.media_type)
            if existing is not None:
                alias = self.store.get_attachment_alias(
                    existing["attachment_id"], resolved)
                if alias is not None:
                    results.append(self._attachment_result(existing, alias))
                    continue
            attachment_id = (existing["attachment_id"] if existing is not None
                             else f"ATTACH::{blob_sha256}")
            alias_body = {
                "attachment_id": attachment_id,
                "origin_path": resolved,
                "original_filename": path.name,
                "media_type": inspection.media_type,
                "status": inspection.status,
                "error": inspection.error,
            }
            alias_id = f"ATTALIAS::{_digest(alias_body, label='附件来源别名')}"
            task_key = f"attachment-import:{alias_id}"
            self.journal.ensure_task(task_key, blob_sha256)
            try:
                claim = self.journal.claim(
                    task_key, "local-intake", blob_sha256)
                blob = self.blobs.put_bytes(data)
                import_id = self.store.add_import_record(
                    "attachment" if inspection.status == "saved"
                    else "attachment_attempt",
                    resolved, blob.sha256,
                    note=json.dumps({
                        "attachment_id": attachment_id,
                        "alias_id": alias_id,
                        "status": inspection.status,
                        "error": inspection.error,
                        "media_type": inspection.media_type,
                        "tool": inspection.tool,
                    }, ensure_ascii=False, sort_keys=True),
                )
                source_id = None
                if inspection.status == "saved":
                    source_id = f"ATT::{blob.sha256}"
                    source = self.store.fetch_one(
                        "sources", "source_id", source_id)
                    if source is None:
                        self.store.add_source(
                            source_id, blob.sha256, blob.byte_length,
                            media_type=inspection.media_type,
                            locator=resolved,
                            published_at_provenance=(
                                "本地附件无抓取/发布时间；仅保留文件来源路径"),
                            source_family="owner_attachment",
                            capture_status="attachment_saved",
                            import_id=import_id,
                        )
                if existing is None:
                    stored = self.store.add_attachment_import({
                        "attachment_id": attachment_id,
                        "origin_path": resolved,
                        "blob_sha256": blob.sha256,
                        "byte_length": blob.byte_length,
                        "original_filename": path.name,
                        "media_type": inspection.media_type,
                        "status": inspection.status,
                        "error": inspection.error,
                        "import_id": import_id,
                        "source_id": source_id,
                    })
                else:
                    stored = existing
                    if source_id is not None and stored.get("source_id") is None:
                        self.store.promote_attachment_source(
                            attachment_id, source_id=source_id,
                            media_type=inspection.media_type,
                            import_id=import_id)
                        stored = self.store.get_attachment_import(
                            origin_path=resolved, blob_sha256=blob_sha256)
                alias = self.store.add_attachment_alias({
                    **alias_body,
                    "alias_id": alias_id,
                    "import_id": import_id,
                    "source_id": source_id,
                })
                self.journal.commit(claim, alias_id)
            except Exception as exc:
                try:
                    self.journal.record_failure(claim, str(exc))
                except (UnboundLocalError, CommitRejected):
                    pass
                raise
            results.append(self._attachment_result(stored, alias))
        return results

    def _projection_public(self, row: dict) -> dict:
        result = {key: row.get(key) for key in (
            "projection_id", "source_id", "source_blob_sha256", "locator",
            "text_sha256", "text_blob_sha256", "char_count", "tool",
            "status", "error", "created_at",
        )}
        if row.get("text_blob_sha256"):
            result["text"] = self.blobs.read_bytes(
                row["text_blob_sha256"]).decode("utf-8")
        else:
            result["text"] = None
        return result

    def project_sources(self, source_ids: list[str]) -> list[dict]:
        if not isinstance(source_ids, (list, tuple)) or not source_ids:
            raise WorkflowRejected("投影来源必须是明确有限source_id列表")
        output = []
        for source_id in source_ids:
            source = self.store.fetch_one("sources", "source_id", source_id)
            if source is None:
                raise WorkflowRejected(f"投影来源不存在：{source_id}")
            data = self.blobs.read_bytes(source["blob_sha256"])
            inspection = inspect_attachment(source.get("locator") or "", data)
            input_id = _digest({
                "source_id": source_id,
                "blob_sha256": source["blob_sha256"],
                "tool": inspection.tool,
            }, label="投影输入")
            records = []
            if inspection.status != "saved" or inspection.projection is None:
                records.append(({}, None, inspection.status, inspection.error))
            else:
                for locator in inspection.projection.locators:
                    text = locator["text"]
                    location = {key: value for key, value in locator.items()
                                if key not in {"text", "text_sha256",
                                               "char_count"}}
                    records.append((location, text, "projected", None))
                for index, error in enumerate(
                        inspection.projection.unprocessed, start=1):
                    records.append((
                        {"unprocessed_index": index}, None,
                        "unprocessed", error))
                if not records:
                    records.append((
                        {"unprocessed_index": 1}, None, "unprocessed",
                        "附件未产生可用文本片段"))
            items = []
            for locator, text, status, error in records:
                text_bytes = text.encode("utf-8") if text is not None else None
                text_blob = (self.blobs.put_bytes(text_bytes)
                             if text_bytes is not None else None)
                identity = {
                    "source_id": source_id,
                    "source_blob_sha256": source["blob_sha256"],
                    "locator": locator,
                    "text_sha256": (sha256_hex(text_bytes)
                                    if text_bytes is not None else None),
                    "tool": inspection.tool,
                    "status": status,
                    "error": error,
                }
                items.append({
                    **identity,
                    "projection_id": (
                        f"PROJ::{_digest(identity, label='文本投影')}"),
                    "text_blob_sha256": (
                        text_blob.sha256 if text_blob is not None else None),
                    "char_count": len(text) if text is not None else None,
                })
            projection_ids = sorted(item["projection_id"] for item in items)
            batch_body = {
                "schema_version": "kth-local.projection-batch.v1",
                "source_id": source_id,
                "source_blob_sha256": source["blob_sha256"],
                "tool": inspection.tool,
                "projection_count": len(projection_ids),
                "projection_ids": projection_ids,
            }
            batch = {
                **batch_body,
                "batch_digest": _digest(batch_body, label="完整投影批次"),
            }
            output_ref = _canonical_bytes(
                batch, label="完整投影批次").decode("utf-8")
            task_key = f"text-projection:{source_id}:{input_id}"
            self.journal.ensure_task(task_key, input_id)
            state = self.journal.task_state(task_key)
            existing = self.store.fetch_text_projections(source_id)
            existing_ids = sorted(item["projection_id"] for item in existing)
            if state["state"] == "succeeded":
                if existing_ids != projection_ids \
                        or state.get("output_ref") != output_ref:
                    raise WorkflowRejected(
                        "Journal成功投影与完整projection identity/count不一致")
                output.extend(self._projection_public(row) for row in existing)
                continue
            if state["state"] == "claimed":
                claim = self.journal.takeover_stale_claim(
                    task_key, "local-projector", input_id,
                    evidence="机械投影恢复：按冻结批次重新原子核验/写入")
            else:
                claim = self.journal.claim(
                    task_key, "local-projector", input_id)
            try:
                stored_rows = self.store.replace_text_projections_atomic(
                    source_id, items)
                if sorted(row["projection_id"] for row in stored_rows) != \
                        projection_ids:
                    raise WorkflowRejected("完整投影批次落库后身份或数量不一致")
                self.journal.commit(claim, output_ref)
                output.extend(
                    self._projection_public(row) for row in stored_rows)
            except Exception as exc:
                try:
                    self.journal.record_failure(claim, str(exc))
                except CommitRejected:
                    pass
                raise
        return output

    def create_job(self, *, assessment_unit: dict, profile_id: str,
                   method_versions: dict, review_specs: list[dict]) -> dict:
        if not isinstance(assessment_unit, dict) \
                or not isinstance(assessment_unit.get("scope_id"), str) \
                or not assessment_unit["scope_id"].strip():
            raise WorkflowRejected("assessment unit缺少scope_id")
        if not isinstance(review_specs, list) or not review_specs:
            raise WorkflowRejected("job至少需要一项专业复核请求输入")
        if len(review_specs) > MAX_REVIEW_SPECS:
            raise WorkflowRejected(
                f"review_specs条目超过上限{MAX_REVIEW_SPECS}")
        _require_bounded_json(
            review_specs, label="review_specs",
            max_bytes=MAX_REVIEW_SPECS_BYTES,
            max_depth=MAX_REVIEW_SPEC_DEPTH)
        try:
            profile = get_aggregation_profile(profile_id)
        except (TypeError, ValueError) as exc:
            raise WorkflowRejected(f"aggregation profile未登记：{exc}") from exc
        current = self.store.get_case_basis()
        if current is None or not isinstance(current.get("version"), int):
            raise WorkflowRejected("CaseBasis尚未初始化")
        case_basis = self.store.get_case_basis_version(current["version"])
        payloads = []
        sources = {}
        projections = {}
        for spec in review_specs:
            try:
                source = self.store.fetch_one(
                    "sources", "source_id", spec.get("source_id"))
                if source is None or source["blob_sha256"] != spec.get("blob_sha256"):
                    raise WorkflowRejected("review request source/blob引用不一致")
                projection = self.store.get_text_projection(
                    spec.get("projection_id"))
                if projection is None or projection["status"] != "projected":
                    raise WorkflowRejected("review request projection不存在或未完成")
                if projection["source_id"] != source["source_id"] \
                        or projection["source_blob_sha256"] != \
                        source["blob_sha256"] \
                        or projection["locator"] != spec.get("locator"):
                    raise WorkflowRejected("review request投影定位或原件绑定不一致")
                quote = self.blobs.read_bytes(
                    projection["text_blob_sha256"]).decode("utf-8")
                if quote != spec.get("quote") \
                        or projection["text_sha256"] != sha256_hex(
                            quote.encode("utf-8")):
                    raise WorkflowRejected("review request quote与投影内容不一致")
                payload = request_input_payload(
                    spec=spec, case_basis=case_basis,
                    assessment_unit=assessment_unit, profile=profile,
                    method_versions=method_versions)
            except ReviewQueueRejected as exc:
                raise WorkflowRejected(str(exc)) from exc
            payloads.append(payload)
            sources[source["source_id"]] = {
                "source_id": source["source_id"],
                "blob_sha256": source["blob_sha256"],
                "byte_length": source["byte_length"],
            }
            projections[projection["projection_id"]] = {
                "projection_id": projection["projection_id"],
                "source_id": projection["source_id"],
                "source_blob_sha256": projection["source_blob_sha256"],
                "locator": projection["locator"],
                "text_sha256": projection["text_sha256"],
                "tool": projection["tool"],
            }
        request_digests = sorted(request_input_digest(item) for item in payloads)
        case_basis_digest = _digest(case_basis, label="CaseBasis")
        identity_payload = {
            "schema_version": JOB_INPUT_SCHEMA,
            "sources": sorted(sources.values(), key=lambda item: item["source_id"]),
            "projections": sorted(
                projections.values(), key=lambda item: item["projection_id"]),
            "case_basis": copy.deepcopy(case_basis),
            "case_basis_digest": case_basis_digest,
            "assessment_unit": copy.deepcopy(assessment_unit),
            "profile_id": profile["profile_id"],
            "profile_digest": profile["profile_digest"],
            "method_versions": copy.deepcopy(method_versions),
            "review_request_input_digests": request_digests,
        }
        input_digest = _digest(identity_payload, label="workflow job input")
        job_id = f"JOB::{input_digest}"
        requests = sorted(
            (build_request(job_id=job_id, payload=payload)
             for payload in payloads),
            key=lambda item: item["request_id"])
        job = {
            "schema_version": JOB_SCHEMA,
            "job_id": job_id,
            "input_digest": input_digest,
            "state": AWAITING,
            **{key: copy.deepcopy(value) for key, value in identity_payload.items()
               if key != "schema_version"},
            "review_request_ids": [item["request_id"] for item in requests],
        }
        existing = self.store.get_workflow_job(job_id)
        if existing is None:
            task_key = f"workflow-create:{job_id}"
            self.journal.ensure_task(task_key, input_digest)
            claim = self.journal.claim(task_key, "local-workflow", input_digest)
            try:
                validated_requests = [
                    self.reviews.validate_request(request)
                    for request in requests]
                self.store.add_workflow_bundle(job, validated_requests)
                self.journal.commit(claim, job_id)
            except Exception as exc:
                try:
                    self.journal.record_failure(claim, str(exc))
                except CommitRejected:
                    pass
                raise
        else:
            for request in requests:
                if self.store.get_review_request(request["request_id"]) is None:
                    self.reviews.add_request(request)
        return self.status(job_id)

    def status(self, job_id: str) -> dict:
        job = self.store.get_workflow_job(job_id)
        if job is None:
            raise WorkflowRejected(f"工作流job不存在：{job_id}")
        required = {
            "schema_version", "job_id", "input_digest", "state", "sources",
            "projections", "case_basis", "case_basis_digest",
            "assessment_unit", "profile_id", "profile_digest",
            "method_versions", "review_request_input_digests",
            "review_request_ids", "created_at", "updated_at",
        }
        allowed = required | {"failure"}
        if set(job) - allowed or not required <= set(job) \
                or job.get("schema_version") != JOB_SCHEMA:
            raise WorkflowRejected("workflow job字段或schema非法")
        if job.get("case_basis_digest") != _digest(
                job.get("case_basis"), label="CaseBasis"):
            raise WorkflowRejected("workflow job CaseBasis身份被改写")
        try:
            profile = get_aggregation_profile(job.get("profile_id"))
        except (TypeError, ValueError) as exc:
            raise WorkflowRejected(f"workflow job profile未登记：{exc}") from exc
        if profile.get("profile_digest") != job.get("profile_digest"):
            raise WorkflowRejected("workflow job profile摘要被改写")
        identity_payload = {
            "schema_version": JOB_INPUT_SCHEMA,
            "sources": copy.deepcopy(job["sources"]),
            "projections": copy.deepcopy(job["projections"]),
            "case_basis": copy.deepcopy(job["case_basis"]),
            "case_basis_digest": job["case_basis_digest"],
            "assessment_unit": copy.deepcopy(job["assessment_unit"]),
            "profile_id": job["profile_id"],
            "profile_digest": job["profile_digest"],
            "method_versions": copy.deepcopy(job["method_versions"]),
            "review_request_input_digests": copy.deepcopy(
                job["review_request_input_digests"]),
        }
        rebuilt_digest = _digest(identity_payload, label="workflow job input")
        if job.get("input_digest") != rebuilt_digest \
                or job.get("job_id") != f"JOB::{rebuilt_digest}":
            raise WorkflowRejected("workflow job内容身份无法重建或正文被改写")
        requests = [self.reviews.get_request(request_id)
                    for request_id in job["review_request_ids"]]
        if [item["request_id"] for item in requests] != \
                job["review_request_ids"] \
                or sorted(item["request_input_digest"] for item in requests) != \
                job["review_request_input_digests"] \
                or any(item["job_id"] != job_id for item in requests):
            raise WorkflowRejected("workflow job与review request身份闭包不一致")
        job["review_requests"] = requests
        return job

    def resume_failed_job(self, job_id: str) -> dict:
        """显式解除机械失败栅栏；复核进度本身无权覆盖failure。"""
        self.reviews.resume_failed_job(job_id)
        return self.status(job_id)

    def close(self) -> None:
        self.journal.close()
        self.store.close()

    def __enter__(self) -> "LocalWorkflow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
