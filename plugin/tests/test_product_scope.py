"""单元候选与集中确认的机械约束；不编写专业黄金答案。"""

import copy
import importlib
import importlib.util

import pytest

from test_agent_host import session_at, started_task, submission


TEXT = "甲公司的产品甲面向市场甲。乙公司的产品乙面向市场乙。"


def scope_module():
    assert importlib.util.find_spec("kth_hybrid.unit_scope") is not None, (
        "AG1单元范围校验尚未实现")
    return importlib.import_module("kth_hybrid.unit_scope")


def coverage():
    return {
        "coverage_digest": "d" * 64,
        "segments": [{"segment_id": "SEG-1", "text_sha256": "a" * 64}],
    }


def candidate():
    return {
        "coverage_digest": "d" * 64,
        "units": [{
            "unit_id": "unit-a", "name": "产品甲", "product": "产品甲",
            "market": "市场甲", "root_task": "进行技术验证",
            "system_boundary": "整机，不包含第三方生产线",
            "financing_entity": "甲公司", "subject": "甲公司",
            "disposition": "included", "absorbed_by": None, "reason": "原文明确",
            "citations": [{"segment_id": "SEG-1", "start": 0, "end": 14,
                           "quote": TEXT[:14]}],
        }],
    }


def confirmation():
    return {
        "candidate_digest": "c" * 64, "actor": "simulated-owner",
        "confirmed_at": "2026-09-18T08:00:00Z",
        "subject": {"legal_name": "甲公司", "aliases": []},
        "evidence_cutoff": "2026-09-17T23:59:59+08:00",
        "units": candidate()["units"], "action": None,
        "permissions": {"mode": "offline", "external_actions": False},
    }


def test_scope_requires_locatable_original_quote_not_title():
    module = scope_module()
    result = module.validate_candidate(candidate(), coverage(), lambda _: TEXT)
    assert result["units"][0]["market"] == "市场甲"
    bad = candidate()
    bad["units"][0]["citations"][0]["quote"] = "这个标题不能代替原文"
    with pytest.raises(ValueError, match="citation"):
        module.validate_candidate(bad, coverage(), lambda _: TEXT)


@pytest.mark.parametrize("mutation", ["foreign_segment", "wrong_coverage", "empty_quotes"])
def test_candidate_cannot_escape_material_boundary(mutation):
    module = scope_module()
    item = candidate()
    if mutation == "foreign_segment":
        item["units"][0]["citations"][0]["segment_id"] = "ANOTHER-RUN"
    elif mutation == "wrong_coverage":
        item["coverage_digest"] = "0" * 64
    else:
        item["units"][0]["citations"] = []
    with pytest.raises(ValueError):
        module.validate_candidate(item, coverage(), lambda _: TEXT)


@pytest.mark.parametrize("mutation", ["unresolved", "wrong_subject", "missing_unit",
                                     "bad_absorption", "external_permission"])
def test_confirmation_cannot_silently_drop_or_merge_units(mutation):
    module = scope_module()
    proposed = {**candidate(), "candidate_digest": "c" * 64}
    item = confirmation()
    if mutation == "unresolved":
        item["units"][0]["disposition"] = "unresolved"
    elif mutation == "wrong_subject":
        item["units"][0]["subject"] = "乙公司"
    elif mutation == "missing_unit":
        item["units"] = []
    elif mutation == "bad_absorption":
        item["units"][0].update(disposition="absorbed", absorbed_by="missing")
    else:
        item["permissions"]["external_actions"] = True
    with pytest.raises(ValueError):
        module.validate_confirmation(item, proposed, coverage(), lambda _: TEXT)


def test_confirmation_records_original_and_user_changes_without_qualification():
    module = scope_module()
    proposed = {**candidate(), "candidate_digest": "c" * 64}
    item = confirmation()
    item["units"][0]["market"] = "经用户确认的细分市场甲"
    result = module.validate_confirmation(item, proposed, coverage(), lambda _: TEXT)
    assert result["candidate_digest"] == "c" * 64
    assert result["changes"]["units"][0]["before"]["market"] == "市场甲"
    assert result["changes"]["units"][0]["after"]["market"] == "经用户确认的细分市场甲"
    assert "evidence_qualified" not in result


def test_product_path_material_discovery_confirmation_and_revision(tmp_path):
    with session_at(tmp_path / "case") as session:
        assert hasattr(session, "materials"), "产品尚未接线M2材料入口"
        request = submission(tmp_path)
        run = session.prepare_submission(request)
        materials = session.materials(run["run_id"])
        batches = session.discovery_batches(run["run_id"])
        assert len(batches) == 1
        segment = materials["segments"][0]
        text = session.blobs.read_bytes(segment["text_blob_sha256"]).decode("utf-8")
        output = candidate()
        output["coverage_digest"] = materials["coverage_digest"]
        output["units"][0]["citations"] = [{
            "segment_id": segment["segment_id"], "start": 0, "end": len(text),
            "quote": text,
        }]
        task, response = started_task(session, run["run_id"])
        response["output"] = output
        session.submit_host_result(task["task_id"], response)
        proposed = session.propose_scope(run["run_id"], task_id=task["task_id"])
        chosen = confirmation()
        chosen["candidate_digest"] = proposed["candidate_digest"]
        chosen["units"] = copy.deepcopy(output["units"])
        confirmed = session.confirm_scope(run["run_id"], chosen)
        assert session.status(run["run_id"])["assessment_status"] == "awaiting_research"
        assert session.confirm_scope(run["run_id"], chosen) == confirmed
        assert session.store.fetch_all("qualifications") == []
        changed = copy.deepcopy(chosen)
        changed["evidence_cutoff"] = "2026-09-18T07:00:00Z"
        with pytest.raises(ValueError, match="new_run"):
            session.confirm_scope(run["run_id"], changed)
        revised = copy.deepcopy(request)
        revised["revision"] = {"parent_run_id": run["run_id"], "reason": "补证"}
        second = session.prepare_submission(revised)
        assert second["run_id"] != run["run_id"]
        assert session.status(second["run_id"])["assessment_status"] == (
            "awaiting_scope_confirmation")


def test_source_tampering_invalidates_material_read(tmp_path):
    import sqlite3

    with session_at(tmp_path / "case") as session:
        run = session.prepare_submission(submission(tmp_path))
        coverage = session.materials(run["run_id"])
        source_id = coverage["segments"][0]["source_id"]
        with sqlite3.connect(tmp_path / "case" / "records.sqlite3") as db:
            db.execute("UPDATE sources SET blob_sha256=? WHERE source_id=?",
                       ("0" * 64, source_id))
        with pytest.raises((ValueError, RuntimeError)):
            session.materials(run["run_id"])


def test_changed_original_creates_new_run_and_old_run_reads_frozen_bytes(tmp_path):
    from pathlib import Path

    with session_at(tmp_path / "case") as session:
        request = submission(tmp_path)
        original = Path(request["attachments"][0])
        before = original.read_text(encoding="utf-8")
        first = session.prepare_submission(request)
        original.write_text("新的产品乙材料", encoding="utf-8")
        second = session.prepare_submission(request)
        assert first["run_id"] != second["run_id"]
        old_text = "".join(
            item["text"] for batch in session.discovery_batches(first["run_id"])
            for item in batch["items"])
        assert old_text == before
        assert "新的产品乙材料" == "".join(
            item["text"] for batch in session.discovery_batches(second["run_id"])
            for item in batch["items"])


def test_discovery_batches_do_not_truncate_long_unicode_material(tmp_path):
    from pathlib import Path

    with session_at(tmp_path / "case") as session:
        request = submission(tmp_path)
        original = "甲乙丙" * 23000
        Path(request["attachments"][0]).write_text(original, encoding="utf-8")
        run = session.prepare_submission(request)
        batches = session.discovery_batches(run["run_id"])
        assert len(batches) >= 3
        assert "".join(item["text"] for batch in batches
                       for item in batch["items"]) == original
        assert all(sum(len(item["text"]) for item in batch["items"]) <= 32768
                   for batch in batches)


def test_discovery_task_grants_frozen_objects_not_mutable_original_paths(tmp_path):
    import hashlib
    from pathlib import Path

    with session_at(tmp_path / "case") as session:
        request = submission(tmp_path)
        original = Path(request["attachments"][0])
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        run = session.prepare_submission(request)
        original.write_text("在提交后变更的未授权内容", encoding="utf-8")
        task = session.prepare_task(run["run_id"], role="scope_discovery", payload={})
        assert all(isinstance(item, dict) for item in task["allowed_materials"])
        assert task["allowed_materials"][0]["blob_sha256"] == original_hash
        assert str(original) not in str(task["allowed_materials"])
