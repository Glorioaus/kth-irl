"""E4：以独立解释器硬退出验证本地工作流的持久恢复边界。"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.audit import trace_crl_dimension, trace_dimension_result
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.journal import CommitRejected, Journal
from kth_hybrid.proposal_requests import candidate_claim_id
from kth_hybrid.store import BlobStore, CaseStore
from kth_hybrid.workflow import LocalWorkflow, WorkflowRejected


PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
CHILD_EXIT = 91
SUBJECT = "E4进程恢复合成主体有限公司"
UNIT = {
    "scope_id": "UNIT-E4-PROCESS",
    "subject_scope": SUBJECT,
    "unit_kind": "current_case_company_level",
    "unit_label": "E4进程恢复公司级评估单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-E4-PROCESS",
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


def _child_script(tmp_path: Path) -> Path:
    script = tmp_path / "e4_process_child.py"
    script.write_text(textwrap.dedent("""
        import json
        import os
        import sys
        from pathlib import Path

        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        source_root = Path(sys.argv[1]).resolve()
        mode = sys.argv[2]
        case_dir = Path(sys.argv[3])
        payload = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
        sys.path.insert(0, str(source_root))

        import kth_hybrid.review_queue as review_queue_module
        import kth_hybrid.workflow as workflow_module
        from kth_hybrid.catalog import build_catalog_from_wheel
        from kth_hybrid.journal import Journal
        from kth_hybrid.workflow import LocalWorkflow

        def emit(event, **fields):
            print(json.dumps({
                "event": event,
                "workflow_file": str(Path(workflow_module.__file__).resolve()),
                "review_queue_file": str(Path(review_queue_module.__file__).resolve()),
                **fields,
            }, ensure_ascii=False, sort_keys=True), flush=True)

        def fault(point):
            if point == payload.get("fault_point"):
                emit("fault", point=point)
                os._exit(payload["exit_code"])

        if mode == "dispatch_then_exit":
            journal = Journal(case_dir / "records.sqlite3")
            try:
                journal.ensure_task(payload["task_key"], payload["input_id"])
                claim = journal.claim(
                    payload["task_key"], "e4-dispatch-child", payload["input_id"])
                journal.record_dispatch(claim)
                emit("dispatch_recorded", task_key=payload["task_key"])
                os._exit(payload["exit_code"])
            finally:
                journal.close()

        emit("started", mode=mode)
        workflow_fault_points = {
            "after_attachment_blob_persisted",
            "after_attachment_import_record_persisted",
            "after_attachment_alias_persisted",
            "after_dimension_output_registered:CRL",
        }
        workflow = LocalWorkflow(
            case_dir,
            fault_hook=(fault if payload.get("fault_point") in workflow_fault_points
                        else None),
        )
        try:
            if mode == "import":
                result = workflow.import_attachments(payload["paths"])
            elif mode == "seal_proposal":
                result = workflow.proposals.seal_response(
                    payload["request_id"], payload["response"],
                    source_mode="manual_import")
                if payload.get("fault_point") == "after_proposal_response_sealed":
                    emit("fault", point="after_proposal_response_sealed")
                    os._exit(payload["exit_code"])
            elif mode == "consume_proposal":
                result = workflow.proposals.consume_response(
                    payload["response_id"], worker_id="e4-proposal-resume",
                    catalog=build_catalog_from_wheel())
            elif mode == "seal_review":
                result = workflow.reviews.seal_response(
                    payload["request_id"], payload["response"],
                    source_mode="manual_import")
                if payload.get("fault_point") == "after_review_response_sealed":
                    emit("fault", point="after_review_response_sealed")
                    os._exit(payload["exit_code"])
            elif mode == "consume_materialize_review":
                consumed = workflow.reviews.consume_response(
                    payload["response_id"], worker_id="e4-review-resume")
                materialized = workflow.materialize_review_response(
                    payload["response_id"], worker_id="e4-materialize-resume")
                result = {"response_id": consumed["response_id"],
                          "review_id": materialized["review_id"]}
            elif mode == "run_job":
                result = workflow.run_job(payload["job_id"])
            elif mode == "status_job":
                result = workflow.status(payload["job_id"])
            elif mode == "resume_job":
                result = workflow.resume_failed_job(payload["job_id"])
            else:
                raise ValueError(f"未知E4子进程模式：{mode}")
            emit("completed", result=result)
        finally:
            workflow.close()
    """), encoding="utf-8")
    return script


def _run_child(tmp_path: Path, case_dir: Path, mode: str, payload: dict,
               evidence: list[dict]) -> subprocess.CompletedProcess[str]:
    index = len(evidence)
    payload_path = tmp_path / f"e4-child-{index}.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", str(_child_script(tmp_path)), str(PLUGIN_SRC), mode,
         str(case_dir), str(payload_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    evidence.append({
        "mode": mode,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })
    return result


def _assert_child_loaded_current_source(result: subprocess.CompletedProcess[str]) -> None:
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    source = str(PLUGIN_SRC.resolve())
    assert all(item["workflow_file"].startswith(source) for item in lines)
    assert all(item["review_queue_file"].startswith(source) for item in lines)


def _write_docx(path: Path, text: str) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph(text)
    document.save(path)


def _case_snapshot(case_dir: Path) -> dict:
    connection = sqlite3.connect(case_dir / "records.sqlite3")
    try:
        counts = {}
        for table in (
                "attachment_imports", "attachment_aliases", "sources",
                "import_records", "text_projections", "claims", "qualifications", "workflow_jobs", "proposal_requests",
                "proposal_responses", "review_requests", "review_responses",
                "workflow_job_dimension_outputs", "workflow_job_artifacts"):
            counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()
    return {
        "counts": counts,
        "blob_ids": sorted(BlobStore(case_dir / "blobs").list_objects()),
    }


def _save_evidence(case_dir: Path, name: str, *, before: dict, after: dict,
                   processes: list[dict]) -> Path:
    audit_dir = case_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    path = audit_dir / f"{name}.json"
    path.write_text(json.dumps({
        "schema_version": "kth-local.e4-process-recovery-evidence.v1",
        "before": before,
        "after": after,
        "processes": processes,
    }, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return path


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
            **{f"{dimension.lower()}_rule_version": values["rule_version"]
               for dimension, values in profile["dimensions"].items()},
            **{f"{dimension.lower()}_result_schema_version":
               values["result_schema_version"]
               for dimension, values in profile["dimensions"].items()},
        },
    }


def _create_candidate_job(case_dir: Path, files_dir: Path, *, tag: str) -> dict:
    path = files_dir / f"{tag}.docx"
    _write_docx(path, f"{SUBJECT}已完成实验室组件集成测试，组件共同产生预期结果。{tag}")
    with LocalWorkflow(case_dir) as workflow:
        _put_provenance(workflow)
        workflow.initialize_case(
            subject_legal_name=SUBJECT,
            subject_aliases=[],
            evidence_cutoff="2026-09-09T00:00:00Z",
            subject_source_basis=json.dumps({
                "kind": "field_reference", "path": "case:identity.json#/subject",
                "status": "claimed",
            }, ensure_ascii=False),
        )
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


def _proposal_response(request: dict) -> dict:
    candidate = {
        "quote": request["quote"],
        "quote_sha256": request["quote_sha256"],
        "locator": copy.deepcopy(request["locator"]),
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
        source_id=request["source_id"], candidate=candidate)
    return {
        "schema_version": "proposal_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {"producer_id": "e4-authorized-proposer",
                     "producer_kind": "authorized_human"},
        "output_schema": request["output_schema"],
        "candidates": [candidate],
    }


def _review_response(request: dict) -> dict:
    authorization = request["authorization"]
    citation = {key: copy.deepcopy(authorization[key]) for key in (
        "source_id", "blob_sha256", "projection_id", "locator", "quote_sha256")}
    return {
        "schema_version": "review_response.v2",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {"producer_id": "e4-authorized-reviewer",
                     "producer_kind": "authorized_human"},
        "output_schema": request["output_schema"],
        "decision": "supports",
        "evidence_class": "test_record",
        "findings": {
            "components_integrated_in_lab": True,
            "project_specific": True,
            "configuration_id": "CFG-E4-TRL4",
            "system_boundary": "实验室组件集成系统",
            "test_environment": "实验室",
            "test_method": "受控组件集成测试",
            "measured_results": "组件共同产生预期结果",
            "requirements_thresholds": "组件集成阈值已满足",
            "environment_kind": "laboratory",
        },
        "citations": [citation],
    }


def _prepare_consumed_review(case_dir: Path, files_dir: Path, *, tag: str) -> dict:
    job = _create_candidate_job(case_dir, files_dir, tag=tag)
    with LocalWorkflow(case_dir) as workflow:
        proposal_request = workflow.proposals.get_request(job["proposal_request_ids"][0])
        proposal = workflow.proposals.seal_response(
            proposal_request["request_id"], _proposal_response(proposal_request),
            source_mode="manual_import")
        consumed_proposal = workflow.proposals.consume_response(
            proposal["response_id"], worker_id="e4-prepare-proposal",
            catalog=build_catalog_from_wheel())
        review_request = workflow.reviews.get_request(
            consumed_proposal["materialization"]["review_request_ids"][0])
        review = workflow.reviews.seal_response(
            review_request["request_id"], _review_response(review_request),
            source_mode="manual_import")
        consumed_review = workflow.reviews.consume_response(
            review["response_id"], worker_id="e4-prepare-review")
        workflow.materialize_review_response(
            consumed_review["response_id"], worker_id="e4-prepare-materialize")
    return job


def test_v2_direct_job_identity_reuses_identical_input_and_separates_changed_request(
        tmp_path):
    case_dir = tmp_path / "case-v2-identity"
    first = _create_candidate_job(case_dir, tmp_path, tag="e4-v2-identity")
    with LocalWorkflow(case_dir) as workflow:
        request = workflow.proposals.get_request(first["proposal_request_ids"][0])
        spec = {
            "source_id": request["source_id"],
            "blob_sha256": request["blob_sha256"],
            "projection_id": request["projection_id"],
            "locator": copy.deepcopy(request["locator"]),
            "quote": request["quote"],
            "purpose": request["purpose"],
            "output_schema": request["output_schema"],
        }
        same = workflow.create_candidate_job(
            evaluation_inputs=_evaluation_inputs(), proposal_specs=[spec],
            catalog=build_catalog_from_wheel())
        changed = workflow.create_candidate_job(
            evaluation_inputs=_evaluation_inputs(),
            proposal_specs=[{**spec, "purpose": "同一原件的另一项受控候选用途"}],
            catalog=build_catalog_from_wheel())

        assert same["job_id"] == first["job_id"]
        assert changed["job_id"] != first["job_id"]
        assert workflow.status(first["job_id"])["input_digest"] == \
            first["input_digest"]
        assert workflow.store.count_workflow_jobs() == 2


@pytest.mark.parametrize("fault_point,expected_before_import_records", [
    ("after_attachment_blob_persisted", 0),
    ("after_attachment_import_record_persisted", 1),
])
def test_process_crash_after_attachment_persistence_recovers_same_attachment_once(
        tmp_path, fault_point, expected_before_import_records):
    case_dir = tmp_path / "case-a"
    attachment = tmp_path / "e4-attachment.docx"
    _write_docx(attachment, "E4附件：原件已持久但业务登记前硬退出。")
    processes: list[dict] = []

    first = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
        "fault_point": fault_point,
        "exit_code": CHILD_EXIT,
    }, processes)
    assert first.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(first)
    before = _case_snapshot(case_dir)
    assert before["counts"]["attachment_imports"] == 0
    assert before["counts"]["import_records"] == expected_before_import_records
    assert before["counts"]["sources"] == 0
    assert len(before["blob_ids"]) == 1

    second = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
    }, processes)
    assert second.returncode == 0, second.stderr
    _assert_child_loaded_current_source(second)
    after = _case_snapshot(case_dir)
    evidence = _save_evidence(
        case_dir, "E4-process-recovery-attachment", before=before, after=after,
        processes=processes)

    assert evidence.is_file()
    assert after["counts"]["attachment_imports"] == 1
    assert after["counts"]["attachment_aliases"] == 1
    assert after["counts"]["sources"] == 1
    assert after["counts"]["import_records"] == 1
    assert after["blob_ids"] == before["blob_ids"]
    journal = Journal(case_dir / "records.sqlite3")
    try:
        rows = journal._conn.execute(
            "SELECT task_key FROM tasks WHERE task_key LIKE 'attachment-import:%'").fetchall()
        assert len(rows) == 1
        task_key = rows[0]["task_key"]
        state = journal.task_state(task_key)
        assert state["state"] == "succeeded"
        assert state["external_actions"] == 0
        assert state["output_ref"].startswith("ATTALIAS::")
        assert len(journal.takeover_events(task_key)) == 1
    finally:
        journal.close()


def test_process_crash_after_attachment_alias_before_commit_closes_exact_task(tmp_path):
    case_dir = tmp_path / "case-alias-commit"
    attachment = tmp_path / "e4-alias-commit.docx"
    _write_docx(attachment, "E4附件：alias已落库但Journal封账前硬退出。")
    processes: list[dict] = []

    first = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
        "fault_point": "after_attachment_alias_persisted",
        "exit_code": CHILD_EXIT,
    }, processes)
    assert first.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(first)
    before = _case_snapshot(case_dir)
    assert before["counts"]["attachment_imports"] == 1
    assert before["counts"]["attachment_aliases"] == 1
    assert before["counts"]["sources"] == 1
    assert before["counts"]["import_records"] == 1

    second = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
    }, processes)
    assert second.returncode == 0, second.stderr
    _assert_child_loaded_current_source(second)
    after = _case_snapshot(case_dir)
    evidence = _save_evidence(
        case_dir, "E4-attachment-alias-before-commit", before=before,
        after=after, processes=processes)

    assert evidence.is_file()
    assert after["counts"] == before["counts"]
    journal = Journal(case_dir / "records.sqlite3")
    try:
        row = journal._conn.execute(
            "SELECT task_key FROM tasks WHERE task_key LIKE 'attachment-import:%'"
        ).fetchone()
        assert row is not None
        state = journal.task_state(row["task_key"])
        assert state["state"] == "succeeded"
        assert state["output_ref"].startswith("ATTALIAS::")
        assert len(journal.attempts(row["task_key"])) == 2
        assert len(journal.takeover_events(row["task_key"])) == 1
    finally:
        journal.close()


@pytest.mark.parametrize("tamper", ["note", "alias_import", "source_blob", "source_import"])
def test_attachment_alias_commit_recovery_rejects_rewritten_import_audit(tmp_path, tamper):
    case_dir = tmp_path / "case-alias-tamper"
    attachment = tmp_path / "e4-alias-tamper.docx"
    _write_docx(attachment, "E4附件：篡改审计记录不得封账。")
    processes: list[dict] = []
    first = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
        "fault_point": "after_attachment_alias_persisted",
        "exit_code": CHILD_EXIT,
    }, processes)
    assert first.returncode == CHILD_EXIT
    with sqlite3.connect(case_dir / "records.sqlite3") as connection:
        if tamper == "note":
            connection.execute(
                "UPDATE import_records SET note='forged-attachment-audit-note'")
        elif tamper == "alias_import":
            connection.execute("UPDATE attachment_aliases SET import_id=999999")
        elif tamper == "source_blob":
            connection.execute("UPDATE sources SET blob_sha256=?",
                               ("0" * 64,))
        else:
            connection.execute("UPDATE sources SET import_id=999999")

    second = _run_child(tmp_path, case_dir, "import", {
        "paths": [str(attachment)],
    }, processes)
    assert second.returncode != 0
    assert ("kind/path/blob/note" in second.stderr if tamper == "note"
            else "闭包不一致" in second.stderr)
    journal = Journal(case_dir / "records.sqlite3")
    try:
        row = journal._conn.execute(
            "SELECT task_key FROM tasks WHERE task_key LIKE 'attachment-import:%'"
        ).fetchone()
        assert row is not None
        assert journal.task_state(row["task_key"])["state"] == "claimed"
    finally:
        journal.close()


def test_active_local_claim_cannot_be_taken_over_and_persists_owner_lease(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        claim = journal.claim("e4-active", "first-worker", "input-e4-active")
        state = journal.task_state("e4-active")
        attempt = journal.attempts("e4-active")[0]

        assert state["owner_pid"] == os.getpid()
        assert state["claimed_at"]
        assert attempt["owner_pid"] == os.getpid()
        assert attempt["claimed_at"]
        with pytest.raises(CommitRejected, match="仍存活|owner_pid|接管"):
            journal.takeover_stale_claim(
                "e4-active", "second-worker", "input-e4-active",
                evidence="不得仅凭字符串接管活跃任务")
        assert journal.task_state("e4-active")["current_token"] == claim.token
        assert journal.takeover_events("e4-active") == []
    finally:
        journal.close()


def test_journal_migrates_legacy_task_lease_columns_without_rewriting_history(tmp_path):
    db_path = tmp_path / "legacy-journal.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript("""
            CREATE TABLE tasks (
                task_key TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                current_token INTEGER,
                current_worker TEXT,
                input_id TEXT NOT NULL,
                output_ref TEXT,
                external_actions INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT '2000-01-01T00:00:00Z'
            );
            CREATE TABLE task_attempts (
                attempt_no INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                token INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                input_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT '2000-01-01T00:00:00Z'
            );
            CREATE TABLE token_sequence (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE takeover_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                evidence TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '2000-01-01T00:00:00Z'
            );
            INSERT INTO tasks(task_key,state,input_id) VALUES
                ('legacy-claimed','claimed','legacy-input');
            INSERT INTO task_attempts(task_key,token,worker_id,input_id,outcome)
                VALUES('legacy-claimed',1,'legacy-worker','legacy-input','claimed');
            INSERT INTO tasks(task_key,state,input_id,external_actions) VALUES
                ('legacy-dispatch','dispatch_recorded','legacy-dispatch-input',1);
            INSERT INTO task_attempts(task_key,token,worker_id,input_id,outcome)
                VALUES('legacy-dispatch',2,'legacy-worker',
                       'legacy-dispatch-input','dispatch_recorded');
        """)
        connection.commit()
    finally:
        connection.close()

    journal = Journal(db_path)
    try:
        state = journal.task_state("legacy-claimed")
        assert "owner_pid" in state and state["owner_pid"] is None
        assert "claimed_at" in state and state["claimed_at"] is None
        attempt = journal.attempts("legacy-claimed")[0]
        assert "owner_pid" in attempt and attempt["owner_pid"] is None
        assert "claimed_at" in attempt and attempt["claimed_at"] is None
        with pytest.raises(CommitRejected, match="owner_pid|历史|不可核验"):
            journal.takeover_stale_claim(
                "legacy-claimed", "new-worker", "legacy-input",
                evidence="旧任务没有可核验本地进程租约")
        fresh = journal.claim("fresh-after-migration", "fresh-worker", "fresh-input")
        assert journal.task_state(fresh.task_key)["owner_pid"] == os.getpid()
        assert journal.recover_dispatched_unknown() == []
        assert journal.task_state("legacy-dispatch")["state"] == \
            "dispatch_recorded"
        recovered = journal.recover_dispatched_unknown(
            manual_recovery_evidence="旧Journal无owner_pid，人工确认未获得外部结果")
        assert [row["task_key"] for row in recovered] == ["legacy-dispatch"]
        assert recovered[0]["recovery_judgment"] == \
            "legacy_missing_owner_pid_manual_evidence"
        assert journal.task_state("legacy-dispatch")["state"] == \
            "outcome_unknown"
        assert "manual_evidence" in journal.attempts("legacy-dispatch")[0]["detail"]
    finally:
        journal.close()


def test_process_sealed_proposal_and_review_resume_without_duplicate_requests_or_responses(
        tmp_path):
    case_dir = tmp_path / "case-b"
    job = _create_candidate_job(case_dir, tmp_path, tag="e4-b")
    processes: list[dict] = []
    with LocalWorkflow(case_dir) as workflow:
        proposal_request = workflow.proposals.get_request(job["proposal_request_ids"][0])
        proposal_response = _proposal_response(proposal_request)

    proposal_crash = _run_child(tmp_path, case_dir, "seal_proposal", {
        "request_id": proposal_request["request_id"],
        "response": proposal_response,
        "fault_point": "after_proposal_response_sealed",
        "exit_code": CHILD_EXIT,
    }, processes)
    assert proposal_crash.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(proposal_crash)
    before_proposal = _case_snapshot(case_dir)
    assert before_proposal["counts"]["proposal_requests"] == 1
    assert before_proposal["counts"]["proposal_responses"] == 1
    assert before_proposal["counts"]["review_requests"] == 0

    with LocalWorkflow(case_dir) as workflow:
        proposal_id = workflow.store.get_proposal_response_for_request(
            proposal_request["request_id"])["response_id"]
    proposal_resume = _run_child(tmp_path, case_dir, "consume_proposal", {
        "response_id": proposal_id,
    }, processes)
    assert proposal_resume.returncode == 0, proposal_resume.stderr
    _assert_child_loaded_current_source(proposal_resume)

    with LocalWorkflow(case_dir) as workflow:
        proposal = workflow.proposals.get_response(proposal_id)
        assert proposal["status"] == "consumed"
        review_request = workflow.reviews.get_request(
            proposal["materialization"]["review_request_ids"][0])
        review_response = _review_response(review_request)

    review_crash = _run_child(tmp_path, case_dir, "seal_review", {
        "request_id": review_request["request_id"],
        "response": review_response,
        "fault_point": "after_review_response_sealed",
        "exit_code": CHILD_EXIT,
    }, processes)
    assert review_crash.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(review_crash)
    sealed = _case_snapshot(case_dir)
    assert sealed["counts"]["review_requests"] == 1
    assert sealed["counts"]["review_responses"] == 1

    with LocalWorkflow(case_dir) as workflow:
        review_id = workflow.store.get_review_response_for_request(
            review_request["request_id"])["response_id"]
    review_resume = _run_child(tmp_path, case_dir, "consume_materialize_review", {
        "response_id": review_id,
    }, processes)
    assert review_resume.returncode == 0, review_resume.stderr
    _assert_child_loaded_current_source(review_resume)
    after = _case_snapshot(case_dir)
    evidence = _save_evidence(
        case_dir, "E4-process-recovery-responses", before=before_proposal,
        after=after, processes=processes)

    assert evidence.is_file()
    assert after["counts"]["proposal_requests"] == 1
    assert after["counts"]["proposal_responses"] == 1
    assert after["counts"]["review_requests"] == 1
    assert after["counts"]["review_responses"] == 1
    with LocalWorkflow(case_dir) as workflow:
        assert workflow.proposals.get_response(proposal_id)["status"] == "consumed"
        assert workflow.reviews.get_response(review_id)["status"] == "consumed"
        materialization = workflow.materialize_review_response(
            review_id, worker_id="e4-idempotent-materialize")
        assert workflow.store.get_dimension_evidence_review(
            materialization["review_id"]) is not None
        assert workflow.store.get_workflow_review_materialization(
            materialization["review_id"])["response_id"] == review_id
    journal = Journal(case_dir / "records.sqlite3")
    try:
        for task_key, input_id in (
                (f"proposal-consume:{proposal_id}", proposal_id.split("::", 1)[1]),
                (f"review-consume:{review_id}", review_id.split("::", 1)[1])):
            state = journal.task_state(task_key)
            assert state["state"] == "succeeded"
            assert state["external_actions"] == 0
            assert state["input_id"] == input_id
    finally:
        journal.close()


def test_process_crash_after_dimension_output_reuses_traced_output_and_freezes_artifacts(
        tmp_path):
    case_dir = tmp_path / "case-c"
    job = _prepare_consumed_review(case_dir, tmp_path, tag="e4-c")
    processes: list[dict] = []

    first = _run_child(tmp_path, case_dir, "run_job", {
        "job_id": job["job_id"],
        "fault_point": "after_dimension_output_registered:CRL",
        "exit_code": CHILD_EXIT,
    }, processes)
    assert first.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(first)
    before = _case_snapshot(case_dir)
    with LocalWorkflow(case_dir) as workflow:
        first_outputs = workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"])
        assert [item["dimension_id"] for item in first_outputs] == ["CRL"]
        crl_id = first_outputs[0]["result_id"]
        assert trace_crl_dimension(workflow.store, workflow.blobs, crl_id)["ok"] is True

    second = _run_child(tmp_path, case_dir, "run_job", {"job_id": job["job_id"]},
                        processes)
    assert second.returncode == 0, second.stderr
    _assert_child_loaded_current_source(second)
    after = _case_snapshot(case_dir)
    evidence = _save_evidence(
        case_dir, "E4-process-recovery-dimensions", before=before, after=after,
        processes=processes)

    assert evidence.is_file()
    with LocalWorkflow(case_dir) as workflow:
        status = workflow.status(job["job_id"])
        outputs = workflow.store.fetch_workflow_job_dimension_outputs(job["job_id"])
        assert status["state"] == "completed"
        assert {item["dimension_id"] for item in outputs} == {
            "CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL"}
        assert len({item["result_id"] for item in outputs}) == 6
        assert next(item for item in outputs if item["dimension_id"] == "CRL")["result_id"] == crl_id
        for output in outputs:
            if output["dimension_id"] == "CRL":
                trace = trace_crl_dimension(workflow.store, workflow.blobs,
                                            output["result_id"])
            else:
                trace = trace_dimension_result(workflow.store, workflow.blobs,
                                               output["result_id"])
            assert trace["ok"] is True
        artifacts = workflow.store.get_workflow_job_artifacts(job["job_id"])
        assert artifacts is not None
        assert artifacts["manifest_id"].startswith("AGGMAN::")
        assert artifacts["view_id"].startswith("OFFLINE6::")


def test_process_dispatch_unknown_never_auto_consumes_or_redispatches(tmp_path):
    case_dir = tmp_path / "case-d"
    job = _create_candidate_job(case_dir, tmp_path, tag="e4-d")
    processes: list[dict] = []
    with LocalWorkflow(case_dir) as workflow:
        request = workflow.proposals.get_request(job["proposal_request_ids"][0])
        sealed = workflow.proposals.seal_response(
            request["request_id"], _proposal_response(request),
            source_mode="manual_import")
    task_key = f"proposal-consume:{sealed['response_id']}"
    input_id = sealed["response_digest"]

    dispatched = _run_child(tmp_path, case_dir, "dispatch_then_exit", {
        "task_key": task_key,
        "input_id": input_id,
        "exit_code": CHILD_EXIT,
    }, processes)
    assert dispatched.returncode == CHILD_EXIT
    _assert_child_loaded_current_source(dispatched)
    before = _case_snapshot(case_dir)
    journal = Journal(case_dir / "records.sqlite3")
    try:
        assert journal.task_state(task_key)["state"] == "dispatch_recorded"
    finally:
        journal.close()

    recovered = _run_child(tmp_path, case_dir, "status_job", {
        "job_id": job["job_id"],
    }, processes)
    assert recovered.returncode == 0, recovered.stderr
    _assert_child_loaded_current_source(recovered)
    journal = Journal(case_dir / "records.sqlite3")
    try:
        state = journal.task_state(task_key)
        assert state["state"] == "outcome_unknown"
        assert state["external_actions"] == 1
        with pytest.raises(CommitRejected, match="不可认领|unknown|终态"):
            journal.claim(task_key, "e4-forbidden-reclaim", input_id)
        with pytest.raises(CommitRejected, match="不允许.*接管|unknown|外部动作"):
            journal.takeover_stale_claim(
                task_key, "e4-forbidden-takeover", input_id, evidence="E4反例")
    finally:
        journal.close()

    consumed = _run_child(tmp_path, case_dir, "consume_proposal", {
        "response_id": sealed["response_id"],
    }, processes)
    assert consumed.returncode != 0
    assert "outcome_unknown" in consumed.stderr or "unknown" in consumed.stderr
    _assert_child_loaded_current_source(consumed)
    resumed = _run_child(tmp_path, case_dir, "run_job", {"job_id": job["job_id"]},
                         processes)
    assert resumed.returncode == 0, resumed.stderr
    _assert_child_loaded_current_source(resumed)
    after = _case_snapshot(case_dir)
    evidence = _save_evidence(
        case_dir, "E4-process-recovery-outcome-unknown", before=before,
        after=after, processes=processes)

    assert evidence.is_file()
    assert after["counts"]["claims"] == 0
    assert after["counts"]["proposal_responses"] == 1
    assert after["counts"]["review_requests"] == 0
    with LocalWorkflow(case_dir) as workflow:
        assert workflow.proposals.get_response(sealed["response_id"])["status"] == \
            "proposal_response_sealed"
        assert workflow.run_job(job["job_id"])["state"] == "proposal_response_sealed"
        assert workflow.resume_failed_job(job["job_id"])["state"] == \
            "proposal_response_sealed"


def test_active_dispatch_stays_recorded_until_owner_commits_late_result(tmp_path):
    case_dir = tmp_path / "case-active-dispatch"
    job = _create_candidate_job(case_dir, tmp_path, tag="e4-active-dispatch")
    with LocalWorkflow(case_dir) as workflow:
        request = workflow.proposals.get_request(job["proposal_request_ids"][0])
        sealed = workflow.proposals.seal_response(
            request["request_id"], _proposal_response(request),
            source_mode="manual_import")
        task_key = f"proposal-consume:{sealed['response_id']}"
        workflow.journal.ensure_task(task_key, sealed["response_digest"])
        claim = workflow.journal.claim(
            task_key, "e4-active-dispatch", sealed["response_digest"])
        workflow.journal.record_dispatch(claim)

        status = workflow.status(job["job_id"])
        state = workflow.journal.task_state(task_key)
        assert state["state"] == "dispatch_recorded"
        assert any(action["task_key"] == task_key
                   and action["state"] == "dispatch_recorded"
                   and "等待" in action["action"]
                   for action in status["manual_actions"])
        workflow.journal.commit(claim, sealed["response_id"])
        assert workflow.journal.task_state(task_key)["state"] == "succeeded"
