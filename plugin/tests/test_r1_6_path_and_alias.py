"""R1.6：引用路径、证明关系和合法别名第一方必须走同一合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kth_hybrid.audit import trace
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.runner import run_criterion_slice
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
ALIAS = "Alias-A"
CUTOFF = "2026-08-27T03:02:29Z"


def _catalog():
    return build_catalog_from_wheel()


def _ref(file: str, field: str) -> dict:
    return {"kind": "field_reference", "path": f"case:{file}#/{field}"}


def _basis(*, use_independent_alias: bool = False) -> dict:
    source_basis = {**_ref("identity.json", "subject"), "status": "claimed"}
    aliases = []
    if use_independent_alias:
        aliases = [ALIAS]
        source_basis.update(
            aliases_path="case:aliases.json#/aliases",
            aliases_subject_path="case:aliases.json#/subject",
        )
    return {
        "subject_legal_name": SUBJECT,
        "subject_aliases": aliases,
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps(source_basis),
    }


def _frl() -> str:
    return next(
        row["criterion_id"]
        for row in _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
        if row.get("na_policy") == "explicit_no_external_financing_only"
    )


def _seed(root: Path, text: str, *, source_family: str = "news-media",
          document_subject: str | None = None, document_subject_basis=None,
          basis: dict | None = None) -> tuple[dict, str]:
    basis = basis or _basis()
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT}).encode("utf-8"))
        case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
        data = text.encode("utf-8")
        source = blobs.put_bytes(data)
        case.add_source(
            "S", source.sha256, len(data), source_family=source_family,
            capture_status="raw_capture_validated", document_subject=document_subject,
            document_subject_basis=(json.dumps(document_subject_basis)
                                    if isinstance(document_subject_basis, dict)
                                    else document_subject_basis),
        )
        date = "2026-07-01"
        start = len(text[:text.index(date)].encode("utf-8"))
        case.append_time_evidence("S", {
            "kind": "document_self_date", "date": date,
            "date_locator": {"kind": "byte_range", "start": start,
                             "end": start + len(date)},
        })
        version = case.set_case_basis(**basis)
        case.add_mapping_review(
            "REV", case_basis_version=version, claim_id="C", criterion_id="CRL1-C1",
            quote_sha256=sha256_hex(data), support_scope="仅支持来源陈述市场需求假设",
            decision="confirmed", reviewer="r1_6_synthetic_offline_review",
            review_basis="受控合成复核记录，不代表Owner或独立审核。",
        )
    finally:
        case.close()
    return basis, source.sha256


def _run(root: Path, text: str, basis: dict, *, criterion_id="CRL1-C1",
         na_proposal: dict | None = None) -> dict:
    spec = {
        "claim_id": "C", "locator_kind": "byte_range", "start": 0,
        "end": len(text.encode("utf-8")), "interpretation": "来源陈述市场需求假设。",
        "subject_scope": SUBJECT,
        "criterion_mapping": {"quote": text, "start": 0,
                              "end": len(text.encode("utf-8"))},
        "mapping_review_id": "REV",
    }
    if na_proposal is not None:
        spec["na_proposal"] = na_proposal
    return run_criterion_slice(
        root, source_id="S", claim_spec=spec, criterion_id=criterion_id,
        catalog=_catalog(), case_basis=basis,
    )


def _add_records(case: CaseStore, blobs: BlobStore) -> None:
    records = {
        "a": {"subject": SUBJECT},
        "b": {"subject": "Company-B", "applicability": "no_external_financing",
              "flag": True},
        "rows": [
            {"subject": SUBJECT},
            {"subject": "Company-B", "applicability": "no_external_financing",
             "flag": True},
        ],
    }
    blob = blobs.put_bytes(json.dumps(records).encode("utf-8"))
    case.add_import_record("case_provenance", "session:records.json", blob.sha256)


@pytest.mark.parametrize(
    ("applicability", "flag", "subject"),
    [
        ("b/applicability", "b/flag", "a/subject"),
        ("b.applicability", "b.flag", "a.subject"),
        ("rows[1].applicability", "rows[1].flag", "rows[0].subject"),
    ],
)
def test_cross_record_na_is_rejected_for_each_supported_path_syntax(
        tmp_path, applicability, flag, subject):
    text = "Company-A office wall is blue. Published 2026-07-01."
    basis, _ = _seed(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        _add_records(case, blobs)
    finally:
        case.close()

    result = _run(tmp_path, text, basis, criterion_id=_frl(), na_proposal={
        "proposal": "not_applicable",
        "applicability_ref": _ref("records.json", applicability),
        "flag_ref": _ref("records.json", flag),
        "subject_ref": _ref("records.json", subject),
    })

    assert result["product_status"] != "succeeded"


@pytest.mark.parametrize(
    ("subject", "applicability", "flag"),
    [
        ("b/subject", "b/applicability", "b/flag"),
        ("b.subject", "b.applicability", "b.flag"),
        ("rows[1].subject", "rows[1].applicability", "rows[1].flag"),
    ],
)
def test_same_record_na_remains_legal_for_each_supported_path_syntax(
        tmp_path, subject, applicability, flag):
    text = "Company-B office wall is blue. Published 2026-07-01."
    basis = {
        **_basis(),
        "subject_legal_name": "Company-B",
        "subject_source_basis": json.dumps({**_ref("records.json", subject),
                                              "status": "claimed"}),
    }
    # 先以Company-A身份建库；随后用同一records记录建立有效Company-B CaseBasis。
    _seed(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        _add_records(case, blobs)
        version = case.set_case_basis(**basis)
        case.add_mapping_review(
            "REV-B", case_basis_version=version, claim_id="C", criterion_id="CRL1-C1",
            quote_sha256=sha256_hex(text.encode("utf-8")),
            support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
            reviewer="r1_6_synthetic_offline_review", review_basis="同记录N/A正例",
        )
    finally:
        case.close()
    result = run_criterion_slice(
        tmp_path, source_id="S", criterion_id=_frl(), catalog=_catalog(),
        case_basis=basis, claim_spec={
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(text.encode("utf-8")), "interpretation": "N/A正例",
            "subject_scope": "Company-B",
            "criterion_mapping": {"quote": text, "start": 0,
                                  "end": len(text.encode("utf-8"))},
            "mapping_review_id": "REV-B",
            "na_proposal": {
                "proposal": "not_applicable",
                "applicability_ref": _ref("records.json", applicability),
                "flag_ref": _ref("records.json", flag),
                "subject_ref": _ref("records.json", subject),
            },
        },
    )
    assert result["product_status"] == "succeeded"


def test_dot_cross_record_first_party_proof_is_rejected(tmp_path):
    text = "Company-A identifies market demand. Published 2026-07-01."
    proof = {
        "kind": "case_field_reference", "path": "case:ownership.json#/a.subject",
        "document_sha256_path": "case:ownership.json#/b.document_sha256",
    }
    basis, source_sha = _seed(
        tmp_path, text, source_family="owner_attachment", document_subject=SUBJECT,
        document_subject_basis=proof,
    )
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        ownership = blobs.put_bytes(json.dumps({
            "a": {"subject": SUBJECT},
            "b": {"subject": "Company-B", "document_sha256": source_sha},
        }).encode())
        case.add_import_record("case_provenance", "session:ownership.json", ownership.sha256)
    finally:
        case.close()

    result = _run(tmp_path, text, basis)
    assert result["qualification_status"] != "qualified"
    assert result["product_status"] != "succeeded"


@pytest.mark.parametrize("owner_name", [SUBJECT, ALIAS])
def test_canonical_and_legal_alias_owner_proofs_persist_replay_and_trace(
        tmp_path, owner_name):
    text = f"{owner_name} identifies market demand. Published 2026-07-01."
    basis = _basis(use_independent_alias=owner_name == ALIAS)
    proof = {
        "kind": "case_field_reference", "path": "case:ownership.json#/owner.subject",
        "document_sha256_path": "case:ownership.json#/owner.document_sha256",
    }
    basis, source_sha = _seed(
        tmp_path, text, source_family="owner_attachment", document_subject=owner_name,
        document_subject_basis=proof, basis=basis,
    )
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        if owner_name == ALIAS:
            aliases = blobs.put_bytes(json.dumps({
                "subject": SUBJECT, "aliases": [ALIAS],
            }).encode())
            case.add_import_record("case_provenance", "session:aliases.json", aliases.sha256)
        ownership = blobs.put_bytes(json.dumps({
            "owner": {"subject": owner_name, "document_sha256": source_sha},
        }).encode())
        case.add_import_record("case_provenance", "session:ownership.json", ownership.sha256)
    finally:
        case.close()

    first = _run(tmp_path, text, basis)
    second = _run(tmp_path, text, basis)
    assert first["qualification_status"] == "qualified"
    assert first["product_status"] == "succeeded"
    assert second["result_id"] == first["result_id"]
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        frozen = json.loads(case.fetch_one(
            "criterion_results", "result_id", first["result_id"])["frozen_inputs"])
        assert "document_subject" in frozen["proof_bindings"]
        assert trace(case, BlobStore(tmp_path / "blobs"), first["result_id"],
                     strict=False).ok
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='session:ownership.json'")
        assert not trace(case, BlobStore(tmp_path / "blobs"), first["result_id"],
                         strict=False).ok
    finally:
        case.close()


def test_alias_proof_removal_breaks_alias_owner_trace(tmp_path):
    text = f"{ALIAS} identifies market demand. Published 2026-07-01."
    basis = _basis(use_independent_alias=True)
    proof = {
        "kind": "case_field_reference", "path": "case:ownership.json#/owner.subject",
        "document_sha256_path": "case:ownership.json#/owner.document_sha256",
    }
    basis, source_sha = _seed(
        tmp_path, text, source_family="owner_attachment", document_subject=ALIAS,
        document_subject_basis=proof, basis=basis,
    )
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        aliases = blobs.put_bytes(json.dumps({"subject": SUBJECT, "aliases": [ALIAS]}).encode())
        case.add_import_record("case_provenance", "session:aliases.json", aliases.sha256)
        ownership = blobs.put_bytes(json.dumps({
            "owner": {"subject": ALIAS, "document_sha256": source_sha},
        }).encode())
        case.add_import_record("case_provenance", "session:ownership.json", ownership.sha256)
    finally:
        case.close()

    result = _run(tmp_path, text, basis)
    assert result["product_status"] == "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='session:aliases.json'")
        assert not trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                         strict=False).ok
    finally:
        case.close()
