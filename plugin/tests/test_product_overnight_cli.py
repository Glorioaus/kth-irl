"""本地产品任务5：统一 CLI、核验包与 Codex Plugin 薄入口。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from kth_hybrid.aggregation_profiles import CURRENT_AGGREGATION_PROFILE_ID
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.evidence_permissions import build_evidence_use_license
from kth_hybrid.store import CaseStore


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC = PLUGIN_ROOT / "src"


def _run(*args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {str(SRC)!r});"
        "from kth_hybrid.cli import main;"
        "raise SystemExit(main(sys.argv[1:]))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap, *args],
        cwd=PLUGIN_ROOT, env=env, text=True, capture_output=True,
        encoding="utf-8", timeout=30,
    )
    if ok:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "错误" in result.stderr or "缺少" in result.stderr
    return result


def _json(result: subprocess.CompletedProcess[str]):
    return json.loads(result.stdout)


def _write_docx(path: Path) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("技术样机已经完成受控验证。")
    document.save(path)


def _case_and_projection(tmp_path: Path):
    case_dir = tmp_path / "case"
    created = _json(_run(
        "case", "create", "--case-dir", str(case_dir),
        "--subject-legal-name", "合成主体有限公司",
        "--subject-alias", "合成主体",
        "--evidence-cutoff", "2026-09-09T00:00:00Z",
        "--subject-source-basis", "synthetic:test",
    ))
    assert created["version"] == 1
    source_file = tmp_path / "source.docx"
    _write_docx(source_file)
    imported = _json(_run(
        "intake", "add", "--case-dir", str(case_dir),
        "--file", str(source_file), "--file", str(source_file),
    ))
    assert len(imported) == 2
    assert imported[0]["attachment_id"] == imported[1]["attachment_id"]
    projected = _json(_run(
        "project", "--case-dir", str(case_dir),
        "--source-id", imported[0]["source_id"],
    ))
    projection = next(item for item in projected if item["status"] == "projected")
    return case_dir, imported[0], projection


def _job_file(tmp_path: Path, imported: dict, projection: dict) -> Path:
    unit = {
        "scope_id": "UNIT-A", "subject_scope": "合成主体有限公司",
        "unit_kind": "company", "unit_label": "合成主体",
    }
    quote_digest = sha256_hex(projection["text"].encode("utf-8"))
    qualification_body = {
        "claim": {
            "claim_id": "CLAIM-SOURCE-1", "excerpt_sha256": quote_digest,
            "subject_scope": "合成主体有限公司",
        },
        "outcome": {"allowed_uses": ["technology_readiness"]},
    }
    qualification_view = {
        **qualification_body,
        "input_digest": sha256_hex(json.dumps(
            qualification_body, ensure_ascii=False,
            sort_keys=True).encode("utf-8")),
    }
    binding = {
        "review": {
            "review_id": "REV-SOURCE-1", "criterion_id": "TRL4-C1",
            "claim_id": "CLAIM-SOURCE-1", "quote_sha256": quote_digest,
            "evidence_class": "technical_validation",
            "subject_scope": "合成主体有限公司",
            "support_scope": "TRL4-C1",
        },
        "qualification_view": qualification_view,
    }
    license_item = build_evidence_use_license(
        dimension_id="TRL", result_id="DIMR2::TRL::" + "1" * 64,
        binding=binding,
        criterion={
            "criterion_id": "TRL4-C1", "dimension": "TRL",
            "evidence_classes": ["technical_validation"],
        },
        scope_id="UNIT-A",
    )
    payload = {
        "assessment_unit": unit,
        "profile_id": CURRENT_AGGREGATION_PROFILE_ID,
        "method_versions": {
            "candidate_method": "kth-local.candidate.v1",
            "qualification_method": "kth-hybrid.qualification.v2",
        },
        "review_specs": [{
            "source_id": imported["source_id"],
            "blob_sha256": imported["blob_sha256"],
            "projection_id": projection["projection_id"],
            "locator": projection["locator"], "quote": projection["text"],
            "dimension_id": "TRL", "criterion_id": "TRL4-C1",
            "scope_id": "UNIT-A", "license": license_item,
            "requested_use": "technology_readiness",
            "purpose": "判断引文是否支持指定TRL准则",
            "output_schema": "kth-local.review-output.trl.v1",
        }],
    }
    path = tmp_path / "job.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _created_job(tmp_path: Path):
    case_dir, imported, projection = _case_and_projection(tmp_path)
    job = _json(_run(
        "job", "create", "--case-dir", str(case_dir),
        "--input", str(_job_file(tmp_path, imported, projection)),
    ))
    return case_dir, imported, projection, job


def _valid_response(request: dict) -> dict:
    return {
        "schema_version": "review_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {
            "producer_id": "human-reviewer-01",
            "producer_kind": "authorized_human",
        },
        "output_schema": request["output_schema"],
        "decision": "supports",
        "evidence_class": "technical_validation",
        "findings": {"summary": "引文支持指定准则，后续仍由规则内核求值。"},
        "citations": [{
            key: request[key] for key in (
                "source_id", "blob_sha256", "projection_id", "locator",
                "quote_sha256")
        }],
    }


def test_cli_mechanical_path_waits_without_provider_and_never_selects_latest(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    job_id = job["job_id"]

    status = _json(_run(
        "status", "--case-dir", str(case_dir), "--job-id", job_id))
    assert status["job_id"] == job_id
    assert status["state"] == "awaiting_authorized_analysis"
    ran = _json(_run(
        "run", "--case-dir", str(case_dir), "--job-id", job_id))
    assert ran["state"] == "awaiting_authorized_analysis"
    assert ran["provider_calls"] == 0
    assert ran["next_action"] == "等待已授权的专业复核返回"

    _run("status", "--case-dir", str(case_dir), ok=False)
    _run("run", "--case-dir", str(case_dir), ok=False)
    _run("resume", "--case-dir", str(case_dir), ok=False)


def test_resume_requires_exact_failed_job_and_calls_controlled_resume(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    with CaseStore(case_dir / "records.sqlite3") as store:
        store.set_workflow_job_state(
            job["job_id"], "failed", failure={"message": "合成机械故障"})
    resumed = _json(_run(
        "resume", "--case-dir", str(case_dir), "--job-id", job["job_id"]))
    assert resumed["job_id"] == job["job_id"]
    assert resumed["state"] == "awaiting_authorized_analysis"
    _run(
        "resume", "--case-dir", str(case_dir), "--job-id", job["job_id"],
        ok=False,
    )


def test_review_commands_enforce_source_mode_and_lifecycle(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    listed = _json(_run(
        "review", "list", "--case-dir", str(case_dir),
        "--job-id", job["job_id"]))
    request = listed[0]
    request_file = tmp_path / "request.json"
    _run(
        "review", "export-request", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--output", str(request_file))
    assert json.loads(request_file.read_text(encoding="utf-8")) == request

    response_file = tmp_path / "response.json"
    response_file.write_text(
        json.dumps(_valid_response(request), ensure_ascii=False), encoding="utf-8")
    _run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "runtime_provider", ok=False)
    simulated = _valid_response(request)
    simulated["producer"] = {
        "producer_id": "simulator-01", "producer_kind": "simulated"}
    response_file.write_text(json.dumps(simulated, ensure_ascii=False), encoding="utf-8")
    _run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "simulated", ok=False)

    response_file.write_text(
        json.dumps(_valid_response(request), ensure_ascii=False), encoding="utf-8")
    sealed = _json(_run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "manual_import"))
    assert sealed["source_mode"] == "manual_import"
    assert sealed["status"] == "response_sealed"
    consumed = _json(_run(
        "review", "consume", "--case-dir", str(case_dir),
        "--response-id", sealed["response_id"], "--worker-id", "cli-test"))
    assert consumed["status"] == "consumed"


def test_review_simulated_requires_both_explicit_mode_and_switch(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    request = job["review_requests"][0]
    response = _valid_response(request)
    response["producer"] = {
        "producer_id": "simulator-01", "producer_kind": "simulated"}
    response_file = tmp_path / "simulated.json"
    response_file.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    sealed = _json(_run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "simulated", "--allow-simulated"))
    assert sealed["source_mode"] == "simulated"


def test_trace_routes_exact_workflow_ids_and_rejects_missing_objects(tmp_path):
    case_dir, imported, projection, job = _created_job(tmp_path)
    request = job["review_requests"][0]
    response_file = tmp_path / "response.json"
    response_file.write_text(
        json.dumps(_valid_response(request), ensure_ascii=False), encoding="utf-8")
    response = _json(_run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "manual_import"))

    exact_ids = {
        imported["source_id"]: "source",
        projection["projection_id"]: "projection",
        request["request_id"]: "review_request",
        response["response_id"]: "review_response",
    }
    for object_id, kind in exact_ids.items():
        traced = _json(_run(
            "trace", "--case-dir", str(case_dir), "--result-id", object_id))
        assert traced["ok"] is True
        assert traced["kind"] == kind
        assert traced["object_id"] == object_id

    for missing_id, expected in [
        ("CRLR2A::" + "0" * 64, "CRL"),
        ("DIMR2::BRL::" + "0" * 64, "维度"),
        ("MISSING", "判据"),
        ("PROJ::" + "0" * 64, "投影"),
    ]:
        result = _run(
            "trace", "--case-dir", str(case_dir), "--result-id", missing_id,
            ok=False)
        assert expected in result.stdout + result.stderr


def test_export_creates_new_verification_package_with_verified_hash_manifest(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    output = tmp_path / "verification-package"
    exported = _json(_run(
        "export", "--case-dir", str(case_dir),
        "--job-id", job["job_id"], "--output-dir", str(output)))
    assert exported["artifact_kind"] == "核验包"
    assert exported["job_id"] == job["job_id"]
    manifest_path = output / "文件SHA256.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "核验包"
    names = {item["path"] for item in manifest["files"]}
    assert {
        "job.json", "status.json", "sources.json", "projections.json",
        "review-requests.json", "review-responses.json",
    } <= names
    for item in manifest["files"]:
        data = (output / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    _run(
        "export", "--case-dir", str(case_dir),
        "--job-id", job["job_id"], "--output-dir", str(output), ok=False)


def test_json_inputs_and_outputs_have_stable_chinese_errors(tmp_path):
    case_dir = tmp_path / "case"
    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    result = _run(
        "job", "create", "--case-dir", str(case_dir),
        "--input", str(broken), ok=False)
    assert "JSON" in result.stderr
    assert "Traceback" not in result.stderr


def test_console_entrypoint_and_single_plugin_manifest_are_declared():
    pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'kth-local = "kth_hybrid.cli:main"' in pyproject
    manifests = list(PLUGIN_ROOT.rglob(".codex-plugin/plugin.json"))
    assert manifests == [PLUGIN_ROOT / ".codex-plugin" / "plugin.json"]
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_ROOT.name
    assert "TODO" not in json.dumps(manifest, ensure_ascii=False)
    command = (PLUGIN_ROOT / "commands" / "kth-local.md").read_text(encoding="utf-8")
    assert "kth-local" in command
    assert "python -m kth_hybrid.cli" not in command
    assert "正式" not in command or "非正式" in command
