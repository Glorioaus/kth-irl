"""R2-B候选：FRL Case入口复用完整资格视图与不可变发布。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import kth_hybrid.audit as audit
import kth_hybrid.runner as runner
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import claim_content_digest, sha256_hex
from kth_hybrid.qualification import QualificationOutcome, qualify_claim
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
CUTOFF = "2026-08-27T03:02:29Z"
ENTITY_ID = "FIN-COMPANY-A"


def _scope_input():
    return {
        "financing_entity_id": ENTITY_ID,
        "subject_scope": SUBJECT,
        "assessment_unit_refs": ["UNIT-PRODUCT", "UNIT-PLATFORM"],
        "entity_ref": {"kind": "field_reference",
                       "path": "case:frl-scope.json#/entity_id"},
        "subject_ref": {"kind": "field_reference",
                        "path": "case:frl-scope.json#/subject"},
        "assessment_units_ref": {"kind": "field_reference",
                                 "path": "case:frl-scope.json#/units"},
    }


def _seed_qualified_funding_claim(root):
    text = "Company-A has an initial funding description. Published 2026-07-01."
    basis = {
        "subject_legal_name": SUBJECT,
        "subject_aliases": [],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity.json#/subject",
            "status": "claimed",
        }),
    }
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT}).encode())
        case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
        scope_blob = blobs.put_bytes(json.dumps({
            "entity_id": ENTITY_ID,
            "subject": SUBJECT,
            "units": ["UNIT-PRODUCT", "UNIT-PLATFORM"],
        }).encode())
        case.add_import_record("case_provenance", "case:frl-scope.json", scope_blob.sha256)
        source_blob = blobs.put_bytes(text.encode())
        case.add_source(
            "FRL-S", source_blob.sha256, len(text.encode()),
            source_family="news-media", capture_status="raw_capture_validated")
        start = len(text[:text.index("2026-07-01")].encode())
        revision = case.append_time_evidence("FRL-S", {
            "kind": "document_self_date", "date": "2026-07-01",
            "date_locator": {"kind": "byte_range", "start": start,
                             "end": start + len("2026-07-01")},
        })
        version = case.set_case_basis(**basis)
        claim = {
            "claim_id": "FRL-C", "source_id": "FRL-S",
            "locator_kind": "byte_range", "locator_start": 0,
            "locator_end": len(text.encode()), "locator_ref": None,
            "excerpt_sha256": source_blob.sha256, "excerpt_text": text,
            "interpretation": "该来源陈述初步融资材料已存在。",
            "subject_scope": SUBJECT,
        }
        digest = claim_content_digest(claim)
        case.add_claim(
            "FRL-C", "FRL-S", locator_kind="byte_range", excerpt_start=0,
            excerpt_end=len(text.encode()), excerpt_sha256=source_blob.sha256,
            excerpt_text=text, interpretation=claim["interpretation"],
            subject_scope=SUBJECT, interpretation_attempt="r2b-test",
            input_digest=digest, content_digest=digest)
        stored_claim = case.fetch_one("claims", "claim_id", "FRL-C")
        source = case.fetch_one("sources", "source_id", "FRL-S")
        time_entry = case.latest_time_evidence("FRL-S")
        source_for_qualification = dict(source)
        source_for_qualification["time_evidence"] = {
            key: value for key, value in time_entry.items()
            if key not in {"revision", "created_at"}
        }
        outcome = qualify_claim(
            stored_claim, source_for_qualification, blobs, basis,
            review_attempt="r2b-test", case=case)
        assert isinstance(outcome, QualificationOutcome)
        assert outcome.status == "qualified"
        case.add_qualification(
            "QUALR::FRL-C", "FRL-C",
            source_judgment=outcome.source_judgment.basis,
            identity_judgment=outcome.identity_judgment.basis,
            time_judgment=outcome.time_judgment.basis,
            independence_judgment=outcome.independence_judgment.basis,
            allowed_uses=outcome.allowed_uses,
            cannot_prove=outcome.cannot_prove,
            review_attempt=outcome.review_attempt, status=outcome.status)
    finally:
        case.close()
    return basis, build_catalog_from_wheel(), version, revision, text


def _add_review(root, version, text, *, evidence_class="funding_description",
                criterion_id="FRL2-PITCH"):
    case = CaseStore(root / "records.sqlite3")
    try:
        case.add_dimension_evidence_review(
            "FRL-REV-1", dimension_id="FRL", case_basis_version=version,
            claim_id="FRL-C", criterion_id=criterion_id,
            quote_sha256=sha256_hex(text.encode()), decision="supports",
            evidence_class=evidence_class,
            findings={"funding_description_exists": True},
            subject_scope=SUBJECT, scope_id=ENTITY_ID,
            support_scope="仅支持FRL2-PITCH", reviewer="r2b-test",
            review_basis="合成离线FRL复核")
    finally:
        case.close()


def _run(root, basis, catalog, **changes):
    args = {
        "catalog": catalog,
        "case_basis": basis,
        "scope": SUBJECT,
        "financing_entity": _scope_input(),
    }
    args.update(changes)
    return runner.run_frl_dimension_slice(root, **args)


def _trace(root, result_id):
    case = CaseStore(root / "records.sqlite3")
    try:
        return audit.trace_dimension_result(
            case, BlobStore(root / "blobs"), result_id)
    finally:
        case.close()


def test_frl_runner_and_trace_interfaces_exist():
    assert callable(getattr(runner, "run_frl_dimension_slice", None))
    assert callable(getattr(audit, "trace_dimension_result", None))


def test_frl_runner_publishes_complete_qualification_view_and_replays(tmp_path):
    basis, catalog, version, revision, text = _seed_qualified_funding_claim(tmp_path)
    _add_review(tmp_path, version, text)
    result = _run(tmp_path, basis, catalog)
    row = next(item for item in result["dimension"]["criteria"]
               if item["criterion_id"] == "FRL2-PITCH")
    assert row["native_disposition"] == "met"
    assert result["dimension"]["attained_level"] == 0
    assert result["dimension"]["financing_entity"]["assessment_unit_refs"] == [
        "UNIT-PLATFORM", "UNIT-PRODUCT"]
    view = result["frozen_inputs"]["evidence_bindings"][0]["qualification_view"]
    assert view["time_evidence"]["revision"] == revision
    assert view["outcome"]["status"] == "qualified"
    assert _trace(tmp_path, result["result_id"])["ok"]
    replay = _run(tmp_path, basis, catalog)
    assert replay["result_id"] == result["result_id"]
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert case.count_dimension_results("FRL") == 1
    finally:
        case.close()


def test_frl_runner_missing_original_or_changed_claim_is_execution_failed(tmp_path):
    basis, catalog, version, _revision, text = _seed_qualified_funding_claim(tmp_path)
    _add_review(tmp_path, version, text)
    for path in (tmp_path / "blobs").rglob("*"):
        if path.is_file():
            path.unlink()
    missing = _run(tmp_path, basis, catalog)
    assert missing["dimension"]["product_status"] == "execution_failed"
    assert all(row["native_disposition"] is None
               for row in missing["dimension"]["criteria"])

    root = tmp_path / "changed"
    basis, catalog, version, _revision, text = _seed_qualified_funding_claim(root)
    _add_review(root, version, text)
    first = _run(root, basis, catalog)
    case = CaseStore(root / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "UPDATE claims SET interpretation='篡改' WHERE claim_id='FRL-C'")
    finally:
        case.close()
    changed = _run(root, basis, catalog)
    assert changed["dimension"]["product_status"] == "execution_failed"
    assert changed["input_digest"] != first["input_digest"]


def test_frl_trace_detects_bound_time_revision_removal(tmp_path):
    basis, catalog, version, revision, text = _seed_qualified_funding_claim(tmp_path)
    _add_review(tmp_path, version, text)
    result = _run(tmp_path, basis, catalog)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM source_time_evidence WHERE revision=?", (revision,))
    finally:
        case.close()
    traced = _trace(tmp_path, result["result_id"])
    assert not traced["ok"]
    assert any("时间修订" in item for item in traced["broken"])


def _policy_input():
    return {
        "external_financing_planned_ref": {
            "kind": "field_reference",
            "path": "case:frl-policy.json#/entity.external_financing_planned",
        },
        "financing_entity_ref": {
            "kind": "field_reference",
            "path": "case:frl-policy.json#/entity.entity_id",
        },
        "subject_ref": {
            "kind": "field_reference",
            "path": "case:frl-policy.json#/entity.subject",
        },
    }


def test_frl_restricted_na_uses_same_record_policy_and_trace_detects_deletion(tmp_path):
    basis, catalog, _version, _revision, _text = _seed_qualified_funding_claim(tmp_path)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        policy = blobs.put_bytes(json.dumps({"entity": {
            "entity_id": ENTITY_ID, "subject": SUBJECT,
            "external_financing_planned": False,
        }}).encode())
        case.add_import_record("case_provenance", "case:frl-policy.json", policy.sha256)
    finally:
        case.close()
    result = _run(tmp_path, basis, catalog, applicability_policy=_policy_input())
    assert sum(row["native_disposition"] == "not_applicable"
               for row in result["dimension"]["criteria"]) == 9
    assert _trace(tmp_path, result["result_id"])["ok"]
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='case:frl-policy.json'")
    finally:
        case.close()
    assert not _trace(tmp_path, result["result_id"])["ok"]


def test_frl_scope_and_policy_relations_cannot_be_spliced(tmp_path):
    basis, catalog, _version, _revision, _text = _seed_qualified_funding_claim(tmp_path)
    wrong_scope = _scope_input()
    wrong_scope["subject_scope"] = "Other-Company"
    with pytest.raises(ValueError, match="融资主体"):
        _run(tmp_path, basis, catalog, financing_entity=wrong_scope)

    invalid_policy = _policy_input()
    invalid_policy["subject_ref"] = {
        "kind": "field_reference", "path": "case:identity.json#/subject"}
    failed = _run(tmp_path, basis, catalog, applicability_policy=invalid_policy)
    assert failed["dimension"]["product_status"] == "execution_failed"
    assert all(row["native_disposition"] is None
               for row in failed["dimension"]["criteria"])


def test_frl_publish_validation_failure_cannot_expose_met(tmp_path, monkeypatch):
    basis, catalog, version, _revision, text = _seed_qualified_funding_claim(tmp_path)
    _add_review(tmp_path, version, text)
    monkeypatch.setattr(
        audit, "validate_dimension_payload",
        lambda _case, _blobs, _result: ["注入发布前断裂"],
    )
    result = _run(tmp_path, basis, catalog)
    assert result["dimension"]["product_status"] == "execution_failed"
    assert all(row["native_disposition"] is None
               for row in result["dimension"]["criteria"])


REAL_FRL = os.environ.get("KTH_REAL_CASE_DIR_FRL")


@pytest.mark.skipif(not REAL_FRL, reason="未设置KTH_REAL_CASE_DIR_FRL")
def test_real_frl_candidate_is_honestly_insufficient_and_traceable():
    root = Path(REAL_FRL)
    case = CaseStore(root / "records.sqlite3")
    try:
        basis = case.get_case_basis()
    finally:
        case.close()
    subject = basis["subject_legal_name"]
    result = runner.run_frl_dimension_slice(
        root, catalog=build_catalog_from_wheel(), case_basis=basis,
        scope=subject, financing_entity={
            "financing_entity_id": "FIN-WEIJU-COMPANY",
            "subject_scope": subject,
            "assessment_unit_refs": ["UNIT-WEIJU-COMPANY-CURRENT-CASE"],
            "entity_ref": {"kind": "field_reference",
                           "path": "case:frl-scope.json#/entity_id"},
            "subject_ref": {"kind": "field_reference",
                            "path": "case:frl-scope.json#/subject"},
            "assessment_units_ref": {"kind": "field_reference",
                                     "path": "case:frl-scope.json#/units"},
        })
    assert result["dimension"]["attained_level"] == 0
    assert all(row["native_disposition"] == "insufficient"
               for row in result["dimension"]["criteria"])
    assert _trace(root, result["result_id"])["ok"]
