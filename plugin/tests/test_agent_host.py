"""AG1宿主桥：只替代外部语义返回，持久化和恢复均走产品服务。"""

import copy
import importlib
import importlib.util
import sqlite3

import pytest


def session_at(path):
    assert importlib.util.find_spec("kth_hybrid.product_session") is not None, (
        "AG1产品入口尚未实现")
    return importlib.import_module("kth_hybrid.product_session").ProductSession(path)


def submission(tmp_path):
    material = tmp_path / "材料.txt"
    material.write_text("产品甲面向客户甲，仍需确认评估边界。", encoding="utf-8")
    return {
        "schema_version": "ag1.submission.v1",
        "question": "是否进入有上限的技术验证？",
        "attachments": [str(material.resolve())],
        "host": {
            "name": "codex", "version": "offline-test",
            "session_id": "simulated-session",
            "capabilities": {
                "file_read": True, "code_execution": True,
                "isolated_contexts": True, "execution_records": True,
            },
        },
        "mode": "offline",
    }


def started_task(session, run_id, context_id="simulated-context-1"):
    task = session.prepare_task(
        run_id, role="scope_discovery", payload={"instruction": "识别单元候选"})
    ticket = session.begin_task(
        task["task_id"], context_id=context_id, source_mode="simulated")
    envelope = {
        **{key: ticket[key] for key in (
            "task_id", "run_id", "input_digest", "role", "context_id",
            "source_mode", "token", "started_at")},
        **{key: ticket[key] for key in ("case_id", "dispatch_id")},
        "schema_version": "ag1.host-result.v1",
        "ended_at": ticket["started_at"],
        "execution_status": "succeeded",
        "output": {"candidates": [], "unknowns": ["尚无明确单元证据"]},
        "usage": {"input_tokens": None, "output_tokens": None},
    }
    return task, envelope


def test_unconfirmed_submission_has_no_professional_tasks(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        assert session.status(run["run_id"])["assessment_status"] == (
            "awaiting_scope_confirmation")
        assert session.pending_tasks(run["run_id"]) == []
        assert session.advance(run["run_id"])["assessment_status"] == (
            "awaiting_scope_confirmation")


def test_missing_capability_is_specific_and_zero_dispatch(tmp_path):
    request = submission(tmp_path)
    request["host"]["capabilities"]["execution_records"] = False
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(request)
        state = session.status(run["run_id"])
        assert state["assessment_status"] == "blocked"
        assert state["blockers"] == ["missing_capability:execution_records"]
        assert session.pending_tasks(run["run_id"]) == []
        with pytest.raises(ValueError, match="blocked"):
            session.prepare_task(run["run_id"], role="scope_discovery", payload={})


@pytest.mark.parametrize("changed,value", [
    ("task_id", "OTHER"), ("run_id", "OTHER"), ("role", "professional_review"),
    ("input_digest", "0" * 64), ("context_id", "another-context"),
    ("source_mode", "host_agent"), ("token", -1),
])
def test_response_cannot_cross_dispatch_binding(tmp_path, changed, value):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        response[changed] = value
        with pytest.raises(ValueError, match="binding"):
            session.submit_host_result(task["task_id"], response)
        assert session.status(run["run_id"])["tasks"][0]["state"] == "dispatch_recorded"


@pytest.mark.parametrize("mode", [
    "host_agent", "runtime_provider", "manual_import", "authorized_human",
])
def test_offline_permission_never_dispatches_other_source_modes(tmp_path, mode):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task = session.prepare_task(run["run_id"], role="scope_discovery", payload={})
        with pytest.raises(ValueError, match="authorization"):
            session.begin_task(task["task_id"], context_id="ctx", source_mode=mode)
        assert session.status(run["run_id"])["tasks"][0]["state"] == "planned"


def test_seal_consume_and_reopen_are_idempotent(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        sealed = session.submit_host_result(task["task_id"], response)
        assert sealed["source_mode"] == "simulated"
        assert sealed["usage"]["input_tokens"] is None
        assert session.submit_host_result(task["task_id"], response) == sealed
        first = session.advance(run["run_id"])
        assert first["tasks"][0]["state"] == "consumed"
    with session_at(tmp_path / "case") as reopened:
        assert reopened.advance(run["run_id"]) == first
        assert len(reopened.journal.attempts(task["task_id"])) == 1
        with pytest.raises(ValueError, match="already_dispatched"):
            reopened.begin_task(
                task["task_id"], context_id="new", source_mode="simulated")


def test_failed_execution_cannot_carry_business_output(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        response["execution_status"] = "failed"
        with pytest.raises(ValueError, match="failed_output"):
            session.submit_host_result(task["task_id"], response)
        response["output"] = None
        session.submit_host_result(task["task_id"], response)
        state = session.advance(run["run_id"])
        assert state["assessment_status"] == "execution_failed"
        assert state["tasks"][0]["professional_result"] is None


def test_terminal_response_replacement_and_self_verified_are_rejected(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        extra = {**response, "verified": True}
        with pytest.raises(ValueError, match="fields"):
            session.submit_host_result(task["task_id"], extra)
        session.submit_host_result(task["task_id"], response)
        changed = copy.deepcopy(response)
        changed["output"]["unknowns"] = []
        with pytest.raises(ValueError, match="immutable"):
            session.submit_host_result(task["task_id"], changed)


def test_persisted_body_tampering_is_detected_on_read(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
    with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
        db.execute("UPDATE product_objects SET body_json='{}' WHERE object_id=?",
                   (run["run_id"],))
    with session_at(tmp_path / "case") as reopened:
        with pytest.raises(ValueError, match="integrity"):
            reopened.status(run["run_id"])


def test_unknown_task_is_never_silently_retried(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, _ = started_task(session, run["run_id"])
        state = session.journal.task_state(task["task_id"])
        from kth_hybrid.journal import Claim
        attempt = next(item for item in session.journal.attempts(task["task_id"])
                       if item["token"] == state["current_token"])
        claim = Claim(task["task_id"], state["current_worker"],
                      state["current_token"], state["input_id"], attempt["attempt_no"])
        session.journal.record_failure(claim, "模拟外部结果未知", outcome_unknown=True)
        with pytest.raises(ValueError, match="already_dispatched"):
            session.begin_task(task["task_id"], context_id="again", source_mode="simulated")
        assert session.status(run["run_id"])["tasks"][0]["state"] == "outcome_unknown"


def test_same_submission_in_different_cases_has_distinct_dispatch_identity(tmp_path):
    request = submission(tmp_path)
    with session_at(tmp_path / "case-a") as first, session_at(tmp_path / "case-b") as second:
        run_a = first.prepare_submission(request)
        run_b = second.prepare_submission(request)
        assert run_a["run_id"] != run_b["run_id"]
        task_a, response_a = started_task(first, run_a["run_id"])
        task_b, _ = started_task(second, run_b["run_id"])
        assert task_a["task_id"] != task_b["task_id"]
        with pytest.raises(ValueError, match="binding"):
            second.submit_host_result(task_b["task_id"], response_a)


@pytest.mark.parametrize("operation", ["pending_tasks", "status", "advance"])
def test_task_index_corruption_cannot_move_a_task_to_another_run(tmp_path, operation):
    request = submission(tmp_path)
    with session_at(tmp_path / "case") as session:
        first = session.prepare_submission(request)
        second = session.prepare_submission({**request, "question": "另一个问题"})
        task, response = started_task(session, first["run_id"])
        if operation != "pending_tasks":
            session.submit_host_result(task["task_id"], response)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute("UPDATE product_objects SET run_id=? WHERE object_id=?",
                       (second["run_id"], task["task_id"]))
        with pytest.raises(ValueError, match="integrity"):
            getattr(session, operation)(second["run_id"])
        assert session.store.list_product_objects(second["run_id"], "consumption") == []


@pytest.mark.parametrize("where", ["payload", "output"])
def test_nested_final_judgments_are_rejected_as_semantic_fields(tmp_path, where):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        prohibited = {"candidates": [{"final_decision": "FORBIDDEN_TEST_VALUE",
                                     "maturity_level": 9, "verified": True}]}
        if where == "payload":
            with pytest.raises(ValueError, match="forbidden"):
                session.prepare_task(
                    run["run_id"], role="scope_discovery", payload=prohibited)
        else:
            task, response = started_task(session, run["run_id"])
            response["output"] = prohibited
            with pytest.raises(ValueError, match="forbidden"):
                session.submit_host_result(task["task_id"], response)


def test_invalid_large_context_has_zero_claim_side_effect(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task = session.prepare_task(run["run_id"], role="scope_discovery", payload={})
        with pytest.raises(ValueError):
            session.begin_task(
                task["task_id"], context_id="x" * (4 * 1024 * 1024 - 100),
                source_mode="simulated")
        assert session.journal.task_state(task["task_id"])["state"] == "planned"
        assert session.journal.attempts(task["task_id"]) == []
        assert session.begin_task(
            task["task_id"], context_id="short", source_mode="simulated")["token"] > 0


@pytest.mark.parametrize("value", [[], {}, None, True])
def test_wrong_enum_type_is_a_contract_error(tmp_path, value):
    request = submission(tmp_path)
    with session_at(tmp_path / "case") as session:
        with pytest.raises(ValueError):
            session.prepare_submission({**request, "mode": value})
        run = session.prepare_submission(request)
        task, response = started_task(session, run["run_id"])
        response["execution_status"] = value
        with pytest.raises(ValueError):
            session.submit_host_result(task["task_id"], response)


def mutate_product_object(case_path, object_id, mutate):
    import hashlib
    import json

    with sqlite3.connect(case_path / "records.sqlite3") as db:
        record = json.loads(db.execute(
            "SELECT body_json FROM product_objects WHERE object_id=?", (object_id,)
        ).fetchone()[0])
        body = (record["body"] if record.get("schema_version") == "ag1.product-record.v1"
                else record)
        mutate(body)
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        db.execute("UPDATE product_objects SET body_json=?,digest=? WHERE object_id=?",
                   (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), object_id))


@pytest.mark.parametrize("field,value", [
    ("contract_version", "UNKNOWN"),
    ("independence_status", "verified"),
])
def test_ticket_contract_corruption_is_rejected_at_read(tmp_path, field, value):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        session.submit_host_result(task["task_id"], response)
        mutate_product_object(
            tmp_path / "case", "DISPATCH::" + task["task_id"],
            lambda body: body.update({field: value}))
        with pytest.raises(ValueError, match="integrity"):
            session.advance(run["run_id"])


def test_consumption_status_must_match_sealed_response(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        session.submit_host_result(task["task_id"], response)
        session.advance(run["run_id"])
        mutate_product_object(
            tmp_path / "case", "CONSUMED::" + task["task_id"],
            lambda body: body.update(execution_status="failed"))
        with pytest.raises(ValueError, match="integrity"):
            session.status(run["run_id"])


@pytest.mark.parametrize("context", ["界" * 171, [], None, False])
def test_invalid_context_does_not_allocate_token_or_attempt(tmp_path, context):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task = session.prepare_task(run["run_id"], role="scope_discovery", payload={})
        with pytest.raises(ValueError):
            session.begin_task(task["task_id"], context_id=context, source_mode="simulated")
        state = session.journal.task_state(task["task_id"])
        assert state["state"] == "planned"
        assert state["current_token"] is None
        assert state["external_actions"] == 0
        assert session.journal.attempts(task["task_id"]) == []


@pytest.mark.parametrize("operation", ["pending_tasks", "status", "advance"])
def test_task_body_alias_cannot_hide_another_tasks_failure(tmp_path, operation):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task_a, response_a = started_task(session, run["run_id"])
        response_a.update(execution_status="failed", output=None)
        session.submit_host_result(task_a["task_id"], response_a)
        task_b = session.prepare_task(
            run["run_id"], role="scope_discovery", payload={"instruction": "不同任务"})
        assert session.status(run["run_id"])["assessment_status"] == "execution_failed"
        mutate_product_object(tmp_path / "case", task_a["task_id"],
                              lambda body: body.update(task_id=task_b["task_id"]))
        with pytest.raises(ValueError, match="integrity"):
            getattr(session, operation)(run["run_id"])
        assert session.store.list_product_objects(run["run_id"], "consumption") == []


def test_task_run_reassignment_is_rejected_before_run_filtering(tmp_path):
    import hashlib
    import json

    request = submission(tmp_path)
    with session_at(tmp_path / "case") as session:
        first = session.prepare_submission(request)
        second = session.prepare_submission(
            {**request, "question": "另一个问题"})
        task, _ = started_task(session, first["run_id"])
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            record = json.loads(db.execute(
                "SELECT body_json FROM product_objects WHERE object_id=?",
                (task["task_id"],),
            ).fetchone()[0])
            record["run_id"] = second["run_id"]
            record["body"]["run_id"] = second["run_id"]
            encoded = json.dumps(
                record, ensure_ascii=False, sort_keys=True, allow_nan=False)
            db.execute(
                "UPDATE product_objects SET run_id=?, body_json=?, digest=? "
                "WHERE object_id=?",
                (second["run_id"], encoded,
                 hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                 task["task_id"]),
            )
        with pytest.raises(ValueError, match="integrity"):
            session.status(first["run_id"])


def test_failed_terminal_task_without_response_is_integrity_failure(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        response["execution_status"] = "failed"
        response["output"] = None
        session.submit_host_result(task["task_id"], response)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute(
                "DELETE FROM product_objects WHERE object_id=?",
                ("RESPONSE::" + task["task_id"],),
            )
        with pytest.raises(ValueError, match="integrity"):
            session.status(run["run_id"])


def test_orphan_attempt_is_in_task_inventory_denominator(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, _ = started_task(session, run["run_id"])
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute(
                "DELETE FROM product_objects WHERE object_id=?",
                (task["task_id"],),
            )
            db.execute(
                "DELETE FROM tasks WHERE task_key=?",
                (task["task_id"],),
            )
        with pytest.raises(ValueError, match="integrity"):
            session.status(run["run_id"])


@pytest.mark.parametrize("operation", ["materials", "discovery_batches", "status"])
def test_public_product_reads_never_rebuild_missing_coverage(
        tmp_path, operation):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute(
                "DELETE FROM product_objects WHERE kind IN "
                "('coverage', 'coverage_part') AND run_id=?",
                (run["run_id"],),
            )
        before = session.store._conn.total_changes
        with pytest.raises((ValueError, RuntimeError), match="coverage"):
            getattr(session, operation)(run["run_id"])
        assert session.store._conn.total_changes == before
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            assert db.execute(
                "SELECT COUNT(*) FROM product_objects "
                "WHERE kind IN ('coverage', 'coverage_part') AND run_id=?",
                (run["run_id"],),
            ).fetchone()[0] == 0


def test_legacy_docx_run_is_blocked_but_same_case_revision_is_new(tmp_path):
    import docx

    from kth_hybrid.agent_host import digest
    from kth_hybrid.intake import freeze_product_inputs, project_product_materials
    from kth_hybrid.product_session import (
        CONTRACT_VERSION, LEGACY_MATERIAL_CONTRACT_VERSION,
        MATERIAL_CONTRACT_VERSION,
    )

    path = tmp_path / "legacy.docx"
    document = docx.Document()
    document.add_paragraph("旧结构投影")
    document.save(path)
    with session_at(tmp_path / "case") as session:
        request = submission(tmp_path)
        request["attachments"] = [str(path.resolve())]
        inputs = freeze_product_inputs(request["attachments"], session.blobs)
        run_id = "RUN::" + digest({
            "case_id": session.case_id, "request": request, "inputs": inputs,
            "contract_version": CONTRACT_VERSION,
            "product_contract": LEGACY_MATERIAL_CONTRACT_VERSION,
        })
        legacy = {
            "run_id": run_id, "contract_version": CONTRACT_VERSION,
            "case_id": session.case_id,
            "product_contract": LEGACY_MATERIAL_CONTRACT_VERSION,
            "request": request, "blockers": [], "inputs": inputs,
        }
        session.store.put_product_object(
            run_id, run_id=run_id, kind="submission", body=legacy)
        project_product_materials(
            session.workflow, run_id, inputs, require_existing=False)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute(
                "UPDATE text_projections SET tool=?",
                ("python-docx 1.2.0; ag1-docx-structure.v2",),
            )
        state = session.status(run_id)
        assert state["assessment_status"] == "blocked"
        assert any("legacy_extraction_contract" in item
                   for item in state["blockers"])

        revision = submission(tmp_path)
        revision["revision"] = {
            "parent_run_id": run_id,
            "reason": "按新结构提取合同重新冻结材料",
        }
        new_run = session.prepare_submission(revision)
        assert new_run["product_contract"] == MATERIAL_CONTRACT_VERSION
        assert new_run["run_id"] != run_id


def test_empty_consumption_is_corruption_not_an_absent_record(tmp_path):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        session.submit_host_result(task["task_id"], response)
        session.advance(run["run_id"])
        mutate_product_object(tmp_path / "case", "CONSUMED::" + task["task_id"],
                              lambda body: body.clear())
        with pytest.raises(ValueError, match="integrity"):
            session.status(run["run_id"])


@pytest.mark.parametrize("kind", [
    "submission", "task", "dispatch", "response", "consumption",
    "scope_candidate", "scope", "coverage", "coverage_part", "unregistered",
])
def test_product_record_types_reject_empty_or_unregistered_writes(tmp_path, kind):
    with session_at(tmp_path / "case") as session:
        with pytest.raises(ValueError):
            session.store.put_product_object(
                "INVALID", run_id="RUN::missing", kind=kind, body={})
        assert session.store.get_product_object("INVALID") is None


def test_product_record_wraps_actual_key_run_and_kind_in_identity(tmp_path):
    import json

    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            saved = json.loads(db.execute(
                "SELECT body_json FROM product_objects WHERE object_id=?", (run["run_id"],)
            ).fetchone()[0])
        assert saved.get("schema_version") == "ag1.product-record.v1"
        assert saved["object_id"] == run["run_id"]
        assert saved["run_id"] == run["run_id"]
        assert saved["kind"] == "submission"
        assert saved["body"] == run


def test_registered_legacy_body_is_read_without_rewriting_it(tmp_path):
    import hashlib
    import json

    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        raw = json.dumps(run, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute("UPDATE product_objects SET body_json=?,digest=? WHERE object_id=?",
                       (raw, hashlib.sha256(raw.encode("utf-8")).hexdigest(), run["run_id"]))
        assert session.status(run["run_id"])["assessment_status"] == (
            "awaiting_scope_confirmation")
        assert session.store.put_product_object(
            run["run_id"], run_id=run["run_id"], kind="submission", body=run) == run
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            assert db.execute(
                "SELECT body_json FROM product_objects WHERE object_id=?", (run["run_id"],)
            ).fetchone()[0] == raw


@pytest.mark.parametrize("kind", [
    "submission", "task", "dispatch", "response", "consumption",
    "scope_candidate", "scope", "coverage", "coverage_part",
])
@pytest.mark.parametrize("damage", ["empty", "extra_field", "object_key"])
def test_every_record_kind_has_one_strict_read_boundary(tmp_path, kind, damage):
    from pathlib import Path
    from test_product_scope import TEXT, candidate, confirmation

    with session_at(tmp_path / "case") as session:
        request = submission(tmp_path)
        Path(request["attachments"][0]).write_text(TEXT, encoding="utf-8")
        run = session.prepare_submission(request)
        material = session.materials(run["run_id"])
        segment = material["segments"][0]
        task, response = started_task(session, run["run_id"])
        output = candidate()
        output["coverage_digest"] = material["coverage_digest"]
        output["units"][0]["citations"] = [{
            "segment_id": segment["segment_id"], "start": 0, "end": len(TEXT),
            "quote": TEXT,
        }]
        response["output"] = output
        session.submit_host_result(task["task_id"], response)
        proposed = session.propose_scope(run["run_id"], task_id=task["task_id"])
        chosen = confirmation()
        chosen.update(candidate_digest=proposed["candidate_digest"], units=output["units"])
        session.confirm_scope(run["run_id"], chosen)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            object_id = db.execute(
                "SELECT object_id FROM product_objects WHERE run_id=? AND kind=? "
                "ORDER BY object_id LIMIT 1", (run["run_id"], kind)
            ).fetchone()[0]
        if damage == "object_key":
            alias = "ALIASED::" + object_id
            with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
                db.execute("UPDATE product_objects SET object_id=? WHERE object_id=?",
                           (alias, object_id))
            object_id = alias
        else:
            mutate_product_object(
                tmp_path / "case", object_id,
                (lambda body: body.clear()) if damage == "empty"
                else (lambda body: body.update(unexpected_field=None)))
        with pytest.raises(ValueError, match="integrity"):
            session.store.get_product_object(object_id, run_id=run["run_id"], kind=kind)


@pytest.mark.parametrize("operation", ["status", "pending_tasks", "advance"])
def test_deleted_product_task_cannot_disappear_from_journal_denominator(tmp_path, operation):
    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        task, response = started_task(session, run["run_id"])
        response.update(execution_status="failed", output=None)
        session.submit_host_result(task["task_id"], response)
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            for key in (task["task_id"], "DISPATCH::" + task["task_id"],
                        "RESPONSE::" + task["task_id"]):
                db.execute("DELETE FROM product_objects WHERE object_id=?", (key,))
        assert session.journal.task_state(task["task_id"])["state"] == "failed"
        with pytest.raises(ValueError, match="integrity"):
            getattr(session, operation)(run["run_id"])
