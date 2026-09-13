"""受控专业复核队列：请求冻结、返回封存与独立消费。"""

from __future__ import annotations

import copy
import json
import re
from typing import Any
import unicodedata

from .contracts import sha256_hex
from .evidence_permissions import validate_evidence_use_license
from .journal import CommitRejected, Journal
from .store import (
    WORKFLOW_REVIEW_MATERIALIZATION_SCHEMA,
    BlobStore,
    CaseStore,
    validate_workflow_review_materialization,
)


REQUEST_SCHEMA = "review_request.v1"
RESPONSE_SCHEMA = "review_response.v1"
REQUEST_SCHEMA_V2 = "review_request.v2"
RESPONSE_SCHEMA_V2 = "review_response.v2"
AWAITING = "awaiting_authorized_analysis"
_ALLOWED_SOURCE_MODES = {"manual_import", "simulated", "runtime_provider"}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_FINDINGS_BYTES = 256 * 1024
MAX_FINDINGS_ITEMS = 512
MAX_CITATIONS = 64
MAX_CITATIONS_BYTES = 256 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_DECODE_LAYERS = 16
MAX_JSON_ESCAPE_DECODE_PASSES = 4
_PRODUCER_KINDS_BY_MODE = {
    "manual_import": {"authorized_human", "human", "manual_import"},
    "simulated": {"simulated", "simulated_test"},
    "runtime_provider": {"runtime_provider"},
}
_FORBIDDEN_OUTPUT_KEYS = {
    "level", "maturity_level", "native_disposition", "native_note",
    "product_status", "final_decision", "investment_recommendation",
    "investment_decision", "approved", "go_no_go",
    "result_id", "rule_version", "criterion_results", "criteria",
    "attained_level", "first_unmet_level", "method_boundary",
    "rule_result", "evaluation_result",
}
_CITATION_FIELDS = {
    "source_id", "blob_sha256", "projection_id", "locator", "quote_sha256",
}
_JSON_COMPATIBLE_ESCAPE = re.compile(
    r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')


class ReviewQueueRejected(RuntimeError):
    """复核请求或返回的身份、权限、引用或输出合同不合法。"""


def _canonical_bytes(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewQueueRejected(f"{label}不是规范JSON：{exc}") from exc


def _digest(value: Any, *, label: str) -> str:
    return sha256_hex(_canonical_bytes(value, label=label))


def _validate_json_limits(value: Any, *, label: str, max_bytes: int,
                          max_depth: int, max_items: int | None = None) -> None:
    stack = [(value, 1, frozenset())]
    item_count = 0
    while stack:
        item, depth, ancestors = stack.pop()
        item_count += 1
        if depth > max_depth:
            raise ReviewQueueRejected(f"{label}深度超过上限{max_depth}")
        if max_items is not None and item_count > max_items:
            raise ReviewQueueRejected(f"{label}条目超过上限{max_items}")
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in ancestors:
                raise ReviewQueueRejected(f"{label}包含循环引用")
            nested = ancestors | {identity}
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1, nested) for child in children)
    if len(_canonical_bytes(value, label=label)) > max_bytes:
        raise ReviewQueueRejected(f"{label}序列化字节超过上限{max_bytes}")


def request_input_payload(*, spec: dict, case_basis: dict,
                          assessment_unit: dict, profile: dict,
                          method_versions: dict) -> dict:
    """构造不含job循环引用的完整请求输入；job身份冻结其摘要。"""
    required = {
        "source_id", "blob_sha256", "projection_id", "locator", "quote",
        "dimension_id", "criterion_id", "scope_id", "license",
        "requested_use", "purpose", "output_schema",
    }
    allowed = required | {"case_basis_version"}
    if not isinstance(spec, dict) or not required <= set(spec) \
            or set(spec) - allowed:
        raise ReviewQueueRejected("review request缺少必需引用或含未知字段")
    quote = spec.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ReviewQueueRejected("review request quote不能为空")
    license_value = spec.get("license")
    try:
        mode = validate_evidence_use_license(
            license_value.get("license_id") if isinstance(license_value, dict)
            else "", license_value)
    except (TypeError, ValueError) as exc:
        raise ReviewQueueRejected(f"review request用途许可非法：{exc}") from exc
    if mode != "v2":
        raise ReviewQueueRejected("review request只接受v2用途许可")
    criterion_id = spec["criterion_id"]
    requested_use = spec["requested_use"]
    allowed_uses = (license_value.get("allowed_criterion_uses") or {}).get(
        criterion_id)
    if not isinstance(allowed_uses, list) or requested_use not in allowed_uses:
        raise ReviewQueueRejected("review request criterion/requested_use越权")
    bindings = {
        "dimension_id": spec["dimension_id"],
        "criterion_id": criterion_id,
        "scope_id": spec["scope_id"],
        "quote_sha256": sha256_hex(quote.encode("utf-8")),
    }
    for field, value in bindings.items():
        if license_value.get(field) != value:
            raise ReviewQueueRejected(
                f"review request {field}与用途许可不一致")
    if spec["scope_id"] != assessment_unit.get("scope_id"):
        raise ReviewQueueRejected("review request scope与评估单元不一致")
    requested_basis_version = spec.get("case_basis_version")
    if requested_basis_version is not None \
            and requested_basis_version != case_basis.get("version"):
        raise ReviewQueueRejected("review request CaseBasis版本过期或不一致")
    for field, value in {
        "source_id": spec["source_id"],
        "blob_sha256": spec["blob_sha256"],
        "projection_id": spec["projection_id"],
        "purpose": spec["purpose"],
        "output_schema": spec["output_schema"],
    }.items():
        if not isinstance(value, str) or not value.strip():
            raise ReviewQueueRejected(f"review request {field}不能为空")
    if not isinstance(spec["locator"], dict) or not spec["locator"]:
        raise ReviewQueueRejected("review request locator非法")
    if not isinstance(method_versions, dict) or not method_versions \
            or any(not isinstance(key, str) or not key.strip()
                   or not isinstance(value, str) or not value.strip()
                   for key, value in method_versions.items()):
        raise ReviewQueueRejected("review request方法版本非法")
    case_basis_body = copy.deepcopy(case_basis)
    case_basis_body.pop("created_at", None)
    return {
        "source_id": spec["source_id"],
        "blob_sha256": spec["blob_sha256"],
        "projection_id": spec["projection_id"],
        "locator": copy.deepcopy(spec["locator"]),
        "quote": quote,
        "quote_sha256": bindings["quote_sha256"],
        "case_basis_digest": _digest(case_basis_body, label="CaseBasis"),
        "case_basis_version": case_basis["version"],
        "assessment_unit": copy.deepcopy(assessment_unit),
        "dimension_id": spec["dimension_id"],
        "criterion_id": criterion_id,
        "scope_id": spec["scope_id"],
        "license_id": license_value["license_id"],
        "license": copy.deepcopy(license_value),
        "requested_use": requested_use,
        "profile_id": profile["profile_id"],
        "profile_digest": profile["profile_digest"],
        "method_versions": copy.deepcopy(method_versions),
        "purpose": spec["purpose"],
        "output_schema": spec["output_schema"],
    }


def request_input_digest(payload: dict) -> str:
    return _digest(payload, label="review request input")


def build_request(*, job_id: str, payload: dict) -> dict:
    input_digest = request_input_digest(payload)
    body = {
        "schema_version": REQUEST_SCHEMA,
        "job_id": job_id,
        "request_input_digest": input_digest,
        **copy.deepcopy(payload),
    }
    request_id = f"REVIEWREQ::{_digest(body, label='review request')}"
    return {**body, "request_id": request_id, "status": AWAITING}


def _authorization_digest(authorization: dict) -> str:
    body = {key: copy.deepcopy(value) for key, value in authorization.items()
            if key != "authorization_id"}
    return _digest(body, label="review authorization")


def build_authorized_request(*, job_id: str, authorization: dict) -> dict:
    body = {
        "schema_version": REQUEST_SCHEMA_V2,
        "job_id": job_id,
        "authorization_id": authorization.get("authorization_id"),
        "authorization": copy.deepcopy(authorization),
        "output_schema": RESPONSE_SCHEMA_V2,
    }
    input_digest = _digest(
        {key: value for key, value in body.items() if key != "schema_version"},
        label="authorized review request input")
    identified = {**body, "request_input_digest": input_digest}
    request_id = f"REVIEWREQ2::{_digest(identified, label='authorized review request')}"
    return {**identified, "request_id": request_id, "status": AWAITING}


def _decode_json_escapes_for_validation(value: str) -> str:
    simple = {
        '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f",
        "n": "\n", "r": "\r", "t": "\t",
    }
    decoded = value
    for _ in range(MAX_JSON_ESCAPE_DECODE_PASSES):
        def replace(match):
            escape = match.group(0)[1:]
            if escape.startswith("u"):
                return chr(int(escape[1:], 16))
            return simple[escape]

        next_value = _JSON_COMPATIBLE_ESCAPE.sub(replace, decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if _JSON_COMPATIBLE_ESCAPE.search(decoded):
        raise ReviewQueueRejected("review response JSON转义解码超过4轮")
    return decoded


def _canonical_forbidden_key(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC", _decode_json_escapes_for_validation(value))
    return re.sub(r"[\s_-]+", "", normalized.casefold())


_FORBIDDEN_OUTPUT_KEY_TOKENS = frozenset(
    _canonical_forbidden_key(key) for key in _FORBIDDEN_OUTPUT_KEYS)


def _find_forbidden_key(value: Any, *, depth: int = 0,
                        json_layers: int = 0) -> str | None:
    if depth > 24:
        raise ReviewQueueRejected("review response嵌套深度超过24")
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _canonical_forbidden_key(key) in \
                    _FORBIDDEN_OUTPUT_KEY_TOKENS:
                return key
            found = _find_forbidden_key(
                item, depth=depth + 1, json_layers=json_layers)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_forbidden_key(
                item, depth=depth + 1, json_layers=json_layers)
            if found is not None:
                return found
    elif isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value)
        stripped = normalized.strip()
        is_container = (
            stripped.startswith("{") and stripped.endswith("}")) \
            or (stripped.startswith("[") and stripped.endswith("]"))
        is_json_string = stripped.startswith('"') and stripped.endswith('"')
        if is_container or is_json_string:
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            except RecursionError as exc:
                raise ReviewQueueRejected(
                    "review response JSON嵌套深度超过24") from exc
            if isinstance(decoded, (dict, list, str)):
                if json_layers >= MAX_JSON_DECODE_LAYERS:
                    raise ReviewQueueRejected(
                        "review response JSON解码层数超过16层")
                return _find_forbidden_key(
                    decoded, depth=depth + 1, json_layers=json_layers + 1)
    return None


class ReviewQueue:
    """同一Case库上的复核请求/响应队列；不含Provider调用。"""

    def __init__(self, store: CaseStore, blobs: BlobStore, journal: Journal):
        self.store = store
        self.blobs = blobs
        self.journal = journal

    materialize_authorization_from_v1 = None

    @staticmethod
    def _workflow_job_input_digest(job: dict) -> str:
        required = {
            "schema_version", "input_schema_version", "job_id", "input_digest",
            "state", "sources", "projections", "case_basis",
            "case_basis_digest", "case_basis_proof_digest", "evaluation_inputs",
            "evaluation_input_proof_bindings", "catalog_sha256", "catalog_digest",
            "proposal_request_input_digests", "proposal_request_ids",
            "created_at", "updated_at",
        }
        if not required <= set(job) or set(job) - (required | {"failure"}):
            raise ReviewQueueRejected("workflow job v2字段集合非法")
        if job.get("schema_version") != "kth-local.workflow-job.v2" \
                or job.get("input_schema_version") != "kth-local.workflow-job-input.v2":
            raise ReviewQueueRejected("workflow job不是可物化的v2候选job")
        body = {
            "schema_version": "kth-local.workflow-job-input.v2",
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
        digest = _digest(body, label="workflow job v2 input")
        if job.get("input_digest") != digest or job.get("job_id") != \
                f"JOB2::{digest}":
            raise ReviewQueueRejected("workflow job v2内容身份无法重建")
        return digest

    def _build_workflow_materialization(self, *, request: dict,
                                        response: dict,
                                        authorization: dict) -> dict:
        """仅以可信授权和已封存返回构建runner可消费的业务字段。"""
        if request.get("schema_version") != REQUEST_SCHEMA_V2 \
                or response.get("schema_version") != RESPONSE_SCHEMA_V2:
            raise ReviewQueueRejected("仅review request/response.v2可物化")
        if response.get("status") != "consumed":
            raise ReviewQueueRejected("专业返回必须先消费后物化")
        if response.get("request_id") != request.get("request_id") \
                or response.get("request_input_digest") != \
                request.get("request_input_digest"):
            raise ReviewQueueRejected("已消费专业返回与request身份不一致")
        if request.get("authorization_id") != authorization.get("authorization_id") \
                or request.get("authorization") != authorization:
            raise ReviewQueueRejected("v2 request与可信authorization不一致")
        job = self.store.get_workflow_job(request["job_id"])
        if job is None:
            raise ReviewQueueRejected("workflow job不存在")
        self._workflow_job_input_digest(job)
        if job.get("evaluation_inputs") != authorization.get("evaluation_inputs") \
                or job.get("evaluation_input_proof_bindings") != \
                authorization.get("evaluation_input_proof_bindings") \
                or job.get("case_basis_digest") != \
                authorization.get("case_basis_digest") \
                or job.get("case_basis_proof_digest") != \
                authorization.get("case_basis_proof_digest"):
            raise ReviewQueueRejected("workflow job与authorization冻结输入不一致")
        profile = authorization["evaluation_inputs"].get("profile")
        if not isinstance(profile, dict) or set(profile) != {
                "profile_id", "profile_digest"}:
            raise ReviewQueueRejected("authorization缺少精确aggregation profile")
        criterion = authorization.get("canonical_criterion")
        if response.get("evidence_class") != authorization.get("evidence_class") \
                or not isinstance(criterion, dict) \
                or response.get("evidence_class") not in \
                (criterion.get("eligible_evidence_classes") or []):
            raise ReviewQueueRejected("review response evidence_class不是canonical允许类别")
        decision = response.get("decision")
        if decision not in {"supports", "does_not_support"}:
            raise ReviewQueueRejected("review response decision不受支持")
        claim = authorization.get("qualification_input_view", {}).get("claim")
        if not isinstance(claim, dict) or claim.get("claim_id") != \
                authorization.get("claim_id"):
            raise ReviewQueueRejected("authorization资格视图缺少可信Claim")
        scope_id = authorization["evaluation_inputs"].get(
            "assessment_unit", {}).get("scope_id")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ReviewQueueRejected("authorization评估单元scope_id非法")
        support_scope = (
            f"仅支持{authorization['dimension_id']}/"
            f"{authorization['criterion_id']}的"
            f"{authorization['requested_use']}专业复核")
        body = {
            "schema_version": WORKFLOW_REVIEW_MATERIALIZATION_SCHEMA,
            "job_id": request["job_id"],
            "request_id": request["request_id"],
            "request_input_digest": request["request_input_digest"],
            "request_schema_version": request["schema_version"],
            "response_id": response["response_id"],
            "response_digest": response["response_digest"],
            "response_blob_sha256": response["response_blob_sha256"],
            "response_schema_version": response["schema_version"],
            "producer": copy.deepcopy(response["producer"]),
            "source_mode": response["source_mode"],
            "authorization_id": authorization["authorization_id"],
            "authorization_digest": _authorization_digest(authorization),
            "case_basis_version": authorization["case_basis_version"],
            "case_basis_digest": authorization["case_basis_digest"],
            "case_basis_proof_digest": authorization["case_basis_proof_digest"],
            "evaluation_inputs": copy.deepcopy(authorization["evaluation_inputs"]),
            "evaluation_input_proof_bindings": copy.deepcopy(
                authorization["evaluation_input_proof_bindings"]),
            "profile": copy.deepcopy(profile),
            "method_versions": copy.deepcopy(authorization["method_versions"]),
            "dimension_id": authorization["dimension_id"],
            "criterion_id": authorization["criterion_id"],
            "claim_id": authorization["claim_id"],
            "quote_sha256": authorization["quote_sha256"],
            "decision": decision,
            "evidence_class": authorization["evidence_class"],
            "findings": copy.deepcopy(response["findings"]),
            "subject_scope": claim.get("subject_scope"),
            "scope_id": scope_id,
            "support_scope": support_scope,
            "reviewer": response["producer"]["producer_id"],
            "review_basis": (
                "workflow_authorization_v1:"
                f"{authorization['authorization_id']}"),
        }
        digest = _digest(body, label="workflow review物化sidecar")
        return {
            **body,
            "materialization_digest": digest,
            "materialization_id": f"WFRMAT::{digest}",
            "review_id": f"DIMREVIEW::{digest}",
        }

    def validate_workflow_materialization_trusted(
            self, materialization: dict, *, review: dict | None = None) -> dict:
        """读时重建sidecar，验证其仍闭合于当前可信Case存储。"""
        try:
            materialization = validate_workflow_review_materialization(
                materialization, review_id=(review or materialization).get(
                    "review_id"))
        except ValueError as exc:
            raise ReviewQueueRejected(
                f"workflow review物化sidecar不可核验：{exc}") from exc
        request = self.get_request(materialization["request_id"])
        response = self.get_response(materialization["response_id"])
        authorization = self.validate_authorization_trusted(
            request.get("authorization"))
        expected = self._build_workflow_materialization(
            request=request, response=response, authorization=authorization)
        if expected != materialization:
            raise ReviewQueueRejected("workflow review物化sidecar与可信输入不一致")
        if review is not None:
            expected_review = {
                key: materialization[key] for key in (
                    "review_id", "dimension_id", "case_basis_version",
                    "claim_id", "criterion_id", "quote_sha256", "decision",
                    "evidence_class", "findings", "subject_scope", "scope_id",
                    "support_scope", "reviewer", "review_basis")}
            if any(review.get(key) != value
                   for key, value in expected_review.items()) \
                    or review.get("permission_mode") != \
                    "workflow_authorization_v1":
                raise ReviewQueueRejected(
                    "workflow review物化sidecar与维度review字段不一致")
        return materialization

    def materialize_consumed_response(self, response_id: str, *,
                                      worker_id: str) -> dict:
        """将已消费的 v2 返回原子写为维度review与内容寻址sidecar。"""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ReviewQueueRejected("物化worker_id不能为空")
        response = self.get_response(response_id)
        request = self.get_request(response["request_id"])
        if request.get("schema_version") != REQUEST_SCHEMA_V2 \
                or response.get("schema_version") != RESPONSE_SCHEMA_V2:
            raise ReviewQueueRejected(
                "legacy_restricted：仅已消费的review request/response.v2可物化")
        authorization = self.validate_authorization_trusted(
            request.get("authorization"))
        materialization = self._build_workflow_materialization(
            request=request, response=response, authorization=authorization)
        review_id = materialization["review_id"]
        task_key = f"workflow-review-materialize:{response_id}"
        input_id = materialization["materialization_digest"]
        self.journal.ensure_task(task_key, input_id)
        existing = self.store.get_dimension_evidence_review(review_id)
        if existing is not None:
            sidecar = self.store.get_workflow_review_materialization(review_id)
            if sidecar is None:
                raise ReviewQueueRejected("已有物化review缺少workflow sidecar")
            self.validate_workflow_materialization_trusted(
                sidecar, review=existing)
            try:
                self.journal.complete_existing_local_task(
                    task_key, input_id=input_id, output_ref=review_id,
                    worker_id=worker_id,
                    evidence="已存在物化review及sidecar逐字重建并闭包核验")
            except CommitRejected as exc:
                raise ReviewQueueRejected(f"workflow review物化恢复封账失败：{exc}") from exc
            return materialization
        try:
            claim = self.journal.claim(task_key, worker_id, input_id)
        except CommitRejected as exc:
            raise ReviewQueueRejected(f"workflow review物化认领失败：{exc}") from exc
        try:
            with self.store.immediate_transaction():
                response = self.get_response(response_id)
                request = self.get_request(response["request_id"])
                authorization = self.validate_authorization_trusted(
                    request.get("authorization"))
                materialization = self._build_workflow_materialization(
                    request=request, response=response,
                    authorization=authorization)
                if self.store.get_dimension_evidence_review(
                        materialization["review_id"]) is not None:
                    raise ReviewQueueRejected("物化review在发布事务内已存在")
                self.store.add_dimension_evidence_review(
                    materialization["review_id"],
                    dimension_id=materialization["dimension_id"],
                    case_basis_version=materialization["case_basis_version"],
                    claim_id=materialization["claim_id"],
                    criterion_id=materialization["criterion_id"],
                    quote_sha256=materialization["quote_sha256"],
                    decision=materialization["decision"],
                    evidence_class=materialization["evidence_class"],
                    findings=materialization["findings"],
                    subject_scope=materialization["subject_scope"],
                    scope_id=materialization["scope_id"],
                    support_scope=materialization["support_scope"],
                    reviewer=materialization["reviewer"],
                    review_basis=materialization["review_basis"],
                    workflow_materialization=materialization)
            self.journal.commit(claim, materialization["review_id"])
        except Exception as exc:
            try:
                self.journal.record_failure(claim, str(exc))
            except CommitRejected:
                pass
            if isinstance(exc, ReviewQueueRejected):
                raise
            raise ReviewQueueRejected(
                f"workflow review物化失败：{type(exc).__name__}: {exc}") from exc
        return materialization

    def validate_authorization(self, authorization: dict) -> dict:
        required = {
            "schema_version", "authorization_id", "claim_id", "source_id",
            "blob_sha256", "projection_id", "locator", "quote_sha256",
            "case_basis_version", "case_basis_digest",
            "case_basis_proof_digest", "qualification_id",
            "qualification_digest", "qualification_input_view_id",
            "qualification_input_view", "canonical_criterion",
            "catalog_sha256", "catalog_digest",
            "dimension_id", "criterion_id", "evidence_class",
            "requested_use", "allowed_uses", "evaluation_inputs",
            "evaluation_input_proof_bindings",
            "method_versions", "proposal_response_id", "proposal_source_mode",
            "proposal_producer", "purpose", "output_contract",
        }
        if not isinstance(authorization, dict) \
                or authorization.get("schema_version") != \
                "review_authorization.v1" or set(authorization) != required:
            raise ReviewQueueRejected("review authorization schema非法")
        if "result_id" in authorization:
            raise ReviewQueueRejected("review authorization不得绑定既有result_id")
        authorization_id = authorization.get("authorization_id")
        if authorization_id != f"REVAUTH::{_authorization_digest(authorization)}":
            raise ReviewQueueRejected("review authorization内容身份无法重建")
        view = authorization.get("qualification_input_view")
        if not isinstance(view, dict) \
                or authorization.get("qualification_input_view_id") != \
                f"QUALVIEW::{view.get('input_digest')}":
            raise ReviewQueueRejected("review authorization资格输入视图身份非法")
        view_body = {key: value for key, value in view.items()
                     if key != "input_digest"}
        view_digest = sha256_hex(json.dumps(
            view_body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if view.get("input_digest") != view_digest:
            raise ReviewQueueRejected("review authorization资格输入视图摘要不一致")
        stored_qualification = view.get("stored_qualification")
        if not isinstance(stored_qualification, dict) \
                or stored_qualification.get("qual_id") != \
                authorization.get("qualification_id"):
            raise ReviewQueueRejected("review authorization资格记录引用不一致")
        from .contracts import qualification_content_digest

        if qualification_content_digest(stored_qualification) != \
                authorization.get("qualification_digest"):
            raise ReviewQueueRejected("review authorization资格摘要不一致")
        if _digest((view.get("proof_bindings") or {}).get("case_basis"),
                   label="CaseBasis proofs") != \
                authorization.get("case_basis_proof_digest"):
            raise ReviewQueueRejected("review authorization证明摘要不一致")
        if authorization.get("requested_use") not in (
                authorization.get("allowed_uses") or []):
            raise ReviewQueueRejected("review authorization requested_use越权")
        criterion = authorization.get("canonical_criterion")
        if not isinstance(criterion, dict) \
                or criterion.get("criterion_id") != authorization.get("criterion_id") \
                or criterion.get("dimension") != authorization.get("dimension_id") \
                or authorization.get("evidence_class") not in (
                    criterion.get("eligible_evidence_classes") or []):
            raise ReviewQueueRejected("review authorization canonical准则/证据类不一致")
        if authorization.get("method_versions") != (
                authorization.get("evaluation_inputs") or {}).get(
                    "method_versions"):
            raise ReviewQueueRejected("review authorization方法版本冻结不一致")
        return copy.deepcopy(authorization)

    def build_authorized_request(self, *, job_id: str,
                                 authorization: dict) -> dict:
        authorization = self.validate_authorization(authorization)
        return build_authorized_request(
            job_id=job_id, authorization=authorization)

    def validate_authorization_trusted(self, authorization: dict) -> dict:
        """以当前Case存储和批准目录为信任根核验授权，不接受仅自洽正文。"""
        authorization = self.validate_authorization(authorization)
        stored = self.store.get_review_authorization(
            authorization["authorization_id"])
        if stored != authorization:
            raise ReviewQueueRejected("review authorization未在当前Case可信存储登记")
        claim = self.store.fetch_one(
            "claims", "claim_id", authorization["claim_id"])
        if claim is None or claim != (
                authorization["qualification_input_view"].get("claim")):
            raise ReviewQueueRejected("review authorization Claim存储闭包断裂")
        source = self.store.fetch_one(
            "sources", "source_id", authorization["source_id"])
        if source is None or source.get("blob_sha256") != \
                authorization["blob_sha256"]:
            raise ReviewQueueRejected("review authorization Source存储闭包断裂")
        projection = self.store.get_text_projection(
            authorization["projection_id"])
        if projection is None \
                or projection.get("source_id") != authorization["source_id"] \
                or projection.get("source_blob_sha256") != \
                authorization["blob_sha256"] \
                or projection.get("locator") != authorization["locator"] \
                or projection.get("text_sha256") != authorization["quote_sha256"]:
            raise ReviewQueueRejected("review authorization投影/引文存储闭包断裂")
        self.blobs.read_bytes(source["blob_sha256"])
        projection_text = self.blobs.read_bytes(
            projection["text_blob_sha256"])
        if sha256_hex(projection_text) != authorization["quote_sha256"]:
            raise ReviewQueueRejected("review authorization投影文本blob摘要不一致")
        qualification = self.store.fetch_one(
            "qualifications", "qual_id", authorization["qualification_id"])
        if qualification is None \
                or qualification != authorization["qualification_input_view"].get(
                    "stored_qualification"):
            raise ReviewQueueRejected("review authorization Qualification存储闭包断裂")
        view_row = self.store.get_qualification_input_view(
            authorization["qualification_input_view_id"])
        if view_row is None or view_row.get("view") != \
                authorization["qualification_input_view"]:
            raise ReviewQueueRejected("review authorization资格输入视图存储闭包断裂")
        basis = self.store.get_case_basis_version(
            authorization["case_basis_version"])
        if basis is None:
            raise ReviewQueueRejected("review authorization CaseBasis版本不存在")
        basis_body = {key: value for key, value in basis.items()
                      if key != "created_at"}
        if _digest(basis_body, label="CaseBasis") != \
                authorization["case_basis_digest"]:
            raise ReviewQueueRejected("review authorization CaseBasis摘要不一致")
        from .qualification import (
            resolve_case_basis_proof_bindings,
            verify_qualification_input_view,
        )

        proofs, proof_error = resolve_case_basis_proof_bindings(
            basis, self.store, self.blobs)
        if proof_error or proofs is None \
                or _digest(proofs, label="CaseBasis proofs") != \
                authorization["case_basis_proof_digest"]:
            raise ReviewQueueRejected("review authorization CaseBasis证明断裂")
        broken = verify_qualification_input_view(
            authorization["qualification_input_view"], basis,
            self.store, self.blobs)
        if broken:
            raise ReviewQueueRejected(
                f"review authorization资格输入视图读时核验失败：{broken}")
        from .proposal_requests import (
            _catalog_criterion,
            approved_catalog,
            validate_approved_catalog,
            validate_evaluation_input_bindings,
        )

        catalog = approved_catalog()
        catalog_digest = validate_approved_catalog(catalog)
        if authorization["catalog_digest"] != catalog_digest \
                or authorization["catalog_sha256"] != catalog["wheel_sha256"]:
            raise ReviewQueueRejected("review authorization批准catalog身份不一致")
        criterion = _catalog_criterion(
            catalog, authorization["dimension_id"],
            authorization["criterion_id"])
        if criterion != authorization["canonical_criterion"]:
            raise ReviewQueueRejected("review authorization canonical准则不是批准正文")
        proof_bindings = validate_evaluation_input_bindings(
            authorization["evaluation_inputs"], case=self.store,
            blobs=self.blobs, subject_scope=basis["subject_legal_name"])
        if proof_bindings != authorization["evaluation_input_proof_bindings"]:
            raise ReviewQueueRejected("review authorization评估输入证明绑定不一致")
        proposal = self.store.get_proposal_response(
            authorization["proposal_response_id"])
        if proposal is None \
                or proposal.get("source_mode") != authorization["proposal_source_mode"] \
                or proposal.get("producer") != authorization["proposal_producer"] \
                or authorization["claim_id"] not in {
                    item.get("claim_id") for item in proposal.get("candidates", [])}:
            raise ReviewQueueRejected("review authorization候选生产者存储闭包断裂")
        proposal_blob = self.blobs.read_bytes(proposal["response_blob_sha256"])
        if sha256_hex(proposal_blob) != proposal["response_digest"] \
                or proposal["response_id"] != \
                f"PROPOSALRESP::{proposal['response_digest']}":
            raise ReviewQueueRejected("review authorization候选响应blob身份断裂")
        return authorization

    def build_trusted_authorized_request(self, *, job_id: str,
                                         authorization: dict) -> dict:
        authorization = self.validate_authorization_trusted(authorization)
        job = self.store.get_workflow_job(job_id)
        if job is None or job.get("schema_version") != \
                "kth-local.workflow-job.v2" \
                or job.get("evaluation_inputs") != \
                authorization["evaluation_inputs"]:
            raise ReviewQueueRejected("review authorization与v2 job冻结输入不一致")
        return build_authorized_request(
            job_id=job_id, authorization=authorization)

    def validate_request(self, request: dict) -> dict:
        if request.get("schema_version") == REQUEST_SCHEMA_V2:
            if request.get("status") != AWAITING:
                raise ReviewQueueRejected("review request v2初始状态非法")
            authorization = self.validate_authorization(
                request.get("authorization"))
            expected = build_authorized_request(
                job_id=request.get("job_id"), authorization=authorization)
            if expected != request:
                raise ReviewQueueRejected("review request v2内容身份无法重建")
            return copy.deepcopy(request)
        if request.get("schema_version") != REQUEST_SCHEMA \
                or request.get("status") != AWAITING:
            raise ReviewQueueRejected("review request schema或初始状态非法")
        expected = build_request(
            job_id=request.get("job_id"),
            payload={key: value for key, value in request.items()
                     if key not in {"schema_version", "job_id", "request_id",
                                    "request_input_digest", "status"}},
        )
        if expected != request:
            raise ReviewQueueRejected("review request内容身份无法重建")
        return copy.deepcopy(request)

    def add_request(self, request: dict) -> dict:
        if request.get("schema_version") == REQUEST_SCHEMA:
            raise ReviewQueueRejected(
                "legacy_restricted：review_request.v1仅保留历史读取")
        request = self.validate_request(request)
        expected = self.build_trusted_authorized_request(
            job_id=request["job_id"],
            authorization=request["authorization"])
        if expected != request:
            raise ReviewQueueRejected("review request v2与可信授权/job不一致")
        return self.store.add_review_request(request)

    def get_request(self, request_id: str) -> dict:
        request = self.store.get_review_request(request_id)
        if request is None:
            raise ReviewQueueRejected(f"review request不存在：{request_id}")
        if request.get("schema_version") == REQUEST_SCHEMA_V2:
            expected = build_authorized_request(
                job_id=request.get("job_id"),
                authorization=self.validate_authorization(
                    request.get("authorization")))
        else:
            payload = {key: copy.deepcopy(value) for key, value in request.items()
                       if key not in {"schema_version", "job_id", "request_id",
                                      "request_input_digest", "status",
                                      "created_at", "updated_at"}}
            expected = build_request(job_id=request.get("job_id"), payload=payload)
        for field in expected:
            if field == "status":
                continue
            if request.get(field) != expected[field]:
                raise ReviewQueueRejected(
                    "review request读时身份无法重建或正文被改写")
        if request.get("schema_version") == REQUEST_SCHEMA_V2:
            trusted = self.build_trusted_authorized_request(
                job_id=request["job_id"], authorization=request["authorization"])
            for field, value in trusted.items():
                if field != "status" and request.get(field) != value:
                    raise ReviewQueueRejected(
                        "review request v2可信授权/job闭包不一致")
        return request

    def _refresh_job_state(self, job_id: str) -> None:
        try:
            self.store.refresh_workflow_job_state(job_id)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise ReviewQueueRejected(str(exc)) from exc

    def resume_failed_job(self, job_id: str) -> None:
        job = self.store.get_workflow_job(job_id)
        if job is None:
            raise ReviewQueueRejected(f"workflow job不存在：{job_id}")
        if job["state"] != "failed":
            raise ReviewQueueRejected("只有failed job需要显式恢复")
        try:
            self.store.refresh_workflow_job_state(
                job_id, resume_failed=True)
        except (KeyError, ValueError, RuntimeError) as exc:
            raise ReviewQueueRejected(str(exc)) from exc

    def seal_response(self, request_id: str, response: dict, *,
                      source_mode: str,
                      allow_simulated: bool = False) -> dict:
        return self._seal_response(
            request_id, response, source_mode=source_mode,
            allow_simulated=allow_simulated, allow_legacy_history=False)

    def _seal_response_v1_history_fixture(
            self, request_id: str, response: dict, *, source_mode: str,
            allow_simulated: bool = False) -> dict:
        """仅供历史迁移回归装载v1返回；产品入口不得调用。"""
        return self._seal_response(
            request_id, response, source_mode=source_mode,
            allow_simulated=allow_simulated, allow_legacy_history=True)

    def _seal_response(self, request_id: str, response: dict, *,
                       source_mode: str, allow_simulated: bool,
                       allow_legacy_history: bool) -> dict:
        if source_mode not in _ALLOWED_SOURCE_MODES:
            raise ReviewQueueRejected(f"非法source_mode：{source_mode}")
        if source_mode == "runtime_provider":
            raise ReviewQueueRejected("runtime_provider尚未授权，不能封存为已完成")
        if source_mode == "simulated" and not allow_simulated:
            raise ReviewQueueRejected("模拟返回必须由接口测试显式启用")
        request = self.get_request(request_id)
        if request.get("schema_version") == REQUEST_SCHEMA \
                and not allow_legacy_history:
            raise ReviewQueueRejected(
                "legacy_restricted：review_request.v1不得新增response")
        if not isinstance(response, dict):
            raise ReviewQueueRejected("review response必须是对象")
        _validate_json_limits(
            response, label="review response", max_bytes=MAX_RESPONSE_BYTES,
            max_depth=MAX_JSON_DEPTH)
        forbidden = _find_forbidden_key(response)
        if forbidden is not None:
            raise ReviewQueueRejected(f"review response含越权禁止字段：{forbidden}")
        required = {
            "schema_version", "request_id", "request_input_digest", "producer",
            "output_schema", "decision", "evidence_class", "findings",
            "citations",
        }
        expected_response_schema = (
            RESPONSE_SCHEMA_V2 if request.get("schema_version") == REQUEST_SCHEMA_V2
            else RESPONSE_SCHEMA)
        if set(response) != required \
                or response.get("schema_version") != expected_response_schema:
            raise ReviewQueueRejected("review response schema或字段集合非法")
        if response.get("request_id") != request_id \
                or response.get("request_input_digest") != \
                request["request_input_digest"]:
            raise ReviewQueueRejected("review response请求输入身份不一致")
        producer = response.get("producer")
        if not isinstance(producer, dict) or set(producer) != {
                "producer_id", "producer_kind"} \
                or any(not isinstance(value, str) or not value.strip()
                       for value in producer.values()):
            raise ReviewQueueRejected("review response producer非法")
        if producer["producer_kind"] not in _PRODUCER_KINDS_BY_MODE[source_mode]:
            raise ReviewQueueRejected(
                "review response source_mode与producer_kind未严格配对")
        if response.get("output_schema") != request["output_schema"]:
            raise ReviewQueueRejected("review response output schema不一致")
        if response.get("decision") not in {"supports", "does_not_support"} \
                or not isinstance(response.get("findings"), dict) \
                or not isinstance(response.get("evidence_class"), str) \
                or not response["evidence_class"].strip():
            raise ReviewQueueRejected("review response受控判断字段非法")
        _validate_json_limits(
            response["findings"], label="review response findings",
            max_bytes=MAX_FINDINGS_BYTES, max_depth=MAX_JSON_DEPTH,
            max_items=MAX_FINDINGS_ITEMS)
        citations = response.get("citations")
        if not isinstance(citations, list) or len(citations) > MAX_CITATIONS:
            raise ReviewQueueRejected(
                f"review response citations条目超过上限{MAX_CITATIONS}")
        _validate_json_limits(
            citations, label="review response citations",
            max_bytes=MAX_CITATIONS_BYTES, max_depth=MAX_JSON_DEPTH,
            max_items=MAX_CITATIONS * (len(_CITATION_FIELDS) + 1) + 1)
        citation_source = (request.get("authorization")
                           if request.get("schema_version") == REQUEST_SCHEMA_V2
                           else request)
        expected_citation = {
            key: copy.deepcopy(citation_source[key]) for key in _CITATION_FIELDS
        }
        if not citations \
                or any(not isinstance(item, dict)
                       or set(item) != _CITATION_FIELDS
                       or item != expected_citation for item in citations):
            raise ReviewQueueRejected("review response引用闭包不一致")
        normalized = {**copy.deepcopy(response), "source_mode": source_mode}
        response_digest = _digest(normalized, label="review response")
        response_id = f"REVIEWRESP::{response_digest}"
        existing = self.store.get_review_response_for_request(request_id)
        if existing is not None:
            if existing.get("response_id") == response_id \
                    and existing.get("response_digest") == response_digest:
                return self._validate_stored_response(existing)
            raise ReviewQueueRejected("同一request已封存不同response")
        if request["status"] != AWAITING:
            raise ReviewQueueRejected(
                f"review request状态{request['status']}不能接收返回")
        sealed_bytes = _canonical_bytes(normalized, label="review response")
        blob = self.blobs.put_bytes(sealed_bytes)
        item = {
            **normalized,
            "response_id": response_id,
            "response_digest": response_digest,
            "response_blob_sha256": blob.sha256,
            "status": "response_sealed",
        }
        stored = self.store.add_review_response(item)
        self._refresh_job_state(request["job_id"])
        return stored

    def _validate_stored_response(self, response: dict) -> dict:
        normalized = {key: copy.deepcopy(response.get(key)) for key in (
            "schema_version", "request_id", "request_input_digest", "producer",
            "output_schema", "decision", "evidence_class", "findings",
            "citations", "source_mode",
        )}
        if normalized["schema_version"] not in {RESPONSE_SCHEMA, RESPONSE_SCHEMA_V2}:
            raise ReviewQueueRejected("review response封存schema非法")
        digest = _digest(normalized, label="review response")
        if response.get("response_digest") != digest \
                or response.get("response_id") != f"REVIEWRESP::{digest}":
            raise ReviewQueueRejected("review response读时身份或正文被改写")
        blob_bytes = self.blobs.read_bytes(response["response_blob_sha256"])
        if blob_bytes != _canonical_bytes(normalized, label="review response"):
            raise ReviewQueueRejected("review response封存blob与数据库正文不一致")
        return response

    def get_response(self, response_id: str) -> dict:
        """按精确ID读取并核验封存返回；不提供latest语义。"""
        response = self.store.get_review_response(response_id)
        if response is None:
            raise ReviewQueueRejected(f"review response不存在：{response_id}")
        return self._validate_stored_response(response)

    def consume_response(self, response_id: str, *, worker_id: str) -> dict:
        return self._consume_response(
            response_id, worker_id=worker_id, allow_legacy_history=False)

    def _consume_response_v1_history_fixture(
            self, response_id: str, *, worker_id: str) -> dict:
        """仅供历史迁移回归推进v1状态；产品入口不得调用。"""
        return self._consume_response(
            response_id, worker_id=worker_id, allow_legacy_history=True)

    def _consume_response(self, response_id: str, *, worker_id: str,
                          allow_legacy_history: bool) -> dict:
        response = self.get_response(response_id)
        request = self.get_request(response["request_id"])
        if request.get("schema_version") == REQUEST_SCHEMA \
                and not allow_legacy_history:
            raise ReviewQueueRejected(
                "legacy_restricted：review_response.v1仅保留历史读取")
        if response["status"] == "consumed":
            return response
        if response["status"] != "response_sealed" \
                or request["status"] != "response_sealed":
            raise ReviewQueueRejected("review response尚未处于可消费封存态")
        sealed = self.blobs.read_bytes(response["response_blob_sha256"])
        if sha256_hex(sealed) != response["response_blob_sha256"]:
            raise ReviewQueueRejected("review response封存blob身份不一致")
        task_key = f"review-consume:{response_id}"
        input_id = response["response_digest"]
        self.journal.ensure_task(task_key, input_id)
        try:
            state = self.journal.task_state(task_key)
            if state["state"] == "succeeded":
                consumed = self.store.mark_review_response_consumed(response_id)
                self._refresh_job_state(request["job_id"])
                return consumed
            claim = self.journal.claim(task_key, worker_id, input_id)
            self.journal.commit(claim, response_id)
        except CommitRejected as exc:
            raise ReviewQueueRejected(f"review response消费认领失败：{exc}") from exc
        consumed = self.store.mark_review_response_consumed(response_id)
        self._refresh_job_state(request["job_id"])
        return consumed
