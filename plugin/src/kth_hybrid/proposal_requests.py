"""新材料候选提出队列与首次专业复核授权。"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from functools import lru_cache
from typing import Any

from .aggregation_profiles import (
    get_aggregation_profile,
    get_evaluation_method_contract,
)
from .catalog import (
    APPROVED_WHEEL_SHA256,
    build_catalog_from_wheel,
    flatten_criteria,
)
from .contracts import (
    claim_content_digest,
    qualification_content_digest,
    sha256_hex,
)
from .journal import CommitRejected, Journal
from .qualification import (
    GapOutcome,
    QualificationOutcome,
    build_qualification_input_view,
    resolve_case_basis_proof_bindings,
)
from .store import BlobStore, CaseStore


REQUEST_SCHEMA = "proposal_request.v1"
RESPONSE_SCHEMA = "proposal_response.v1"
AUTHORIZATION_SCHEMA = "review_authorization.v1"
AWAITING = "awaiting_candidate_proposal"
MAX_CANDIDATES = 512
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 24
_SOURCE_PRODUCERS = {
    "manual_import": {"authorized_human", "human", "manual_import"},
    "simulated": {"simulated", "simulated_test"},
}
_FORBIDDEN_FIELDS = {
    "level", "maturity_level", "native_disposition", "native_note",
    "final_decision", "investment_recommendation", "investment_decision",
    "approved", "go_no_go", "result_id",
}
_CANDIDATE_FIELDS = {
    "claim_id", "quote", "quote_sha256", "locator", "interpretation",
    "subject_scope", "dimension_id", "criterion_id", "mapping",
}
_MAPPING_FIELDS = {"criterion_id", "evidence_class", "requested_use"}


class ProposalQueueRejected(RuntimeError):
    """候选请求、返回、资格或授权关系不满足合同。"""


def _canonical_bytes(value: Any, *, label: str) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProposalQueueRejected(f"{label}不是规范JSON：{exc}") from exc


def _digest(value: Any, *, label: str) -> str:
    return sha256_hex(_canonical_bytes(value, label=label))


def _catalog_body(catalog: dict) -> dict:
    return {key: copy.deepcopy(catalog.get(key)) for key in (
        "schema_version", "wheel_sha256", "dimensions", "totals")}


@lru_cache(maxsize=1)
def _approved_catalog_value() -> dict:
    return build_catalog_from_wheel()


def approved_catalog() -> dict:
    return copy.deepcopy(_approved_catalog_value())


@lru_cache(maxsize=1)
def _approved_catalog_digest() -> str:
    return _digest(_catalog_body(_approved_catalog_value()),
                   label="approved catalog")


def validate_approved_catalog(catalog: dict) -> str:
    if not isinstance(catalog, dict) \
            or catalog.get("schema_version") != "kth-hybrid.catalog.v1" \
            or catalog.get("wheel_sha256") != APPROVED_WHEEL_SHA256:
        raise ProposalQueueRejected("catalog不是批准wheel目录")
    digest = _digest(_catalog_body(catalog), label="catalog")
    if digest != _approved_catalog_digest():
        raise ProposalQueueRejected("catalog正文与批准机械目录不一致")
    return digest


def _validate_limits(value: Any, *, label: str, max_bytes: int) -> None:
    stack = [(value, 1, frozenset())]
    while stack:
        item, depth, ancestors = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise ProposalQueueRejected(f"{label}深度超过上限{MAX_JSON_DEPTH}")
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in ancestors:
                raise ProposalQueueRejected(f"{label}包含循环引用")
            nested = ancestors | {identity}
            values = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1, nested) for child in values)
    if len(_canonical_bytes(value, label=label)) > max_bytes:
        raise ProposalQueueRejected(f"{label}字节超过上限{max_bytes}")


def _require_nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProposalQueueRejected(f"{label}不能为空")
    return value


def validate_evaluation_inputs(value: dict) -> dict:
    """核验 v2 job 冻结的六维完整输入，不解析调用方自报 profile。"""
    required = {
        "assessment_unit", "financing_entity", "frl_applicability",
        "profile", "method_versions",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ProposalQueueRejected("evaluation_inputs字段集合不完整")
    unit = value["assessment_unit"]
    unit_required = {
        "scope_id", "subject_scope", "unit_kind", "unit_label",
        "scope_id_ref", "subject_ref", "unit_kind_ref", "unit_label_ref",
    }
    if not isinstance(unit, dict) or set(unit) != unit_required:
        raise ProposalQueueRejected("assessment_unit缺少四项字段引用")
    entity = value["financing_entity"]
    entity_required = {
        "financing_entity_id", "subject_scope", "assessment_unit_refs",
        "entity_ref", "subject_ref", "assessment_units_ref",
    }
    if not isinstance(entity, dict) or set(entity) != entity_required:
        raise ProposalQueueRejected("financing_entity缺少完整引用")
    if entity.get("subject_scope") != unit.get("subject_scope") \
            or entity.get("assessment_unit_refs") != [unit.get("scope_id")]:
        raise ProposalQueueRejected("financing_entity与assessment_unit关系不一致")
    applicability = value["frl_applicability"]
    if applicability is not None and not isinstance(applicability, dict):
        raise ProposalQueueRejected("frl_applicability必须是对象或null")
    if applicability is not None:
        applicability_required = {
            "external_financing_planned", "financing_entity_id",
            "subject_scope", "external_financing_planned_ref",
            "financing_entity_ref", "subject_ref",
        }
        if set(applicability) != applicability_required \
                or not isinstance(
                    applicability.get("external_financing_planned"), bool) \
                or applicability.get("financing_entity_id") != \
                entity.get("financing_entity_id") \
                or applicability.get("subject_scope") != unit.get("subject_scope"):
            raise ProposalQueueRejected("frl_applicability关系或字段集合非法")
    profile_ref = value["profile"]
    if not isinstance(profile_ref, dict) or set(profile_ref) != {
            "profile_id", "profile_digest"}:
        raise ProposalQueueRejected("profile引用非法")
    try:
        profile = get_aggregation_profile(profile_ref.get("profile_id"))
    except (TypeError, ValueError) as exc:
        raise ProposalQueueRejected(f"profile未登记：{exc}") from exc
    if profile_ref != {key: profile[key] for key in (
            "profile_id", "profile_digest")}:
        raise ProposalQueueRejected("profile摘要与登记正文不一致")
    methods = value["method_versions"]
    expected_methods = get_evaluation_method_contract(profile["profile_id"])
    if methods != expected_methods:
        raise ProposalQueueRejected(
            "method_versions必须逐字等于登记的完整方法合同"
            "（资格、六维规则/结果schema、catalog与profile）")
    _validate_limits(value, label="evaluation_inputs", max_bytes=512 * 1024)
    return copy.deepcopy(value)


def validate_evaluation_input_bindings(value: dict, *, case: CaseStore,
                                       blobs: BlobStore,
                                       subject_scope: str) -> dict:
    """用既有六维字段解析器核验所有评估输入引用及同记录关系。"""
    value = validate_evaluation_inputs(value)
    from .runner import (
        _resolve_assessment_unit,
        _resolve_financing_entity,
        _resolve_frl_applicability,
    )

    try:
        unit = _resolve_assessment_unit(
            case, blobs, value["assessment_unit"], subject_scope)
        entity = _resolve_financing_entity(
            case, blobs, value["financing_entity"], subject_scope)
        applicability, applicability_error = _resolve_frl_applicability(
            case, blobs, value["frl_applicability"],
            scope=subject_scope,
            financing_entity_id=entity["financing_entity_id"])
    except ValueError as exc:
        raise ProposalQueueRejected(
            f"evaluation_inputs字段引用不可核验：{exc}") from exc
    if applicability_error:
        raise ProposalQueueRejected(
            f"evaluation_inputs FRL引用不可核验：{applicability_error}")
    return {
        "assessment_unit": unit["proof_bindings"],
        "financing_entity": entity["proof_bindings"],
        "frl_applicability": (
            applicability["proof_bindings"] if applicability else None),
    }


def build_proposal_request(*, job_id: str, payload: dict) -> dict:
    body = {
        "schema_version": REQUEST_SCHEMA,
        "job_id": _require_nonempty_string(job_id, label="job_id"),
        **copy.deepcopy(payload),
    }
    input_body = {key: value for key, value in body.items()
                  if key not in {"schema_version", "job_id"}}
    input_digest = _digest(input_body, label="proposal request input")
    identified = {**body, "request_input_digest": input_digest}
    request_id = f"PROPOSALREQ::{_digest(identified, label='proposal request')}"
    return {**identified, "request_id": request_id, "status": AWAITING}


def candidate_claim_id(*, source_id: str, candidate: dict) -> str:
    body = {key: copy.deepcopy(value) for key, value in candidate.items()
            if key != "claim_id"}
    return f"CLAIM::{_digest({'source_id': source_id, **body}, label='claim candidate')}"


def _find_forbidden(value: Any, *, depth: int = 0) -> str | None:
    if depth > MAX_JSON_DEPTH:
        raise ProposalQueueRejected("候选响应嵌套深度超过上限")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in _FORBIDDEN_FIELDS:
                return str(key)
            found = _find_forbidden(item, depth=depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_forbidden(item, depth=depth + 1)
            if found:
                return found
    return None


def _catalog_criterion(catalog: dict, dimension_id: str,
                       criterion_id: str) -> dict:
    if catalog.get("schema_version") != "kth-hybrid.catalog.v1":
        raise ProposalQueueRejected("catalog schema非法")
    matches = [row for row in flatten_criteria(catalog)
               if row.get("dimension") == dimension_id
               and row.get("criterion_id") == criterion_id]
    if len(matches) != 1:
        raise ProposalQueueRejected("candidate criterion不在批准catalog精确路径中")
    return matches[0]


def _claim_from_candidate(request: dict, candidate: dict,
                          source: dict, *, proposal_response_id: str,
                          proposal_created_at: str | None = None) -> dict:
    media_type = source.get("media_type")
    locator = candidate["locator"]
    if media_type == "application/pdf":
        locator_kind = "pdf_page"
        locator_ref = copy.deepcopy(locator)
    elif media_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        locator_kind = "zip_member"
        locator_ref = {"member": "word/document.xml", **copy.deepcopy(locator)}
    else:
        raise ProposalQueueRejected("候选原件媒体类型尚不支持受控定位物化")
    row = {
        "claim_id": candidate["claim_id"],
        "source_id": request["source_id"],
        "locator_kind": locator_kind,
        "locator_start": 0,
        "locator_end": 0,
        "locator_ref": json.dumps(locator_ref, ensure_ascii=False, sort_keys=True),
        "excerpt_sha256": candidate["quote_sha256"],
        "excerpt_text": candidate["quote"],
        "interpretation": candidate["interpretation"],
        "subject_scope": candidate["subject_scope"],
        "interpretation_attempt": proposal_response_id,
    }
    digest = claim_content_digest(row)
    row["input_digest"] = digest
    row["content_digest"] = digest
    if proposal_created_at is not None:
        row["created_at"] = proposal_created_at
    return row


def _qualification_row(outcome: QualificationOutcome, *, created_at: str) -> dict:
    return {
        "qual_id": f"QUALR::{outcome.claim_id}",
        "claim_id": outcome.claim_id,
        "source_judgment": outcome.source_judgment.basis,
        "identity_judgment": outcome.identity_judgment.basis,
        "time_judgment": outcome.time_judgment.basis,
        "independence_judgment": outcome.independence_judgment.basis,
        "allowed_uses": json.dumps(
            outcome.allowed_uses, ensure_ascii=False),
        "cannot_prove": json.dumps(
            outcome.cannot_prove, ensure_ascii=False),
        "review_attempt": outcome.review_attempt,
        "status": outcome.status,
        "created_at": created_at,
    }


def _authorization(*, request: dict, candidate: dict, criterion: dict,
                   qualification: dict, view: dict,
                   case_basis_proof_digest: str, proposal_response: dict) -> dict:
    mapping = candidate["mapping"]
    body = {
        "schema_version": AUTHORIZATION_SCHEMA,
        "claim_id": candidate["claim_id"],
        "source_id": request["source_id"],
        "blob_sha256": request["blob_sha256"],
        "projection_id": request["projection_id"],
        "locator": copy.deepcopy(candidate["locator"]),
        "quote_sha256": candidate["quote_sha256"],
        "case_basis_version": request["case_basis_version"],
        "case_basis_digest": request["case_basis_digest"],
        "case_basis_proof_digest": case_basis_proof_digest,
        "qualification_id": qualification["qual_id"],
        "qualification_digest": qualification_content_digest(qualification),
        "qualification_input_view_id": (
            f"QUALVIEW::{view['input_digest']}"),
        "qualification_input_view": copy.deepcopy(view),
        "canonical_criterion": copy.deepcopy(criterion),
        "catalog_sha256": request["catalog_sha256"],
        "catalog_digest": request["catalog_digest"],
        "dimension_id": candidate["dimension_id"],
        "criterion_id": candidate["criterion_id"],
        "evidence_class": mapping["evidence_class"],
        "requested_use": mapping["requested_use"],
        "allowed_uses": json.loads(qualification["allowed_uses"]),
        "evaluation_inputs": copy.deepcopy(request["evaluation_inputs"]),
        "evaluation_input_proof_bindings": copy.deepcopy(
            request["evaluation_input_proof_bindings"]),
        "method_versions": copy.deepcopy(
            request["evaluation_inputs"]["method_versions"]),
        "proposal_response_id": proposal_response["response_id"],
        "proposal_source_mode": proposal_response["source_mode"],
        "proposal_producer": copy.deepcopy(proposal_response["producer"]),
        "purpose": "对已资格化Claim执行指定准则的专业证据复核",
        "output_contract": {
            "schema_version": "review_response.v2",
            "forbidden_fields": sorted(_FORBIDDEN_FIELDS),
        },
    }
    digest = _digest(body, label="review authorization")
    return {**body, "authorization_id": f"REVAUTH::{digest}"}


class ProposalQueue:
    """候选提出队列；不执行 Provider，也不调用六维内核。"""

    def __init__(self, store: CaseStore, blobs: BlobStore, journal: Journal,
                 reviews):
        self.store = store
        self.blobs = blobs
        self.journal = journal
        self.reviews = reviews

    def validate_request(self, request: dict) -> dict:
        if request.get("schema_version") != REQUEST_SCHEMA \
                or request.get("status") != AWAITING:
            raise ProposalQueueRejected("proposal request schema或状态非法")
        payload = {key: copy.deepcopy(value) for key, value in request.items()
                   if key not in {"schema_version", "job_id", "request_id",
                                  "request_input_digest", "status"}}
        if build_proposal_request(job_id=request.get("job_id"), payload=payload) != request:
            raise ProposalQueueRejected("proposal request内容身份无法重建")
        return copy.deepcopy(request)

    def get_request(self, request_id: str) -> dict:
        request = self.store.get_proposal_request(request_id)
        if request is None:
            raise ProposalQueueRejected(f"proposal request不存在：{request_id}")
        payload = {key: copy.deepcopy(value) for key, value in request.items()
                   if key not in {"schema_version", "job_id", "request_id",
                                  "request_input_digest", "status",
                                  "created_at", "updated_at"}}
        expected = build_proposal_request(job_id=request.get("job_id"), payload=payload)
        for key, value in expected.items():
            if key != "status" and request.get(key) != value:
                raise ProposalQueueRejected("proposal request读时身份无法重建")
        return request

    def _validate_candidate(self, request: dict, candidate: dict) -> dict:
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
            raise ProposalQueueRejected("candidate字段集合非法或含禁止字段")
        if _find_forbidden(candidate):
            raise ProposalQueueRejected("candidate含禁止规则或决定字段")
        for key in ("quote", "quote_sha256", "interpretation", "subject_scope",
                    "dimension_id", "criterion_id", "claim_id"):
            _require_nonempty_string(candidate.get(key), label=f"candidate {key}")
        if candidate["quote"] != request["quote"] \
                or candidate["quote_sha256"] != request["quote_sha256"] \
                or candidate["locator"] != request["locator"]:
            raise ProposalQueueRejected("candidate quote/locator与请求不精确一致")
        mapping = candidate.get("mapping")
        if not isinstance(mapping, dict) or set(mapping) != _MAPPING_FIELDS \
                or mapping.get("criterion_id") != candidate["criterion_id"]:
            raise ProposalQueueRejected("candidate mapping与criterion不一致")
        if candidate["claim_id"] != candidate_claim_id(
                source_id=request["source_id"], candidate=candidate):
            raise ProposalQueueRejected("candidate claim_id内容身份无法重建")
        return copy.deepcopy(candidate)

    def seal_response(self, request_id: str, response: dict, *, source_mode: str,
                      allow_simulated: bool = False) -> dict:
        if source_mode == "runtime_provider":
            raise ProposalQueueRejected("runtime_provider尚未授权")
        if source_mode not in _SOURCE_PRODUCERS:
            raise ProposalQueueRejected("proposal response source_mode非法")
        if source_mode == "simulated" and not allow_simulated:
            raise ProposalQueueRejected("模拟候选必须显式启用")
        request = self.get_request(request_id)
        if not isinstance(response, dict):
            raise ProposalQueueRejected("proposal response必须是对象")
        _validate_limits(response, label="proposal response", max_bytes=MAX_RESPONSE_BYTES)
        if _find_forbidden(response):
            raise ProposalQueueRejected("proposal response含禁止输出字段")
        required = {
            "schema_version", "request_id", "request_input_digest", "producer",
            "output_schema", "candidates",
        }
        if set(response) != required or response.get("schema_version") != RESPONSE_SCHEMA:
            raise ProposalQueueRejected("proposal response schema或字段集合非法")
        if response.get("request_id") != request_id \
                or response.get("request_input_digest") != request["request_input_digest"] \
                or response.get("output_schema") != request["output_schema"]:
            raise ProposalQueueRejected("proposal response请求身份或输出合同不一致")
        producer = response.get("producer")
        if not isinstance(producer, dict) or set(producer) != {
                "producer_id", "producer_kind"}:
            raise ProposalQueueRejected("proposal response producer非法")
        if producer.get("producer_kind") not in _SOURCE_PRODUCERS[source_mode]:
            raise ProposalQueueRejected("proposal response source_mode与producer未配对")
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates \
                or len(candidates) > MAX_CANDIDATES:
            raise ProposalQueueRejected(f"candidates条目超过上限{MAX_CANDIDATES}或为空")
        normalized_candidates = [self._validate_candidate(request, item)
                                 for item in candidates]
        claim_ids = [item["claim_id"] for item in normalized_candidates]
        if len(set(claim_ids)) != len(claim_ids):
            raise ProposalQueueRejected("candidates含重复claim_id内容身份")
        normalized = {
            **copy.deepcopy(response),
            "candidates": normalized_candidates,
            "source_mode": source_mode,
        }
        digest = _digest(normalized, label="proposal response")
        response_id = f"PROPOSALRESP::{digest}"
        existing = self.store.get_proposal_response_for_request(request_id)
        if existing is not None:
            if existing.get("response_id") == response_id:
                return self._validate_stored_response(existing)
            raise ProposalQueueRejected("同一proposal request已封存不同返回")
        blob = self.blobs.put_bytes(_canonical_bytes(
            normalized, label="proposal response"))
        return self.store.add_proposal_response({
            **normalized,
            "response_id": response_id,
            "response_digest": digest,
            "response_blob_sha256": blob.sha256,
            "status": "proposal_response_sealed",
        })

    def _validate_stored_response(self, response: dict) -> dict:
        normalized = {key: copy.deepcopy(response.get(key)) for key in (
            "schema_version", "request_id", "request_input_digest", "producer",
            "output_schema", "candidates", "source_mode",
        )}
        digest = _digest(normalized, label="proposal response")
        if response.get("response_digest") != digest \
                or response.get("response_id") != f"PROPOSALRESP::{digest}":
            raise ProposalQueueRejected("proposal response读时身份被改写")
        if self.blobs.read_bytes(response["response_blob_sha256"]) != \
                _canonical_bytes(normalized, label="proposal response"):
            raise ProposalQueueRejected("proposal response封存blob不一致")
        return response

    def get_response(self, response_id: str) -> dict:
        response = self.store.get_proposal_response(response_id)
        if response is None:
            raise ProposalQueueRejected(f"proposal response不存在：{response_id}")
        return self._validate_stored_response(response)

    def _verify_materialized_response(self, response: dict,
                                      catalog: dict) -> None:
        materialization = response.get("materialization")
        required = {
            "claim_ids", "qualification_view_ids", "authorization_ids",
            "review_request_ids", "gap_ids", "candidate_statuses",
        }
        if not isinstance(materialization, dict) \
                or set(materialization) != required:
            raise ProposalQueueRejected("已消费候选缺少完整物化清单")
        validate_approved_catalog(catalog)
        for claim_id in materialization["claim_ids"]:
            if self.store.fetch_one("claims", "claim_id", claim_id) is None:
                raise ProposalQueueRejected("候选物化Claim引用断裂")
        for view_id in materialization["qualification_view_ids"]:
            if self.store.get_qualification_input_view(view_id) is None:
                raise ProposalQueueRejected("候选物化资格视图引用断裂")
        for authorization_id in materialization["authorization_ids"]:
            authorization = self.store.get_review_authorization(authorization_id)
            if authorization is None:
                raise ProposalQueueRejected("候选物化authorization授权引用断裂")
            self.reviews.validate_authorization_trusted(authorization)
        for request_id in materialization["review_request_ids"]:
            request = self.reviews.get_request(request_id)
            if request.get("authorization_id") not in \
                    materialization["authorization_ids"]:
                raise ProposalQueueRejected("候选物化专业请求与授权闭包不一致")

    def consume_response(self, response_id: str, *, worker_id: str,
                         catalog: dict) -> dict:
        response = self.get_response(response_id)
        task_key = f"proposal-consume:{response_id}"
        input_id = response["response_digest"]
        self.journal.ensure_task(task_key, input_id)
        if response["status"] == "consumed":
            self._verify_materialized_response(response, catalog)
            try:
                self.journal.complete_existing_local_task(
                    task_key, input_id=input_id, output_ref=response_id,
                    worker_id=worker_id,
                    evidence="候选物化清单已从同库逐字核验")
            except CommitRejected as exc:
                raise ProposalQueueRejected(f"候选消费恢复封账失败：{exc}") from exc
            return response
        if response["status"] != "proposal_response_sealed":
            raise ProposalQueueRejected("proposal response尚未封存")
        state = self.journal.task_state(task_key)
        if state["state"] != "planned":
            raise ProposalQueueRejected(
                f"候选消费任务认领被拒：当前状态{state['state']}，"
                "claimed/dispatch_recorded/outcome_unknown均不得自动接管")
        try:
            consume_claim = self.journal.claim(task_key, worker_id, input_id)
        except CommitRejected as exc:
            raise ProposalQueueRejected(f"候选消费任务认领失败：{exc}") from exc
        try:
            request = self.get_request(response["request_id"])
            catalog_digest = validate_approved_catalog(catalog)
            if catalog_digest != request.get("catalog_digest"):
                raise ProposalQueueRejected("候选消费catalog摘要与请求不一致")
            source = self.store.fetch_one(
                "sources", "source_id", request["source_id"])
            if source is None or source.get("blob_sha256") != request["blob_sha256"]:
                raise ProposalQueueRejected("候选消费时source/blob绑定断裂")
            basis = self.store.get_case_basis_version(request["case_basis_version"])
            if basis is None:
                raise ProposalQueueRejected("候选消费时CaseBasis版本不存在")
            basis_body = {key: value for key, value in basis.items()
                          if key != "created_at"}
            if _digest(basis_body, label="CaseBasis") != request["case_basis_digest"]:
                raise ProposalQueueRejected("候选消费时CaseBasis摘要变化")
            proof_bindings, proof_error = resolve_case_basis_proof_bindings(
                basis, self.store, self.blobs)
            if proof_error or proof_bindings is None:
                raise ProposalQueueRejected(f"CaseBasis证明不可核验：{proof_error}")
            proof_digest = _digest(proof_bindings, label="CaseBasis proofs")
            if proof_digest != request["case_basis_proof_digest"]:
                raise ProposalQueueRejected("CaseBasis证明摘要与请求不一致")
            evaluation_proofs = validate_evaluation_input_bindings(
                request["evaluation_inputs"], case=self.store,
                blobs=self.blobs, subject_scope=basis["subject_legal_name"])
            if evaluation_proofs != request["evaluation_input_proof_bindings"]:
                raise ProposalQueueRejected("评估输入字段证明与请求冻结值不一致")
            plans = []
            statuses = []
            for candidate in response["candidates"]:
                candidate = self._validate_candidate(request, candidate)
                criterion = _catalog_criterion(
                    catalog, candidate["dimension_id"], candidate["criterion_id"])
                evidence_class = candidate["mapping"]["evidence_class"]
                if evidence_class not in criterion.get("eligible_evidence_classes", []):
                    raise ProposalQueueRejected(
                        "candidate evidence_class不在canonical证据类中")
                claim = _claim_from_candidate(
                    request, candidate, source,
                    proposal_response_id=response["response_id"],
                    proposal_created_at=response["created_at"])
                existing_claim = self.store.fetch_one(
                    "claims", "claim_id", claim["claim_id"])
                if existing_claim is not None:
                    if claim_content_digest(existing_claim) != \
                            claim_content_digest(claim):
                        raise ProposalQueueRejected(
                            "既有Claim内容与候选身份冲突")
                    claim = existing_claim
                same_body = sum(1 for row in self.store.fetch_all("sources")
                                if row["blob_sha256"] == source["blob_sha256"])
                _preview, outcome, proof_errors = build_qualification_input_view(
                    claim, source, None, basis, self.store, self.blobs,
                    same_body_sources=same_body,
                    review_attempt=response["response_id"],
                    case_basis_proofs=proof_bindings,
                )
                if isinstance(outcome, GapOutcome):
                    status = "gap"
                    qualification = None
                    authorization = None
                    view = _preview
                    gap = {
                        "gap_id": f"GAPR::{outcome.claim_id}",
                        "gap_type": outcome.gap_type,
                        "affected_criteria": outcome.affected_criteria or [
                            candidate["criterion_id"]],
                        "pipeline_fault": outcome.pipeline_fault,
                        "investigation": outcome.investigation,
                        "unconfirmed": outcome.unconfirmed,
                    }
                else:
                    status = outcome.status
                    gap = None
                    planned_qualification = _qualification_row(
                        outcome, created_at=response["created_at"])
                    existing_qualification = self.store.fetch_one(
                        "qualifications", "qual_id",
                        planned_qualification["qual_id"])
                    if existing_qualification is not None:
                        if qualification_content_digest(existing_qualification) != \
                                qualification_content_digest(planned_qualification) \
                                or existing_qualification["status"] != \
                                planned_qualification["status"] \
                                or existing_qualification["allowed_uses"] != \
                                planned_qualification["allowed_uses"]:
                            raise ProposalQueueRejected(
                                "既有Qualification内容、状态或allowed_uses冲突")
                        qualification = existing_qualification
                    else:
                        qualification = planned_qualification
                    view, rebuilt_outcome, proof_errors = \
                        build_qualification_input_view(
                            claim, source, qualification, basis, self.store,
                            self.blobs, same_body_sources=same_body,
                            review_attempt=response["response_id"],
                            case_basis_proofs=proof_bindings)
                    if asdict(rebuilt_outcome) != asdict(outcome):
                        raise ProposalQueueRejected("资格预判与冻结重建不一致")
                    if outcome.status == "qualified":
                        requested_use = candidate["mapping"]["requested_use"]
                        if requested_use not in outcome.allowed_uses:
                            raise ProposalQueueRejected(
                                "requested_use不在实际资格allowed_uses中")
                        if proof_errors:
                            raise ProposalQueueRejected("qualified资格证明闭包仍有错误")
                        authorization = _authorization(
                            request=request, candidate=candidate,
                            criterion=criterion, qualification=qualification,
                            view=view, case_basis_proof_digest=proof_digest,
                            proposal_response=response)
                        self.reviews.validate_authorization(authorization)
                    else:
                        authorization = None
                view_id = f"QUALVIEW::{view['input_digest']}"
                plans.append({
                    "claim": claim,
                    "qualification": qualification,
                    "gap": gap,
                    "qualification_view_id": view_id,
                    "qualification_view": view,
                    "authorization": authorization,
                })
                statuses.append({"claim_id": claim["claim_id"], "status": status})
            materialization = self.store.materialize_candidate_response_atomic(
                response_id=response_id, request_id=request["request_id"],
                job_id=request["job_id"], plans=plans,
                candidate_statuses=statuses,
                review_request_builder=self.reviews.build_trusted_authorized_request,
            )
        except Exception as exc:
            try:
                self.journal.record_failure(consume_claim, str(exc))
            except CommitRejected:
                pass
            raise
        try:
            self.journal.commit(consume_claim, response_id)
        except CommitRejected as exc:
            raise ProposalQueueRejected(f"候选消费封账失败：{exc}") from exc
        stored = self.get_response(response_id)
        if stored.get("materialization") != materialization:
            raise ProposalQueueRejected("候选消费物化摘要不一致")
        return stored
