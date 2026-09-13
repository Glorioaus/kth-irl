"""本地产品任务5：统一 CLI、核验包与 Codex Plugin 薄入口。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import kth_hybrid.cli as cli_module
from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.aggregate import (
    build_offline_dimension_view,
    freeze_aggregation_manifest,
)
from kth_hybrid.proposal_requests import candidate_claim_id
from kth_hybrid.store import BlobStore, CaseStore
from test_product_overnight_profiles import profile_case as aggregation_case


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC = PLUGIN_ROOT / "src"
SUBJECT = "合成主体有限公司"
UNIT = {
    "scope_id": "UNIT-SYNTHETIC-CURRENT",
    "subject_scope": SUBJECT,
    "unit_kind": "company",
    "unit_label": "合成主体当前Case公司级单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-SYNTHETIC",
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


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _case_and_projection(tmp_path: Path):
    case_dir = tmp_path / "case"
    identity_file = tmp_path / "identity.json"
    units_file = tmp_path / "units.json"
    financing_file = tmp_path / "financing.json"
    frl_file = tmp_path / "frl.json"
    _write_json(identity_file, {"subject": SUBJECT})
    _write_json(units_file, {
        "scope_id": UNIT["scope_id"], "subject": SUBJECT,
        "kind": UNIT["unit_kind"], "label": UNIT["unit_label"],
    })
    _write_json(financing_file, {
        "entity": FINANCING["financing_entity_id"], "subject": SUBJECT,
        "units": FINANCING["assessment_unit_refs"],
    })
    _write_json(frl_file, {
        "planned": False, "entity": FINANCING["financing_entity_id"],
        "subject": SUBJECT,
    })
    created = _json(_run(
        "case", "create", "--case-dir", str(case_dir),
        "--subject-legal-name", SUBJECT,
        "--evidence-cutoff", "2026-09-09T00:00:00Z",
        "--subject-source-basis", json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    ))
    assert created["version"] == 1
    source_file = tmp_path / "source.docx"
    _write_docx(source_file)
    imported = _json(_run(
        "intake", "add", "--case-dir", str(case_dir),
        "--file", str(identity_file), "--file", str(units_file),
        "--file", str(financing_file), "--file", str(frl_file),
        "--file", str(source_file),
    ))
    source = next(item for item in imported
                  if item["origin_path"] == str(source_file))
    projected = _json(_run(
        "project", "--case-dir", str(case_dir),
        "--source-id", source["source_id"],
    ))
    projection = next(item for item in projected if item["status"] == "projected")
    return case_dir, source, projection


def _job_file(tmp_path: Path, imported: dict, projection: dict) -> Path:
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    payload = {
        "evaluation_inputs": {
            "assessment_unit": UNIT,
            "financing_entity": FINANCING,
            "frl_applicability": FRL_APPLICABILITY,
            "profile": {
                "profile_id": profile["profile_id"],
                "profile_digest": profile["profile_digest"],
            },
            "method_versions": {
                "candidate_proposal": "kth-local.candidate-proposal.v1",
                "qualification": "kth-hybrid.qualification.v4",
                "professional_review": "kth-local.professional-review.v2",
            },
        },
        "proposal_specs": [{
            "source_id": imported["source_id"],
            "blob_sha256": imported["blob_sha256"],
            "projection_id": projection["projection_id"],
            "locator": projection["locator"], "quote": projection["text"],
            "purpose": "从精确文本投影提出待资格核验的准则候选",
            "output_schema": "proposal_response.v1",
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


def _valid_proposal_response(request: dict) -> dict:
    candidate = {
        "quote": request["quote"],
        "quote_sha256": request["quote_sha256"],
        "locator": request["locator"],
        "interpretation": "该主体已完成受控技术样机验证。",
        "subject_scope": SUBJECT,
        "dimension_id": "TRL",
        "criterion_id": "TRL4-C1",
        "mapping": {
            "criterion_id": "TRL4-C1",
            "evidence_class": "test_record",
            "requested_use": "third_party_reported_fact",
        },
    }
    candidate["claim_id"] = candidate_claim_id(
        source_id=request["source_id"], candidate=candidate)
    return {
        "schema_version": "proposal_response.v1",
        "request_id": request["request_id"],
        "request_input_digest": request["request_input_digest"],
        "producer": {
            "producer_id": "human-proposer-01",
            "producer_kind": "authorized_human",
        },
        "output_schema": request["output_schema"],
        "candidates": [candidate],
    }


def test_cli_mechanical_path_waits_without_provider_and_never_selects_latest(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    job_id = job["job_id"]

    status = _json(_run(
        "status", "--case-dir", str(case_dir), "--job-id", job_id))
    assert status["job_id"] == job_id
    assert job_id.startswith("JOB2::")
    assert status["state"] == "awaiting_candidate_proposal"
    assert len(status["proposal_requests"]) == 1
    assert status["review_requests"] == []
    ran = _json(_run(
        "run", "--case-dir", str(case_dir), "--job-id", job_id))
    assert ran["state"] == "awaiting_candidate_proposal"
    assert ran["provider_calls"] == 0
    assert ran["next_action"] == "等待受控候选提出返回"

    _run("status", "--case-dir", str(case_dir), ok=False)
    _run("run", "--case-dir", str(case_dir), ok=False)
    _run("resume", "--case-dir", str(case_dir), ok=False)


def test_cli_rejects_legacy_v1_job_input_without_falling_back(tmp_path):
    case_dir, _, _, _job = _created_job(tmp_path)
    legacy_input = tmp_path / "legacy-job.json"
    legacy_input.write_text(json.dumps({
        "assessment_unit": {"scope_id": "UNIT-LEGACY"},
        "profile_id": "legacy-profile",
        "method_versions": {"legacy": "v1"},
        "review_specs": [],
    }, ensure_ascii=False), encoding="utf-8")

    rejected = _run(
        "job", "create", "--case-dir", str(case_dir),
        "--input", str(legacy_input), ok=False)

    assert "legacy_restricted" in rejected.stderr
    assert "workflow-job.v1" in rejected.stderr


def test_resume_requires_exact_failed_job_and_calls_controlled_resume(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    with CaseStore(case_dir / "records.sqlite3") as store:
        store.set_workflow_job_state(
            job["job_id"], "failed", failure={"message": "合成机械故障"})
    resumed = _json(_run(
        "resume", "--case-dir", str(case_dir), "--job-id", job["job_id"]))
    assert resumed["job_id"] == job["job_id"]
    assert resumed["state"] == "awaiting_candidate_proposal"
    _run(
        "resume", "--case-dir", str(case_dir), "--job-id", job["job_id"],
        ok=False,
    )


def test_review_commands_route_candidate_request_by_exact_id_and_lifecycle(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    listed = _json(_run(
        "review", "list", "--case-dir", str(case_dir),
        "--job-id", job["job_id"]))
    request = listed[0]
    assert request["request_id"].startswith("PROPOSALREQ::")
    request_file = tmp_path / "request.json"
    _run(
        "review", "export-request", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--output", str(request_file))
    assert json.loads(request_file.read_text(encoding="utf-8")) == request

    response_file = tmp_path / "response.json"
    response_file.write_text(
        json.dumps(_valid_proposal_response(request), ensure_ascii=False), encoding="utf-8")
    _run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "runtime_provider", ok=False)
    simulated = _valid_proposal_response(request)
    simulated["producer"] = {
        "producer_id": "simulator-01", "producer_kind": "simulated"}
    response_file.write_text(json.dumps(simulated, ensure_ascii=False), encoding="utf-8")
    _run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "simulated", ok=False)

    response_file.write_text(
        json.dumps(_valid_proposal_response(request), ensure_ascii=False), encoding="utf-8")
    sealed = _json(_run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "manual_import"))
    assert sealed["source_mode"] == "manual_import"
    assert sealed["status"] == "proposal_response_sealed"
    consumed = _json(_run(
        "review", "consume", "--case-dir", str(case_dir),
        "--response-id", sealed["response_id"], "--worker-id", "cli-test"))
    assert consumed["status"] == "consumed"


def test_review_simulated_requires_both_explicit_mode_and_switch(tmp_path):
    case_dir, _, _, job = _created_job(tmp_path)
    request = job["proposal_requests"][0]
    response = _valid_proposal_response(request)
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
    request = job["proposal_requests"][0]
    response_file = tmp_path / "response.json"
    response_file.write_text(
        json.dumps(_valid_proposal_response(request), ensure_ascii=False), encoding="utf-8")
    response = _json(_run(
        "review", "import", "--case-dir", str(case_dir),
        "--request-id", request["request_id"], "--response-file", str(response_file),
        "--source-mode", "manual_import"))

    exact_ids = {
        imported["source_id"]: "source",
        projection["projection_id"]: "projection",
        job["job_id"]: "workflow_job",
        request["request_id"]: "proposal_request",
        response["response_id"]: "proposal_response",
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


def test_trace_routes_manifest_and_view_from_controlled_case_audit_artifacts(
        aggregation_case):
    root, _basis, _catalog, results = aggregation_case
    with CaseStore(root / "records.sqlite3") as case:
        manifest = freeze_aggregation_manifest(
            case, BlobStore(root / "blobs"),
            {dimension: result["result_id"]
             for dimension, result in results.items()},
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)
        view = build_offline_dimension_view(
            case, BlobStore(root / "blobs"), manifest)
    audit = root / "audit"
    audit.mkdir(exist_ok=True)
    (audit / "aggregation-manifest-v2.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (audit / "offline-six-dimension-view-v3.json").write_text(
        json.dumps(view, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    traced_manifest = _json(_run(
        "trace", "--case-dir", str(root),
        "--result-id", manifest["manifest_id"]))
    assert traced_manifest["kind"] == "aggregation_manifest"
    assert traced_manifest["object_id"] == manifest["manifest_id"]
    assert traced_manifest["ok"] is True
    traced_view = _json(_run(
        "trace", "--case-dir", str(root), "--result-id", view["view_id"]))
    assert traced_view["kind"] == "offline_dimension_view"
    assert traced_view["object_id"] == view["view_id"]
    assert traced_view["manifest_id"] == manifest["manifest_id"]
    assert traced_view["ok"] is True

    missing_manifest = _run(
        "trace", "--case-dir", str(root),
        "--result-id", "AGGMAN::" + "0" * 64, ok=False)
    assert "aggregation manifest不存在" in missing_manifest.stderr
    missing_view = _run(
        "trace", "--case-dir", str(root),
        "--result-id", "OFFLINE6::" + "0" * 64, ok=False)
    assert "六维view不存在" in missing_view.stderr


def test_export_creates_new_verification_package_with_verified_hash_manifest(tmp_path):
    case_dir, imported, _, job = _created_job(tmp_path)
    unrelated = BlobStore(case_dir / "blobs").put_bytes(b"unrelated-case-material")
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
        "proposal-requests.json", "proposal-responses.json",
        "review-requests.json", "review-responses.json",
    } <= names
    source_index = json.loads((output / "sources.json").read_text(encoding="utf-8"))
    assert len(source_index) == 1
    assert source_index[0]["blob_sha256"] == imported["blob_sha256"]
    assert source_index[0]["export_blob_path"] == (
        f"blobs/{imported['blob_sha256']}.bin")
    assert f"blobs/{imported['blob_sha256']}.bin" in names
    assert f"blobs/{unrelated.sha256}.bin" not in names
    for item in manifest["files"]:
        data = (output / item["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]

    detached = tmp_path / "detached-verification-package"
    shutil.copytree(output, detached)
    shutil.move(str(case_dir), str(tmp_path / "case-unavailable"))
    detached_sources = json.loads(
        (detached / "sources.json").read_text(encoding="utf-8"))
    for source in detached_sources:
        blob = (detached / source["export_blob_path"]).read_bytes()
        assert len(blob) == source["byte_length"]
        assert hashlib.sha256(blob).hexdigest() == source["blob_sha256"]
    _run(
        "export", "--case-dir", str(tmp_path / "case-unavailable"),
        "--job-id", job["job_id"], "--output-dir", str(output), ok=False)


def test_json_inputs_and_outputs_have_stable_chinese_errors(tmp_path):
    case_dir = tmp_path / "case"
    _run(
        "case", "create", "--case-dir", str(case_dir),
        "--subject-legal-name", "合成主体有限公司",
        "--evidence-cutoff", "2026-09-09T00:00:00Z",
        "--subject-source-basis", "synthetic:test")
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


def test_plugin_launcher_is_self_contained_from_unrelated_cwd_without_pythonpath(
        tmp_path):
    launcher = PLUGIN_ROOT / "scripts" / "kth-local.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    unrelated_cwd = tmp_path / "unrelated-cwd"
    unrelated_cwd.mkdir()
    help_result = subprocess.run(
        [sys.executable, "-I", str(launcher), "--help"],
        cwd=unrelated_cwd, env=env, text=True, capture_output=True,
        encoding="utf-8", timeout=30)
    assert help_result.returncode == 0, help_result.stderr
    assert "kth-local" in help_result.stdout

    case_dir = tmp_path / "launcher-case"
    created = subprocess.run([
        sys.executable, "-I", str(launcher), "case", "create",
        "--case-dir", str(case_dir),
        "--subject-legal-name", "启动器主体有限公司",
        "--evidence-cutoff", "2026-09-10T00:00:00Z",
        "--subject-source-basis", "launcher:test",
    ], cwd=unrelated_cwd, env=env, text=True, capture_output=True,
        encoding="utf-8", timeout=30)
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["subject_legal_name"] == "启动器主体有限公司"
    assert (case_dir / "records.sqlite3").is_file()


def test_atomic_file_and_directory_publish_clean_up_after_fsync_failure(
        tmp_path, monkeypatch):
    file_target = tmp_path / "request.json"
    directory_target = tmp_path / "package"

    def fail_fsync(_fd):
        raise OSError("合成fsync故障")

    real_fsync = os.fsync
    monkeypatch.setattr(cli_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="fsync"):
        cli_module._atomic_write_file(file_target, b"request")
    assert not file_target.exists()
    assert not list(tmp_path.glob(".request.json.tmp-*"))
    with pytest.raises(OSError, match="fsync"):
        cli_module._atomic_publish_directory(
            directory_target, {"job.json": b"job"})
    assert not directory_target.exists()
    assert not list(tmp_path.glob(".package.tmp-*"))

    monkeypatch.setattr(cli_module.os, "fsync", real_fsync)
    cli_module._atomic_write_file(file_target, b"request")
    cli_module._atomic_publish_directory(
        directory_target, {"job.json": b"job"})
    assert file_target.read_bytes() == b"request"
    assert (directory_target / "job.json").read_bytes() == b"job"


def test_atomic_publish_removes_own_target_if_rename_reports_after_move(
        tmp_path, monkeypatch):
    real_rename = os.rename

    def move_then_fail(source, target):
        real_rename(source, target)
        raise OSError("合成rename后报告故障")

    monkeypatch.setattr(cli_module.os, "rename", move_then_fail)
    file_target = tmp_path / "request.json"
    with pytest.raises(OSError, match="rename"):
        cli_module._atomic_write_file(file_target, b"request")
    assert not file_target.exists()
    directory_target = tmp_path / "package"
    with pytest.raises(OSError, match="rename"):
        cli_module._atomic_publish_directory(
            directory_target, {"job.json": b"job"})
    assert not directory_target.exists()

    monkeypatch.setattr(cli_module.os, "rename", real_rename)
    cli_module._atomic_write_file(file_target, b"request")
    cli_module._atomic_publish_directory(
        directory_target, {"job.json": b"job"})
    assert file_target.exists() and directory_target.is_dir()


def test_review_request_and_verification_export_use_atomic_publish(
        tmp_path, monkeypatch):
    case_dir, _, _, job = _created_job(tmp_path)
    request = job["proposal_requests"][0]
    writes = []
    directories = []
    real_file = cli_module._atomic_write_file
    real_directory = cli_module._atomic_publish_directory

    def record_file(path, data):
        writes.append(Path(path))
        return real_file(path, data)

    def record_directory(path, files):
        directories.append((Path(path), sorted(files)))
        return real_directory(path, files)

    monkeypatch.setattr(cli_module, "_atomic_write_file", record_file)
    monkeypatch.setattr(cli_module, "_atomic_publish_directory", record_directory)
    request_path = tmp_path / "request.json"
    assert cli_module.main([
        "review", "export-request", "--case-dir", str(case_dir),
        "--request-id", request["request_id"],
        "--output", str(request_path)]) == 0
    package = tmp_path / "package"
    assert cli_module.main([
        "export", "--case-dir", str(case_dir), "--job-id", job["job_id"],
        "--output-dir", str(package)]) == 0
    assert writes == [request_path]
    assert directories and directories[0][0] == package


def test_audit_artifact_walk_rejects_symlink_without_following_it(tmp_path):
    case_dir = tmp_path / "case"
    audit = case_dir / "audit"
    outside = tmp_path / "outside"
    audit.mkdir(parents=True)
    outside.mkdir()
    object_id = "AGGMAN::" + "a" * 64
    target = outside / "manifest.json"
    target.write_text(json.dumps({
        "schema_version": "kth-hybrid.aggregation-manifest.v2",
        "manifest_id": object_id,
    }), encoding="utf-8")
    link = audit / "linked.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.fail(f"测试环境无法创建文件符号链接：{exc}")
    with pytest.raises(ValueError, match="符号链接"):
        cli_module._find_audit_artifact(
            case_dir, object_id=object_id, id_field="manifest_id",
            label="aggregation manifest",
            schema_prefix="kth-hybrid.aggregation-manifest.")


def test_audit_root_symlink_cannot_escape_case_and_match_external_artifact(
        tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    outside = tmp_path / "outside-audit"
    outside.mkdir()
    object_id = "AGGMAN::" + "d" * 64
    (outside / "manifest.json").write_text(json.dumps({
        "schema_version": "kth-hybrid.aggregation-manifest.v2",
        "manifest_id": object_id,
    }), encoding="utf-8")
    audit_link = case_dir / "audit"
    try:
        audit_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.fail(f"测试环境无法创建目录符号链接：{exc}")

    with pytest.raises(ValueError, match="audit根目录.*(?:符号链接|重解析|越界)"):
        cli_module._find_audit_artifact(
            case_dir, object_id=object_id, id_field="manifest_id",
            label="aggregation manifest",
            schema_prefix="kth-hybrid.aggregation-manifest.")


@pytest.mark.parametrize("limit_name,limit_value,expected", [
    ("MAX_AUDIT_DIRECTORY_DEPTH", 1, "深度"),
    ("MAX_AUDIT_TOTAL_ENTRIES", 1, "目录项"),
    ("MAX_AUDIT_JSON_FILES", 1, "JSON文件"),
    ("MAX_AUDIT_TOTAL_BYTES", 10, "累计字节"),
    ("MAX_AUDIT_ARTIFACT_BYTES", 10, "单件字节"),
])
def test_audit_artifact_walk_rejects_each_budget_before_unbounded_read(
        tmp_path, monkeypatch, limit_name, limit_value, expected):
    case_dir = tmp_path / limit_name
    deep = case_dir / "audit" / "one" / "two"
    deep.mkdir(parents=True)
    for index in range(2):
        (deep / f"{index}.json").write_text(
            json.dumps({"padding": "x" * 64}), encoding="utf-8")
    monkeypatch.setattr(cli_module, limit_name, limit_value)
    with pytest.raises(ValueError, match=expected):
        cli_module._find_audit_artifact(
            case_dir, object_id="AGGMAN::" + "b" * 64,
            id_field="manifest_id", label="aggregation manifest",
            schema_prefix="kth-hybrid.aggregation-manifest.")


def test_audit_entry_budget_stops_scandir_iterator_before_overread(
        tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    (case_dir / "audit").mkdir(parents=True)

    class FakeEntry:
        def __init__(self, index):
            self.name = f"entry-{index}"
            self.path = str(case_dir / "audit" / self.name)

        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def stat(*, follow_symlinks):
            class FakeStat:
                st_file_attributes = 0
            return FakeStat()

        @staticmethod
        def is_dir(*, follow_symlinks):
            return False

        @staticmethod
        def is_file(*, follow_symlinks):
            return False

    class GuardedEntries:
        def __init__(self):
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.index += 1
            if self.index > 3:
                raise AssertionError("目录项预算后仍在读取")
            return FakeEntry(self.index)

    monkeypatch.setattr(cli_module, "MAX_AUDIT_TOTAL_ENTRIES", 2)
    monkeypatch.setattr(cli_module.os, "scandir", lambda _path: GuardedEntries())
    with pytest.raises(ValueError, match="目录项"):
        cli_module._find_audit_artifact(
            case_dir, object_id="AGGMAN::" + "c" * 64,
            id_field="manifest_id", label="aggregation manifest",
            schema_prefix="kth-hybrid.aggregation-manifest.")
