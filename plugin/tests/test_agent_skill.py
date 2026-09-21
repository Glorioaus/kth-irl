"""从插件内部入口执行产品往返；不把离线CLI成功当Codex真实发现。"""

import json
import subprocess
import sys
from pathlib import Path

from test_agent_host import submission


PLUGIN = Path(__file__).resolve().parents[1]


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-I", "-B", str(PLUGIN / "scripts" / "kth-local.py"), *args],
        capture_output=True, text=True, encoding="utf-8", check=False)


def test_plugin_internal_entry_creates_and_recovers_same_run(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(submission(tmp_path)), encoding="utf-8")
    case = str(tmp_path / "case")
    created = invoke("agent", "prepare", "--case-dir", case, "--input", str(request))
    assert created.returncode == 0, created.stderr
    run_id = json.loads(created.stdout)["run_id"]
    status = invoke("agent", "status", "--case-dir", case, "--run-id", run_id)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["assessment_status"] == "awaiting_scope_confirmation"
    resumed = invoke("agent", "advance", "--case-dir", case, "--run-id", run_id)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout) == json.loads(status.stdout)


def test_cli_rejects_unknown_capability_types_without_creating_run(tmp_path):
    body = submission(tmp_path)
    body["host"]["capabilities"]["execution_records"] = "true"
    request = tmp_path / "bad.json"
    request.write_text(json.dumps(body), encoding="utf-8")
    result = invoke("agent", "prepare", "--case-dir", str(tmp_path / "case"),
                    "--input", str(request))
    assert result.returncode == 3
    assert "capabilities" in result.stderr


def test_full_task_roundtrip_across_separate_cli_processes(tmp_path):
    case = str(tmp_path / "case")
    body = tmp_path / "input.json"

    def call(operation, payload=None, *extra):
        if payload is not None:
            body.write_text(json.dumps(payload), encoding="utf-8")
        result = invoke("agent", operation, "--case-dir", case, *extra,
                        *(["--input", str(body)] if payload is not None else []))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    run = call("prepare", submission(tmp_path))
    task = call("prepare-task", {"role": "scope_discovery", "payload": {}},
                "--run-id", run["run_id"])
    ticket = call("begin-task", {"context_id": "isolated-test", "source_mode": "simulated"},
                  "--task-id", task["task_id"])
    # 仅保留返回合同字段；票据中的审计字段不混入语义返回。
    response = {key: ticket[key] for key in (
        "task_id", "run_id", "input_digest", "role", "context_id",
        "source_mode", "token", "started_at", "case_id", "dispatch_id")}
    response.update({
        "schema_version": "ag1.host-result.v1", "ended_at": ticket["started_at"],
        "execution_status": "succeeded", "output": {"candidates": []},
        "usage": {"input_tokens": None, "output_tokens": None},
    })
    call("submit", response, "--task-id", task["task_id"])
    state = call("advance", None, "--run-id", run["run_id"])
    assert state["tasks"][0]["state"] == "consumed"
    assert state["tasks"][0]["source_mode"] == "simulated"
    assert call("tasks", None, "--run-id", run["run_id"]) == []


def test_cli_deep_json_has_controlled_rejection(tmp_path):
    body = tmp_path / "deep.json"
    body.write_text("[" * 20000 + "0" + "]" * 20000, encoding="utf-8")
    result = invoke("agent", "prepare", "--case-dir", str(tmp_path / "case"),
                    "--input", str(body))
    assert result.returncode == 3
    assert "Traceback" not in result.stderr


def test_material_operations_route_to_product_service():
    from kth_hybrid.cli import _build_parser

    for operation in ("materials", "batches", "scope"):
        try:
            args = _build_parser().parse_args([
                "agent", operation, "--case-dir", "unused", "--run-id", "run"])
        except SystemExit:
            args = None
        assert args is not None, f"产品内部路由未接线: {operation}"
        assert args.agent_command == operation


def test_scope_confirmation_through_plugin_cli_and_fresh_process(tmp_path):
    from test_product_scope import TEXT, candidate, confirmation

    case = str(tmp_path / "case")
    input_file = tmp_path / "input.json"

    def call(operation, payload=None, *extra):
        if payload is not None:
            input_file.write_text(json.dumps(payload), encoding="utf-8")
        result = invoke("agent", operation, "--case-dir", case, *extra,
                        *(["--input", str(input_file)] if payload is not None else []))
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    request = submission(tmp_path)
    Path(request["attachments"][0]).write_text(TEXT, encoding="utf-8")
    run = call("prepare", request)
    material = call("materials", None, "--run-id", run["run_id"])
    batches = call("batches", None, "--run-id", run["run_id"])
    item = batches[0]["items"][0]
    task = call("prepare-task", {"role": "scope_discovery", "payload": {}},
                "--run-id", run["run_id"])
    ticket = call("begin-task", {"context_id": "scope-simulated", "source_mode": "simulated"},
                  "--task-id", task["task_id"])
    proposed = candidate()
    proposed["coverage_digest"] = material["coverage_digest"]
    proposed["units"][0]["citations"] = [{
        "segment_id": item["segment_id"], "start": 0,
        "end": len(item["text"]), "quote": item["text"],
    }]
    response = {key: ticket[key] for key in (
        "task_id", "run_id", "input_digest", "role", "context_id", "source_mode",
        "token", "started_at", "case_id", "dispatch_id")}
    response.update({
        "schema_version": "ag1.host-result.v1", "ended_at": ticket["started_at"],
        "execution_status": "succeeded", "output": proposed,
        "usage": {"input_tokens": None, "output_tokens": None},
    })
    call("submit", response, "--task-id", task["task_id"])
    proposal = call("propose-scope", None, "--run-id", run["run_id"],
                    "--task-id", task["task_id"])
    confirmed = confirmation()
    confirmed.update(candidate_digest=proposal["candidate_digest"], units=proposed["units"])
    scope = call("confirm-scope", confirmed, "--run-id", run["run_id"])
    assert call("scope", None, "--run-id", run["run_id"]) == scope
    assert call("status", None, "--run-id", run["run_id"])["assessment_status"] == (
        "awaiting_research")


def test_cli_material_integrity_failure_is_not_an_unhandled_traceback(tmp_path):
    import sqlite3
    from test_agent_host import mutate_product_object, session_at

    case_path = tmp_path / "case"
    with session_at(case_path) as session:
        run = session.prepare_submission(submission(tmp_path))
    with sqlite3.connect(case_path / "records.sqlite3") as db:
        part_id = db.execute(
            "SELECT object_id FROM product_objects WHERE kind='coverage_part' "
            "ORDER BY object_id LIMIT 1").fetchone()[0]
    mutate_product_object(case_path, part_id, lambda body: body.clear())
    result = invoke("agent", "materials", "--case-dir", str(case_path),
                    "--run-id", run["run_id"])
    assert result.returncode == 3
    assert "Traceback" not in result.stderr
    assert not result.stdout.strip()
