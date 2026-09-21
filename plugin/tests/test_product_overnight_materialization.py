"""E2：已消费 v2 专业复核返回必须物化为可追溯的维度复核。"""

from __future__ import annotations

import copy
import json

import pytest

from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_evaluation_method_contract,
    get_aggregation_profile,
)
from kth_hybrid.audit import trace_dimension_result
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.review_queue import (
    ReviewQueueRejected,
    _FORBIDDEN_OUTPUT_KEYS,
    build_request,
)
from kth_hybrid.runner import run_trl_dimension_slice
from kth_hybrid.workflow import LocalWorkflow, WorkflowRejected


SUBJECT = "武汉微玖光电科技有限公司"
UNIT = {
    "scope_id": "UNIT-WEIJU-E2",
    "subject_scope": SUBJECT,
    "unit_kind": "current_case_company_level",
    "unit_label": "微玖E2公司级评估单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-WEIJU-E2",
    "subject_scope": SUBJECT,
    "assessment_unit_refs": [UNIT["scope_id"]],
    "entity_ref": {"kind": "field_reference", "path": "case:financing.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:financing.json#/subject"},
    "assessment_units_ref": {"kind": "field_reference", "path": "case:financing.json#/units"},
}
FRL = {
    "external_financing_planned": False,
    "financing_entity_id": FINANCING["financing_entity_id"],
    "subject_scope": SUBJECT,
    "external_financing_planned_ref": {"kind": "field_reference", "path": "case:frl.json#/planned"},
    "financing_entity_ref": {"kind": "field_reference", "path": "case:frl.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:frl.json#/subject"},
}
METHODS = get_evaluation_method_contract(CURRENT_AGGREGATION_PROFILE_ID)


def _put_case_provenance(workflow: LocalWorkflow) -> None:
    documents = {
        "identity.json": {"subject": SUBJECT},
        "units.json": {"scope_id": UNIT["scope_id"], "subject": SUBJECT,
                       "kind": UNIT["unit_kind"], "label": UNIT["unit_label"]},
        "financing.json": {"entity": FINANCING["financing_entity_id"],
                           "subject": SUBJECT,
                           "units": FINANCING["assessment_unit_refs"]},
        "frl.json": {"planned": False, "entity": FINANCING["financing_entity_id"],
                     "subject": SUBJECT},
    }
    for name, body in documents.items():
        blob = workflow.blobs.put_bytes(json.dumps(body, ensure_ascii=False).encode())
        workflow.store.add_import_record("case_provenance", f"session:{name}", blob.sha256)


@pytest.fixture()
def workflow(tmp_path):
    item = LocalWorkflow(tmp_path / "case")
    _put_case_provenance(item)
    item.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=[],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis=json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    )
    yield item
    item.close()


def _evaluation_inputs() -> dict:
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    return {
        "assessment_unit": copy.deepcopy(UNIT),
        "financing_entity": copy.deepcopy(FINANCING),
        "frl_applicability": copy.deepcopy(FRL),
        "profile": {"profile_id": profile["profile_id"],
                    "profile_digest": profile["profile_digest"]},
        "method_versions": copy.deepcopy(METHODS),
    }


def _prepare_authorized_review(workflow: LocalWorkflow, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "qualified.docx"
    document = docx.Document()
    document.add_paragraph(
        f"{SUBJECT}已完成实验室组件集成测试，组件共同产生预期结果。")
    document.save(path)
    imported = workflow.import_attachments([path])[0]
    projections = workflow.project_sources([imported["source_id"]])
    projection = next(item for item in projections if item["status"] == "projected")
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE sources SET source_family='news-media', "
            "retrieved_at='2026-09-08T00:00:00Z', "
            "capture_status='raw_capture_validated' WHERE source_id=?",
            (imported["source_id"],),
        )
    catalog = build_catalog_from_wheel()
    job = workflow.create_candidate_job(
        evaluation_inputs=_evaluation_inputs(),
        proposal_specs=[{
            "source_id": imported["source_id"],
            "blob_sha256": imported["blob_sha256"],
            "projection_id": projection["projection_id"],
            "locator": projection["locator"],
            "quote": projection["text"],
            "purpose": "提出待资格核验的TRL候选",
            "output_schema": "proposal_response.v1",
        }],
        catalog=catalog,
    )
    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    candidate = {
        "quote": proposal["quote"],
        "quote_sha256": proposal["quote_sha256"],
        "locator": copy.deepcopy(proposal["locator"]),
        "interpretation": "已完成实验室组件集成测试。",
        "subject_scope": SUBJECT,
        "dimension_id": "TRL",
        "criterion_id": "TRL4-C1",
        "mapping": {
            "criterion_id": "TRL4-C1", "evidence_class": "test_record",
            "requested_use": "third_party_reported_fact",
        },
    }
    from kth_hybrid.proposal_requests import candidate_claim_id
    candidate["claim_id"] = candidate_claim_id(
        source_id=proposal["source_id"], candidate=candidate)
    sealed_proposal = workflow.proposals.seal_response(
        proposal["request_id"], {
            "schema_version": "proposal_response.v1",
            "request_id": proposal["request_id"],
            "request_input_digest": proposal["request_input_digest"],
            "producer": {"producer_id": "human-proposer-e2",
                         "producer_kind": "authorized_human"},
            "output_schema": proposal["output_schema"],
            "candidates": [candidate],
        }, source_mode="manual_import")
    consumed = workflow.proposals.consume_response(
        sealed_proposal["response_id"], worker_id="e2-proposal", catalog=catalog)
    request = workflow.reviews.get_request(consumed["materialization"]["review_request_ids"][0])
    authorization = request["authorization"]
    citation = {key: copy.deepcopy(authorization[key]) for key in (
        "source_id", "blob_sha256", "projection_id", "locator", "quote_sha256")}
    response = {
        "schema_version": "review_response.v2",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {"producer_id": "human-reviewer-e2",
                     "producer_kind": "authorized_human"},
        "output_schema": request["output_schema"],
        "decision": "supports",
        "evidence_class": "test_record",
        "findings": {
            "components_integrated_in_lab": True,
            "project_specific": True,
            "configuration_id": "CFG-E2-TRL4",
            "system_boundary": "实验室组件集成系统",
            "test_environment": "实验室",
            "test_method": "受控组件集成测试",
            "measured_results": "组件共同产生预期结果",
            "requirements_thresholds": "组件集成阈值已满足",
            "environment_kind": "laboratory",
        },
        "citations": [citation],
    }
    return catalog, job, request, response


def _create_awaiting_candidate_job(workflow: LocalWorkflow, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "awaiting-candidate.docx"
    document = docx.Document()
    document.add_paragraph(f"{SUBJECT}已完成实验室组件集成测试。")
    document.save(path)
    imported = workflow.import_attachments([path])[0]
    projection = next(item for item in workflow.project_sources(
        [imported["source_id"]]) if item["status"] == "projected")
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE sources SET source_family='news-media', "
            "retrieved_at='2026-09-08T00:00:00Z', "
            "capture_status='raw_capture_validated' WHERE source_id=?",
            (imported["source_id"],),
        )
    return workflow.create_candidate_job(
        evaluation_inputs=_evaluation_inputs(),
        proposal_specs=[{
            "source_id": imported["source_id"],
            "blob_sha256": imported["blob_sha256"],
            "projection_id": projection["projection_id"],
            "locator": projection["locator"],
            "quote": projection["text"],
            "purpose": "提出待资格核验的TRL候选",
            "output_schema": "proposal_response.v1",
        }],
        catalog=build_catalog_from_wheel(),
    )


def _seal_and_consume_review(workflow, request, response):
    sealed = workflow.reviews.seal_response(
        request["request_id"], response, source_mode="manual_import")
    return workflow.reviews.consume_response(
        sealed["response_id"], worker_id="e2-review")


def test_consumed_v2_response_materializes_review_for_real_trl_runner_and_trace(
        workflow, tmp_path):
    catalog, job, request, response = _prepare_authorized_review(workflow, tmp_path)
    consumed = _seal_and_consume_review(workflow, request, response)

    materialized = workflow.materialize_review_response(
        consumed["response_id"], worker_id="e2-materialize")

    review = workflow.store.get_dimension_evidence_review(materialized["review_id"])
    sidecar = workflow.store.get_workflow_review_materialization(review["review_id"])
    assert review["permission_mode"] == "workflow_authorization_v1"
    assert review["decision"] == "supports"
    assert review["evidence_class"] == "test_record"
    assert sidecar["response_id"] == consumed["response_id"]
    assert sidecar["authorization_id"] == request["authorization_id"]
    assert sidecar["job_id"] == job["job_id"]
    assert sidecar["producer"] == response["producer"]
    assert sidecar["source_mode"] == "manual_import"

    basis = workflow.store.get_case_basis_version(1)
    result = run_trl_dimension_slice(
        workflow.case_dir, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=UNIT)
    criterion = next(item for item in result["dimension"]["criteria"]
                     if item["criterion_id"] == "TRL4-C1")
    assert criterion["native_disposition"] == "met"
    assert trace_dimension_result(
        workflow.store, workflow.blobs, result["result_id"])["ok"] is True


def test_materialization_rejects_untrusted_authorization_and_noncanonical_evidence_class(
        workflow, tmp_path):
    _catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    response["evidence_class"] = "sales_contract"
    consumed = _seal_and_consume_review(workflow, request, response)
    with pytest.raises(WorkflowRejected, match="evidence_class|证据类|canonical"):
        workflow.materialize_review_response(
            consumed["response_id"], worker_id="e2-materialize")


def test_materialization_rejects_response_after_trusted_authorization_is_removed(
        workflow, tmp_path):
    _catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    consumed = _seal_and_consume_review(workflow, request, response)
    with workflow.store._conn:
        workflow.store._conn.execute(
            "DELETE FROM review_authorizations WHERE authorization_id=?",
            (request["authorization_id"],),
        )

    with pytest.raises(WorkflowRejected, match="authorization|授权|可信|存储"):
        workflow.materialize_review_response(
            consumed["response_id"], worker_id="e2-materialize")


def test_v2_response_rejects_nested_rule_result_fields_before_materialization(
        workflow, tmp_path):
    _catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    response["findings"]["result_id"] = "DIMR2::TRL::forged"
    response["findings"]["rule_version"] = "forged-rule-version"

    with pytest.raises(ReviewQueueRejected, match="禁止字段|越权"):
        workflow.reviews.seal_response(
            request["request_id"], response, source_mode="manual_import")


def _camel_key(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def _fullwidth_key(value: str) -> str:
    return "".join(
        chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char
        for char in _camel_key(value))


def _escaped_key(value: str) -> str:
    return "\\u" + f"{ord(value[0]):04x}" + value[1:]


def test_response_seal_rejects_all_forbidden_key_equivalents_in_nested_values(
        workflow, tmp_path):
    _catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    for forbidden in sorted(_FORBIDDEN_OUTPUT_KEYS):
        variants = {
            forbidden,
            _camel_key(forbidden),
            _camel_key(forbidden).title(),
            _fullwidth_key(forbidden),
            forbidden.replace("_", " - "),
            _escaped_key(forbidden),
        }
        for variant in variants:
            forged = copy.deepcopy(response)
            forged["findings"] = {"outer": [{variant: "forged-rule-result"}]}
            with pytest.raises(ReviewQueueRejected, match="禁止字段|越权"):
                workflow.reviews.seal_response(
                    request["request_id"], forged, source_mode="manual_import")

        json_key = _escaped_key(forbidden)
        forged_json = copy.deepcopy(response)
        forged_json["findings"] = {
            "serialized": "{\"nested\":[{\"" + json_key
            + "\":\"forged-rule-result\"}]}",
        }
        with pytest.raises(ReviewQueueRejected, match="禁止字段|越权"):
            workflow.reviews.seal_response(
                request["request_id"], forged_json, source_mode="manual_import")


def test_response_seal_allows_neutral_text_mentioning_forbidden_key_names(
        workflow, tmp_path):
    _catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    response["findings"] = {
        "discussion": (
            "专业复核只提交证据判断；attainedLevel、FinalDecision 和 "
            "native_disposition 仅作为禁止字段名称被说明。"),
        "metadata": {"note": "不对任何成熟度或投资结论赋值"},
    }

    sealed = workflow.reviews.seal_response(
        request["request_id"], response, source_mode="manual_import")

    assert sealed["status"] == "response_sealed"


@pytest.mark.parametrize("tamper", ["deleted", "rewritten"])
def test_deleted_or_rewritten_materialization_sidecar_breaks_runner_and_trace(
        workflow, tmp_path, tamper):
    catalog, _job, request, response = _prepare_authorized_review(workflow, tmp_path)
    consumed = _seal_and_consume_review(workflow, request, response)
    materialized = workflow.materialize_review_response(
        consumed["response_id"], worker_id="e2-materialize")
    basis = workflow.store.get_case_basis_version(1)
    result = run_trl_dimension_slice(
        workflow.case_dir, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=UNIT)
    with workflow.store._conn:
        if tamper == "deleted":
            workflow.store._conn.execute(
                "DELETE FROM workflow_review_materializations WHERE review_id=?",
                (materialized["review_id"],),
            )
        else:
            workflow.store._conn.execute(
                "UPDATE workflow_review_materializations SET body_json=? "
                "WHERE review_id=?",
                ("{}", materialized["review_id"]),
            )
    rerun = run_trl_dimension_slice(
        workflow.case_dir, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=UNIT)
    assert any(item["review_id"] == materialized["review_id"]
               for item in rerun["frozen_inputs"]["rejected_reviews"])
    assert trace_dimension_result(
        workflow.store, workflow.blobs, result["result_id"])["ok"] is False


def test_legacy_v1_review_response_cannot_enter_new_materialization_path(workflow):
    legacy_job = {
        "schema_version": "kth-local.workflow-job.v1",
        "job_id": "JOB::e2-legacy",
        "input_digest": "e2-legacy-input",
        "state": "awaiting_authorized_analysis",
    }
    payload = {
        "source_id": "SRC-E2-LEGACY", "blob_sha256": "2" * 64,
        "projection_id": "PROJ::e2-legacy", "locator": {"paragraph": 1},
        "quote_sha256": "3" * 64, "output_schema": "review_response.v1",
    }
    request = build_request(job_id=legacy_job["job_id"], payload=payload)
    workflow.store.add_workflow_bundle(legacy_job, [request])
    response = {
        "schema_version": "review_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {"producer_id": "legacy-human",
                     "producer_kind": "authorized_human"},
        "output_schema": "review_response.v1",
        "decision": "supports",
        "evidence_class": "test_record",
        "findings": {},
        "citations": [{key: copy.deepcopy(payload[key]) for key in (
            "source_id", "blob_sha256", "projection_id", "locator",
            "quote_sha256")}],
    }
    sealed = workflow.reviews._seal_response_v1_history_fixture(
        request["request_id"], response, source_mode="manual_import")
    consumed = workflow.reviews._consume_response_v1_history_fixture(
        sealed["response_id"], worker_id="legacy-history")

    with pytest.raises(WorkflowRejected, match="legacy_restricted"):
        workflow.materialize_review_response(
            consumed["response_id"], worker_id="e2-materialize")
    with pytest.raises(ReviewQueueRejected, match="legacy_restricted"):
        workflow.reviews.materialize_consumed_response(
            consumed["response_id"], worker_id="e2-materialize")


def test_resume_v2_candidate_waiting_state_returns_exact_status_without_review_queue(
        workflow, tmp_path):
    job = _create_awaiting_candidate_job(workflow, tmp_path)
    assert job["state"] == "awaiting_candidate_proposal"
    assert job["review_requests"] == []

    resumed = workflow.resume_failed_job(job["job_id"])

    assert resumed["state"] == "awaiting_candidate_proposal"
    assert resumed["review_requests"] == []
