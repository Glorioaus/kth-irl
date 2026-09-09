"""NIGHT二次限定整改：fresh-context剩余P1反例。"""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import shutil

import pytest

from kth_hybrid.aggregate import build_offline_dimension_view
from kth_hybrid import runner
from kth_hybrid.audit import trace_crl_dimension, trace_dimension_result
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.kernels.brl import RULE_REQUIREMENTS as BRL_REQUIREMENTS
from kth_hybrid.kernels.brl import evaluate_brl_dimension
from kth_hybrid.kernels.tmrl import RULE_REQUIREMENTS as TMRL_REQUIREMENTS
from kth_hybrid.kernels.tmrl import evaluate_tmrl_dimension
from kth_hybrid.kernels.trl import RULE_REQUIREMENTS as TRL_REQUIREMENTS
from kth_hybrid.kernels.trl import evaluate_trl_dimension
from kth_hybrid.roles import (
    confirm_role_candidate,
    role_candidate_digest,
    validate_role_attempt,
)
from kth_hybrid.store import BlobStore, CaseStore
from kth_hybrid.contracts import claim_content_digest, sha256_hex
from kth_hybrid.qualification import QualificationOutcome, qualify_claim


CATALOG = build_catalog_from_wheel()
SCOPE = "Company-A"
UNIT = {"scope_id": "UNIT-A", "subject_scope": SCOPE,
        "unit_kind": "material_unit", "unit_label": "Unit-A"}
BASE_CASE = os.environ.get("KTH_NIGHT_SECOND_FIX_BASE_CASE")


def _criterion(dimension: str, criterion_id: str) -> dict:
    return next(
        row for row in CATALOG["dimensions"][dimension]["registry"]["criteria"]
        if row["criterion_id"] == criterion_id)


def _result_row(result: dict, criterion_id: str) -> dict:
    return next(row for row in result["criteria"]
                if row["criterion_id"] == criterion_id)


@pytest.fixture
def case_copy(tmp_path):
    if not BASE_CASE:
        pytest.skip("未设置KTH_NIGHT_SECOND_FIX_BASE_CASE")
    source = Path(BASE_CASE)
    root = tmp_path / "case"
    root.mkdir()
    shutil.copy2(source / "records.sqlite3", root / "records.sqlite3")
    shutil.copytree(source / "blobs", root / "blobs")
    shutil.copytree(source / "audit", root / "audit")
    return root


def test_crl_detail_forgery_breaks_trace_and_manifest(case_copy):
    manifest = json.loads((case_copy / "audit" / "aggregation-manifest-v1.json").read_text(
        encoding="utf-8"))
    result_id = manifest["dimensions"]["CRL"]["result_id"]
    case = CaseStore(case_copy / "records.sqlite3")
    blobs = BlobStore(case_copy / "blobs")
    try:
        row = case.get_crl_dimension_result_by_id(result_id)
        payload = json.loads(blobs.read_bytes(row["result_blob_sha256"]).decode("utf-8"))
        assert trace_crl_dimension(case, blobs, result_id)["ok"]
        forged = copy.deepcopy(payload)
        target = next(item for item in forged["dimension"]["criteria"]
                      if item["criterion_id"] == "CRL1-C2")
        target.update(native_disposition="met", product_status="succeeded")
        forged_ref = blobs.put_bytes(json.dumps(
            forged, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        with case._conn:
            case._conn.execute(
                "UPDATE crl_dimension_results SET result_blob_sha256=? WHERE result_id=?",
                (forged_ref.sha256, result_id))
        traced = trace_crl_dimension(case, blobs, result_id)
        with pytest.raises(ValueError, match="trace|结果|manifest|求值"):
            build_offline_dimension_view(case, blobs, manifest)
    finally:
        case.close()
    assert not traced["ok"], traced


def _real_view_and_attempt() -> tuple[dict, dict]:
    if not BASE_CASE:
        pytest.skip("未设置KTH_NIGHT_SECOND_FIX_BASE_CASE")
    root = Path(BASE_CASE)
    view = json.loads((root / "audit" / "offline-six-dimension-view-v2.json").read_text(
        encoding="utf-8"))
    role = json.loads((root / "audit" / "offline-role-deliberation-v2.json").read_text(
        encoding="utf-8"))
    return view, role["pro"]


@pytest.mark.parametrize("statement", [
    "因此值得投资。", "该项目不值得投资。", "可以批准立项。", "建议拒绝立项。",
    "该项目可投资。", "YES", "NO", "CRL 为 4。", "TRL=7。",
])
def test_role_rejects_canonical_authority_phrases(statement):
    view, original = _real_view_and_attempt()
    attempt = copy.deepcopy(original)
    attempt["statement"] = statement
    attempt["candidate_digest"] = role_candidate_digest(attempt)
    with pytest.raises(ValueError, match="越权"):
        validate_role_attempt(attempt, view)


def test_role_rejects_tampered_view_and_license_with_old_ids():
    view, attempt = _real_view_and_attempt()
    tampered_view = copy.deepcopy(view)
    tampered_view["limitations"].append("伪造限制")
    with pytest.raises(ValueError, match="视图|摘要"):
        validate_role_attempt(attempt, tampered_view)

    tampered_license = copy.deepcopy(view)
    license_id = attempt["evidence_refs"][0]
    tampered_license["evidence_licenses"][license_id]["claim_id"] = "FORGED-CLAIM"
    with pytest.raises(ValueError, match="license|许可|视图|摘要"):
        validate_role_attempt(attempt, tampered_license)


def test_tampered_license_cannot_be_laundered_into_support_review():
    view, original = _real_view_and_attempt()
    tampered = copy.deepcopy(view)
    license_id = original["evidence_refs"][0]
    target = {
        "dimension_id": "BRL", "criterion_id": "BRL1-BM",
        "claim_id": "FORGED-CLAIM", "quote_sha256": "f" * 64,
        "evidence_class": "business_concept", "subject_scope": tampered["scope"],
        "scope_id": tampered["dimensions"]["BRL"]["scope_id"],
        "support_scope": "伪造许可支持BRL1-BM",
        "findings": {"business_idea_or_model_stated": True},
    }
    tampered["evidence_licenses"][license_id].update(
        dimension_id=target["dimension_id"], claim_id=target["claim_id"],
        quote_sha256=target["quote_sha256"], evidence_class=target["evidence_class"],
        subject_scope=target["subject_scope"], scope_id=target["scope_id"])
    candidate = copy.deepcopy(original)
    candidate.update(candidate_id="PRO-LAUNDERED", review_target=target)
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    confirmation = {
        "schema_version": "kth-hybrid.role-confirmation.v2",
        "confirmation_id": "CONF-LAUNDERED",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_view_id": tampered["view_id"],
        "input_digest": tampered["input_digest"],
        "producer_id": candidate["producer_id"], "role": candidate["role"],
        "case_basis_version": tampered["case_basis_version"],
        "decision": "supports", "reviewer": "human-reviewer",
        "review_basis": "伪造许可洗入反例",
        "evidence_refs": candidate["evidence_refs"], **target,
    }
    with pytest.raises(ValueError, match="license|许可|视图|摘要"):
        confirm_role_candidate(candidate, confirmation, dimension_id="BRL", view=tampered)


def test_role_requires_nonempty_evidence_refs():
    view, original = _real_view_and_attempt()
    attempt = copy.deepcopy(original)
    attempt["evidence_refs"] = []
    attempt["candidate_digest"] = role_candidate_digest(attempt)
    with pytest.raises(ValueError, match="引用|许可"):
        validate_role_attempt(attempt, view)


@pytest.mark.parametrize("nonfinite", [math.nan, math.inf, -math.inf])
def test_brl_store_and_kernel_reject_nonfinite_metrics(tmp_path, nonfinite):
    criterion = _criterion("BRL", "BRL8-BM")
    findings = {
        BRL_REQUIREMENTS["BRL8-BM"]: True,
        "operating_period": "2026-Q2", "metric_denominator": "all orders",
        "actual_metrics": {"profit": nonfinite, "growth": 2.0},
        "target_metrics": {"profit": 1.0, "growth": 1.0},
    }
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with pytest.raises(ValueError, match="有限|JSON|数值"):
            case.add_dimension_evidence_review(
                "BRL-NONFINITE", dimension_id="BRL", case_basis_version=1,
                claim_id="CLAIM-BRL", criterion_id="BRL8-BM",
                quote_sha256="a" * 64, decision="supports",
                evidence_class="operating_metrics", findings=findings,
                subject_scope=SCOPE, scope_id="UNIT-A", support_scope="BRL8-BM",
                reviewer="audit", review_basis="非有限数持久化反例")
    finally:
        case.close()
    review = {"review_id": "BRL-NONFINITE", "criterion_id": "BRL8-BM",
              "claim_id": "CLAIM-BRL", "decision": "supports",
              "evidence_class": "operating_metrics", "findings": findings,
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "内核二次防御", "support_scope": "BRL8-BM"}
    result = evaluate_brl_dimension(
        CATALOG["dimensions"]["BRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _result_row(result, "BRL8-BM")["native_disposition"] != "met"


@pytest.mark.parametrize("evidence_class", [
    "longitudinal_operation", "independent_operation_record"])
def test_trl9_all_allowed_classes_require_two_users_and_period(evidence_class):
    criterion = _criterion("TRL", "TRL9-C1")
    base = {
        TRL_REQUIREMENTS["TRL9-C1"]: True,
        "project_specific": True, "configuration_id": "CFG-A",
        "system_boundary": "complete System-A", "test_environment": "actual use",
        "test_method": "field observation", "measured_results": "bounded result",
        "requirements_thresholds": "declared thresholds",
        "environment_kind": "actual_operation",
    }
    def evaluate(findings):
        review = {"review_id": "TRL9-" + evidence_class,
                  "criterion_id": "TRL9-C1", "claim_id": "CLAIM-TRL9",
                  "decision": "supports", "evidence_class": evidence_class,
                  "findings": findings, "scope_id": "UNIT-A",
                  "reviewer": "audit", "review_basis": "TRL9纵向门",
                  "support_scope": "TRL9-C1"}
        return _result_row(evaluate_trl_dimension(
            CATALOG["dimensions"]["TRL"]["registry"]["criteria"], [review],
            scope=SCOPE, assessment_unit=UNIT), "TRL9-C1")
    assert evaluate({**base, "independent_user_ids": ["USER-1"],
                     "longitudinal_period": "six months"})["native_disposition"] != "met"
    assert evaluate({**base, "independent_user_ids": ["USER-1", "USER-2"],
                     "longitudinal_period": ""})["native_disposition"] != "met"
    assert evaluate({**base, "independent_user_ids": ["USER-1", "USER-2"],
                     "longitudinal_period": "six months"})["native_disposition"] == "met"


@pytest.mark.parametrize("subject_ids", [
    [None], [1], [""], ["  "], ["PERSON-1", "PERSON-1"]])
def test_tmrl_rejects_invalid_or_duplicate_subject_ids(subject_ids):
    criterion = _criterion("TMRL", "TMRL5-C3")
    review = {"review_id": "TMRL-BAD-SUBJECT", "criterion_id": "TMRL5-C3",
              "claim_id": "CLAIM-TMRL", "decision": "supports",
              "evidence_class": "executed_ownership_agreement",
              "findings": {TMRL_REQUIREMENTS["TMRL5-C3"]: True,
                           "team_specific": True, "subject_ids": subject_ids,
                           "current_period": "2026-Q3",
                           "agreement_status": "executed_agreement"},
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "非法人员身份", "support_scope": "TMRL5-C3"}
    result = evaluate_tmrl_dimension(
        CATALOG["dimensions"]["TMRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _result_row(result, "TMRL5-C3")["native_disposition"] != "met"


def test_tmrl_ignores_self_reported_identity_state_without_controlled_overlay():
    criterion = _criterion("TMRL", "TMRL5-C1")
    review = {"review_id": "TMRL-UNRESOLVED", "criterion_id": "TMRL5-C1",
              "claim_id": "CLAIM-TMRL", "decision": "supports",
              "evidence_class": "founder_team_operating_record",
              "findings": {TMRL_REQUIREMENTS["TMRL5-C1"]: True,
                           "team_specific": True, "subject_ids": ["PERSON-X"],
                           "identity_resolution_states": ["verified"],
                           "current_period": "2026-Q3",
                           "record_status": "verified_operating_record",
                           "operating_period": "2026-Q2 to Q3",
                           "actual_behaviors": ["worked together"]},
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "调用方自报身份", "support_scope": "TMRL5-C1"}
    result = evaluate_tmrl_dimension(
        CATALOG["dimensions"]["TMRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _result_row(result, "TMRL5-C1")["native_disposition"] != "met"


def _controlled_identity_overlays(status="verified"):
    return {"PERSON-1": {
        "record": {"overlay_id": "OVERLAY-1", "case_basis_version": 1,
                   "scope_id": "UNIT-A", "subject_id": "PERSON-1",
                   "resolution_status": status,
                   "subject_ref": {"kind": "field_reference",
                                   "path": "case:team-identities.json#/identities/0/subject_id"},
                   "status_ref": {"kind": "field_reference",
                                  "path": "case:team-identities.json#/identities/0/status"},
                   "scope_ref": {"kind": "field_reference",
                                 "path": "case:team-identities.json#/identities/0/scope_id"}},
        "proof_bindings": {"subject": {"value": "PERSON-1"},
                           "status": {"value": status},
                           "scope": {"value": "UNIT-A"}},
    }}


def test_tmrl_controlled_identity_overlay_allows_only_supported_states():
    criterion = _criterion("TMRL", "TMRL5-C1")
    review = {"review_id": "TMRL-CONTROLLED", "criterion_id": "TMRL5-C1",
              "claim_id": "CLAIM-TMRL", "decision": "supports",
              "evidence_class": "founder_team_operating_record",
              "findings": {TMRL_REQUIREMENTS["TMRL5-C1"]: True,
                           "team_specific": True, "subject_ids": ["PERSON-1"],
                           "current_period": "2026-Q3",
                           "record_status": "verified_operating_record",
                           "operating_period": "2026-Q2 to Q3",
                           "actual_behaviors": ["worked together"]},
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "受控身份正例", "support_scope": "TMRL5-C1"}
    qualified = evaluate_tmrl_dimension(
        CATALOG["dimensions"]["TMRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT,
        identity_overlays=_controlled_identity_overlays("verified"))
    assert _result_row(qualified, "TMRL5-C1")["native_disposition"] == "met"
    unresolved = evaluate_tmrl_dimension(
        CATALOG["dimensions"]["TMRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT,
        identity_overlays=_controlled_identity_overlays("unverified"))
    assert _result_row(unresolved, "TMRL5-C1")["native_disposition"] != "met"


def _seed_tmrl_identity_case(root):
    text = "Company-A team record names PERSON-1. Published 2026-07-01."
    basis = {"subject_legal_name": SCOPE, "subject_aliases": [],
             "evidence_cutoff": "2026-08-27T03:02:29Z",
             "subject_source_basis": json.dumps({
                 "kind": "field_reference", "path": "case:identity.json#/subject",
                 "status": "claimed"})}
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SCOPE}).encode())
        case.add_import_record("case_provenance", "case:identity.json", identity.sha256)
        unit_blob = blobs.put_bytes(json.dumps({"unit": {
            "scope_id": "UNIT-A", "subject": SCOPE,
            "kind": "material_team_unit", "label": "Team-A"}}).encode())
        case.add_import_record("case_provenance", "case:assessment-unit.json",
                               unit_blob.sha256)
        overlay_blob = blobs.put_bytes(json.dumps({"identities": [{
            "subject_id": "PERSON-1", "status": "verified", "scope_id": "UNIT-A"}]}).encode())
        case.add_import_record("case_provenance", "case:team-identities.json",
                               overlay_blob.sha256)
        source_blob = blobs.put_bytes(text.encode())
        case.add_source("TM-S", source_blob.sha256, len(text.encode()),
                        source_family="news-media",
                        capture_status="raw_capture_validated")
        start = len(text[:text.index("2026-07-01")].encode())
        case.append_time_evidence("TM-S", {
            "kind": "document_self_date", "date": "2026-07-01",
            "date_locator": {"kind": "byte_range", "start": start,
                             "end": start + 10}})
        version = case.set_case_basis(**basis)
        claim_row = {"claim_id": "TM-C", "source_id": "TM-S",
                     "locator_kind": "byte_range", "locator_start": 0,
                     "locator_end": len(text.encode()), "locator_ref": None,
                     "excerpt_sha256": source_blob.sha256, "excerpt_text": text,
                     "interpretation": "该来源载明团队运行记录。",
                     "subject_scope": SCOPE}
        digest = claim_content_digest(claim_row)
        case.add_claim("TM-C", "TM-S", locator_kind="byte_range",
                       excerpt_start=0, excerpt_end=len(text.encode()),
                       excerpt_sha256=source_blob.sha256, excerpt_text=text,
                       interpretation=claim_row["interpretation"],
                       subject_scope=SCOPE, interpretation_attempt="tmrl-overlay-test",
                       input_digest=digest, content_digest=digest)
        stored_claim = case.fetch_one("claims", "claim_id", "TM-C")
        source = case.fetch_one("sources", "source_id", "TM-S")
        time_evidence = case.latest_time_evidence("TM-S")
        source_for_qualification = dict(source)
        source_for_qualification["time_evidence"] = {
            key: value for key, value in time_evidence.items()
            if key not in {"revision", "created_at"}}
        outcome = qualify_claim(stored_claim, source_for_qualification, blobs, basis,
                                review_attempt="tmrl-overlay-test", case=case)
        assert isinstance(outcome, QualificationOutcome) and outcome.status == "qualified"
        case.add_qualification(
            "QUALR::TM-C", "TM-C", source_judgment=outcome.source_judgment.basis,
            identity_judgment=outcome.identity_judgment.basis,
            time_judgment=outcome.time_judgment.basis,
            independence_judgment=outcome.independence_judgment.basis,
            allowed_uses=outcome.allowed_uses, cannot_prove=outcome.cannot_prove,
            review_attempt=outcome.review_attempt, status=outcome.status)
        case.add_tmrl_identity_overlay(
            "OVERLAY-1", case_basis_version=version, scope_id="UNIT-A",
            subject_id="PERSON-1", resolution_status="verified",
            subject_ref={"kind": "field_reference",
                         "path": "case:team-identities.json#/identities/0/subject_id"},
            status_ref={"kind": "field_reference",
                        "path": "case:team-identities.json#/identities/0/status"},
            scope_ref={"kind": "field_reference",
                       "path": "case:team-identities.json#/identities/0/scope_id"})
        case.add_dimension_evidence_review(
            "TM-REV", dimension_id="TMRL", case_basis_version=version,
            claim_id="TM-C", criterion_id="TMRL5-C1",
            quote_sha256=sha256_hex(text.encode()), decision="supports",
            evidence_class="founder_team_operating_record",
            findings={TMRL_REQUIREMENTS["TMRL5-C1"]: True,
                      "team_specific": True, "subject_ids": ["PERSON-1"],
                      "current_period": "2026-Q3",
                      "record_status": "verified_operating_record",
                      "operating_period": "2026-Q2 to Q3",
                      "actual_behaviors": ["worked together"]},
            subject_scope=SCOPE, scope_id="UNIT-A", support_scope="TMRL5-C1",
            reviewer="audit", review_basis="受控身份overlay集成测试")
    finally:
        case.close()
    return basis, version


def _tmrl_unit_input():
    return {"scope_id": "UNIT-A", "subject_scope": SCOPE,
            "unit_kind": "material_team_unit", "unit_label": "Team-A",
            "scope_id_ref": {"kind": "field_reference",
                             "path": "case:assessment-unit.json#/unit/scope_id"},
            "subject_ref": {"kind": "field_reference",
                            "path": "case:assessment-unit.json#/unit/subject"},
            "unit_kind_ref": {"kind": "field_reference",
                              "path": "case:assessment-unit.json#/unit/kind"},
            "unit_label_ref": {"kind": "field_reference",
                               "path": "case:assessment-unit.json#/unit/label"}}


def test_tmrl_runner_freezes_overlay_and_trace_detects_proof_removal(tmp_path):
    basis, _version = _seed_tmrl_identity_case(tmp_path)
    result = runner.run_tmrl_dimension_slice(
        tmp_path, catalog=CATALOG, case_basis=basis, scope=SCOPE,
        assessment_unit=_tmrl_unit_input())
    assert _result_row(result["dimension"], "TMRL5-C1")["native_disposition"] == "met"
    overlays = result["frozen_inputs"]["tmrl_identity_overlays"]
    assert overlays["PERSON-1"]["record"]["resolution_status"] == "verified"
    case = CaseStore(tmp_path / "records.sqlite3")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        assert trace_dimension_result(case, blobs, result["result_id"])["ok"]
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='case:team-identities.json'")
        traced = trace_dimension_result(case, blobs, result["result_id"])
    finally:
        case.close()
    assert not traced["ok"]
    assert any("身份" in item or "overlay" in item for item in traced["broken"])
