"""受控专业复核队列：请求冻结、返回封存与独立消费。"""

from __future__ import annotations

import copy
import json
from typing import Any

from .contracts import sha256_hex
from .evidence_permissions import validate_evidence_use_license
from .journal import CommitRejected, Journal
from .store import BlobStore, CaseStore


REQUEST_SCHEMA = "review_request.v1"
RESPONSE_SCHEMA = "review_response.v1"
AWAITING = "awaiting_authorized_analysis"
_ALLOWED_SOURCE_MODES = {"manual_import", "simulated", "runtime_provider"}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_FINDINGS_BYTES = 256 * 1024
MAX_FINDINGS_ITEMS = 512
MAX_CITATIONS = 64
MAX_CITATIONS_BYTES = 256 * 1024
MAX_JSON_DEPTH = 24
_PRODUCER_KINDS_BY_MODE = {
    "manual_import": {"authorized_human", "human", "manual_import"},
    "simulated": {"simulated", "simulated_test"},
    "runtime_provider": {"runtime_provider"},
}
_FORBIDDEN_OUTPUT_KEYS = {
    "level", "maturity_level", "native_disposition", "native_note",
    "product_status", "final_decision", "investment_recommendation",
    "investment_decision", "approved", "go_no_go",
}
_CITATION_FIELDS = {
    "source_id", "blob_sha256", "projection_id", "locator", "quote_sha256",
}


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


def _find_forbidden_key(value: Any, *, depth: int = 0) -> str | None:
    if depth > 24:
        raise ReviewQueueRejected("review response嵌套深度超过24")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in _FORBIDDEN_OUTPUT_KEYS:
                return str(key)
            found = _find_forbidden_key(item, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_forbidden_key(item, depth=depth + 1)
            if found is not None:
                return found
    return None


class ReviewQueue:
    """同一Case库上的复核请求/响应队列；不含Provider调用。"""

    def __init__(self, store: CaseStore, blobs: BlobStore, journal: Journal):
        self.store = store
        self.blobs = blobs
        self.journal = journal

    def validate_request(self, request: dict) -> dict:
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
        request = self.validate_request(request)
        return self.store.add_review_request(request)

    def get_request(self, request_id: str) -> dict:
        request = self.store.get_review_request(request_id)
        if request is None:
            raise ReviewQueueRejected(f"review request不存在：{request_id}")
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
        if source_mode not in _ALLOWED_SOURCE_MODES:
            raise ReviewQueueRejected(f"非法source_mode：{source_mode}")
        if source_mode == "runtime_provider":
            raise ReviewQueueRejected("runtime_provider尚未授权，不能封存为已完成")
        if source_mode == "simulated" and not allow_simulated:
            raise ReviewQueueRejected("模拟返回必须由接口测试显式启用")
        request = self.get_request(request_id)
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
        if set(response) != required or response.get("schema_version") != RESPONSE_SCHEMA:
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
        expected_citation = {
            key: copy.deepcopy(request[key]) for key in _CITATION_FIELDS
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
        if normalized["schema_version"] != RESPONSE_SCHEMA:
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
        response = self.get_response(response_id)
        request = self.get_request(response["request_id"])
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
