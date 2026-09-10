"""E1：新材料候选提出、资格物化与首次专业复核授权。"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.proposal_requests import (
    ProposalQueueRejected,
    candidate_claim_id,
)
from kth_hybrid.review_queue import ReviewQueueRejected
from kth_hybrid.workflow import LocalWorkflow, WorkflowRejected


SUBJECT = "武汉微玖光电科技有限公司"
UNIT = {
    "scope_id": "UNIT-WEIJU-CURRENT",
    "subject_scope": SUBJECT,
    "unit_kind": "current_case_company_level",
    "unit_label": "微玖当前Case公司级候选单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-WEIJU",
    "subject_scope": SUBJECT,
    "assessment_unit_refs": [UNIT["scope_id"]],
    "entity_ref": {"kind": "field_reference", "path": "case:financing.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:financing.json#/subject"},
    "assessment_units_ref": {
        "kind": "field_reference", "path": "case:financing.json#/units"},
}
FRL_APPLICABILITY = {
    "external_financing_planned": False,
    "financing_entity_id": FINANCING["financing_entity_id"],
    "subject_scope": SUBJECT,
    "external_financing_planned_ref": {
        "kind": "field_reference", "path": "case:frl.json#/planned"},
    "financing_entity_ref": {
        "kind": "field_reference", "path": "case:frl.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:frl.json#/subject"},
}
METHODS = {
    "candidate_proposal": "kth-local.candidate-proposal.v1",
    "qualification": "kth-hybrid.qualification.v4",
    "professional_review": "kth-local.professional-review.v2",
}


@pytest.fixture(scope="module")
def catalog():
    return build_catalog_from_wheel()


@pytest.fixture()
def workflow(tmp_path):
    item = LocalWorkflow(tmp_path / "case")
    identity = {"subject": SUBJECT}
    identity_blob = item.blobs.put_bytes(json.dumps(
        identity, ensure_ascii=False).encode("utf-8"))
    item.store.add_import_record(
        "case_provenance", "session:identity.json", identity_blob.sha256)
    item.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=[],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis=json.dumps({
            "kind": "field_reference",
            "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    )
    yield item
    item.close()


def _source_projection(workflow, tmp_path, *, qualified=True):
    docx = pytest.importorskip("docx")
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / ("qualified.docx" if qualified else "unknown.docx")
    document = docx.Document()
    document.add_paragraph(
        f"{SUBJECT}已完成实验室组件集成测试，组件共同产生预期结果。")
    document.save(path)
    imported = workflow.import_attachments([path])[0]
    projection = next(row for row in workflow.project_sources(
        [imported["source_id"]]) if row["status"] == "projected")
    if qualified:
        with workflow.store._conn:
            workflow.store._conn.execute(
                "UPDATE sources SET source_family='news-media', "
                "retrieved_at='2026-09-08T00:00:00Z', "
                "capture_status='raw_capture_validated' WHERE source_id=?",
                (imported["source_id"],),
            )
    return imported, projection


def _evaluation_inputs():
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    return {
        "assessment_unit": copy.deepcopy(UNIT),
        "financing_entity": copy.deepcopy(FINANCING),
        "frl_applicability": copy.deepcopy(FRL_APPLICABILITY),
        "profile": {
            "profile_id": profile["profile_id"],
            "profile_digest": profile["profile_digest"],
        },
        "method_versions": copy.deepcopy(METHODS),
    }


def _proposal_spec(imported, projection):
    return {
        "source_id": imported["source_id"],
        "blob_sha256": imported["blob_sha256"],
        "projection_id": projection["projection_id"],
        "locator": projection["locator"],
        "quote": projection["text"],
        "purpose": "从精确文本投影提出待资格核验的准则候选",
        "output_schema": "proposal_response.v1",
    }


def _create_candidate_job(workflow, catalog, imported, projection):
    return workflow.create_candidate_job(
        evaluation_inputs=_evaluation_inputs(),
        proposal_specs=[_proposal_spec(imported, projection)],
        catalog=catalog,
    )


def _candidate(request, **changes):
    value = {
        "quote": request["quote"],
        "quote_sha256": request["quote_sha256"],
        "locator": copy.deepcopy(request["locator"]),
        "interpretation": "该主体已在实验室完成组件集成并取得预期结果。",
        "subject_scope": SUBJECT,
        "dimension_id": "TRL",
        "criterion_id": "TRL4-C1",
        "mapping": {
            "criterion_id": "TRL4-C1",
            "evidence_class": "test_record",
            "requested_use": "third_party_reported_fact",
        },
    }
    value.update(changes)
    value["claim_id"] = candidate_claim_id(
        source_id=request["source_id"], candidate=value)
    return value


def _response(request, **changes):
    value = {
        "schema_version": "proposal_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {
            "producer_id": "human-proposer-01",
            "producer_kind": "authorized_human",
        },
        "output_schema": request["output_schema"],
        "candidates": [_candidate(request)],
    }
    value.update(changes)
    return value


def test_v2_job_freezes_complete_inputs_and_waits_for_candidate_proposal(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])

    assert job["schema_version"] == "kth-local.workflow-job.v2"
    assert job["input_schema_version"] == "kth-local.workflow-job-input.v2"
    assert job["state"] == "awaiting_candidate_proposal"
    assert request["schema_version"] == "proposal_request.v1"
    assert request["status"] == "awaiting_candidate_proposal"
    assert request["source_id"] == imported["source_id"]
    assert request["blob_sha256"] == imported["blob_sha256"]
    assert request["projection_id"] == projection["projection_id"]
    assert request["locator"] == projection["locator"]
    assert request["quote_sha256"] == projection["text_sha256"]
    assert request["case_basis_version"] == 1
    assert request["case_basis_digest"] == job["case_basis_digest"]
    assert request["case_basis_proof_digest"]
    assert request["catalog_digest"]
    assert request["evaluation_inputs"] == _evaluation_inputs()
    assert request["purpose"]
    assert request["output_contract"]["forbidden_fields"]
    assert workflow.store.fetch_all("claims") == []
    assert workflow.store.fetch_all("qualifications") == []


def test_v2_job_identity_changes_with_complete_evaluation_input(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    first = _create_candidate_job(workflow, catalog, imported, projection)
    changed = _evaluation_inputs()
    changed["financing_entity"]["financing_entity_id"] = "FIN-OTHER"
    changed["frl_applicability"]["financing_entity_id"] = "FIN-OTHER"
    second = workflow.create_candidate_job(
        evaluation_inputs=changed,
        proposal_specs=[_proposal_spec(imported, projection)],
        catalog=catalog,
    )
    assert second["job_id"] != first["job_id"]


@pytest.mark.parametrize("field,value", [
    ("assessment_unit", {}),
    ("financing_entity", {"financing_entity_id": "FIN"}),
    ("frl_applicability", "unknown"),
    ("profile", {"profile_id": CURRENT_AGGREGATION_PROFILE_ID,
                 "profile_digest": "0" * 64}),
    ("method_versions", {}),
])
def test_v2_job_rejects_incomplete_or_unregistered_evaluation_inputs(
        workflow, tmp_path, catalog, field, value):
    imported, projection = _source_projection(workflow, tmp_path)
    inputs = _evaluation_inputs()
    inputs[field] = value
    with pytest.raises(WorkflowRejected):
        workflow.create_candidate_job(
            evaluation_inputs=inputs,
            proposal_specs=[_proposal_spec(imported, projection)],
            catalog=catalog,
        )


@pytest.mark.parametrize("tamper", [
    "assessment_unknown_field", "financing_wrong_unit", "frl_wrong_entity",
])
def test_v2_job_rejects_noncanonical_evaluation_input_relations(
        workflow, tmp_path, catalog, tamper):
    imported, projection = _source_projection(workflow, tmp_path)
    inputs = _evaluation_inputs()
    if tamper == "assessment_unknown_field":
        inputs["assessment_unit"]["caller_extension"] = True
    elif tamper == "financing_wrong_unit":
        inputs["financing_entity"]["assessment_unit_refs"] = ["UNIT-OTHER"]
    else:
        inputs["frl_applicability"]["financing_entity_id"] = "FIN-OTHER"
    with pytest.raises(WorkflowRejected):
        workflow.create_candidate_job(
            evaluation_inputs=inputs,
            proposal_specs=[_proposal_spec(imported, projection)],
            catalog=catalog,
        )


def test_proposal_response_rejects_runtime_and_requires_mode_producer_pair(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    with pytest.raises(ProposalQueueRejected, match="runtime|未授权"):
        workflow.proposals.seal_response(
            request["request_id"], _response(request), source_mode="runtime_provider")
    bad = _response(request)
    bad["producer"] = {"producer_id": "sim", "producer_kind": "simulated"}
    with pytest.raises(ProposalQueueRejected, match="producer|配对"):
        workflow.proposals.seal_response(
            request["request_id"], bad, source_mode="manual_import")


@pytest.mark.parametrize("forbidden", [
    {"level": 4},
    {"native_disposition": "met"},
    {"final_decision": "approved"},
])
def test_candidate_rejects_rule_or_final_output_fields(
        workflow, tmp_path, catalog, forbidden):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    candidate = _candidate(request)
    candidate.update(forbidden)
    response = _response(request, candidates=[candidate])
    with pytest.raises(ProposalQueueRejected, match="禁止|字段"):
        workflow.proposals.seal_response(
            request["request_id"], response, source_mode="manual_import")


@pytest.mark.parametrize("change", ["quote", "locator", "claim_id", "mapping"])
def test_candidate_must_bind_exact_quote_locator_identity_and_mapping(
        workflow, tmp_path, catalog, change):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    candidate = _candidate(request)
    if change == "quote":
        candidate["quote"] += "篡改"
    elif change == "locator":
        candidate["locator"] = {"paragraph": 99}
    elif change == "claim_id":
        candidate["claim_id"] = "CLAIM::" + "0" * 64
    else:
        candidate["mapping"]["criterion_id"] = "TRL5-C1"
    response = _response(request, candidates=[candidate])
    with pytest.raises(ProposalQueueRejected):
        workflow.proposals.seal_response(
            request["request_id"], response, source_mode="manual_import")


def test_candidate_response_has_bounded_resource_contract(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    response = _response(request)
    response["candidates"] = [copy.deepcopy(response["candidates"][0]) for _ in range(513)]
    with pytest.raises(ProposalQueueRejected, match="上限|条目"):
        workflow.proposals.seal_response(
            request["request_id"], response, source_mode="manual_import")


def test_candidate_response_rejects_duplicate_content_identity(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    candidate = _candidate(request)
    response = _response(
        request, candidates=[candidate, copy.deepcopy(candidate)])
    with pytest.raises(ProposalQueueRejected, match="重复|claim_id"):
        workflow.proposals.seal_response(
            request["request_id"], response, source_mode="manual_import")


def test_seal_then_consume_materializes_real_qualification_and_v2_authorization(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    sealed = workflow.proposals.seal_response(
        request["request_id"], _response(request), source_mode="manual_import")

    assert sealed["status"] == "proposal_response_sealed"
    assert workflow.store.fetch_all("claims") == []
    consumed = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="candidate-consumer", catalog=catalog)
    assert consumed["status"] == "consumed"
    assert consumed["source_mode"] == "manual_import"
    assert len(consumed["materialization"]["claim_ids"]) == 1
    assert len(consumed["materialization"]["qualification_view_ids"]) == 1
    assert len(consumed["materialization"]["authorization_ids"]) == 1
    assert len(consumed["materialization"]["review_request_ids"]) == 1

    claim_id = consumed["materialization"]["claim_ids"][0]
    qualification = workflow.store.fetch_one(
        "qualifications", "claim_id", claim_id)
    assert qualification["status"] == "qualified"
    assert "third_party_reported_fact" in json.loads(qualification["allowed_uses"])
    view = workflow.store.get_qualification_input_view(
        consumed["materialization"]["qualification_view_ids"][0])
    assert view["view"]["claim"]["claim_id"] == claim_id
    assert view["view"]["proof_errors"] == []
    assert view["view"]["stored_qualification"]["qual_id"] == \
        qualification["qual_id"]
    assert view["view"]["stored_qualification"]["status"] == "qualified"

    authorization = workflow.store.get_review_authorization(
        consumed["materialization"]["authorization_ids"][0])
    assert authorization["schema_version"] == "review_authorization.v1"
    assert "result_id" not in authorization
    assert authorization["qualification_input_view"] == view["view"]
    assert authorization["requested_use"] == "third_party_reported_fact"
    assert authorization["requested_use"] in authorization["allowed_uses"]
    assert authorization["canonical_criterion"]["criterion_id"] == "TRL4-C1"
    assert authorization["canonical_criterion"]["dimension"] == "TRL"
    assert authorization["evidence_class"] == "test_record"
    assert authorization["evaluation_inputs"] == _evaluation_inputs()
    assert authorization["proposal_source_mode"] == "manual_import"
    assert authorization["proposal_producer"] == {
        "producer_id": "human-proposer-01",
        "producer_kind": "authorized_human",
    }
    assert authorization["proposal_response_id"] == sealed["response_id"]
    review = workflow.reviews.get_request(
        consumed["materialization"]["review_request_ids"][0])
    assert review["schema_version"] == "review_request.v2"
    assert review["authorization_id"] == authorization["authorization_id"]
    assert review["authorization"] == authorization
    assert review["status"] == "awaiting_authorized_analysis"


def test_unqualified_candidate_persists_honest_status_without_review_request(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path, qualified=False)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    sealed = workflow.proposals.seal_response(
        request["request_id"], _response(request), source_mode="manual_import")
    consumed = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="candidate-consumer", catalog=catalog)

    assert consumed["materialization"]["review_request_ids"] == []
    assert consumed["materialization"]["authorization_ids"] == []
    assert consumed["materialization"]["candidate_statuses"][0]["status"] in {
        "needs_review", "rejected", "gap"}
    assert workflow.status(job["job_id"])["state"] == "insufficient"


def test_requested_use_and_canonical_evidence_class_are_revalidated_at_consume(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    candidate = _candidate(request)
    candidate["mapping"]["requested_use"] = "company_self_statement"
    candidate["claim_id"] = candidate_claim_id(
        source_id=request["source_id"], candidate=candidate)
    sealed = workflow.proposals.seal_response(
        request["request_id"], _response(request, candidates=[candidate]),
        source_mode="manual_import")
    with pytest.raises(ProposalQueueRejected, match="allowed_uses|用途"):
        workflow.proposals.consume_response(
            sealed["response_id"], worker_id="candidate-consumer", catalog=catalog)

    other_root = tmp_path / "other"
    with LocalWorkflow(other_root) as other:
        identity_blob = other.blobs.put_bytes(json.dumps(
            {"subject": SUBJECT}, ensure_ascii=False).encode())
        other.store.add_import_record(
            "case_provenance", "session:identity.json", identity_blob.sha256)
        other.initialize_case(
            subject_legal_name=SUBJECT, subject_aliases=[],
            evidence_cutoff="2026-09-09T00:00:00Z",
            subject_source_basis=json.dumps({
                "kind": "field_reference", "path": "case:identity.json#/subject",
                "status": "claimed"}),
        )
        imported2, projection2 = _source_projection(other, tmp_path / "other-files")
        job2 = _create_candidate_job(other, catalog, imported2, projection2)
        request2 = other.proposals.get_request(job2["proposal_request_ids"][0])
        candidate2 = _candidate(request2)
        candidate2["mapping"]["evidence_class"] = "sales_contract"
        candidate2["claim_id"] = candidate_claim_id(
            source_id=request2["source_id"], candidate=candidate2)
        sealed2 = other.proposals.seal_response(
            request2["request_id"], _response(request2, candidates=[candidate2]),
            source_mode="manual_import")
        with pytest.raises(ProposalQueueRejected, match="evidence_class|证据类"):
            other.proposals.consume_response(
                sealed2["response_id"], worker_id="consumer", catalog=catalog)


def test_consume_is_idempotent_and_authorization_identity_covers_full_body(
        workflow, tmp_path, catalog):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    sealed = workflow.proposals.seal_response(
        request["request_id"], _response(request), source_mode="manual_import")
    first = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="consumer", catalog=catalog)
    second = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="consumer", catalog=catalog)
    assert second["materialization"] == first["materialization"]
    assert len(workflow.store.fetch_all("claims")) == 1
    authorization = workflow.store.get_review_authorization(
        first["materialization"]["authorization_ids"][0])
    changed = copy.deepcopy(authorization)
    changed["purpose"] += "篡改"
    with pytest.raises((ReviewQueueRejected, ValueError), match="身份|摘要|authorization"):
        workflow.reviews.validate_authorization(changed)

    forged = copy.deepcopy(authorization)
    forged["qualification_input_view"]["claim"]["interpretation"] = "伪造解释"
    auth_body = {key: value for key, value in forged.items()
                 if key != "authorization_id"}
    auth_digest = hashlib.sha256(json.dumps(
        auth_body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    forged["authorization_id"] = f"REVAUTH::{auth_digest}"
    with pytest.raises(ReviewQueueRejected, match="资格输入视图|摘要"):
        workflow.reviews.validate_authorization(forged)


def test_candidate_materialization_rolls_back_as_one_transaction(
        workflow, tmp_path, catalog, monkeypatch):
    imported, projection = _source_projection(workflow, tmp_path)
    job = _create_candidate_job(workflow, catalog, imported, projection)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    sealed = workflow.proposals.seal_response(
        request["request_id"], _response(request), source_mode="manual_import")

    def fail_request(*, job_id, authorization):
        raise RuntimeError("注入：专业请求落库前中断")

    monkeypatch.setattr(workflow.reviews, "build_authorized_request", fail_request)
    with pytest.raises(RuntimeError, match="注入"):
        workflow.proposals.consume_response(
            sealed["response_id"], worker_id="consumer", catalog=catalog)
    assert workflow.store.fetch_all("claims") == []
    assert workflow.store.fetch_all("qualifications") == []
    assert workflow.proposals.get_response(sealed["response_id"])["status"] == \
        "proposal_response_sealed"


def test_legacy_v1_request_is_readable_but_cannot_materialize_authorization(
        workflow, tmp_path):
    imported, projection = _source_projection(workflow, tmp_path)
    # v1仍由既有测试覆盖创建与响应；这里锁定它没有新授权物化入口。
    assert workflow.reviews.materialize_authorization_from_v1 is None
    assert imported["source_id"] == projection["source_id"]
