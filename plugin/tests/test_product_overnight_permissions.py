"""本地产品闭环：用途许可、角色消费与持久化追溯边界。"""

from __future__ import annotations

import copy
import json
import shutil

import pytest

from kth_hybrid import runner
from kth_hybrid.aggregate import (
    build_offline_dimension_view,
    freeze_aggregation_manifest,
    validate_offline_dimension_view,
)
from kth_hybrid.audit import trace_dimension_result
from kth_hybrid.evidence_permissions import build_permission_binding
from kth_hybrid.roles import (
    confirm_role_candidate,
    role_candidate_digest,
    validate_role_attempt,
)
from kth_hybrid.store import BlobStore, CaseStore
from test_night_brl_runner import SUBJECT, _seed, _unit_input
from test_night_roles import VIEW as LEGACY_VIEW, attempt as legacy_attempt


def _build_real_view(root):
    """经真实CaseStore、资格重核、六维runner和manifest生成view。"""
    basis, catalog, _revision = _seed(root)
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        scope_blob = blobs.put_bytes(json.dumps({
            "entity_id": "FIN-A",
            "subject": SUBJECT,
            "units": [_unit_input()["scope_id"]],
        }).encode())
        case.add_import_record(
            "case_provenance", "case:frl-scope.json", scope_blob.sha256)
        claim = case.fetch_one("claims", "claim_id", "BRL-C")
        case.add_dimension_evidence_review(
            "BRL-REV-2", dimension_id="BRL",
            case_basis_version=case.get_case_basis()["version"],
            claim_id="BRL-C", criterion_id="BRL2-BM",
            quote_sha256=claim["excerpt_sha256"], decision="supports",
            evidence_class="business_concept",
            findings={"structured_business_concept": True},
            subject_scope=SUBJECT, scope_id=_unit_input()["scope_id"],
            support_scope="仅支持BRL2-BM结构化商业概念",
            reviewer="permission-test", review_basis="第二条准则许可反例基线",
        )
    finally:
        case.close()

    unit = _unit_input()
    entity = {
        "financing_entity_id": "FIN-A",
        "subject_scope": SUBJECT,
        "assessment_unit_refs": [unit["scope_id"]],
        "entity_ref": {"kind": "field_reference",
                       "path": "case:frl-scope.json#/entity_id"},
        "subject_ref": {"kind": "field_reference",
                        "path": "case:frl-scope.json#/subject"},
        "assessment_units_ref": {"kind": "field_reference",
                                 "path": "case:frl-scope.json#/units"},
    }
    results = {
        "CRL": runner.run_crl_dimension_slice(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT),
        "FRL": runner.run_frl_dimension_slice(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT,
            financing_entity=entity),
    }
    for dimension, run in (
            ("BRL", runner.run_brl_dimension_slice),
            ("TRL", runner.run_trl_dimension_slice),
            ("IPRL", runner.run_iprl_dimension_slice),
            ("TMRL", runner.run_tmrl_dimension_slice)):
        results[dimension] = run(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT,
            assessment_unit=unit)

    case = CaseStore(root / "records.sqlite3")
    try:
        manifest = freeze_aggregation_manifest(
            case, blobs,
            {dimension: result["result_id"]
             for dimension, result in results.items()},
        )
        view = build_offline_dimension_view(case, blobs, manifest)
    finally:
        case.close()
    return basis, catalog, view


@pytest.fixture(scope="module")
def real_permission_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("permission-base")
    basis, catalog, view = _build_real_view(root)
    return root, basis, catalog, view


def _brl_licenses(view):
    return sorted(
        (license_value for license_value in view["evidence_licenses"].values()
         if license_value["dimension_id"] == "BRL"),
        key=lambda value: value["criterion_id"],
    )


def _candidate(view, license_value, **target_changes):
    target = {
        "license_id": license_value["license_id"],
        "requested_use": license_value["allowed_uses"][0],
        "dimension_id": license_value["dimension_id"],
        "criterion_id": license_value["criterion_id"],
        "claim_id": license_value["claim_id"],
        "quote_sha256": license_value["quote_sha256"],
        "evidence_class": license_value["evidence_class"],
        "subject_scope": license_value["subject_scope"],
        "scope_id": license_value["scope_id"],
        "support_scope": license_value["support_scope"],
        "findings": {"business_idea_or_model_stated": True},
    }
    target.update(target_changes)
    candidate = {
        "schema_version": "kth-hybrid.offline-role-attempt.v3",
        "role": "PRO",
        "producer_id": "P",
        "context_id": "CTX-PERMISSION",
        "simulated": True,
        "input_view_id": view["view_id"],
        "input_digest": view["input_digest"],
        "scope": view["scope"],
        "dimension_result_refs": {
            dimension: row["result_id"]
            for dimension, row in view["dimensions"].items()
        },
        "candidate_id": "PRO-PERMISSION-C1",
        "statement": "仅按指定许可提出准则复核候选。",
        "evidence_refs": [license_value["license_id"]],
        "limitations": ["离线模拟"],
        "review_target": target,
    }
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    return candidate


def _confirmation(view, candidate, **changes):
    confirmation = {
        "schema_version": "kth-hybrid.role-confirmation.v3",
        "confirmation_id": "CONF-PERMISSION-1",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_view_id": view["view_id"],
        "input_digest": view["input_digest"],
        "producer_id": candidate["producer_id"],
        "role": candidate["role"],
        "case_basis_version": view["case_basis_version"],
        "decision": "supports",
        "reviewer": "permission-human",
        "review_basis": "逐项核对许可后确认",
        "evidence_refs": candidate["evidence_refs"],
        **candidate["review_target"],
    }
    confirmation.update(changes)
    return confirmation


def test_real_chain_emits_v3_view_and_content_addressed_v2_licenses(
        real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    assert validate_offline_dimension_view(view) == view
    assert view["schema_version"] == "kth-hybrid.offline-six-dimension-view.v3"
    licenses = _brl_licenses(view)
    assert [value["criterion_id"] for value in licenses] == [
        "BRL1-BM", "BRL2-BM"]
    for license_value in licenses:
        assert license_value["schema_version"] == \
            "kth-hybrid.evidence-use-license.v2"
        assert license_value["source_review_id"] == \
            license_value["source_review"]["review_id"]
        assert license_value["criterion_id"] == \
            license_value["criterion"]["criterion_id"]
        assert license_value["qualification_view_digest"] == \
            license_value["qualification_view"]["input_digest"]
        assert license_value["allowed_criterion_uses"] == {
            license_value["criterion_id"]: license_value["allowed_uses"]}
        assert "maturity_assessment" not in license_value["allowed_uses"]


@pytest.mark.parametrize("criterion_id", ["BRL2-BM", "BRL3-BM"])
def test_same_evidence_class_cannot_cross_criterion(
        real_permission_case, criterion_id):
    _root, _basis, _catalog, view = real_permission_case
    first = _brl_licenses(view)[0]
    with pytest.raises(ValueError, match="criterion|准则|许可"):
        validate_role_attempt(
            _candidate(view, first, criterion_id=criterion_id), view)


@pytest.mark.parametrize("support_scope", [
    "",
    "支持BRL全部准则",
    "仅支持BRL2-BM结构化商业概念",
])
def test_support_scope_cannot_be_empty_expanded_or_spliced(
        real_permission_case, support_scope):
    _root, _basis, _catalog, view = real_permission_case
    first = _brl_licenses(view)[0]
    with pytest.raises(ValueError, match="support_scope|范围|许可"):
        validate_role_attempt(
            _candidate(view, first, support_scope=support_scope), view)


def test_requested_use_must_come_from_qualification_allowed_uses(
        real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    first = _brl_licenses(view)[0]
    assert "maturity_assessment" not in first["allowed_uses"]
    with pytest.raises(ValueError, match="requested_use|用途|许可"):
        validate_role_attempt(
            _candidate(view, first, requested_use="maturity_assessment"), view)


def test_wrong_license_id_cannot_authorize_other_license_fields(
        real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    first, second = _brl_licenses(view)
    with pytest.raises(ValueError, match="license|许可"):
        validate_role_attempt(
            _candidate(view, first, license_id=second["license_id"]), view)


def test_multiple_licenses_cannot_launder_fields_across_selected_license(
        real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    first, second = _brl_licenses(view)
    candidate = _candidate(
        view, first, license_id=second["license_id"],
        criterion_id=first["criterion_id"],
        support_scope=first["support_scope"],
    )
    candidate["evidence_refs"] = [first["license_id"], second["license_id"]]
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    with pytest.raises(ValueError, match="license|许可|准则"):
        validate_role_attempt(candidate, view)


def test_candidate_and_confirmation_agreement_cannot_replace_original_license(
        real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    first, second = _brl_licenses(view)
    candidate = _candidate(
        view, first, criterion_id=second["criterion_id"],
        support_scope=second["support_scope"],
    )
    confirmation = _confirmation(view, candidate)
    with pytest.raises(ValueError, match="criterion|准则|许可"):
        confirm_role_candidate(
            candidate, confirmation, dimension_id="BRL", view=view)


def test_permission_binding_revalidates_original_license(real_permission_case):
    _root, _basis, _catalog, view = real_permission_case
    license_value = _brl_licenses(view)[0]
    candidate = _candidate(
        view, license_value, requested_use="maturity_assessment",
        support_scope="支持BRL全部准则")
    candidate["candidate_digest"] = role_candidate_digest(candidate)
    confirmation = _confirmation(view, candidate)
    with pytest.raises(ValueError, match="requested_use|用途|support_scope|许可"):
        build_permission_binding(
            review_id=confirmation["confirmation_id"],
            license_value=license_value,
            requested_use=candidate["review_target"]["requested_use"],
            candidate=candidate,
            confirmation=confirmation,
        )


def test_legacy_v2_view_is_readable_but_target_is_legacy_restricted():
    assert validate_offline_dimension_view(LEGACY_VIEW) == LEGACY_VIEW
    assert validate_role_attempt(
        legacy_attempt("PRO", "P", "CTX-P"), LEGACY_VIEW)["candidate_id"] \
        == "PRO-C1"
    candidate = validate_role_attempt(
        legacy_attempt("PRO", "P", "CTX-P", target=True), LEGACY_VIEW)
    target = candidate["review_target"]
    confirmation = {
        "schema_version": "kth-hybrid.role-confirmation.v2",
        "confirmation_id": "LEGACY-CONF-1",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate["candidate_digest"],
        "input_view_id": LEGACY_VIEW["view_id"],
        "input_digest": LEGACY_VIEW["input_digest"],
        "producer_id": candidate["producer_id"],
        "role": candidate["role"],
        "case_basis_version": LEGACY_VIEW["case_basis_version"],
        "decision": "supports",
        "reviewer": "legacy-human",
        "review_basis": "旧合同只读探针",
        "evidence_refs": candidate["evidence_refs"],
        **target,
    }
    with pytest.raises(ValueError, match="legacy_restricted"):
        confirm_role_candidate(
            candidate, confirmation, dimension_id="BRL", view=LEGACY_VIEW)


@pytest.mark.parametrize("tamper", ["delete", "rewrite"])
def test_runner_freezes_permission_sidecar_and_trace_rejects_change(
        tmp_path, real_permission_case, tamper):
    base_root, basis, catalog, view = real_permission_case
    root = tmp_path / "case"
    shutil.copytree(base_root, root)
    license_value = _brl_licenses(view)[0]
    candidate = validate_role_attempt(_candidate(view, license_value), view)
    confirmation = _confirmation(view, candidate)
    review = confirm_role_candidate(
        candidate, confirmation, dimension_id="BRL", view=view)
    assert review["review_basis"] == confirmation["review_basis"]

    case = CaseStore(root / "records.sqlite3")
    try:
        case.add_dimension_evidence_review(
            review["review_id"], dimension_id=review["dimension_id"],
            case_basis_version=review["case_basis_version"],
            claim_id=review["claim_id"], criterion_id=review["criterion_id"],
            quote_sha256=review["quote_sha256"], decision=review["decision"],
            evidence_class=review["evidence_class"], findings=review["findings"],
            subject_scope=review["subject_scope"], scope_id=review["scope_id"],
            support_scope=review["support_scope"], reviewer=review["reviewer"],
            review_basis=review["review_basis"],
            permission_binding=review["permission_binding"],
        )
        saved = case.get_dimension_review_permission(review["review_id"])
        assert saved["license_id"] == license_value["license_id"]
        assert saved["requested_use"] == candidate["review_target"]["requested_use"]
    finally:
        case.close()

    result = runner.run_brl_dimension_slice(
        root, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=_unit_input())
    frozen = next(
        binding for binding in result["frozen_inputs"]["evidence_bindings"]
        if binding["review"]["review_id"] == review["review_id"])
    assert frozen["permission_binding"]["binding_digest"] == \
        review["permission_binding"]["binding_digest"]

    case = CaseStore(root / "records.sqlite3")
    try:
        assert trace_dimension_result(
            case, BlobStore(root / "blobs"), result["result_id"])["ok"]
        with case._conn:
            if tamper == "delete":
                case._conn.execute(
                    "DELETE FROM dimension_review_permissions WHERE review_id=?",
                    (review["review_id"],))
            else:
                case._conn.execute(
                    "UPDATE dimension_review_permissions SET requested_use=? "
                    "WHERE review_id=?",
                    ("maturity_assessment", review["review_id"]))
        traced = trace_dimension_result(
            case, BlobStore(root / "blobs"), result["result_id"])
    finally:
        case.close()
    assert not traced["ok"]
    assert any("许可" in problem or "permission" in problem
               for problem in traced["broken"])

    rerun = runner.run_brl_dimension_slice(
        root, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=_unit_input())
    rerun_row = next(
        row for row in rerun["dimension"]["criteria"]
        if row["criterion_id"] == review["criterion_id"])
    assert rerun_row["native_disposition"] != "met"
    assert any(
        item.get("review_id") == review["review_id"]
        and ("许可" in item.get("reason", "")
             or "permission" in item.get("reason", ""))
        for item in rerun["frozen_inputs"]["rejected_reviews"])
