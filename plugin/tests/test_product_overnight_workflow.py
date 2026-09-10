"""本地产品任务4：持久工作流、附件投影与受控专业复核队列。"""

from __future__ import annotations

import copy
import io
import json
import sqlite3

import pytest

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
    return workflow.create_job(
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
            workflow.create_job(
                assessment_unit=unit, profile_id=profile_id,
                method_versions=methods, review_specs=specs)
    else:
        second = workflow.create_job(
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
    second_path = tmp_path / "second.docx"
    _write_docx(first_path)
    second_path.write_bytes(first_path.read_bytes())
    first = workflow.import_attachments([first_path])[0]
    second = workflow.import_attachments([second_path])[0]
    assert second["attachment_id"] == first["attachment_id"]
    assert second["source_id"] == first["source_id"]
    assert workflow.store.count_attachment_imports() == 1
    assert len(workflow.store.fetch_all("import_records")) == 1


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
    assert persisted["locator"][locator_key] == 1
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
        workflow.create_job(
            assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
            method_versions=METHODS, review_specs=[spec])
    with pytest.raises(WorkflowRejected, match="profile|登记"):
        workflow.create_job(
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
    sealed = workflow.reviews.seal_response(
        request["request_id"], _valid_response(request),
        source_mode="manual_import")

    assert sealed["status"] == "response_sealed"
    assert sealed["source_mode"] == "manual_import"
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    assert workflow.blobs.read_bytes(sealed["response_blob_sha256"])
    consumed = workflow.reviews.consume_response(
        sealed["response_id"], worker_id="consumer-A")
    assert consumed["status"] == "consumed"
    assert consumed["source_mode"] == "manual_import"
    assert workflow.status(job["job_id"])["state"] == "consumed"
    assert workflow.reviews.consume_response(
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
        workflow.reviews.seal_response(
            request["request_id"], response, source_mode="manual_import")


@pytest.mark.parametrize("mode", ["runtime_provider", "automatic", "fixture"])
def test_response_rejects_unapproved_or_fake_source_mode(
        workflow, tmp_path, mode):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    with pytest.raises(ReviewQueueRejected, match="source_mode|未授权|模式"):
        workflow.reviews.seal_response(
            request["request_id"], _valid_response(request), source_mode=mode)


def test_simulated_response_requires_explicit_test_switch(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    with pytest.raises(ReviewQueueRejected, match="显式|模拟"):
        workflow.reviews.seal_response(
            request["request_id"], _valid_response(request),
            source_mode="simulated")
    sealed = workflow.reviews.seal_response(
        request["request_id"], _valid_response(request),
        source_mode="simulated", allow_simulated=True)
    assert sealed["source_mode"] == "simulated"


def test_response_wrong_request_identity_or_citation_closure_is_rejected(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    response = _valid_response(request)
    response["request_input_digest"] = "0" * 64
    with pytest.raises(ReviewQueueRejected, match="输入身份"):
        workflow.reviews.seal_response(
            request["request_id"], response, source_mode="manual_import")


def test_request_order_does_not_change_job_but_all_request_ids_are_frozen(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    first_spec = _review_spec(imported, projection, purpose="目的A")
    second_spec = _review_spec(imported, projection, purpose="目的B")
    first = workflow.create_job(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[first_spec, second_spec])
    second = workflow.create_job(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS, review_specs=[second_spec, first_spec])
    assert first["job_id"] == second["job_id"]
    assert len(first["review_request_ids"]) == 2
    assert first["review_request_ids"] == sorted(first["review_request_ids"])


def test_multi_request_job_state_requires_all_responses_to_advance(
        workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = workflow.create_job(
        assessment_unit=UNIT, profile_id=CURRENT_AGGREGATION_PROFILE_ID,
        method_versions=METHODS,
        review_specs=[
            _review_spec(imported, projection, purpose="目的A"),
            _review_spec(imported, projection, purpose="目的B"),
        ])
    requests = [workflow.reviews.get_request(request_id)
                for request_id in job["review_request_ids"]]
    first = workflow.reviews.seal_response(
        requests[0]["request_id"], _valid_response(requests[0]),
        source_mode="manual_import")
    assert workflow.status(job["job_id"])["state"] == \
        "awaiting_authorized_analysis"
    second = workflow.reviews.seal_response(
        requests[1]["request_id"], _valid_response(requests[1]),
        source_mode="manual_import")
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    workflow.reviews.consume_response(first["response_id"], worker_id="consumer")
    assert workflow.status(job["job_id"])["state"] == "response_sealed"
    workflow.reviews.consume_response(second["response_id"], worker_id="consumer")
    assert workflow.status(job["job_id"])["state"] == "consumed"


def test_same_request_rejects_a_different_resealed_response(workflow, tmp_path):
    imported, projection = _import_and_project(workflow, tmp_path)
    job = _create_job(workflow, imported, projection)
    request = workflow.reviews.get_request(job["review_request_ids"][0])
    workflow.reviews.seal_response(
        request["request_id"], _valid_response(request),
        source_mode="manual_import")
    changed = _valid_response(request)
    changed["findings"]["summary"] = "不同正文"
    with pytest.raises(ReviewQueueRejected, match="不同response|已封存"):
        workflow.reviews.seal_response(
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
    sealed = workflow.reviews.seal_response(
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
        workflow.reviews.consume_response(
            sealed["response_id"], worker_id="consumer")
    response = _valid_response(request)
    response["citations"][0]["quote_sha256"] = "0" * 64
    with pytest.raises(ReviewQueueRejected, match="引用|闭包"):
        workflow.reviews.seal_response(
            request["request_id"], response, source_mode="manual_import")


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
        workflow.journal.ensure_task("mechanical:test", "input-1")
        verifier = Journal(db)
        assert verifier.task_state("mechanical:test")["input_id"] == "input-1"
    finally:
        if verifier is not None:
            verifier.close()
        workflow.close()


def test_failed_mechanical_stage_remains_failed_not_business_no(workflow):
    workflow.store.add_workflow_job({
        "schema_version": "kth-local.workflow-job.v1",
        "job_id": "JOB::" + "0" * 64,
        "input_digest": "0" * 64,
        "state": "failed",
        "failure": {"stage": "projection", "detail": "parser crash"},
    })
    state = workflow.status("JOB::" + "0" * 64)
    assert state["state"] == "failed"
    assert state["failure"]["detail"] == "parser crash"
    assert "insufficient" not in json.dumps(state, ensure_ascii=False)
