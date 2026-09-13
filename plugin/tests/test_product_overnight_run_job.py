"""E3：工作流以冻结 v2 输入驱动真实六维运行，不从 latest 拼接结果。"""

from __future__ import annotations

import copy
import json

import pytest

from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.audit import trace_crl_dimension, trace_dimension_result
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.proposal_requests import candidate_claim_id
from kth_hybrid.workflow import LocalWorkflow, LocalWorkflowCrash, WorkflowRejected


SUBJECT = "武汉微玖光电科技有限公司"
UNIT = {
    "scope_id": "UNIT-WEIJU-E3",
    "subject_scope": SUBJECT,
    "unit_kind": "current_case_company_level",
    "unit_label": "微玖E3公司级评估单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-WEIJU-E3",
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


def _put_provenance(workflow: LocalWorkflow) -> None:
    documents = {
        "identity.json": {"subject": SUBJECT},
        "units.json": {"scope_id": UNIT["scope_id"], "subject": SUBJECT,
                       "kind": UNIT["unit_kind"], "label": UNIT["unit_label"]},
        "financing.json": {"entity": FINANCING["financing_entity_id"],
                           "subject": SUBJECT, "units": FINANCING["assessment_unit_refs"]},
        "frl.json": {"planned": False, "entity": FINANCING["financing_entity_id"],
                     "subject": SUBJECT},
    }
    for name, body in documents.items():
        blob = workflow.blobs.put_bytes(json.dumps(body, ensure_ascii=False).encode())
        workflow.store.add_import_record("case_provenance", f"session:{name}", blob.sha256)


@pytest.fixture()
def workflow(tmp_path):
    value = LocalWorkflow(tmp_path / "case")
    _put_provenance(value)
    value.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=[],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis=json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    )
    yield value
    value.close()


def _evaluation_inputs() -> dict:
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    return {
        "assessment_unit": copy.deepcopy(UNIT),
        "financing_entity": copy.deepcopy(FINANCING),
        "frl_applicability": copy.deepcopy(FRL),
        "profile": {"profile_id": profile["profile_id"],
                    "profile_digest": profile["profile_digest"]},
        "method_versions": {
            "contract_schema": "kth-local.evaluation-method-contract.v1",
            "candidate_proposal": "kth-local.candidate-proposal.v1",
            "qualification": "kth-hybrid.qualification.v4",
            "professional_review": "kth-local.professional-review.v2",
            "catalog_sha256": profile["catalog_sha256"],
            "profile_id": profile["profile_id"],
            "profile_digest": profile["profile_digest"],
            **{
                f"{dimension.lower()}_rule_version": values["rule_version"]
                for dimension, values in profile["dimensions"].items()
            },
            **{
                f"{dimension.lower()}_result_schema_version":
                    values["result_schema_version"]
                for dimension, values in profile["dimensions"].items()
            },
        },
    }


def _candidate_job(workflow: LocalWorkflow, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "qualified.docx"
    document = docx.Document()
    document.add_paragraph(f"{SUBJECT}已完成实验室组件集成测试，组件共同产生预期结果。")
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


def _consume_full_trl_path(workflow: LocalWorkflow, job: dict) -> None:
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
    candidate["claim_id"] = candidate_claim_id(
        source_id=proposal["source_id"], candidate=candidate)
    sealed = workflow.proposals.seal_response(
        proposal["request_id"], {
            "schema_version": "proposal_response.v1",
            "request_id": proposal["request_id"],
            "request_input_digest": proposal["request_input_digest"],
            "producer": {"producer_id": "human-proposer-e3",
                         "producer_kind": "authorized_human"},
            "output_schema": proposal["output_schema"],
            "candidates": [candidate],
        }, source_mode="manual_import")
    consumed = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="e3-proposal",
        catalog=build_catalog_from_wheel())
    request = workflow.reviews.get_request(
        consumed["materialization"]["review_request_ids"][0])
    authorization = request["authorization"]
    citation = {key: copy.deepcopy(authorization[key]) for key in (
        "source_id", "blob_sha256", "projection_id", "locator", "quote_sha256")}
    sealed_review = workflow.reviews.seal_response(
        request["request_id"], {
            "schema_version": "review_response.v2",
            "request_id": request["request_id"],
            "request_input_digest": request["request_input_digest"],
            "producer": {"producer_id": "human-reviewer-e3",
                         "producer_kind": "authorized_human"},
            "output_schema": request["output_schema"],
            "decision": "supports",
            "evidence_class": "test_record",
            "findings": {
                "components_integrated_in_lab": True,
                "project_specific": True,
                "configuration_id": "CFG-E3-TRL4",
                "system_boundary": "实验室组件集成系统",
                "test_environment": "实验室",
                "test_method": "受控组件集成测试",
                "measured_results": "组件共同产生预期结果",
                "requirements_thresholds": "组件集成阈值已满足",
                "environment_kind": "laboratory",
            },
            "citations": [citation],
        }, source_mode="manual_import")
    workflow.reviews.consume_response(
        sealed_review["response_id"], worker_id="e3-review")


def test_run_job_keeps_unconsumed_candidate_awaiting_without_dimension_outputs(
        workflow, tmp_path):
    job = _candidate_job(workflow, tmp_path)

    status = workflow.run_job(job["job_id"])

    assert status["state"] == "awaiting_candidate_proposal"
    assert workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"]) == []
    assert workflow.store.get_workflow_job_artifacts(job["job_id"]) is None


def test_run_job_materializes_consumed_review_then_freezes_exact_six_dimension_outputs(
        workflow, tmp_path):
    job = _candidate_job(workflow, tmp_path)
    _consume_full_trl_path(workflow, job)

    status = workflow.run_job(job["job_id"])

    assert status["state"] == "completed"
    outputs = workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"])
    assert {row["dimension_id"] for row in outputs} == {
        "CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
    trl = next(row for row in outputs if row["dimension_id"] == "TRL")
    assert trace_dimension_result(workflow.store, workflow.blobs, trl["result_id"])["ok"] is True
    crl = next(row for row in outputs if row["dimension_id"] == "CRL")
    assert trace_crl_dimension(workflow.store, workflow.blobs, crl["result_id"])["ok"] is True
    artifacts = workflow.store.get_workflow_job_artifacts(job["job_id"])
    assert artifacts["manifest_id"].startswith("AGGMAN::")
    assert artifacts["view_id"].startswith("OFFLINE6::")
    assert artifacts["source_modes"] == ["manual_import"]
    assert status["dimension_outputs"] == outputs
    assert status["artifacts"] == artifacts


def test_run_job_reuses_registered_dimension_after_controlled_crash(workflow, tmp_path):
    job = _candidate_job(workflow, tmp_path)
    _consume_full_trl_path(workflow, job)

    with pytest.raises(LocalWorkflowCrash, match="CRL维度登记后"):
        workflow.run_job(job["job_id"], crash_after_dimension="CRL")
    first = workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"])
    assert [row["dimension_id"] for row in first] == ["CRL"]

    resumed = workflow.run_job(job["job_id"])

    assert resumed["state"] == "completed"
    assert next(row for row in resumed["dimension_outputs"]
                if row["dimension_id"] == "CRL")["result_id"] == first[0]["result_id"]


@pytest.mark.parametrize("tamper", [
    "qualification", "rule_version", "result_schema", "catalog", "missing_dimension",
])
def test_create_candidate_job_rejects_unregistered_method_contract(
        workflow, tmp_path, tamper):
    job = _candidate_job(workflow, tmp_path)
    request = workflow.proposals.get_request(job["proposal_request_ids"][0])
    inputs = _evaluation_inputs()
    methods = inputs["method_versions"]
    if tamper == "qualification":
        methods["qualification"] = "forged.qualification.v99"
    elif tamper == "rule_version":
        methods["trl_rule_version"] = "forged.trl.v99"
    elif tamper == "result_schema":
        methods["crl_result_schema_version"] = "forged.schema.v99"
    elif tamper == "catalog":
        methods["catalog_sha256"] = "0" * 64
    else:
        del methods["frl_rule_version"]

    with pytest.raises(WorkflowRejected, match="method_versions|方法合同|资格|profile"):
        workflow.create_candidate_job(
            evaluation_inputs=inputs,
            proposal_specs=[{
                "source_id": request["source_id"],
                "blob_sha256": request["blob_sha256"],
                "projection_id": request["projection_id"],
                "locator": request["locator"],
                "quote": request["quote"],
                "purpose": "伪造方法合同反例",
                "output_schema": "proposal_response.v1",
            }],
            catalog=build_catalog_from_wheel(),
        )


def test_run_job_rejects_and_marks_failed_when_frozen_method_contract_is_forged(
        workflow, tmp_path):
    job = _candidate_job(workflow, tmp_path)
    _consume_full_trl_path(workflow, job)
    with workflow.store._conn:
        row = workflow.store._conn.execute(
            "SELECT body_json FROM workflow_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        body = json.loads(row["body_json"])
        body["evaluation_inputs"]["method_versions"]["qualification"] = \
            "forged.qualification.v99"
        workflow.store._conn.execute(
            "UPDATE workflow_jobs SET body_json=? WHERE job_id=?",
            (json.dumps(body, ensure_ascii=False, sort_keys=True), job["job_id"]),
        )

    with pytest.raises(WorkflowRejected, match="method_versions|方法合同|资格"):
        workflow.run_job(job["job_id"])

    assert workflow.store.get_workflow_job(job["job_id"])["state"] == "failed"


def test_run_job_rejects_runtime_runner_version_drift_before_trl_execution(
        workflow, tmp_path, monkeypatch):
    job = _candidate_job(workflow, tmp_path)
    _consume_full_trl_path(workflow, job)
    import kth_hybrid.kernels.trl as trl_kernel

    monkeypatch.setattr(trl_kernel, "RULE_VERSION", "forged.trl.runner.v99")

    with pytest.raises(WorkflowRejected, match="TRL runner方法版本"):
        workflow.run_job(job["job_id"])

    assert workflow.store.get_workflow_job(job["job_id"])["state"] == "failed"
    assert {row["dimension_id"] for row in
            workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"])} == {
                "BRL", "CRL"}
