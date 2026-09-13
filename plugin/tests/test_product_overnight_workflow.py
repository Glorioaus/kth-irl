"""本地产品任务4：持久工作流、附件投影与受控专业复核队列。"""

from __future__ import annotations

import copy
import io
import json
import sqlite3
import time

import pytest
import kth_hybrid.intake as intake_module
import kth_hybrid.review_queue as review_queue_module
import kth_hybrid.workflow as workflow_module

from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.evidence_permissions import build_evidence_use_license
from kth_hybrid.journal import Journal
from kth_hybrid.review_queue import ReviewQueue, ReviewQueueRejected
from kth_hybrid.store import BlobStore, CaseStore
from kth_hybrid.workflow import LocalWorkflow, WorkflowRejected


SUBJECT = "合成主体有限公司"
UNIT = {
    "scope_id": "UNIT-A",
    "subject_scope": SUBJECT,
    "unit_kind": "company",
    "unit_label": "合成主体",
}
METHODS = {
    "candidate_method": "kth-local.candidate.v1",
    "qualification_method": "kth-hybrid.qualification.v2",
}


@pytest.fixture()
def workflow(tmp_path):
    item = LocalWorkflow(tmp_path / "case")
    item.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=["合成主体"],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis="synthetic:test",
    )
    yield item
    item.close()


def _write_docx(path, paragraphs=("第一段：技术已完成受控验证。", "第二段：尚待专业解释。")):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(path)


def _write_pdf(path, text: str | None):
    pytest.importorskip("PyPDF2")
    if text is None:
        from PyPDF2 import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with path.open("wb") as handle:
            writer.write(handle)
        return
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    canvas = reportlab.Canvas(str(path), pagesize=(300, 300))
    canvas.drawString(24, 260, text)
    canvas.save()


def _import_and_project(workflow, tmp_path, suffix=".docx"):
    path = tmp_path / f"source{suffix}"
    if suffix == ".docx":
        _write_docx(path)
    else:
        _write_pdf(path, "verified technical evidence")
    imported = workflow.import_attachments([path])[0]
    projections = workflow.project_sources([imported["source_id"]])
    projected = next(row for row in projections if row["status"] == "projected")
    return imported, projected


def _license(projection, source, *, criterion="TRL4-C1", requested_use="technology_readiness"):
    quote = projection["text"]
    digest = sha256_hex(quote.encode("utf-8"))
    qualification_body = {
        "claim": {
            "claim_id": "CLAIM-SOURCE-1",
            "excerpt_sha256": digest,
            "subject_scope": SUBJECT,
        },
        "outcome": {"allowed_uses": [requested_use]},
    }
    qualification_view = {
        **qualification_body,
        "input_digest": sha256_hex(json.dumps(
            qualification_body, ensure_ascii=False,
            sort_keys=True).encode("utf-8")),
    }
    binding = {
        "review": {
            "review_id": "REV-SOURCE-1",
            "criterion_id": criterion,
            "claim_id": "CLAIM-SOURCE-1",
            "quote_sha256": digest,
            "evidence_class": "technical_validation",
            "subject_scope": SUBJECT,
            "support_scope": criterion,
        },
        "qualification_view": qualification_view,
    }
    return build_evidence_use_license(
        dimension_id="TRL",
        result_id="DIMR2::TRL::" + "1" * 64,
        binding=binding,
        criterion={
            "criterion_id": criterion,
            "dimension": "TRL",
            "evidence_classes": ["technical_validation"],
        },
        scope_id=UNIT["scope_id"],
    )


def _review_spec(imported, projection, **changes):
    spec = {
        "source_id": imported["source_id"],
        "blob_sha256": imported["blob_sha256"],
        "projection_id": projection["projection_id"],
        "locator": projection["locator"],
        "quote": projection["text"],
        "dimension_id": "TRL",
        "criterion_id": "TRL4-C1",
        "scope_id": UNIT["scope_id"],
        "license": _license(projection, imported),
        "requested_use": "technology_readiness",
        "purpose": "判断引文是否支持指定TRL准则",
        "output_schema": "kth-local.review-output.trl.v1",
    }
    spec.update(changes)
    return spec


def _create_job(workflow, imported, projection, **spec_changes):
    return workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT,
        profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS,
        review_specs=[_review_spec(imported, projection, **spec_changes)],
    )


def test_initialization_and_job_identity_freeze_complete_inputs(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    same = _create_job(workflow, imported, projection)

    assert same["job_id"] == job["job_id"]
    assert job["schema_version"] == "kth-local.workflow-job.v1"
    assert job["case_basis"]["version"] == 1
    assert job["case_basis"]["subject_legal_name"] == SUBJECT
    assert job["assessment_unit"] == UNIT
    assert job["profile_id"] == CURRENT_AGGREGATION_PROFILE_ID
    assert job["profile_digest"] == get_aggregation_profile(
        CURRENT_AGGREGATION_PROFILE_ID)["profile_digest"]
    assert job["method_versions"] == METHODS
    assert job["sources"] == [{
        "source_id": imported["source_id"],
        "blob_sha256": imported["blob_sha256"],
        "byte_length": imported["byte_length"],
    }]
    assert job["projections"][0]["projection_id"] == projection["projection_id"]
    assert len(job["review_request_ids"]) == 1
    assert workflow.store.count_workflow_jobs() == 1


@pytest.mark.parametrize("change", [
    "basis", "unit", "profile", "method", "source", "projection", "request",
])
def test_any_frozen_input_change_produces_new_job(workflow, tmp_path, change):
    imported, projection = _import_and_project(workflow, tmp_path)
    first = _create_job(workflow, imported, projection)
    unit = copy.deepcopy(UNIT)
    methods = copy.deepcopy(METHODS)
    specs = [_review_spec(imported, projection)]
    profile_id = CURRENT_AGGREGATION_PROFILE_ID
    if change == "basis":
        workflow.initialize_case(
            subject_legal_name=SUBJECT, subject_aliases=["合成主体", "新别名"],
            evidence_cutoff="2026-09-09T00:00:00Z",
            subject_source_basis="synthetic:test")
    elif change == "unit":
        unit["unit_label"] = "新评估单元"
    elif change == "profile":
        profile_id = "AGGPROF::" + "0" * 64
    elif change == "method":
        methods["candidate_method"] = "kth-local.candidate.v2"
    elif change == "source":
        other = tmp_path / "other.docx"
        _write_docx(other, ("另一份原件。",))
        imported2 = workflow.import_attachments([other])[0]
        projection2 = workflow.project_sources([imported2["source_id"]])[0]
        specs = [_review_spec(imported2, projection2)]
    elif change == "projection":
        specs[0]["projection_id"] = "PROJ::" + "0" * 64
    elif change == "request":
        specs[0]["purpose"] = "另一项受控目的"

    if change in {"profile", "projection"}:
        with pytest.raises(WorkflowRejected):
            workflow._create_job_v1_history_fixture(
                assessment_unit=unit, profile_id=profile_id,
                method_versions=methods, review_specs=specs)
    else:
        second = workflow._create_job_v1_history_fixture(
            assessment_unit=unit, profile_id=profile_id,
            method_versions=methods, review_specs=specs)
        assert second["job_id"] != first["job_id"]


def test_attachment_import_is_idempotent_by_path_and_content(workflow, tmp_path):
    path = tmp_path / "same.docx"
    _write_docx(path)
    first = workflow.import_attachments([path])[0]
    second = workflow.import_attachments([path])[0]
    assert second == first
    assert workflow.store.count_attachment_imports() == 1
    assert len(workflow.store.fetch_all("sources")) == 1


def test_same_original_bytes_at_another_path_do_not_create_another_import(
        workflow, tmp_path):
    first_path = tmp_path / "first.docx"
    second_path = tmp_path / "second.pdf"
    _write_docx(first_path)
    second_path.write_bytes(first_path.read_bytes())
    first = workflow.import_attachments([first_path])[0]
    second = workflow.import_attachments([second_path])[0]
    assert second["attachment_id"] == first["attachment_id"]
    assert second["source_id"] == first["source_id"]
    assert workflow.store.count_attachment_imports() == 1
    assert workflow.store.count_attachment_aliases() == 2
    assert len(workflow.store.fetch_all("import_records")) == 2


def test_attachment_list_must_be_explicit_finite_sequence(workflow, tmp_path):
    with pytest.raises(WorkflowRejected, match="有限文件列表"):
        workflow.import_attachments(path for path in [tmp_path / "x.docx"])
    with pytest.raises(WorkflowRejected, match="有限文件列表"):
        workflow.import_attachments([tmp_path / f"{i}.docx" for i in range(257)])


@pytest.mark.parametrize("kind", ["unsupported", "broken_pdf", "broken_docx"])
def test_invalid_or_unsupported_attachment_persists_honest_state(
        workflow, tmp_path, kind):
    suffix = {"unsupported": ".exe", "broken_pdf": ".pdf",
              "broken_docx": ".docx"}[kind]
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(b"not-a-valid-document")
    result = workflow.import_attachments([path])[0]
    assert result["status"] == ("unsupported" if kind == "unsupported" else "failed")
    assert result["error"]
    assert workflow.blobs.read_bytes(result["blob_sha256"]) == path.read_bytes()
    assert workflow.store.count_attachment_imports() == 1
    assert workflow.store.fetch_all("sources") == []


def test_scanned_pdf_is_unprocessed_not_business_insufficient(workflow, tmp_path):
    path = tmp_path / "scan.pdf"
    _write_pdf(path, None)
    imported = workflow.import_attachments([path])[0]
    assert imported["status"] == "saved"
    projections = workflow.project_sources([imported["source_id"]])
    assert len(projections) == 1
    assert projections[0]["status"] == "unprocessed"
    assert "无文本层" in projections[0]["error"]
    assert "insufficient" not in projections[0]["status"]


@pytest.mark.parametrize("suffix,locator_key", [
    (".pdf", "page"), (".docx", "paragraph"),
])
def test_pdf_page_and_docx_paragraph_projection_are_persisted(
        workflow, tmp_path, suffix, locator_key):
    imported, projection = _import_and_project(workflow, tmp_path, suffix)
    persisted = workflow.store.get_text_projection(projection["projection_id"])
    assert persisted["source_id"] == imported["source_id"]
    assert persisted["source_blob_sha256"] == imported["blob_sha256"]
    assert isinstance(persisted["locator"][locator_key], int)
    assert persisted["locator"][locator_key] >= 1
    text = workflow.blobs.read_bytes(
        persisted["text_blob_sha256"]).decode("utf-8")
    assert persisted["text_sha256"] == sha256_hex(
        text.encode("utf-8"))
    assert persisted["tool"]
    assert text == projection["text"]


def test_review_request_binds_complete_authorized_input_and_waits_without_provider(
        workflow, tmp_path, monkeypatch):
    imported, projection = _import_and_project(workflow, tmp_path)
    calls = []
    monkeypatch.setattr(
        "kth_hybrid.runner.CountingSimulatedProvider.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)))
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])

    assert request["schema_version"] == "review_request.v1"
    assert request["job_id"] == job["job_id"]
    assert request["source_id"] == imported["source_id"]
    assert request["blob_sha256"] == imported["blob_sha256"]
    assert request["projection_id"] == projection["projection_id"]
    assert request["locator"] == projection["locator"]
    assert request["quote_sha256"] == projection["text_sha256"]
    assert request["case_basis_digest"] == job["case_basis_digest"]
    assert request["case_basis_version"] == 1
    assert request["assessment_unit"] == UNIT
    assert request["license_id"].startswith("EVIDUSE::")
    assert request["criterion_id"] == "TRL4-C1"
    assert request["requested_use"] == "technology_readiness"
    assert request["profile_id"] == CURRENT_AGGREGATION_PROFILE_ID
    assert request["method_versions"] == METHODS
    assert request["purpose"]
    assert request["output_schema"]
    assert request["status"] == "awaiting_authorized_analysis"
    assert workflow.status(job["job_id"])["state"] == "awaiting_authorized_analysis"
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("source_id", "ATT::missing"),
    ("blob_sha256", "0" * 64),
    ("projection_id", "PROJ::" + "0" * 64),
    ("locator", {"paragraph": 999}),
    ("quote", "错误引文"),
    ("scope_id", "UNIT-B"),
    ("criterion_id", "TRL5-C1"),
    ("requested_use", "financial_readiness"),
])
def test_review_request_rejects_missing_or_wrong_binding(
        workflow, tmp_path, field, value):
    imported, projection = _import_and_project(workflow, tmp_path)
    with pytest.raises((WorkflowRejected, ReviewQueueRejected)):
        _create_job(workflow, imported, projection, **{field: value})


def test_stale_case_basis_or_profile_identity_is_rejected(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    spec = _review_spec(imported, projection)
    spec["case_basis_version"] = 999
    with pytest.raises(WorkflowRejected, match="CaseBasis|过期"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
    with pytest.raises(WorkflowRejected, match="profile|登记"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id="AGGPROF::" + "0" * 64,
            method_versions=METHODS, review_specs=[_review_spec(imported, projection)])


def _valid_response(request):
    return {
        "schema_version": "review_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {"producer_id": "human-reviewer-01",
                     "producer_kind": "authorized_human"},
        "output_schema": request["output_schema"],
        "decision": "supports",
        "evidence_class": "technical_validation",
        "findings": {"summary": "引文支持指定准则，仍由规则内核求值。"},
        "citations": [{
            "source_id": request["source_id"],
            "blob_sha256": request["blob_sha256"],
            "projection_id": request["projection_id"],
            "locator": request["locator"],
            "quote_sha256": request["quote_sha256"],
        }],
    }


def test_manual_response_is_sealed_then_consumed_in_separate_step(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    sealed = workflow.reviews._seal_response_v1_history_fixture(
        request["request_id"], _valid_response(request),
        source_mode="manual_import")

    assert sealed["status"] == "response_sealed"
    assert sealed["source_mode"] == "manual_import"
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    assert workflow.blobs.read_bytes(sealed["response_blob_sha256"])
    consumed = workflow.reviews._consume_response_v1_history_fixture(
        sealed["response_id"], worker_id="consumer-A")
    assert consumed["status"] == "consumed"
    assert consumed["source_mode"] == "manual_import"
    assert workflow.status(job["job_id"])["state"] == "consumed"
    assert workflow.reviews._consume_response_v1_history_fixture(
        sealed["response_id"], worker_id="consumer-A")["response_id"] == \
        sealed["response_id"]


@pytest.mark.parametrize("forbidden", [
    {"level": 4},
    {"native_disposition": "met"},
    {"final_decision": "approved"},
    {"findings": {"investment_recommendation": "invest"}},
])
def test_response_rejects_rule_or_decision_fields(workflow, tmp_path, forbidden):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    response = _valid_response(request)
    response.update(forbidden)
    with pytest.raises(ReviewQueueRejected, match="越权|禁止"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")


@pytest.mark.parametrize("mode", ["runtime_provider", "automatic", "fixture"])
def test_response_rejects_unapproved_or_fake_source_mode(
        workflow, tmp_path, mode):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    with pytest.raises(ReviewQueueRejected, match="source_mode|未授权|模式"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], _valid_response(request), source_mode=mode)


def test_simulated_response_requires_explicit_test_switch(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    with pytest.raises(ReviewQueueRejected, match="显式|模拟"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], _valid_response(request),
            source_mode="simulated")
    response = _valid_response(request)
    response["producer"] = {
        "producer_id": "simulator-01", "producer_kind": "simulated"}
    sealed = workflow.reviews._seal_response_v1_history_fixture(
        request["request_id"], response,
        source_mode="simulated", allow_simulated=True)
    assert sealed["source_mode"] == "simulated"


@pytest.mark.parametrize("mode,producer_kind,allow_simulated", [
    ("manual_import", "simulated", False),
    ("simulated", "authorized_human", True),
    ("runtime_provider", "runtime_provider", False),
])
def test_source_mode_must_match_real_producer_kind(
        workflow, tmp_path, mode, producer_kind, allow_simulated):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    response = _valid_response(request)
    response["producer"] = {
        "producer_id": "producer-01", "producer_kind": producer_kind}
    with pytest.raises(ReviewQueueRejected, match="producer|来源|未授权|配对"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode=mode,
            allow_simulated=allow_simulated)


def test_response_wrong_request_identity_or_citation_closure_is_rejected(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    response = _valid_response(request)
    response["request_input_digest"] = "0" * 64
    with pytest.raises(ReviewQueueRejected, match="输入身份"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")
    response = _valid_response(request)
    response["citations"][0]["quote_sha256"] = "0" * 64
    with pytest.raises(ReviewQueueRejected, match="引用|闭包"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")


def test_request_order_does_not_change_job_but_all_request_ids_are_frozen(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    first_spec = _review_spec(imported, projection, purpose="目的A")
    second_spec = _review_spec(imported, projection, purpose="目的B")
    first = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[first_spec, second_spec])
    second = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[second_spec, first_spec])
    assert first["job_id"] == second["job_id"]
    assert len(first["review_request_ids"]) == 2
    assert first["review_request_ids"] == sorted(first["review_request_ids"])


def test_same_request_input_can_belong_to_x_and_xy_jobs_atomically(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    x = _review_spec(imported, projection, purpose="请求X")
    y = _review_spec(imported, projection, purpose="请求Y")
    job_x = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[x])
    job_xy = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[x, y])
    assert job_x["job_id"] != job_xy["job_id"]
    assert len(job_x["review_request_ids"]) == 1
    assert len(job_xy["review_request_ids"]) == 2
    assert workflow.store.count_workflow_jobs() == 2
    assert workflow.store._conn.execute(
        "SELECT COUNT(*) FROM review_requests").fetchone()[0] == 3
    assert all(len(workflow.store.fetch_review_requests(job_id)) == count
               for job_id, count in ((job_x["job_id"], 1),
                                     (job_xy["job_id"], 2)))


def test_job_and_all_requests_roll_back_when_second_request_insert_fails(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    x = _review_spec(imported, projection, purpose="请求X")
    y = _review_spec(imported, projection, purpose="请求Y")
    with workflow.store._conn:
        workflow.store._conn.execute("""
            CREATE TRIGGER fail_second_job_request
            BEFORE INSERT ON review_requests
            WHEN (SELECT COUNT(*) FROM review_requests
                  WHERE job_id=NEW.job_id) >= 1
            BEGIN
                SELECT RAISE(ABORT, 'injected second request failure');
            END
        """)
    with pytest.raises(sqlite3.IntegrityError,
                       match="injected second request failure"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[x, y])
    assert workflow.store.count_workflow_jobs() == 0
    assert workflow.store._conn.execute(
        "SELECT COUNT(*) FROM review_requests").fetchone()[0] == 0


def test_create_job_recovers_complete_bundle_after_keyboard_interrupt(
        tmp_path, monkeypatch):
    root = tmp_path / "create-recovery"
    first = LocalWorkflow(root)
    first.initialize_case(
        subject_legal_name=SUBJECT, subject_aliases=["合成主体"],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis="synthetic:test")
    path = tmp_path / "create-recovery.docx"
    _write_docx(path)
    imported = first.import_attachments([path])[0]
    projection = next(row for row in first.project_sources(
        [imported["source_id"]]) if row["status"] == "projected")
    spec = _review_spec(imported, projection)
    real_commit = first.journal.commit

    def interrupt_after_bundle(claim, output_ref):
        if claim.task_key.startswith("workflow-create:"):
            raise KeyboardInterrupt("injected after bundle")
        return real_commit(claim, output_ref)

    monkeypatch.setattr(first.journal, "commit", interrupt_after_bundle)
    with pytest.raises(KeyboardInterrupt, match="injected after bundle"):
        first._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
    job_id = first.store._conn.execute(
        "SELECT job_id FROM workflow_jobs").fetchone()[0]
    input_digest = first.store._conn.execute(
        "SELECT input_digest FROM workflow_jobs WHERE job_id=?",
        (job_id,)).fetchone()[0]
    task_key = f"workflow-create:{job_id}"
    assert first.journal.task_state(task_key)["state"] == "claimed"
    assert len(first.store.fetch_review_requests(job_id)) == 1
    first.close()

    recovered = LocalWorkflow(root)
    try:
        result = recovered._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
        task = recovered.journal.task_state(task_key)
        assert result["job_id"] == job_id
        assert task["state"] == "succeeded"
        assert task["input_id"] == input_digest
        assert task["output_ref"] == job_id
        assert task["external_actions"] == 0
        assert len(recovered.journal.attempts(task_key)) == 2
        assert len(recovered.journal.takeover_events(task_key)) == 1
    finally:
        recovered.close()


@pytest.mark.parametrize("field,bad_value,match", [
    ("output_ref", "JOB::wrong-output", "output|输出|不一致"),
    ("input_id", "wrong-input", "input|输入|不一致"),
])
def test_existing_succeeded_creation_task_rejects_wrong_identity(
        workflow, tmp_path, field, bad_value, match):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    task_key = f"workflow-create:{job['job_id']}"
    with workflow.store._conn:
        workflow.store._conn.execute(
            f"UPDATE tasks SET {field}=? WHERE task_key=?",
            (bad_value, task_key))
    with pytest.raises(WorkflowRejected, match=match):
        _create_job(workflow, imported, projection)


def test_failed_creation_task_requires_explicit_controlled_resume(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    task_key = f"workflow-create:{job['job_id']}"
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE tasks SET state='failed',output_ref=NULL WHERE task_key=?",
            (task_key,))
    with pytest.raises(WorkflowRejected, match="failed|显式|恢复"):
        _create_job(workflow, imported, projection)
    resumed = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS,
        review_specs=[_review_spec(imported, projection)],
        resume_failed_creation=True)
    assert resumed["job_id"] == job["job_id"]
    assert workflow.journal.task_state(task_key)["state"] == "succeeded"


@pytest.mark.parametrize("state", ["dispatch_recorded", "outcome_unknown"])
def test_dispatched_or_unknown_creation_task_is_never_auto_taken_over(
        workflow, tmp_path, state):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    task_key = f"workflow-create:{job['job_id']}"
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE tasks SET state=?,external_actions=1,output_ref=NULL "
            "WHERE task_key=?", (state, task_key))
    with pytest.raises(WorkflowRejected, match="派发|unknown|接管|外部动作"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS,
            review_specs=[_review_spec(imported, projection)],
            resume_failed_creation=True)


def test_v5_global_request_digest_schema_migrates_without_losing_requests(
        tmp_path):
    root = tmp_path / "request-v5"
    workflow = LocalWorkflow(root)
    workflow.initialize_case(
        subject_legal_name=SUBJECT, subject_aliases=["合成主体"],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis="synthetic:test")
    path = tmp_path / "request-source.docx"
    _write_docx(path)
    imported = workflow.import_attachments([path])[0]
    projection = next(row for row in workflow.project_sources(
        [imported["source_id"]]) if row["status"] == "projected")
    x = _review_spec(imported, projection, purpose="请求X")
    y = _review_spec(imported, projection, purpose="请求Y")
    job_x = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[x])
    request_before = workflow.reviews.get_request(job_x["review_request_ids"][0])
    response_before = workflow.reviews._seal_response_v1_history_fixture(
        request_before["request_id"], _valid_response(request_before),
        source_mode="manual_import")
    request_before = workflow.reviews.get_request(job_x["review_request_ids"][0])
    workflow.close()

    conn = sqlite3.connect(root / "records.sqlite3")
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE review_requests_v5 (
                request_id TEXT PRIMARY KEY,
                request_input_digest TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id),
                status TEXT NOT NULL CHECK (status IN
                    ('awaiting_authorized_analysis','response_sealed',
                     'consumed','failed')),
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO review_requests_v5 SELECT * FROM review_requests;
            DROP TABLE review_requests;
            ALTER TABLE review_requests_v5 RENAME TO review_requests;
            CREATE INDEX idx_review_requests_job
                ON review_requests(job_id,request_id);
            UPDATE meta SET value='kth-hybrid.store.v5'
                WHERE key='schema_version';
            COMMIT;
        """)
    finally:
        conn.close()

    migrated = LocalWorkflow(root)
    try:
        request_after = migrated.reviews.get_request(request_before["request_id"])
        assert request_after["created_at"] == request_before["created_at"]
        assert request_after["updated_at"] == request_before["updated_at"]
        assert request_after["request_input_digest"] == \
            request_before["request_input_digest"]
        response_after = migrated.store.get_review_response(
            response_before["response_id"])
        assert response_after["response_id"] == response_before["response_id"]
        assert response_after["status"] == "response_sealed"
        assert response_after["created_at"] == response_before["created_at"]
        job_xy = migrated._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[x, y])
        assert len(job_xy["review_request_ids"]) == 2
        assert migrated.store._conn.execute(
            "SELECT COUNT(*) FROM review_requests").fetchone()[0] == 3
        assert migrated.store._conn.execute(
            "PRAGMA foreign_key_check").fetchall() == []
        assert migrated.store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == \
                    "kth-hybrid.store.v9"
    finally:
        migrated.close()


def test_job_state_refresh_cannot_downgrade_from_stale_request_snapshot(
        workflow, tmp_path, monkeypatch):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS,
        review_specs=[
            _review_spec(imported, projection, purpose="请求X"),
            _review_spec(imported, projection, purpose="请求Y"),
        ])
    first_id, second_id = job["review_request_ids"]
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE review_requests SET status='response_sealed' "
            "WHERE request_id=?", (first_id,))
        workflow.store._conn.execute(
            "UPDATE workflow_jobs SET state='response_sealed' WHERE job_id=?",
            (job["job_id"],))
    original_fetch = workflow.store.fetch_review_requests

    def stale_then_interleave(job_id):
        stale = original_fetch(job_id)
        with workflow.store._conn:
            workflow.store._conn.execute(
                "UPDATE review_requests SET status='response_sealed' "
                "WHERE request_id=?", (second_id,))
        return stale

    monkeypatch.setattr(
        workflow.store, "fetch_review_requests", stale_then_interleave)
    workflow.reviews._refresh_job_state(job["job_id"])
    assert workflow.store.get_workflow_job(job["job_id"])["state"] == \
        "response_sealed"


def test_multi_request_job_state_requires_all_responses_to_advance(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = workflow._create_job_v1_history_fixture(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS,
        review_specs=[
            _review_spec(imported, projection, purpose="目的A"),
            _review_spec(imported, projection, purpose="目的B"),
        ])
    requests = [workflow.reviews.get_request(request_id)
                for request_id in job["review_request_ids"]]
    first = workflow.reviews._seal_response_v1_history_fixture(
        requests[0]["request_id"], _valid_response(requests[0]),
        source_mode="manual_import")
    assert workflow.status(job["job_id"])["state"] == \
        "awaiting_authorized_analysis"
    second = workflow.reviews._seal_response_v1_history_fixture(
        requests[1]["request_id"], _valid_response(requests[1]),
        source_mode="manual_import")
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    workflow.reviews._consume_response_v1_history_fixture(first["response_id"], worker_id="consumer")
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    workflow.reviews._consume_response_v1_history_fixture(second["response_id"], worker_id="consumer")
    assert workflow.status(job["job_id"])["state"] == "consumed"


def test_same_request_rejects_a_different_resealed_response(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    workflow.reviews._seal_response_v1_history_fixture(
        request["request_id"], _valid_response(request),
        source_mode="manual_import")
    changed = _valid_response(request)
    changed["findings"]["summary"] = "不同正文"
    with pytest.raises(ReviewQueueRejected, match="不同response|已封存"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], changed, source_mode="manual_import")


def test_read_time_rejects_tampered_request_and_response_body(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request_id = job["review_request_ids"][0]
    request = workflow.reviews.get_request(request_id)
    original_request_json = workflow.store._conn.execute(
        "SELECT body_json FROM review_requests WHERE request_id=?",
        (request_id,)).fetchone()[0]
    forged = json.loads(original_request_json)
    forged["purpose"] = "被改写的目的"
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE review_requests SET body_json=? WHERE request_id=?",
            (json.dumps(forged, ensure_ascii=False, sort_keys=True), request_id))
    with pytest.raises(ReviewQueueRejected, match="身份|重建"):
        workflow.reviews.get_request(request_id)
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE review_requests SET body_json=? WHERE request_id=?",
            (original_request_json, request_id))
    request = workflow.reviews.get_request(request_id)
    sealed = workflow.reviews._seal_response_v1_history_fixture(
        request_id, _valid_response(request), source_mode="manual_import")
    response_json = workflow.store._conn.execute(
        "SELECT body_json FROM review_responses WHERE response_id=?",
        (sealed["response_id"],)).fetchone()[0]
    forged_response = json.loads(response_json)
    forged_response["findings"] = {"summary": "被改写"}
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE review_responses SET body_json=? WHERE response_id=?",
            (json.dumps(forged_response, ensure_ascii=False, sort_keys=True),
             sealed["response_id"]))
    with pytest.raises(ReviewQueueRejected, match="封存|身份|正文"):
        workflow.reviews._consume_response_v1_history_fixture(
            sealed["response_id"], worker_id="consumer")


def test_status_rebuilds_content_addressed_job_before_reporting(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    row = workflow.store._conn.execute(
        "SELECT body_json FROM workflow_jobs WHERE job_id=?",
        (job["job_id"],)).fetchone()
    forged = json.loads(row[0])
    forged["method_versions"]["candidate_method"] = "forged.v99"
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE workflow_jobs SET body_json=? WHERE job_id=?",
            (json.dumps(forged, ensure_ascii=False, sort_keys=True), job["job_id"]))
    with pytest.raises(WorkflowRejected, match="job|身份|改写"):
        workflow.status(job["job_id"])


def test_old_database_migrates_and_journal_uses_same_records_database(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    db = root / "records.sqlite3"
    sqlite3.connect(db).close()
    workflow = LocalWorkflow(root)
    verifier = None
    try:
        tables = {row[0] for row in workflow.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"workflow_jobs", "attachment_imports", "text_projections",
                "review_requests", "review_responses", "tasks"} <= tables
        assert workflow.store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == \
                    "kth-hybrid.store.v9"
        workflow.journal.ensure_task("mechanical:test", "input-1")
        verifier = Journal(db)
        assert verifier.task_state("mechanical:test")["input_id"] == "input-1"
    finally:
        if verifier is not None:
            verifier.close()
        workflow.close()


def test_v4_duplicate_attachment_rows_migrate_to_one_canonical_object(
        tmp_path):
    root = tmp_path / "legacy-v4-duplicates"
    root.mkdir()
    first_path = root / "a.txt"
    second_path = root / "b.md"
    payload = "同一原件正文。".encode("utf-8")
    first_path.write_bytes(payload)
    second_path.write_bytes(payload)
    blobs = BlobStore(root / "blobs")
    blob = blobs.put_bytes(payload)
    legacy = CaseStore(root / "records.sqlite3")
    try:
        first_import = legacy.add_import_record(
            "attachment", str(first_path.resolve()), blob.sha256)
        second_import = legacy.add_import_record(
            "attachment", str(second_path.resolve()), blob.sha256)
        legacy.add_source(
            "SRC-V4-A", blob.sha256, blob.byte_length,
            media_type="text/plain", locator=str(first_path.resolve()),
            source_family="owner_attachment", capture_status="attachment_saved",
            import_id=first_import)
        legacy.add_source(
            "SRC-V4-B", blob.sha256, blob.byte_length,
            media_type="text/markdown", locator=str(second_path.resolve()),
            source_family="owner_attachment", capture_status="attachment_saved",
            import_id=second_import)
        with legacy._conn:
            legacy._conn.execute("DROP TABLE attachment_aliases")
            legacy._conn.execute(
                "DROP INDEX IF EXISTS idx_attachment_imports_content")
            legacy._conn.execute(
                "CREATE UNIQUE INDEX idx_attachment_imports_content "
                "ON attachment_imports(blob_sha256,media_type)")
            legacy._conn.executemany(
                "INSERT INTO attachment_imports("
                "attachment_id,origin_path,blob_sha256,byte_length,"
                "original_filename,media_type,status,error,import_id,source_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (f"ATTACH::{blob.sha256}", "canonical-poor", blob.sha256,
                     blob.byte_length, "poor.bin", "application/octet-stream",
                     "unsupported", "unsupported", first_import, None),
                    ("ATTIMP::legacy-a", str(first_path.resolve()), blob.sha256,
                     blob.byte_length, "a.txt", "text/plain", "saved", None,
                     first_import, "SRC-V4-A"),
                    ("ATTIMP::legacy-b", str(second_path.resolve()), blob.sha256,
                     blob.byte_length, "b.md", "text/markdown", "saved", None,
                     second_import, "SRC-V4-B"),
                ],
            )
            legacy._conn.execute(
                "UPDATE meta SET value='kth-hybrid.store.v4' "
                "WHERE key='schema_version'")
    finally:
        legacy.close()

    migrated = LocalWorkflow(root)
    try:
        objects = migrated.store._conn.execute(
            "SELECT * FROM attachment_imports").fetchall()
        aliases = migrated.store._conn.execute(
            "SELECT * FROM attachment_aliases ORDER BY origin_path").fetchall()
        assert len(objects) == 1
        assert objects[0]["attachment_id"] == f"ATTACH::{blob.sha256}"
        assert objects[0]["blob_sha256"] == blob.sha256
        assert len(aliases) == 3
        assert objects[0]["status"] == "saved"
        assert objects[0]["source_id"] in {"SRC-V4-A", "SRC-V4-B"}
        assert {row["origin_path"] for row in aliases} == {
            "canonical-poor", str(first_path.resolve()), str(second_path.resolve())}
        assert {row["media_type"] for row in aliases} == {
            "application/octet-stream", "text/plain", "text/markdown"}
        assert {row["source_id"] for row in aliases} == {
            None, "SRC-V4-A", "SRC-V4-B"}
        assert all(row["attachment_id"] == f"ATTACH::{blob.sha256}"
                   for row in aliases)
        assert migrated.store._conn.execute(
            "PRAGMA foreign_key_check").fetchall() == []
        assert migrated.store._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == \
                    "kth-hybrid.store.v9"
        with pytest.raises(sqlite3.IntegrityError):
            with migrated.store._conn:
                migrated.store._conn.execute(
                    "INSERT INTO attachment_imports("
                    "attachment_id,origin_path,blob_sha256,byte_length,"
                    "original_filename,media_type,status,error,import_id,source_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("ATTIMP::duplicate", "duplicate", blob.sha256,
                     blob.byte_length, "duplicate.bin", "application/octet-stream",
                     "unsupported", "duplicate", first_import, None))
        before_imports = len(migrated.store.fetch_all("import_records"))
        first = migrated.import_attachments([first_path])[0]
        second = migrated.import_attachments([second_path])[0]
        assert first["attachment_id"] == second["attachment_id"] == \
            f"ATTACH::{blob.sha256}"
        assert len(migrated.store.fetch_all("import_records")) == before_imports
        assert migrated.store.count_attachment_imports() == 1
        assert migrated.store.count_attachment_aliases() == 3
        assert len(migrated.store.fetch_all("sources")) == 2
        created_at = {row["alias_id"]: row["created_at"] for row in aliases}
    finally:
        migrated.close()
    time.sleep(0.02)
    reopened = LocalWorkflow(root)
    try:
        assert {row["alias_id"]: row["created_at"] for row in
                reopened.store._conn.execute(
                    "SELECT alias_id,created_at FROM attachment_aliases")} == \
            created_at
    finally:
        reopened.close()


def test_attachment_single_and_batch_byte_limits_fail_before_any_import(
        workflow, tmp_path, monkeypatch):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(b"abcd")
    second.write_bytes(b"xyz")
    monkeypatch.setattr(workflow_module, "MAX_ATTACHMENT_BYTES", 3)
    with pytest.raises(WorkflowRejected, match="单文件|字节|上限"):
        workflow.import_attachments([first])
    assert workflow.store.count_attachment_imports() == 0
    monkeypatch.setattr(workflow_module, "MAX_ATTACHMENT_BYTES", 4)
    monkeypatch.setattr(workflow_module, "MAX_ATTACHMENT_BATCH_BYTES", 6)
    with pytest.raises(WorkflowRejected, match="批次|总字节|上限"):
        workflow.import_attachments([first, second])
    assert workflow.store.count_attachment_imports() == 0


def test_docx_zip_preflight_rejects_member_limit_before_parser(
        workflow, tmp_path, monkeypatch):
    path = tmp_path / "large.docx"
    _write_docx(path)
    monkeypatch.setattr(intake_module, "DOCX_MAX_MEMBERS", 1)
    result = workflow.import_attachments([path])[0]
    assert result["status"] == "failed"
    assert "DOCX" in result["error"] and "成员" in result["error"]
    assert workflow.store.fetch_all("sources") == []


def test_review_specs_and_response_payload_limits_fail_closed(
        workflow, tmp_path, monkeypatch):
    imported, projection = _import_and_project(workflow, tmp_path)
    spec = _review_spec(imported, projection)
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPECS", 1)
    with pytest.raises(WorkflowRejected, match="review_specs|条目|上限"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec, copy.deepcopy(spec)])
    assert workflow.store.count_workflow_jobs() == 0
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPECS", 512)
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPECS_BYTES", 64)
    with pytest.raises(WorkflowRejected, match="review_specs|序列化|字节|上限"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPECS_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPEC_DEPTH", 3)
    with pytest.raises(WorkflowRejected, match="review_specs|深度|上限"):
        workflow._create_job_v1_history_fixture(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
    monkeypatch.setattr(workflow_module, "MAX_REVIEW_SPEC_DEPTH", 24)

    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    response = _valid_response(request)
    monkeypatch.setattr(review_queue_module, "MAX_FINDINGS_BYTES", 16)
    response["findings"] = {"summary": "x" * 100}
    with pytest.raises(ReviewQueueRejected, match="findings|字节|上限"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")
    response = _valid_response(request)
    monkeypatch.setattr(review_queue_module, "MAX_CITATIONS", 0)
    with pytest.raises(ReviewQueueRejected, match="citations|条目|上限"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")
    response = _valid_response(request)
    monkeypatch.setattr(review_queue_module, "MAX_CITATIONS", 64)
    monkeypatch.setattr(review_queue_module, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(ReviewQueueRejected, match="response|序列化|字节|上限"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")
    monkeypatch.setattr(review_queue_module, "MAX_RESPONSE_BYTES", 1024 * 1024)
    monkeypatch.setattr(review_queue_module, "MAX_JSON_DEPTH", 2)
    response["findings"] = {"a": {"b": {"c": "too-deep"}}}
    with pytest.raises(ReviewQueueRejected, match="深度|上限"):
        workflow.reviews._seal_response_v1_history_fixture(
            request["request_id"], response, source_mode="manual_import")


def test_failed_mechanical_stage_remains_failed_not_business_no(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    workflow.store.set_workflow_job_state(
        job["job_id"], "failed",
        failure={"stage": "projection", "detail": "parser crash"})
    state = workflow.status(job["job_id"])
    assert state["state"] == "failed"
    assert state["failure"]["detail"] == "parser crash"
    assert "insufficient" not in json.dumps(state, ensure_ascii=False)


def test_review_progress_cannot_overwrite_failed_job_without_explicit_resume(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    workflow.store.set_workflow_job_state(
        job["job_id"], "failed",
        failure={"stage": "deterministic", "detail": "worker crashed"})
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    sealed = workflow.reviews._seal_response_v1_history_fixture(
        request["request_id"], _valid_response(request),
        source_mode="manual_import")
    after_seal = workflow.status(job["job_id"])
    assert after_seal["state"] == "failed"
    assert after_seal["failure"] == {
        "stage": "deterministic", "detail": "worker crashed"}
    workflow.reviews._consume_response_v1_history_fixture(
        sealed["response_id"], worker_id="consumer")
    after_consume = workflow.status(job["job_id"])
    assert after_consume["state"] == "failed"
    assert after_consume["failure"]["detail"] == "worker crashed"

    resumed = workflow.resume_failed_job(job["job_id"])
    assert resumed["state"] == "consumed"
    assert "failure" not in resumed


def test_multi_locator_projection_rolls_back_partial_rows_and_retries_complete(
        workflow, tmp_path):
    path = tmp_path / "multi.docx"
    _write_docx(path, ("第一段。", "第二段。", "第三段。"))
    imported = workflow.import_attachments([path])[0]
    source_id = imported["source_id"]
    with workflow.store._conn:
        workflow.store._conn.execute("""
            CREATE TRIGGER fail_second_projection
            BEFORE INSERT ON text_projections
            WHEN (SELECT COUNT(*) FROM text_projections
                  WHERE source_id=NEW.source_id) >= 1
            BEGIN
                SELECT RAISE(ABORT, 'injected projection failure');
            END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="injected projection failure"):
        workflow.project_sources([source_id])
    assert workflow.store.fetch_text_projections(source_id) == []
    task = workflow.store._conn.execute(
        "SELECT task_key,state FROM tasks WHERE task_key LIKE 'text-projection:%'"
    ).fetchone()
    assert dict(task)["state"] == "failed"

    with workflow.store._conn:
        workflow.store._conn.execute("DROP TRIGGER fail_second_projection")
    projections = workflow.project_sources([source_id])
    assert len(projections) == 3
    assert len(workflow.store.fetch_text_projections(source_id)) == 3
    task = workflow.store._conn.execute(
        "SELECT task_key,state,output_ref FROM tasks "
        "WHERE task_key LIKE 'text-projection:%'"
    ).fetchone()
    assert task["state"] == "succeeded"
    batch = json.loads(task["output_ref"])
    assert batch["projection_count"] == 3
    assert batch["projection_ids"] == sorted(
        projection["projection_id"] for projection in projections)
