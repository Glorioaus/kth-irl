"""R2-A：CRL 离线维度 runner 必须只消费落库的受控复核记录。"""

from __future__ import annotations

import kth_hybrid.runner as runner
import json

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore


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
                                     support_scope="仅支持CRL1-C1", reviewer="r2a-test",
                                     review_basis="合成离线CRL复核")
    finally:
        case.close()
    result = runner.run_crl_dimension_slice(tmp_path, catalog=catalog, case_basis=basis,
                                            scope="合成产品单元")
    c1 = next(row for row in result["dimension"]["criteria"] if row["criterion_id"] == "CRL1-C1")
    c2 = next(row for row in result["dimension"]["criteria"] if row["criterion_id"] == "CRL1-C2")
    assert c1["native_disposition"] == "met"
    assert c2["product_status"] == "insufficient"
    assert result["dimension"]["attained_level"] is None
    assert (tmp_path / "audit" / "crl-dimension-r2a.json").is_file()
