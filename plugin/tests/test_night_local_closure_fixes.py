"""NIGHT局部闭环：TRL9、TMRL trace与角色文本赋值反例。"""

from __future__ import annotations

import copy
import json

import pytest

from kth_hybrid import runner
from kth_hybrid.audit import trace_dimension_result
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.kernels.trl import RULE_REQUIREMENTS as TRL_REQUIREMENTS
from kth_hybrid.roles import (
    assemble_offline_deliberation,
    confirm_role_candidate,
    role_candidate_digest,
    validate_role_attempt,
)
from kth_hybrid.store import BlobStore, CaseStore
from test_night_roles import LICENSE_ID, VIEW, attempt
from test_night_second_limited_fixes import (
    CATALOG,
    _result_row,
    _seed_tmrl_identity_case,
    _tmrl_unit_input,
)


def _trl9_findings(users, period="2026-01-01 to 2026-06-30"):
    return {
        TRL_REQUIREMENTS["TRL9-C1"]: True,
        "project_specific": True,
        "configuration_id": "CFG-A",
        "system_boundary": "complete System-A",
        "test_environment": "actual use",
        "test_method": "field observation",
        "measured_results": "bounded result",
        "requirements_thresholds": "declared thresholds",
        "environment_kind": "actual_operation",
        "independent_user_ids": users,
        "longitudinal_period": period,
    }


@pytest.mark.parametrize(
    "evidence_class", ["longitudinal_operation", "independent_operation_record"])
@pytest.mark.parametrize("users,period,expected", [
    (["USER-1", "USER-2"], "six months", "met"),
    (["USER-1", "USER-1"], "six months", "insufficient"),
    (["USER-1", " USER-1 "], "six months", "insufficient"),
    (["USER-1"], "six months", "insufficient"),
    (["USER-1", "USER-2"], "", "insufficient"),
])
def test_trl9_normalized_users_persist_runner_and_trace(
        tmp_path, evidence_class, users, period, expected):
    basis, version = _seed_tmrl_identity_case(tmp_path)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        claim = case.fetch_one("claims", "claim_id", "TM-C")
        case.add_dimension_evidence_review(
            "TRL9-PROBE", dimension_id="TRL", case_basis_version=version,
            claim_id="TM-C", criterion_id="TRL9-C1",
            quote_sha256=claim["excerpt_sha256"], decision="supports",
            evidence_class=evidence_class,
            findings=_trl9_findings(users, period), subject_scope="Company-A",
            scope_id="UNIT-A", support_scope="TRL9-C1", reviewer="local-closure",
            review_basis="TRL9规范化用户去重端到端反例")
        assert case.get_dimension_evidence_review("TRL9-PROBE")[
            "findings"]["independent_user_ids"] == users
    finally:
        case.close()
    result = runner.run_trl_dimension_slice(
        tmp_path, catalog=CATALOG, case_basis=basis, scope="Company-A",
        assessment_unit=_tmrl_unit_input())
    observed = _result_row(result["dimension"], "TRL9-C1")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        traced = trace_dimension_result(
            case, BlobStore(tmp_path / "blobs"), result["result_id"])
    finally:
        case.close()
    assert traced["ok"], traced
    assert observed["native_disposition"] == expected


def _run_valid_tmrl(root):
    basis, version = _seed_tmrl_identity_case(root)
    result = runner.run_tmrl_dimension_slice(
        root, catalog=CATALOG, case_basis=basis, scope="Company-A",
        assessment_unit=_tmrl_unit_input())
    assert _result_row(result["dimension"], "TMRL5-C1")[
        "native_disposition"] == "met"
    return basis, version, result


def _reseal_tmrl_result(case, blobs, result_id, live_record):
    row = case.get_dimension_result_by_id(result_id)
    payload = json.loads(blobs.read_bytes(
        row["result_blob_sha256"]).decode("utf-8"))
    payload["frozen_inputs"]["tmrl_identity_overlays"]["PERSON-1"][
        "record"] = live_record
    digest = sha256_hex(json.dumps(
        payload["frozen_inputs"], ensure_ascii=False,
        sort_keys=True).encode("utf-8"))
    forged_id = f"DIMR2::TMRL::{digest}"
    payload["result_id"] = forged_id
    payload["input_digest"] = digest
    result_ref = blobs.put_bytes(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    with case._conn:
        case._conn.execute(
            "UPDATE dimension_results SET result_id=?,input_digest=?,"
            "result_blob_sha256=? WHERE result_id=?",
            (forged_id, digest, result_ref.sha256, result_id),
        )
    return forged_id


def test_tmrl_trace_rejects_resealed_record_with_unbound_refs(tmp_path):
    _basis, _version, result = _run_valid_tmrl(tmp_path)
    case = CaseStore(tmp_path / "records.sqlite3")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        assert trace_dimension_result(case, blobs, result["result_id"])["ok"]
        absent_refs = {
            "subject_ref": {"kind": "field_reference",
                            "path": "case:team-identities.json#/identities/0/absent_subject"},
            "status_ref": {"kind": "field_reference",
                           "path": "case:team-identities.json#/identities/0/absent_status"},
            "scope_ref": {"kind": "field_reference",
                          "path": "case:team-identities.json#/identities/0/absent_scope"},
        }
        with case._conn:
            case._conn.execute(
                "UPDATE tmrl_identity_overlays SET subject_ref_json=?,"
                "status_ref_json=?,scope_ref_json=? WHERE overlay_id='OVERLAY-1'",
                tuple(json.dumps(absent_refs[key], ensure_ascii=False,
                                 sort_keys=True)
                      for key in ("subject_ref", "status_ref", "scope_ref")),
            )
        live_record = case.get_tmrl_identity_overlay("OVERLAY-1")
        forged_id = _reseal_tmrl_result(
            case, blobs, result["result_id"], live_record)
        traced = trace_dimension_result(case, blobs, forged_id)
    finally:
        case.close()
    assert not traced["ok"], traced
    assert any("身份" in problem or "overlay" in problem
               for problem in traced["broken"])


def test_tmrl_trace_rejects_resealed_cross_casebasis_overlay(tmp_path):
    basis, original_version, result = _run_valid_tmrl(tmp_path)
    case = CaseStore(tmp_path / "records.sqlite3")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        assert trace_dimension_result(case, blobs, result["result_id"])["ok"]
        other_version = case.set_case_basis(
            subject_legal_name=basis["subject_legal_name"],
            subject_aliases=basis["subject_aliases"],
            evidence_cutoff=basis["evidence_cutoff"],
            subject_source_basis=basis["subject_source_basis"],
            note="仅用于跨CaseBasis关系反例")
        assert other_version != original_version
        with case._conn:
            case._conn.execute(
                "UPDATE tmrl_identity_overlays SET case_basis_version=? "
                "WHERE overlay_id='OVERLAY-1'", (other_version,))
        live_record = case.get_tmrl_identity_overlay("OVERLAY-1")
        forged_id = _reseal_tmrl_result(
            case, blobs, result["result_id"], live_record)
        traced = trace_dimension_result(case, blobs, forged_id)
    finally:
        case.close()
    assert not traced["ok"], traced
    assert any("CaseBasis" in problem or "身份" in problem
               or "overlay" in problem for problem in traced["broken"])


_ASSIGNMENTS = [
    "final_decision: approved",
    " FINAL_DECISION = approved ",
    "investment_recommendation: invest",
    '{"final_decision":"approved"}',
]


@pytest.mark.parametrize("text", _ASSIGNMENTS)
@pytest.mark.parametrize("location", ["pro", "chair", "round", "nested"])
def test_role_rejects_explicit_decision_text_assignment(text, location):
    pro = attempt("PRO", "P", "CTX-P", target=location == "nested")
    con = attempt("CON", "C", "CTX-C")
    chair = attempt("CHAIR", "H", "CTX-H")
    rounds = [{
        "round_number": 1,
        "pro_response": {"producer_id": "P", "observed_candidate_id": "CON-C1",
                         "statement": "回应"},
        "con_response": {"producer_id": "C", "observed_candidate_id": "PRO-C1",
                         "statement": "回应"},
    }]
    if location in {"pro", "chair"}:
        candidate = pro if location == "pro" else chair
        candidate["statement"] = text
        candidate["candidate_digest"] = role_candidate_digest(candidate)
    elif location == "round":
        rounds[0]["pro_response"]["statement"] = text
    else:
        pro["review_target"]["findings"]["comment"] = text
        pro["candidate_digest"] = role_candidate_digest(pro)
    with pytest.raises(ValueError, match="越权"):
        assemble_offline_deliberation(
            VIEW, pro, con, rounds, chair, owner_selected_round_count=1)


@pytest.mark.parametrize("text", _ASSIGNMENTS)
def test_confirmation_rejects_explicit_decision_text_assignment(text):
    candidate = validate_role_attempt(
        attempt("PRO", "P", "CTX-P", target=True), VIEW)
    target = candidate["review_target"]
    confirmation = {
        "schema_version": "kth-hybrid.role-confirmation.v2",
        "confirmation_id": "CONF-TEXT-ASSIGNMENT",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_view_id": VIEW["view_id"],
        "input_digest": VIEW["input_digest"],
        "producer_id": candidate["producer_id"],
        "role": candidate["role"],
        "case_basis_version": VIEW["case_basis_version"],
        "decision": "supports",
        "reviewer": "human",
        "review_basis": text,
        "evidence_refs": [LICENSE_ID],
        **target,
    }
    with pytest.raises(ValueError, match="越权"):
        confirm_role_candidate(
            candidate, confirmation, dimension_id="BRL", view=VIEW)


@pytest.mark.parametrize("text", [
    "讨论final_decision字段的命名。",
    "investment_recommendation只是字段名，不在此处赋值。",
    "The final decision remains outside this role.",
])
def test_role_allows_neutral_decision_field_mentions(text):
    candidate = attempt("PRO", "P", "CTX-P")
    candidate["statement"] = text
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    assert validate_role_attempt(candidate, VIEW)["statement"] == text
