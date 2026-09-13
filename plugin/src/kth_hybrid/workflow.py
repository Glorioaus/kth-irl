"""KTH 本地持久工作流核心；CLI/Plugin 仅应调用本模块。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Iterable

from .aggregate import (
    build_offline_dimension_view,
    freeze_aggregation_manifest,
    validate_offline_dimension_view,
)
from .aggregation_profiles import get_aggregation_profile
from .audit import trace_crl_dimension, trace_dimension_result
from .contracts import sha256_hex
from .intake import inspect_attachment
from .journal import CommitRejected, Journal
from .proposal_requests import (
    AWAITING as AWAITING_PROPOSAL,
    ProposalQueue,
    ProposalQueueRejected,
    approved_catalog,
    build_proposal_request,
    validate_approved_catalog,
    validate_evaluation_input_bindings,
    validate_evaluation_inputs,
)
from .qualification import resolve_case_basis_proof_bindings
from .review_queue import (
    AWAITING,
    ReviewQueue,
    ReviewQueueRejected,
    build_request,
    request_input_digest,
    request_input_payload,
)
from .store import BlobStore, CaseStore
from .runner import (
    run_brl_dimension_slice,
    run_crl_dimension_slice,
    run_frl_dimension_slice,
    run_iprl_dimension_slice,
    run_tmrl_dimension_slice,
    run_trl_dimension_slice,
)


JOB_SCHEMA = "kth-local.workflow-job.v1"
JOB_INPUT_SCHEMA = "kth-local.workflow-job-input.v1"
JOB_SCHEMA_V2 = "kth-local.workflow-job.v2"
JOB_INPUT_SCHEMA_V2 = "kth-local.workflow-job-input.v2"
MAX_ATTACHMENT_FILES = 256
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENT_BATCH_BYTES = 256 * 1024 * 1024
MAX_REVIEW_SPECS = 512
MAX_REVIEW_SPECS_BYTES = 2 * 1024 * 1024
MAX_REVIEW_SPEC_DEPTH = 24


class WorkflowRejected(RuntimeError):
    """工作流输入、状态或持久化关系不满足受控合同。"""


class LocalWorkflowCrash(RuntimeError):
    """仅供恢复测试在已持久化边界明确中断本地工作流。"""


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
        self.proposals = ProposalQueue(
            self.store, self.blobs, self.journal, self.reviews)

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
                   method_versions: dict, review_specs: list[dict],
                   resume_failed_creation: bool = False) -> dict:
        raise WorkflowRejected(
            "legacy_restricted：workflow-job.v1仅保留历史读取，"
            "新材料必须创建workflow-job.v2")

    def _create_job_v1_history_fixture(
            self, *, assessment_unit: dict, profile_id: str,
            method_versions: dict, review_specs: list[dict],
            resume_failed_creation: bool = False) -> dict:
        """仅供历史迁移回归装载v1行；产品入口不得调用。"""
        if not isinstance(resume_failed_creation, bool):
            raise WorkflowRejected("resume_failed_creation必须是显式布尔值")
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
        task_key = f"workflow-create:{job_id}"
        if existing is None:
            self.journal.ensure_task(task_key, input_digest)
            state = self.journal.task_state(task_key)
            if state["state"] == "failed" and not resume_failed_creation:
                raise WorkflowRejected(
                    "workflow-create任务failed，必须显式受控恢复")
            if state["state"] not in {"planned", "failed"}:
                raise WorkflowRejected(
                    "workflow-create任务已有非终态但bundle不完整，"
                    "本阶段不自动接管或补写")
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
            try:
                verified = self.status(job_id)
                if verified["input_digest"] != input_digest \
                        or verified["review_request_ids"] != \
                        job["review_request_ids"]:
                    raise WorkflowRejected(
                        "existing workflow job/request bundle与本次输入不一致")
                self.journal.complete_existing_local_task(
                    task_key, input_id=input_digest, output_ref=job_id,
                    worker_id="local-workflow-recovery",
                    evidence=(
                        "existing workflow job/request bundle已逐字核验完整"),
                    allow_failed_recovery=resume_failed_creation)
            except (KeyError, CommitRejected, ReviewQueueRejected) as exc:
                raise WorkflowRejected(
                    f"workflow-create既有bundle封账被拒：{exc}") from exc
        return self.status(job_id)

    def create_candidate_job(self, *, evaluation_inputs: dict,
                             proposal_specs: list[dict], catalog: dict,
                             resume_failed_creation: bool = False) -> dict:
        """建立v2候选提出job；无Provider时停在候选待执行。"""
        if not isinstance(resume_failed_creation, bool):
            raise WorkflowRejected("resume_failed_creation必须是显式布尔值")
        try:
            evaluation = validate_evaluation_inputs(evaluation_inputs)
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        if not isinstance(proposal_specs, list) or not proposal_specs \
                or len(proposal_specs) > MAX_REVIEW_SPECS:
            raise WorkflowRejected("proposal_specs必须是有界非空列表")
        _require_bounded_json(
            proposal_specs, label="proposal_specs",
            max_bytes=MAX_REVIEW_SPECS_BYTES,
            max_depth=MAX_REVIEW_SPEC_DEPTH)
        try:
            catalog_digest = validate_approved_catalog(catalog)
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        current = self.store.get_case_basis()
        if current is None or not isinstance(current.get("version"), int):
            raise WorkflowRejected("CaseBasis尚未初始化")
        case_basis = self.store.get_case_basis_version(current["version"])
        if evaluation["assessment_unit"].get("subject_scope") != \
                case_basis["subject_legal_name"] \
                or evaluation["financing_entity"].get("subject_scope") != \
                case_basis["subject_legal_name"]:
            raise WorkflowRejected("evaluation_inputs主体范围与CaseBasis不一致")
        case_basis_body = {key: copy.deepcopy(value)
                           for key, value in case_basis.items()
                           if key != "created_at"}
        case_basis_digest = _digest(case_basis_body, label="CaseBasis")
        proofs, proof_error = resolve_case_basis_proof_bindings(
            case_basis, self.store, self.blobs)
        if proof_error or proofs is None:
            raise WorkflowRejected(
                f"CaseBasis证明不可核验：{proof_error or '无绑定'}")
        case_basis_proof_digest = _digest(
            proofs, label="CaseBasis proofs")
        try:
            evaluation_proof_bindings = validate_evaluation_input_bindings(
                evaluation, case=self.store, blobs=self.blobs,
                subject_scope=case_basis["subject_legal_name"])
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        payloads = []
        sources = {}
        projections = {}
        output_contract = {
            "schema_version": "proposal_response.v1",
            "candidate_schema_version": "claim_candidate.v1",
            "max_candidates": 512,
            "forbidden_fields": [
                "final_decision", "investment_recommendation", "level",
                "maturity_level", "native_disposition", "result_id"],
        }
        required_spec = {
            "source_id", "blob_sha256", "projection_id", "locator", "quote",
            "purpose", "output_schema",
        }
        for spec in proposal_specs:
            if not isinstance(spec, dict) or set(spec) != required_spec:
                raise WorkflowRejected("proposal spec字段集合非法")
            source = self.store.fetch_one(
                "sources", "source_id", spec.get("source_id"))
            projection = self.store.get_text_projection(
                spec.get("projection_id"))
            if source is None or source.get("blob_sha256") != spec.get("blob_sha256"):
                raise WorkflowRejected("proposal source/blob引用不一致")
            if projection is None or projection.get("status") != "projected" \
                    or projection.get("source_id") != source["source_id"] \
                    or projection.get("source_blob_sha256") != source["blob_sha256"] \
                    or projection.get("locator") != spec.get("locator"):
                raise WorkflowRejected("proposal projection/locator引用不一致")
            quote = self.blobs.read_bytes(
                projection["text_blob_sha256"]).decode("utf-8")
            if quote != spec.get("quote") or sha256_hex(
                    quote.encode("utf-8")) != projection.get("text_sha256"):
                raise WorkflowRejected("proposal quote与持久化投影不一致")
            for field in ("purpose", "output_schema"):
                if not isinstance(spec.get(field), str) or not spec[field].strip():
                    raise WorkflowRejected(f"proposal {field}不能为空")
            if spec["output_schema"] != "proposal_response.v1":
                raise WorkflowRejected("proposal output_schema不是批准v1合同")
            payloads.append({
                "source_id": source["source_id"],
                "blob_sha256": source["blob_sha256"],
                "projection_id": projection["projection_id"],
                "locator": copy.deepcopy(projection["locator"]),
                "quote": quote,
                "quote_sha256": projection["text_sha256"],
                "case_basis_version": case_basis["version"],
                "case_basis_digest": case_basis_digest,
                "case_basis_proof_digest": case_basis_proof_digest,
                "evaluation_inputs": copy.deepcopy(evaluation),
                "evaluation_input_proof_bindings": copy.deepcopy(
                    evaluation_proof_bindings),
                "catalog_sha256": catalog["wheel_sha256"],
                "catalog_digest": catalog_digest,
                "purpose": spec["purpose"],
                "output_schema": spec["output_schema"],
                "output_contract": copy.deepcopy(output_contract),
            })
            sources[source["source_id"]] = {
                "source_id": source["source_id"],
                "blob_sha256": source["blob_sha256"],
                "byte_length": source["byte_length"],
            }
            projections[projection["projection_id"]] = {
                key: copy.deepcopy(projection[key]) for key in (
                    "projection_id", "source_id", "source_blob_sha256",
                    "locator", "text_sha256", "tool")}
        input_payload = {
            "schema_version": JOB_INPUT_SCHEMA_V2,
            "sources": sorted(sources.values(), key=lambda item: item["source_id"]),
            "projections": sorted(
                projections.values(), key=lambda item: item["projection_id"]),
            "case_basis": copy.deepcopy(case_basis_body),
            "case_basis_digest": case_basis_digest,
            "case_basis_proof_digest": case_basis_proof_digest,
            "evaluation_inputs": copy.deepcopy(evaluation),
            "evaluation_input_proof_bindings": copy.deepcopy(
                evaluation_proof_bindings),
            "catalog_sha256": catalog["wheel_sha256"],
            "catalog_digest": catalog_digest,
            "proposal_request_input_digests": sorted(
                _digest(payload, label="proposal request input")
                for payload in payloads),
        }
        input_digest = _digest(input_payload, label="workflow job v2 input")
        job_id = f"JOB2::{input_digest}"
        requests = sorted(
            (build_proposal_request(job_id=job_id, payload=payload)
             for payload in payloads),
            key=lambda item: item["request_id"])
        # build_proposal_request的输入摘要仅覆盖payload；用实际值替换，防止实现漂移。
        input_payload["proposal_request_input_digests"] = sorted(
            item["request_input_digest"] for item in requests)
        input_digest = _digest(input_payload, label="workflow job v2 input")
        job_id = f"JOB2::{input_digest}"
        requests = sorted(
            (build_proposal_request(job_id=job_id, payload=payload)
             for payload in payloads),
            key=lambda item: item["request_id"])
        job = {
            "schema_version": JOB_SCHEMA_V2,
            "input_schema_version": JOB_INPUT_SCHEMA_V2,
            "job_id": job_id,
            "input_digest": input_digest,
            "state": AWAITING_PROPOSAL,
            **{key: copy.deepcopy(value) for key, value in input_payload.items()
               if key != "schema_version"},
            "proposal_request_ids": [item["request_id"] for item in requests],
        }
        existing = self.store.get_workflow_job(job_id)
        if existing is None:
            task_key = f"workflow-create-v2:{job_id}"
            self.journal.ensure_task(task_key, input_digest)
            state = self.journal.task_state(task_key)
            if state["state"] == "failed" and not resume_failed_creation:
                raise WorkflowRejected("workflow-create-v2失败，必须显式恢复")
            claim = self.journal.claim(task_key, "local-workflow-v2", input_digest)
            try:
                validated = [self.proposals.validate_request(item)
                             for item in requests]
                self.store.add_candidate_workflow_bundle(job, validated)
                self.journal.commit(claim, job_id)
            except Exception as exc:
                try:
                    self.journal.record_failure(claim, str(exc))
                except CommitRejected:
                    pass
                raise
        return self.status(job_id)

    def status(self, job_id: str) -> dict:
        job = self.store.get_workflow_job(job_id)
        if job is None:
            raise WorkflowRejected(f"工作流job不存在：{job_id}")
        if job.get("schema_version") == JOB_SCHEMA_V2:
            return self._status_v2(job)
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

    def _status_v2(self, job: dict) -> dict:
        required = {
            "schema_version", "input_schema_version", "job_id", "input_digest",
            "state", "sources", "projections", "case_basis",
            "case_basis_digest", "case_basis_proof_digest", "evaluation_inputs",
            "evaluation_input_proof_bindings",
            "catalog_sha256", "proposal_request_input_digests",
            "catalog_digest",
            "proposal_request_ids", "created_at", "updated_at",
        }
        if not required <= set(job) or set(job) - (required | {"failure"}):
            raise WorkflowRejected("workflow job v2字段集合非法")
        if job["input_schema_version"] != JOB_INPUT_SCHEMA_V2:
            raise WorkflowRejected("workflow job v2输入schema非法")
        try:
            validate_evaluation_inputs(job["evaluation_inputs"])
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        input_payload = {
            "schema_version": JOB_INPUT_SCHEMA_V2,
            "sources": copy.deepcopy(job["sources"]),
            "projections": copy.deepcopy(job["projections"]),
            "case_basis": copy.deepcopy(job["case_basis"]),
            "case_basis_digest": job["case_basis_digest"],
            "case_basis_proof_digest": job["case_basis_proof_digest"],
            "evaluation_inputs": copy.deepcopy(job["evaluation_inputs"]),
            "evaluation_input_proof_bindings": copy.deepcopy(
                job["evaluation_input_proof_bindings"]),
            "catalog_sha256": job["catalog_sha256"],
            "catalog_digest": job["catalog_digest"],
            "proposal_request_input_digests": copy.deepcopy(
                job["proposal_request_input_digests"]),
        }
        digest = _digest(input_payload, label="workflow job v2 input")
        if job["input_digest"] != digest or job["job_id"] != f"JOB2::{digest}":
            raise WorkflowRejected("workflow job v2内容身份无法重建")
        requests = [self.proposals.get_request(request_id)
                    for request_id in job["proposal_request_ids"]]
        if sorted(item["request_input_digest"] for item in requests) != \
                job["proposal_request_input_digests"] \
                or any(item["job_id"] != job["job_id"] for item in requests):
            raise WorkflowRejected("workflow job v2与proposal request闭包不一致")
        job["proposal_requests"] = requests
        job["review_requests"] = self.store.fetch_review_requests(job["job_id"])
        job["dimension_outputs"] = self.store.fetch_workflow_job_dimension_outputs(
            job["job_id"])
        job["artifacts"] = self.store.get_workflow_job_artifacts(job["job_id"])
        if job["artifacts"] is not None:
            job["source_modes"] = copy.deepcopy(job["artifacts"]["source_modes"])
        else:
            modes = []
            for request in requests:
                response = self.store.get_proposal_response_for_request(
                    request["request_id"])
                if response is not None:
                    modes.append(response["source_mode"])
            for request in job["review_requests"]:
                response = self.store.get_review_response_for_request(
                    request["request_id"])
                if response is not None:
                    modes.append(response["source_mode"])
            if modes:
                job["source_modes"] = sorted(set(modes))
        return job

    @staticmethod
    def _trace_digest(report: dict) -> str:
        if not isinstance(report, dict) or report.get("ok") is not True:
            raise WorkflowRejected("维度结果trace未通过，禁止登记workflow输出")
        return _digest(report, label="workflow维度trace")

    def _revalidate_v2_execution_inputs(self, job: dict) -> tuple[dict, dict]:
        """在运行前从存储重核冻结输入；调用方声明不是执行事实。"""
        catalog = approved_catalog()
        try:
            catalog_digest = validate_approved_catalog(catalog)
            evaluation = validate_evaluation_inputs(job["evaluation_inputs"])
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        if catalog.get("wheel_sha256") != job.get("catalog_sha256") \
                or catalog_digest != job.get("catalog_digest"):
            raise WorkflowRejected("workflow job冻结catalog与当前批准目录不一致")
        try:
            profile = get_aggregation_profile(evaluation["profile"]["profile_id"])
        except (TypeError, ValueError) as exc:
            raise WorkflowRejected(f"workflow job profile未登记：{exc}") from exc
        if evaluation["profile"] != {key: profile[key] for key in (
                "profile_id", "profile_digest")}:
            raise WorkflowRejected("workflow job profile正文或摘要不一致")
        frozen_basis = job.get("case_basis")
        if not isinstance(frozen_basis, dict) or not isinstance(
                frozen_basis.get("version"), int):
            raise WorkflowRejected("workflow job缺少冻结CaseBasis版本")
        live_basis = self.store.get_case_basis_version(frozen_basis["version"])
        if live_basis is None:
            raise WorkflowRejected("workflow job冻结CaseBasis版本已不存在")
        live_body = {key: copy.deepcopy(value) for key, value in live_basis.items()
                     if key != "created_at"}
        if live_body != frozen_basis or _digest(live_body, label="CaseBasis") != \
                job.get("case_basis_digest"):
            raise WorkflowRejected("workflow job冻结CaseBasis正文或身份已变化")
        proofs, proof_error = resolve_case_basis_proof_bindings(
            live_basis, self.store, self.blobs)
        if proof_error or proofs is None or _digest(
                proofs, label="CaseBasis proofs") != \
                job.get("case_basis_proof_digest"):
            raise WorkflowRejected(
                f"workflow job CaseBasis证明不可核验：{proof_error or '绑定变化'}")
        try:
            evaluation_proofs = validate_evaluation_input_bindings(
                evaluation, case=self.store, blobs=self.blobs,
                subject_scope=live_basis["subject_legal_name"])
        except ProposalQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc
        if evaluation_proofs != job.get("evaluation_input_proof_bindings"):
            raise WorkflowRejected("workflow job评估输入证明与冻结视图不一致")
        return catalog, live_basis

    def _verify_registered_dimension_output(self, job_id: str,
                                            dimension_id: str) -> dict | None:
        row = self.store.get_workflow_job_dimension_output(job_id, dimension_id)
        if row is None:
            return None
        report = (trace_crl_dimension(self.store, self.blobs, row["result_id"])
                  if dimension_id == "CRL" else
                  trace_dimension_result(self.store, self.blobs, row["result_id"]))
        trace_digest = self._trace_digest(report)
        if row["input_digest"] != report.get("input_digest") \
                or row["trace_digest"] != trace_digest:
            raise WorkflowRejected("已登记workflow维度输出与精确trace不一致")
        return row

    def _run_and_register_dimension(self, *, job: dict, catalog: dict,
                                    case_basis: dict, dimension_id: str) -> dict:
        existing = self._verify_registered_dimension_output(
            job["job_id"], dimension_id)
        if existing is not None:
            return existing
        evaluation = job["evaluation_inputs"]
        scope = case_basis["subject_legal_name"]
        try:
            if dimension_id == "CRL":
                result = run_crl_dimension_slice(
                    self.case_dir, catalog=catalog, case_basis=case_basis,
                    scope=scope)
            elif dimension_id == "FRL":
                result = run_frl_dimension_slice(
                    self.case_dir, catalog=catalog, case_basis=case_basis,
                    scope=scope, financing_entity=evaluation["financing_entity"],
                    applicability_policy=evaluation["frl_applicability"])
            else:
                runners = {
                    "BRL": run_brl_dimension_slice,
                    "TRL": run_trl_dimension_slice,
                    "IPRL": run_iprl_dimension_slice,
                    "TMRL": run_tmrl_dimension_slice,
                }
                result = runners[dimension_id](
                    self.case_dir, catalog=catalog, case_basis=case_basis,
                    scope=scope, assessment_unit=evaluation["assessment_unit"])
        except Exception as exc:
            raise WorkflowRejected(
                f"{dimension_id}维度runner系统失败：{type(exc).__name__}: {exc}") from exc
        if not isinstance(result, dict) or result.get("result_id") is None \
                or result.get("input_digest") is None:
            raise WorkflowRejected(f"{dimension_id}维度runner未返回精确结果身份")
        if result.get("dimension", {}).get("product_status") == \
                "execution_failed":
            raise WorkflowRejected(
                f"{dimension_id}维度runner报告系统执行失败，不能登记为业务insufficient")
        report = (trace_crl_dimension(self.store, self.blobs, result["result_id"])
                  if dimension_id == "CRL" else
                  trace_dimension_result(self.store, self.blobs, result["result_id"]))
        trace_digest = self._trace_digest(report)
        if report.get("input_digest") != result["input_digest"]:
            raise WorkflowRejected(f"{dimension_id}维度runner结果与trace输入不一致")
        try:
            return self.store.add_workflow_job_dimension_output(
                job_id=job["job_id"], dimension_id=dimension_id,
                result_id=result["result_id"], input_digest=result["input_digest"],
                trace_digest=trace_digest)
        except Exception as exc:
            raise WorkflowRejected(
                f"{dimension_id}维度输出登记失败：{type(exc).__name__}: {exc}") from exc

    def _source_modes_for_job(self, job_id: str) -> list[str]:
        modes = []
        for request in self.store.fetch_proposal_requests(job_id):
            response = self.store.get_proposal_response_for_request(
                request["request_id"])
            if response is not None:
                modes.append(response["source_mode"])
        for request in self.store.fetch_review_requests(job_id):
            response = self.store.get_review_response_for_request(
                request["request_id"])
            if response is not None:
                modes.append(response["source_mode"])
        if not modes:
            raise WorkflowRejected("workflow job未封存任何来源模式，不能冻结输出工件")
        return sorted(set(modes))

    def _verify_registered_artifacts(self, *, job: dict) -> dict | None:
        artifacts = self.store.get_workflow_job_artifacts(job["job_id"])
        if artifacts is None:
            return None
        try:
            manifest = json.loads(self.blobs.read_bytes(
                artifacts["manifest_blob_sha256"]).decode("utf-8"))
            expected_manifest = freeze_aggregation_manifest(
                self.store, self.blobs,
                {row["dimension_id"]: row["result_id"] for row in
                 self.store.fetch_workflow_job_dimension_outputs(job["job_id"])},
                profile_id=job["evaluation_inputs"]["profile"]["profile_id"])
            if manifest != expected_manifest \
                    or manifest.get("manifest_id") != artifacts["manifest_id"] \
                    or manifest.get("manifest_digest") != artifacts["manifest_digest"]:
                raise WorkflowRejected("已登记workflow manifest无法按精确输出重建")
            view = json.loads(self.blobs.read_bytes(
                artifacts["view_blob_sha256"]).decode("utf-8"))
            expected_view = build_offline_dimension_view(
                self.store, self.blobs, manifest)
            validate_offline_dimension_view(view)
            if view != expected_view or view.get("view_id") != artifacts["view_id"] \
                    or view.get("input_digest") != artifacts["view_digest"]:
                raise WorkflowRejected("已登记workflow view无法按冻结manifest重建")
            if artifacts["source_modes"] != self._source_modes_for_job(job["job_id"]):
                raise WorkflowRejected("workflow工件来源模式与封存响应不一致")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowRejected(f"已登记workflow工件不可核验：{exc}") from exc
        return artifacts

    def run_job(self, job_id: str, *, crash_after_dimension: str | None = None,
                crash_before_manifest: bool = False) -> dict:
        """运行精确v2 job：只消费封存输入，实际调用既有六维runner。"""
        if crash_after_dimension is not None and crash_after_dimension not in {
                "CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}:
            raise WorkflowRejected("crash_after_dimension必须是精确六维ID或null")
        if not isinstance(crash_before_manifest, bool):
            raise WorkflowRejected("crash_before_manifest必须为显式布尔值")
        job = self.status(job_id)
        if job.get("schema_version") != JOB_SCHEMA_V2:
            raise WorkflowRejected("legacy_restricted：run_job仅运行v2候选job")
        if job["state"] == "failed":
            raise WorkflowRejected("workflow job处于failed，必须显式恢复后再运行")
        proposals = job["proposal_requests"]
        proposal_states = {request["status"] for request in proposals}
        if "failed" in proposal_states:
            self.store.set_workflow_job_state(job_id, "failed", failure={
                "stage": "proposal", "reason": "候选提出请求处于系统失败状态"})
            raise WorkflowRejected("候选提出请求失败，未写入业务insufficient")
        if "awaiting_candidate_proposal" in proposal_states:
            return self.status(job_id)
        if "proposal_response_sealed" in proposal_states:
            self.store.set_workflow_job_state(job_id, "proposal_response_sealed")
            return self.status(job_id)
        if proposal_states != {"consumed"}:
            raise WorkflowRejected("候选提出请求状态组合非法")
        reviews = self.store.fetch_review_requests(job_id)
        review_states = {request["status"] for request in reviews}
        if not reviews:
            return self.status(job_id)
        if "failed" in review_states:
            self.store.set_workflow_job_state(job_id, "failed", failure={
                "stage": "review", "reason": "专业复核请求处于系统失败状态"})
            raise WorkflowRejected("专业复核请求失败，未写入业务insufficient")
        if "awaiting_authorized_analysis" in review_states:
            self.store.set_workflow_job_state(job_id, "awaiting_authorized_analysis")
            return self.status(job_id)
        if "response_sealed" in review_states:
            self.store.set_workflow_job_state(job_id, "response_sealed")
            return self.status(job_id)
        if review_states != {"consumed"}:
            raise WorkflowRejected("专业复核请求状态组合非法")
        self.store.set_workflow_job_state(job_id, "reviews_consumed")
        try:
            for request in reviews:
                response = self.store.get_review_response_for_request(
                    request["request_id"])
                if response is None or response.get("status") != "consumed":
                    raise WorkflowRejected("已消费专业请求缺少封存response")
                self.materialize_review_response(
                    response["response_id"], worker_id="workflow-run-job")
        except Exception as exc:
            self.store.set_workflow_job_state(job_id, "failed", failure={
                "stage": "materialize", "reason": str(exc)})
            if isinstance(exc, WorkflowRejected):
                raise
            raise WorkflowRejected(f"专业复核物化失败：{exc}") from exc
        self.store.set_workflow_job_state(job_id, "evidence_materialized")
        try:
            catalog, case_basis = self._revalidate_v2_execution_inputs(job)
            self.store.set_workflow_job_state(job_id, "evaluating")
            for dimension_id in ("CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"):
                self._run_and_register_dimension(
                    job=job, catalog=catalog, case_basis=case_basis,
                    dimension_id=dimension_id)
                if crash_after_dimension == dimension_id:
                    raise LocalWorkflowCrash(
                        f"workflow job在{dimension_id}维度登记后受控中断")
            self.store.set_workflow_job_state(job_id, "evaluated")
            if crash_before_manifest:
                raise LocalWorkflowCrash("workflow job在manifest冻结前受控中断")
            artifacts = self._verify_registered_artifacts(job=job)
            if artifacts is None:
                result_ids = {row["dimension_id"]: row["result_id"] for row in
                              self.store.fetch_workflow_job_dimension_outputs(job_id)}
                manifest = freeze_aggregation_manifest(
                    self.store, self.blobs, result_ids,
                    profile_id=job["evaluation_inputs"]["profile"]["profile_id"])
                view = build_offline_dimension_view(self.store, self.blobs, manifest)
                validate_offline_dimension_view(view)
                manifest_ref = self.blobs.put_bytes(_canonical_bytes(
                    manifest, label="workflow manifest"))
                view_ref = self.blobs.put_bytes(_canonical_bytes(
                    view, label="workflow view"))
                rebuilt_manifest = freeze_aggregation_manifest(
                    self.store, self.blobs, result_ids,
                    profile_id=job["evaluation_inputs"]["profile"]["profile_id"])
                rebuilt_view = build_offline_dimension_view(
                    self.store, self.blobs, rebuilt_manifest)
                if rebuilt_manifest != manifest or rebuilt_view != view \
                        or self.blobs.read_bytes(manifest_ref.sha256) != \
                        _canonical_bytes(manifest, label="workflow manifest") \
                        or self.blobs.read_bytes(view_ref.sha256) != \
                        _canonical_bytes(view, label="workflow view"):
                    raise WorkflowRejected("workflow manifest/view对象或字节重建失败")
                self.store.set_workflow_job_state(job_id, "manifest_frozen")
                artifacts = self.store.add_workflow_job_artifacts(
                    job_id=job_id, manifest_id=manifest["manifest_id"],
                    manifest_digest=manifest["manifest_digest"],
                    manifest_blob_sha256=manifest_ref.sha256,
                    view_id=view["view_id"], view_digest=view["input_digest"],
                    view_blob_sha256=view_ref.sha256,
                    source_modes=self._source_modes_for_job(job_id))
            self._verify_registered_artifacts(job=job)
            self.store.set_workflow_job_state(job_id, "completed")
        except LocalWorkflowCrash:
            raise
        except Exception as exc:
            self.store.set_workflow_job_state(job_id, "failed", failure={
                "stage": "evaluate_or_manifest", "reason": str(exc)})
            if isinstance(exc, WorkflowRejected):
                raise
            raise WorkflowRejected(f"workflow运行系统失败：{exc}") from exc
        return self.status(job_id)

    def resume_failed_job(self, job_id: str) -> dict:
        """按精确job合同恢复本地状态，不派发或自动消费任何专业动作。"""
        job = self.store.get_workflow_job(job_id)
        if job is None:
            raise WorkflowRejected(f"workflow job不存在：{job_id}")
        if job.get("schema_version") == JOB_SCHEMA_V2:
            # v2候选阶段没有review request；非failed调用只读取精确状态。
            if job.get("state") != "failed":
                return self.status(job_id)
            proposals = self.store.fetch_proposal_requests(job_id)
            if not proposals:
                raise WorkflowRejected("v2候选job缺少proposal request，不能解除failed栅栏")
            states = {item["status"] for item in proposals}
            if "failed" in states:
                raise WorkflowRejected("proposal request仍为failed，不能解除workflow failed栅栏")
            if "awaiting_candidate_proposal" in states:
                self.store.set_workflow_job_state(
                    job_id, "awaiting_candidate_proposal")
                return self.status(job_id)
            if "proposal_response_sealed" in states:
                self.store.set_workflow_job_state(
                    job_id, "proposal_response_sealed")
                return self.status(job_id)
            if states != {"consumed"}:
                raise WorkflowRejected("v2候选proposal request状态组合非法")
            if self.store.fetch_review_requests(job_id):
                try:
                    self.reviews.resume_failed_job(job_id)
                except ReviewQueueRejected as exc:
                    raise WorkflowRejected(str(exc)) from exc
            else:
                self.store.set_workflow_job_state(job_id, "insufficient")
            return self.status(job_id)
        # v1历史review工作流仅保留既有显式failed恢复路径。
        self.reviews.resume_failed_job(job_id)
        return self.status(job_id)

    def materialize_review_response(self, response_id: str, *,
                                    worker_id: str) -> dict:
        """把已消费的可信 v2 专业返回转换为既有维度runner可消费的review。"""
        try:
            response = self.reviews.get_response(response_id)
            request = self.reviews.get_request(response["request_id"])
            if request.get("schema_version") != "review_request.v2" \
                    or response.get("schema_version") != "review_response.v2":
                raise WorkflowRejected(
                    "legacy_restricted：仅已消费的review request/response.v2可物化")
            self.status(request["job_id"])
            return self.reviews.materialize_consumed_response(
                response_id, worker_id=worker_id)
        except ReviewQueueRejected as exc:
            raise WorkflowRejected(str(exc)) from exc

    def close(self) -> None:
        self.journal.close()
        self.store.close()

    def __enter__(self) -> "LocalWorkflow":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
