"""R2-A.1：维度结果必须冻结并追溯实际使用的完整资格视图。"""

from __future__ import annotations

import json

import kth_hybrid.audit as audit
import kth_hybrid.runner as runner
import pytest
from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
ALIAS = "Alias-A"
CUTOFF = "2026-08-27T03:02:29Z"


def _seed_reviewed_case(root, *, timezone_rule: str | None = None,
                        first_party_owner: str | None = None):
    owner_name = first_party_owner or SUBJECT
    if timezone_rule:
        visible_time = "2026-07-01 17:17:00"
        declared_time = f"2026-07-01T17:17:00{timezone_rule}"
    else:
        visible_time = "2026-07-01"
        declared_time = visible_time
    text = f"{owner_name} identifies market demand. Published {visible_time}."
    aliases = [ALIAS] if first_party_owner == ALIAS else []
    source_basis = {
        "kind": "field_reference",
        "path": "case:identity.json#/subject",
        "status": "claimed",
    }
    if aliases:
        source_basis.update({
            "aliases_path": "case:identity.json#/aliases",
            "aliases_subject_path": "case:identity.json#/subject",
        })
    basis = {
        "subject_legal_name": SUBJECT,
        "subject_aliases": aliases,
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps(source_basis),
    }

    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({
            "subject": SUBJECT, "aliases": aliases,
        }).encode())
        case.add_import_record("case_provenance", "session:identity.json", identity.sha256)
        source_blob = blobs.put_bytes(text.encode())
        ownership_basis = None
        source_family = "news-media"
        if first_party_owner:
            source_family = "owner_attachment"
            ownership_basis = {
                "kind": "case_field_reference",
                "path": "case:ownership.json#/owner.subject",
                "document_sha256_path":
                    "case:ownership.json#/owner.document_sha256",
            }
        case.add_source(
            "S", source_blob.sha256, len(text.encode()),
            source_family=source_family,
            capture_status="raw_capture_validated",
            document_subject=owner_name if first_party_owner else None,
            document_subject_basis=(json.dumps(ownership_basis)
                                    if ownership_basis else None),
        )
        if first_party_owner:
            ownership = blobs.put_bytes(json.dumps({
                "owner": {
                    "subject": owner_name,
                    "document_sha256": source_blob.sha256,
                },
            }).encode())
            case.add_import_record(
                "case_provenance", "session:ownership.json", ownership.sha256)

        start = len(text[:text.index(visible_time)].encode())
        time_evidence = {
            "kind": "document_self_date",
            "date": declared_time,
            "date_locator": {
                "kind": "byte_range", "start": start,
                "end": start + len(visible_time.encode()),
            },
        }
        if timezone_rule:
            timezone = blobs.put_bytes(json.dumps({
                "rule": timezone_rule, "family": source_family,
            }).encode())
            case.add_import_record(
                "case_provenance", "case:timezone-rules.json", timezone.sha256)
            time_evidence.update({
                "timezone_rule": timezone_rule,
                "timezone_basis": {
                    "kind": "field_reference",
                    "path": "case:timezone-rules.json#/rule",
                },
                "timezone_scope_ref": {
                    "kind": "field_reference",
                    "path": "case:timezone-rules.json#/family",
                },
            })
        revision = case.append_time_evidence("S", time_evidence)
        version = case.set_case_basis(**basis)
        case.add_mapping_review(
            "MAP", case_basis_version=version, claim_id="C",
            criterion_id="CRL1-C1", quote_sha256=source_blob.sha256,
            support_scope="仅支持来源陈述市场需求假设", decision="confirmed",
            reviewer="r2a1-test", review_basis="合成受控映射复核",
        )
    finally:
        case.close()

    catalog = build_catalog_from_wheel()
    runner.run_criterion_slice(
        root, source_id="S", criterion_id="CRL1-C1", catalog=catalog,
        case_basis=basis, claim_spec={
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(text.encode()), "interpretation": text,
            "subject_scope": SUBJECT,
            "criterion_mapping": {
                "quote": text, "start": 0, "end": len(text.encode()),
            },
            "mapping_review_id": "MAP",
        },
    )
    case = CaseStore(root / "records.sqlite3")
    try:
        case.add_crl_evidence_review(
            "CRL-1", case_basis_version=version, claim_id="C",
            criterion_id="CRL1-C1", quote_sha256=sha256_hex(text.encode()),
            decision="supports", findings={"market_need_hypothesis": True},
            subject_scope=SUBJECT, support_scope="仅支持CRL1-C1",
            reviewer="r2a1-test", review_basis="合成离线CRL复核",
        )
    finally:
        case.close()
    return basis, catalog, revision


def _run(root, basis, catalog):
    return runner.run_crl_dimension_slice(
        root, catalog=catalog, case_basis=basis, scope=SUBJECT)


def _trace(root, result_id):
    case = CaseStore(root / "records.sqlite3")
    try:
        return audit.trace_crl_dimension(case, BlobStore(root / "blobs"), result_id)
    finally:
        case.close()


def test_removed_used_timezone_proof_breaks_old_dimension_trace(tmp_path):
    basis, catalog, _ = _seed_reviewed_case(tmp_path, timezone_rule="+08:00")
    result = _run(tmp_path, basis, catalog)
    assert _trace(tmp_path, result["result_id"])["ok"]

    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='case:timezone-rules.json'")
    finally:
        case.close()

    traced = _trace(tmp_path, result["result_id"])
    assert not traced["ok"]
    assert any("时区" in item for item in traced["broken"])


def test_removed_used_time_revision_breaks_old_dimension_trace(tmp_path):
    basis, catalog, revision = _seed_reviewed_case(tmp_path)
    result = _run(tmp_path, basis, catalog)
    assert _trace(tmp_path, result["result_id"])["ok"]

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


def test_modified_used_time_revision_breaks_old_dimension_trace(tmp_path):
    basis, catalog, revision = _seed_reviewed_case(tmp_path)
    result = _run(tmp_path, basis, catalog)

    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        changed = {
            "kind": "document_self_date",
            "date": "2026-07-02",
            "date_locator": {"kind": "byte_range", "start": 47, "end": 57},
        }
        with case._conn:
            case._conn.execute(
                "UPDATE source_time_evidence SET evidence_json=? WHERE revision=?",
                (json.dumps(changed), revision),
            )
    finally:
        case.close()

    traced = _trace(tmp_path, result["result_id"])
    assert not traced["ok"]
    assert any("时间修订" in item for item in traced["broken"])


def test_new_time_basis_creates_new_identity_without_breaking_old_trace(tmp_path):
    basis, catalog, old_revision = _seed_reviewed_case(
        tmp_path, timezone_rule="+08:00")
    old_result = _run(tmp_path, basis, catalog)

    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        source = case.fetch_one("sources", "source_id", "S")
        old_time = case.latest_time_evidence("S")
        new_rule = blobs.put_bytes(json.dumps({
            "rule": "+07:00", "family": source["source_family"],
        }).encode())
        case.add_import_record(
            "case_provenance", "case:timezone-rules-v2.json", new_rule.sha256)
        new_time = {
            key: value for key, value in old_time.items()
            if key not in {"revision", "created_at"}
        }
        new_time.update({
            "date": "2026-07-01T17:17:00+07:00",
            "timezone_rule": "+07:00",
            "timezone_basis": {
                "kind": "field_reference",
                "path": "case:timezone-rules-v2.json#/rule",
            },
            "timezone_scope_ref": {
                "kind": "field_reference",
                "path": "case:timezone-rules-v2.json#/family",
            },
        })
        new_revision = case.append_time_evidence("S", new_time)
    finally:
        case.close()

    new_result = _run(tmp_path, basis, catalog)
    assert new_revision != old_revision
    assert new_result["input_digest"] != old_result["input_digest"]
    assert new_result["result_id"] != old_result["result_id"]
    assert _trace(tmp_path, old_result["result_id"])["ok"]
    assert _trace(tmp_path, new_result["result_id"])["ok"]

    replay = _run(tmp_path, basis, catalog)
    assert replay["result_id"] == new_result["result_id"]


@pytest.mark.parametrize("owner_name", [SUBJECT, ALIAS])
def test_first_party_view_binds_canonical_or_alias_owner_proof(tmp_path, owner_name):
    basis, catalog, revision = _seed_reviewed_case(
        tmp_path, first_party_owner=owner_name)
    result = _run(tmp_path, basis, catalog)
    binding = result["frozen_inputs"]["evidence_bindings"][0]
    view = binding["qualification_view"]
    assert view["time_evidence"]["revision"] == revision
    assert view["outcome"]["status"] == "qualified"
    assert "company_self_statement" in view["outcome"]["allowed_uses"]
    if owner_name == ALIAS:
        assert "aliases" in view["proof_bindings"]["case_basis"]
    expected_kind = "alias" if owner_name == ALIAS else "canonical"
    assert view["proof_bindings"]["document_subject"]["matched_subject_kind"] == expected_kind

    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='session:ownership.json'")
    finally:
        case.close()
    traced = _trace(tmp_path, result["result_id"])
    assert not traced["ok"]
    assert any("第一方归属" in item for item in traced["broken"])


def test_alias_proof_removal_breaks_alias_first_party_dimension_trace(tmp_path):
    basis, catalog, _ = _seed_reviewed_case(
        tmp_path, first_party_owner=ALIAS)
    result = _run(tmp_path, basis, catalog)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE origin_path='session:identity.json'")
    finally:
        case.close()

    traced = _trace(tmp_path, result["result_id"])
    assert not traced["ok"]
    assert any("别名" in item or "CaseBasis" in item for item in traced["broken"])


def test_publish_validation_failure_cannot_expose_met(tmp_path, monkeypatch):
    basis, catalog, _ = _seed_reviewed_case(tmp_path)
    monkeypatch.setattr(
        audit, "validate_crl_dimension_payload",
        lambda _case, _blobs, _result: ["注入的发布前完整性断裂"],
    )

    result = _run(tmp_path, basis, catalog)
    assert result["dimension"]["product_status"] == "execution_failed"
    assert all(row["native_disposition"] is None
               for row in result["dimension"]["criteria"])
    assert result["frozen_inputs"]["publish_validation_errors"] == [
        "注入的发布前完整性断裂"]
