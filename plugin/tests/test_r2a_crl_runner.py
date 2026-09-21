"""R2-A：CRL 离线维度 runner 必须只消费落库的受控复核记录。"""

from __future__ import annotations

import kth_hybrid.runner as runner
import json
import os
from pathlib import Path
import pytest

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore
import kth_hybrid.audit as audit


def _seed_reviewed_case(root):
    text = "Company-A identifies market demand. Published 2026-07-01."
    basis = {"subject_legal_name": "Company-A", "subject_aliases": [],
             "evidence_cutoff": "2026-08-27T03:02:29Z",
             "subject_source_basis": json.dumps({"kind": "field_reference",
                                                   "path": "case:identity.json#/subject",
                                                   "status": "claimed"})}
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    identity = blobs.put_bytes(json.dumps({"subject": "Company-A"}).encode())
    case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
    source = blobs.put_bytes(text.encode())
    case.add_source("S", source.sha256, len(text.encode()), source_family="news-media",
                    capture_status="raw_capture_validated")
    start = len(text[:text.index("2026-07-01")].encode())
    case.append_time_evidence("S", {"kind": "document_self_date", "date": "2026-07-01",
                                     "date_locator": {"kind": "byte_range", "start": start,
                                                      "end": start + len("2026-07-01")}})
    version = case.set_case_basis(**basis)
    case.add_mapping_review("MAP", case_basis_version=version, claim_id="C",
                            criterion_id="CRL1-C1", quote_sha256=source.sha256,
                            support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
                            reviewer="r2a-test", review_basis="合成受控映射复核")
    case.close()
    catalog = build_catalog_from_wheel()
    runner.run_criterion_slice(root, source_id="S", criterion_id="CRL1-C1",
                               catalog=catalog, case_basis=basis, claim_spec={
                                   "claim_id": "C", "locator_kind": "byte_range", "start": 0,
                                   "end": len(text.encode()), "interpretation": text,
                                   "subject_scope": "Company-A",
                                   "criterion_mapping": {"quote": text, "start": 0,
                                                         "end": len(text.encode())},
                                   "mapping_review_id": "MAP"})
    case = CaseStore(root / "records.sqlite3")
    case.add_crl_evidence_review("CRL-1", case_basis_version=version, claim_id="C",
                                 criterion_id="CRL1-C1", quote_sha256=sha256_hex(text.encode()),
                                 decision="supports", findings={"market_need_hypothesis": True},
                                 subject_scope="Company-A", support_scope="仅支持CRL1-C1",
                                 reviewer="r2a-test",
                                 review_basis="合成离线CRL复核")
    case.close()
    return basis, catalog


def test_r2a_exposes_a_case_backed_crl_dimension_runner():
    assert callable(getattr(runner, "run_crl_dimension_slice", None)), \
        "R2-A 必须提供读取Case、冻结受控CRL复核并输出维度结果的入口"


def test_case_backed_crl_slice_persists_reviews_and_reports_real_gaps(tmp_path):
    text = "Company-A identifies market demand. Published 2026-07-01."
    basis = {
        "subject_legal_name": "Company-A", "subject_aliases": [],
        "evidence_cutoff": "2026-08-27T03:02:29Z",
        "subject_source_basis": json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed"}),
    }
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": "Company-A"}).encode())
        case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
        source = blobs.put_bytes(text.encode())
        case.add_source("S", source.sha256, len(text.encode()), source_family="news-media",
                        capture_status="raw_capture_validated")
        start = len(text[:text.index("2026-07-01")].encode())
        case.append_time_evidence("S", {"kind": "document_self_date", "date": "2026-07-01",
                                         "date_locator": {"kind": "byte_range", "start": start,
                                                          "end": start + len("2026-07-01")}})
        version = case.set_case_basis(**basis)
        case.add_mapping_review("MAP", case_basis_version=version, claim_id="C",
                                criterion_id="CRL1-C1", quote_sha256=source.sha256,
                                support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
                                reviewer="r2a-test", review_basis="合成受控映射复核")
    finally:
        case.close()
    catalog = build_catalog_from_wheel()
    runner.run_criterion_slice(tmp_path, source_id="S", criterion_id="CRL1-C1",
                               catalog=catalog, case_basis=basis, claim_spec={
                                   "claim_id": "C", "locator_kind": "byte_range", "start": 0,
                                   "end": len(text.encode()), "interpretation": text,
                                   "subject_scope": "Company-A",
                                   "criterion_mapping": {"quote": text, "start": 0,
                                                         "end": len(text.encode())},
                                   "mapping_review_id": "MAP"})
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.add_crl_evidence_review("CRL-1", case_basis_version=version, claim_id="C",
                                     criterion_id="CRL1-C1", quote_sha256=sha256_hex(text.encode()),
                                     decision="supports", findings={"market_need_hypothesis": True},
                                     subject_scope="Company-A", support_scope="仅支持CRL1-C1",
                                     reviewer="r2a-test",
                                     review_basis="合成离线CRL复核")
    finally:
        case.close()
    result = runner.run_crl_dimension_slice(tmp_path, catalog=catalog, case_basis=basis,
                                            scope="Company-A")
    c1 = next(row for row in result["dimension"]["criteria"] if row["criterion_id"] == "CRL1-C1")
    c2 = next(row for row in result["dimension"]["criteria"] if row["criterion_id"] == "CRL1-C2")
    assert c1["native_disposition"] == "met"
    assert c2["product_status"] == "insufficient"
    assert result["dimension"]["attained_level"] is None
    assert (tmp_path / "audit" / "crl-dimension-r2a.json").is_file()
    replay = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A")
    assert replay["result_id"] == result["result_id"]
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert case.count_crl_dimension_results() == 1
    finally:
        case.close()


def test_missing_original_bytes_is_execution_failure_not_business_insufficient(tmp_path):
    basis, catalog = _seed_reviewed_case(tmp_path)
    for path in (tmp_path / "blobs").rglob("*"):
        if path.is_file():
            path.unlink()
    result = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A")
    c1 = next(row for row in result["dimension"]["criteria"] if row["criterion_id"] == "CRL1-C1")
    assert result["dimension"]["product_status"] == "execution_failed"
    assert c1["native_disposition"] is None


def test_changed_claim_is_execution_failure_and_changes_frozen_identity(tmp_path):
    basis, catalog = _seed_reviewed_case(tmp_path)
    first = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A")
    case = CaseStore(tmp_path / "records.sqlite3")
    with case._conn:
        case._conn.execute("UPDATE claims SET interpretation='篡改解释' WHERE claim_id='C'")
    case.close()
    second = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A")
    assert second["dimension"]["product_status"] == "execution_failed"
    assert second["input_digest"] != first["input_digest"]


def test_dimension_scope_must_equal_frozen_case_subject(tmp_path):
    basis, catalog = _seed_reviewed_case(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        runner.run_crl_dimension_slice(
            tmp_path, catalog=catalog, case_basis=basis, scope="Other-Company")


def test_crl_allowed_review_ids_freezes_exact_selector_without_case_scan(tmp_path):
    basis, catalog = _seed_reviewed_case(tmp_path)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.add_crl_evidence_review(
            "CRL-OTHER", case_basis_version=1, claim_id="C",
            criterion_id="CRL1-C1", quote_sha256=sha256_hex(
                "Company-A identifies market demand. Published 2026-07-01.".encode()),
            decision="does_not_support", findings={"market_need_hypothesis": False},
            subject_scope="Company-A", support_scope="仅支持CRL1-C1",
            reviewer="other-job", review_basis="另一job受控复核")
    finally:
        case.close()

    result = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A",
        allowed_review_ids={"CRL-1"})

    assert result["frozen_inputs"]["review_selector"]["allowed_review_ids"] == ["CRL-1"]
    assert [item["review_id"] for item in result["frozen_inputs"]["reviews"]] == ["CRL-1"]


def test_dimension_trace_rechecks_frozen_claim_and_source(tmp_path):
    basis, catalog = _seed_reviewed_case(tmp_path)
    result = runner.run_crl_dimension_slice(
        tmp_path, catalog=catalog, case_basis=basis, scope="Company-A")
    assert callable(getattr(audit, "trace_crl_dimension", None))
    case = CaseStore(tmp_path / "records.sqlite3")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        assert audit.trace_crl_dimension(case, blobs, result["result_id"])["ok"]
        with case._conn:
            case._conn.execute("UPDATE claims SET interpretation='篡改' WHERE claim_id='C'")
        traced = audit.trace_crl_dimension(case, blobs, result["result_id"])
        assert not traced["ok"]
        assert any("Claim" in item for item in traced["broken"])
    finally:
        case.close()


REAL_R2A = os.environ.get("KTH_REAL_CASE_DIR_R2A")


@pytest.mark.skipif(not REAL_R2A, reason="未设置KTH_REAL_CASE_DIR_R2A")
def test_real_r2a_crl_dimension_and_trace():
    root = Path(REAL_R2A)
    case = CaseStore(root / "records.sqlite3")
    try:
        basis = case.get_case_basis()
    finally:
        case.close()
    result = runner.run_crl_dimension_slice(
        root, catalog=build_catalog_from_wheel(), case_basis=basis,
        scope=basis["subject_legal_name"])
    rows = result["dimension"]["criteria"]
    met = [row["criterion_id"] for row in rows if row["native_disposition"] == "met"]
    assert met == ["CRL1-C1"]
    assert sum(row["product_status"] == "insufficient" for row in rows) == 12
    assert result["dimension"]["attained_level"] is None
    case = CaseStore(root / "records.sqlite3")
    try:
        traced = audit.trace_crl_dimension(
            case, BlobStore(root / "blobs"), result["result_id"])
        assert traced["ok"], traced["broken"]
    finally:
        case.close()
