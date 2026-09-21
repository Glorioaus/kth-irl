"""R1.5：证明必须表达主体、对象与作用域之间的关系。"""

from __future__ import annotations

import json
from pathlib import Path

from kth_hybrid.audit import trace
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.qualification import qualify_claim
from kth_hybrid.runner import run_criterion_slice
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
CUTOFF = "2026-08-27T03:02:29Z"


def _catalog():
    return build_catalog_from_wheel()


def _ref(file: str, field: str) -> dict:
    return {"kind": "field_reference", "path": f"case:{file}#/{field}"}


def _basis(*, aliases: list[str] | None = None,
           aliases_path: str | None = None,
           aliases_subject_path: str | None = None) -> dict:
    source_basis = {**_ref("identity.json", "subject"), "status": "claimed"}
    if aliases_path is not None:
        source_basis["aliases_path"] = aliases_path
    if aliases_subject_path is not None:
        source_basis["aliases_subject_path"] = aliases_subject_path
    return {
        "subject_legal_name": SUBJECT,
        "subject_aliases": aliases or [],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps(source_basis),
    }


def _slice_spec(text: str, *, claim_id: str = "C", review_id: str = "REVIEW",
                na_proposal: dict | None = None) -> dict:
    spec = {
        "claim_id": claim_id,
        "locator_kind": "byte_range",
        "start": 0,
        "end": len(text.encode("utf-8")),
        "interpretation": "来源陈述市场需求假设。",
        "subject_scope": SUBJECT,
        "criterion_mapping": {
            "quote": text,
            "start": 0,
            "end": len(text.encode("utf-8")),
        },
        "mapping_review_id": review_id,
    }
    if na_proposal is not None:
        spec["na_proposal"] = na_proposal
    return spec


def _seed(root: Path, text: str, *, basis: dict | None = None,
          source_family: str = "news-media",
          document_subject: str | None = None,
          document_subject_basis=None) -> tuple[dict, str]:
    basis = basis or _basis()
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT, "aliases": []}).encode())
        case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
        raw = text.encode("utf-8")
        source = blobs.put_bytes(raw)
        case.add_source(
            "S", source.sha256, len(raw), source_family=source_family,
            capture_status="raw_capture_validated", document_subject=document_subject,
            document_subject_basis=(json.dumps(document_subject_basis)
                                    if isinstance(document_subject_basis, dict)
                                    else document_subject_basis),
        )
        date = "2026-07-01"
        if date in text:
            start = len(text[:text.index(date)].encode("utf-8"))
            case.append_time_evidence("S", {
                "kind": "document_self_date", "date": date,
                "date_locator": {"kind": "byte_range", "start": start,
                                 "end": start + len(date)},
            })
        version = case.set_case_basis(**basis)
        case.add_mapping_review(
            "REVIEW", case_basis_version=version, claim_id="C",
            criterion_id="CRL1-C1", quote_sha256=sha256_hex(raw),
            support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
            reviewer="r1_5_synthetic_offline_review",
            review_basis="受控合成复核记录，不代表Owner或独立审核。",
        )
    finally:
        case.close()
    return basis, source.sha256


def _run(root: Path, text: str, basis: dict, *, criterion_id="CRL1-C1",
         na_proposal: dict | None = None) -> dict:
    return run_criterion_slice(
        root, source_id="S", claim_spec=_slice_spec(text, na_proposal=na_proposal),
        criterion_id=criterion_id, catalog=_catalog(), case_basis=basis,
    )


def _frl() -> str:
    return next(
        item["criterion_id"]
        for item in _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
        if item.get("na_policy") == "explicit_no_external_financing_only"
    )


def test_b1_spliced_na_subject_is_not_legal_na(tmp_path):
    text = "Company-A office wall is blue. Published 2026-07-01."
    basis, _ = _seed(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        policy = blobs.put_bytes(json.dumps({
            "subject": "Company-B", "applicability": "no_external_financing",
            "flag": True,
        }).encode())
        case.add_import_record("case_provenance", "session:company-b-policy.json",
                               policy.sha256)
    finally:
        case.close()

    result = _run(tmp_path, text, basis, criterion_id=_frl(), na_proposal={
        "proposal": "not_applicable",
        "applicability_ref": _ref("company-b-policy.json", "applicability"),
        "flag_ref": _ref("company-b-policy.json", "flag"),
        "subject_ref": _ref("identity.json", "subject"),
    })

    assert result["product_status"] != "succeeded"


def test_a1_unresolved_timezone_basis_stays_nonqualified(tmp_path):
    text = "Company-A identifies market demand. Published 2026-08-27 10:00:00."
    basis, _ = _seed(tmp_path, text)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.append_time_evidence("S", {
            "kind": "document_self_date", "date": "2026-08-27T10:00:00+08:00",
            "date_locator": {"kind": "byte_range", "start": 0,
                             "end": len(text.encode("utf-8"))},
            "timezone_rule": "+08:00",
            "timezone_basis": "case:missing-rule.json#/timezone",
        })
    finally:
        case.close()

    result = _run(tmp_path, text, basis)

    assert result["qualification_status"] != "qualified"
    assert result["product_status"] != "succeeded"


def test_a2_first_party_subject_and_document_hash_cannot_be_spliced(tmp_path):
    text = "Company-A identifies market demand. Published 2026-07-01."
    basis, source_sha = _seed(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        ownership = blobs.put_bytes(json.dumps({
            "subject": "Company-B", "document_sha256": source_sha,
        }).encode())
        case.add_import_record("case_provenance", "session:company-b-ownership.json",
                               ownership.sha256)
        source = case.fetch_one("sources", "source_id", "S")
        claim = {
            "claim_id": "C", "source_id": "S", "locator_kind": "byte_range",
            "locator_start": 0, "locator_end": len(text.encode("utf-8")),
            "excerpt_sha256": source_sha, "excerpt_text": text,
            "interpretation": text, "subject_scope": SUBJECT,
        }
        outcome = qualify_claim(
            claim,
            {**source, "source_family": "owner_attachment",
             "document_subject": SUBJECT,
             "document_subject_basis": {
                 "kind": "case_field_reference",
                 "path": "case:identity.json#/subject",
                 "document_sha256_path":
                     "case:company-b-ownership.json#/document_sha256",
             }, "time_evidence": case.latest_time_evidence("S")},
            blobs, basis, case=case,
        )
    finally:
        case.close()

    assert outcome.status != "qualified"
    assert outcome.identity_judgment.verdict != "ok"


def test_a3_persisted_first_party_proof_runs_after_reopen_and_replays(tmp_path):
    text = "Company-A identifies market demand. Published 2026-07-01."
    proof = {
        "kind": "case_field_reference",
        "path": "case:ownership.json#/subject",
        "document_sha256_path": "case:ownership.json#/document_sha256",
    }
    basis, source_sha = _seed(
        tmp_path, text, source_family="owner_attachment",
        document_subject=SUBJECT, document_subject_basis=proof,
    )
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        ownership = blobs.put_bytes(json.dumps({
            "subject": SUBJECT, "document_sha256": source_sha,
        }).encode())
        case.add_import_record("case_provenance", "session:ownership.json",
                               ownership.sha256)
        source = case.fetch_one("sources", "source_id", "S")
        direct_claim = {
            "claim_id": "C", "source_id": "S", "locator_kind": "byte_range",
            "locator_start": 0, "locator_end": len(text.encode("utf-8")),
            "excerpt_sha256": source_sha, "excerpt_text": text,
            "interpretation": text, "subject_scope": SUBJECT,
        }
        direct = qualify_claim(
            direct_claim,
            {**source, "document_subject_basis": proof,
             "time_evidence": case.latest_time_evidence("S")},
            blobs, basis, case=case,
        )
    finally:
        case.close()

    assert direct.status == "qualified"
    first = _run(tmp_path, text, basis)
    assert first["product_status"] == "succeeded", first
    second = _run(tmp_path, text, basis)
    assert first["qualification_status"] == "qualified"
    assert first["product_status"] == "succeeded"
    assert second["result_id"] == first["result_id"]
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert trace(case, BlobStore(tmp_path / "blobs"), first["result_id"],
                     strict=False).ok
    finally:
        case.close()


def test_c2_alias_proof_used_by_qualification_is_in_trace_closure(tmp_path):
    text = "Alias-A identifies market demand. Published 2026-07-01."
    basis = _basis(
        aliases=["Alias-A"], aliases_path="case:aliases.json#/aliases",
        aliases_subject_path="case:aliases.json#/subject")
    basis, _ = _seed(tmp_path, text, basis=basis)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        aliases = blobs.put_bytes(json.dumps({
            "subject": SUBJECT, "aliases": ["Alias-A"],
        }).encode())
        case.add_import_record("case_provenance", "session:aliases.json", aliases.sha256)
    finally:
        case.close()

    # 新增别名证明后建立与该 CaseBasis 对应的新版本及受控映射复核。
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        version = case.set_case_basis_if_changed(**basis)
        case.add_mapping_review(
            "REVIEW-ALIAS", case_basis_version=version, claim_id="C",
            criterion_id="CRL1-C1", quote_sha256=sha256_hex(text.encode("utf-8")),
            support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
            reviewer="r1_5_synthetic_offline_review",
            review_basis="受控合成别名复核记录",
        )
    finally:
        case.close()
    spec = _slice_spec(text, review_id="REVIEW-ALIAS")
    result = run_criterion_slice(
        tmp_path, source_id="S", claim_spec=spec, criterion_id="CRL1-C1",
        catalog=_catalog(), case_basis=basis,
    )
    assert result["product_status"] == "succeeded"

    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='session:aliases.json'")
        report = trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                       strict=False)
    finally:
        case.close()

    assert not report.ok
