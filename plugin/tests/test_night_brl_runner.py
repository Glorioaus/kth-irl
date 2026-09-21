"""夜间BRL候选：材料评估单元Case入口与完整trace。"""

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
UNIT_ID = "BRL-UNIT-A"


def _unit_input():
    return {
        "scope_id": UNIT_ID,
        "subject_scope": SUBJECT,
        "unit_kind": "material_business_unit",
        "unit_label": "Product-A",
        "scope_id_ref": {"kind": "field_reference",
                         "path": "case:assessment-unit.json#/unit.scope_id"},
        "subject_ref": {"kind": "field_reference",
                        "path": "case:assessment-unit.json#/unit.subject"},
        "unit_kind_ref": {"kind": "field_reference",
                          "path": "case:assessment-unit.json#/unit.kind"},
        "unit_label_ref": {"kind": "field_reference",
                           "path": "case:assessment-unit.json#/unit.label"},
    }


def _seed(root):
    text = "Company-A states a business concept. Published 2026-07-01."
    basis = {
        "subject_legal_name": SUBJECT,
        "subject_aliases": [],
        "evidence_cutoff": "2026-08-27T03:02:29Z",
        "subject_source_basis": json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed"}),
    }
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT}).encode())
        case.add_import_record("case_provenance", "case:identity.json", identity.sha256)
        unit = blobs.put_bytes(json.dumps({"unit": {
            "scope_id": UNIT_ID, "subject": SUBJECT,
            "kind": "material_business_unit", "label": "Product-A",
        }}).encode())
        case.add_import_record(
            "case_provenance", "case:assessment-unit.json", unit.sha256)
        source_blob = blobs.put_bytes(text.encode())
        case.add_source("BRL-S", source_blob.sha256, len(text.encode()),
                        source_family="news-media",
                        capture_status="raw_capture_validated")
        start = len(text[:text.index("2026-07-01")].encode())
        revision = case.append_time_evidence("BRL-S", {
            "kind": "document_self_date", "date": "2026-07-01",
            "date_locator": {"kind": "byte_range", "start": start,
                             "end": start + 10},
        })
        version = case.set_case_basis(**basis)
        claim = {
            "claim_id": "BRL-C", "source_id": "BRL-S",
            "locator_kind": "byte_range", "locator_start": 0,
            "locator_end": len(text.encode()), "locator_ref": None,
            "excerpt_sha256": source_blob.sha256, "excerpt_text": text,
            "interpretation": "该来源陈述一个商业概念。",
            "subject_scope": SUBJECT,
        }
        digest = claim_content_digest(claim)
        case.add_claim(
            "BRL-C", "BRL-S", locator_kind="byte_range", excerpt_start=0,
            excerpt_end=len(text.encode()), excerpt_sha256=source_blob.sha256,
            excerpt_text=text, interpretation=claim["interpretation"],
            subject_scope=SUBJECT, interpretation_attempt="night-brl",
            input_digest=digest, content_digest=digest)
        stored = case.fetch_one("claims", "claim_id", "BRL-C")
        source = case.fetch_one("sources", "source_id", "BRL-S")
        time = case.latest_time_evidence("BRL-S")
        qualified_source = dict(source)
        qualified_source["time_evidence"] = {
            key: value for key, value in time.items()
            if key not in {"revision", "created_at"}}
        outcome = qualify_claim(
            stored, qualified_source, blobs, basis,
            review_attempt="night-brl", case=case)
        assert isinstance(outcome, QualificationOutcome) and outcome.status == "qualified"
        case.add_qualification(
            "QUALR::BRL-C", "BRL-C",
            source_judgment=outcome.source_judgment.basis,
            identity_judgment=outcome.identity_judgment.basis,
            time_judgment=outcome.time_judgment.basis,
            independence_judgment=outcome.independence_judgment.basis,
            allowed_uses=outcome.allowed_uses,
            cannot_prove=outcome.cannot_prove,
            review_attempt=outcome.review_attempt, status=outcome.status)
        case.add_dimension_evidence_review(
            "BRL-REV-1", dimension_id="BRL", case_basis_version=version,
            claim_id="BRL-C", criterion_id="BRL1-BM",
            quote_sha256=sha256_hex(text.encode()), decision="supports",
            evidence_class="business_concept",
            findings={"business_idea_or_model_stated": True},
            subject_scope=SUBJECT, scope_id=UNIT_ID,
            support_scope="仅支持BRL1-BM商业概念陈述",
            reviewer="night-brl", review_basis="合成离线BRL复核")
    finally:
        case.close()
    return basis, build_catalog_from_wheel(), revision


def _run(root, basis, catalog, unit=None):
    return runner.run_brl_dimension_slice(
        root, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=unit or _unit_input())


def _trace(root, result_id):
    case = CaseStore(root / "records.sqlite3")
    try:
        return audit.trace_dimension_result(
            case, BlobStore(root / "blobs"), result_id)
    finally:
        case.close()


def test_brl_runner_publishes_unit_result_with_qualification_view(tmp_path):
    basis, catalog, revision = _seed(tmp_path)
    result = _run(tmp_path, basis, catalog)
    row = next(item for item in result["dimension"]["criteria"]
               if item["criterion_id"] == "BRL1-BM")
    assert row["native_disposition"] == "met"
    assert result["dimension"]["assessment_unit"]["scope_id"] == UNIT_ID
    view = result["frozen_inputs"]["evidence_bindings"][0]["qualification_view"]
    assert view["time_evidence"]["revision"] == revision
    assert _trace(tmp_path, result["result_id"])["ok"]
    assert _run(tmp_path, basis, catalog)["result_id"] == result["result_id"]


def test_brl_trace_detects_time_revision_removal(tmp_path):
    basis, catalog, revision = _seed(tmp_path)
    result = _run(tmp_path, basis, catalog)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM source_time_evidence WHERE revision=?", (revision,))
    finally:
        case.close()
    assert not _trace(tmp_path, result["result_id"])["ok"]


def test_brl_scope_proof_and_missing_original_fail_closed(tmp_path):
    basis, catalog, _revision = _seed(tmp_path)
    wrong = _unit_input()
    wrong["unit_label"] = "Other-Product"
    failed = _run(tmp_path, basis, catalog, wrong)
    assert failed["dimension"]["product_status"] == "execution_failed"
    assert all(row["native_disposition"] is None
               for row in failed["dimension"]["criteria"])

    root = tmp_path / "missing"
    basis, catalog, _revision = _seed(root)
    for path in (root / "blobs").rglob("*"):
        if path.is_file():
            path.unlink()
    missing = _run(root, basis, catalog)
    assert missing["dimension"]["product_status"] == "execution_failed"


REAL_BRL = os.environ.get("KTH_REAL_CASE_DIR_BRL")


@pytest.mark.skipif(not REAL_BRL, reason="未设置KTH_REAL_CASE_DIR_BRL")
def test_real_brl_candidate_is_all_insufficient_and_traceable():
    root = Path(REAL_BRL)
    case = CaseStore(root / "records.sqlite3")
    try:
        basis = case.get_case_basis()
    finally:
        case.close()
    subject = basis["subject_legal_name"]
    result = runner.run_brl_dimension_slice(
        root, catalog=build_catalog_from_wheel(), case_basis=basis,
        scope=subject, assessment_unit={
            "scope_id": "UNIT-WEIJU-COMPANY-CURRENT-CASE",
            "subject_scope": subject,
            "unit_kind": "current_case_company_level",
            "unit_label": "微玖当前Case公司级候选单元",
            "scope_id_ref": {"kind": "field_reference",
                             "path": "case:assessment-units.json#/units/0/scope_id"},
            "subject_ref": {"kind": "field_reference",
                            "path": "case:assessment-units.json#/units/0/subject"},
            "unit_kind_ref": {"kind": "field_reference",
                              "path": "case:assessment-units.json#/units/0/kind"},
            "unit_label_ref": {"kind": "field_reference",
                               "path": "case:assessment-units.json#/units/0/label"},
        })
    assert result["dimension"]["attained_level"] == 0
    assert all(row["native_disposition"] == "insufficient"
               for row in result["dimension"]["criteria"])
    assert _trace(root, result["result_id"])["ok"]
