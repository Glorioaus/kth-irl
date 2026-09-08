"""R1.4-B 回归：候选输入不得冒充映射确认或合法 N/A。"""

from __future__ import annotations

import json

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.runner import run_criterion_slice
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
CUTOFF = "2026-08-27T03:02:29Z"
UNTRUSTED_CONFIRMATION = {
    "confirmed": True,
    "confirmator": "missing-reviewer",
    "review_basis": "调用方自报的审核完成",
}


def _catalog():
    return build_catalog_from_wheel()


def _frl_criterion() -> str:
    return next(
        item["criterion_id"]
        for item in _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
        if item.get("na_policy") == "explicit_no_external_financing_only"
    )


def _basis() -> dict:
    return {
        "subject_legal_name": SUBJECT,
        "subject_aliases": [],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity.json#/subject",
            "status": "claimed",
        }),
    }


def _seed_case(tmp_path, text: str) -> None:
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT}).encode("utf-8"))
        case.add_import_record("case_provenance", "session:identity.json",
                               identity.sha256)
        raw = text.encode("utf-8")
        source = blobs.put_bytes(raw)
        case.add_source("S", source.sha256, len(raw), source_family="news-media",
                        capture_status="raw_capture_validated")
        date = "2026-07-01"
        start = len(text[:text.index(date)].encode("utf-8"))
        case.append_time_evidence("S", {
            "kind": "document_self_date",
            "date": date,
            "date_locator": {
                "kind": "byte_range",
                "start": start,
                "end": start + len(date),
            },
        })
    finally:
        case.close()


def _spec(text: str, *, na_proposal: dict | None = None,
          mapping_review_id: str | None = None) -> dict:
    spec = {
        "claim_id": "C-R1-4-B",
        "locator_kind": "byte_range",
        "start": 0,
        "end": len(text.encode("utf-8")),
        "interpretation": "来源陈述的窄主张。",
        "subject_scope": SUBJECT,
        "criterion_mapping": {
            "quote": text,
            "start": 0,
            "end": len(text.encode("utf-8")),
        },
        "semantic_confirmation": dict(UNTRUSTED_CONFIRMATION),
    }
    if na_proposal is not None:
        spec["na_proposal"] = na_proposal
    if mapping_review_id is not None:
        spec["mapping_review_id"] = mapping_review_id
    return spec


def _run(tmp_path, text: str, *, criterion_id: str = "CRL1-C1",
         na_proposal: dict | None = None, mapping_review_id: str | None = None):
    return run_criterion_slice(
        tmp_path,
        source_id="S",
        claim_spec=_spec(text, na_proposal=na_proposal,
                         mapping_review_id=mapping_review_id),
        criterion_id=criterion_id,
        catalog=_catalog(),
        case_basis=_basis(),
    )


def test_b1_untrusted_confirmation_cannot_confirm_mapping(tmp_path):
    text = "Company-A market department office wall is blue. Published 2026-07-01."
    _seed_case(tmp_path, text)

    result = _run(tmp_path, text)

    assert result["mapping_status"] != "confirmed"
    assert result["product_status"] != "succeeded"


def test_b2_injected_resolved_na_is_not_accepted(tmp_path):
    text = "Company-A office wall is blue. Published 2026-07-01."
    _seed_case(tmp_path, text)

    result = _run(
        tmp_path, text, criterion_id=_frl_criterion(),
        na_proposal={
            "proposal": "not_applicable",
            "applicability_resolved": {
                "_resolved": True,
                "value": "no external financing",
                "path": "case:missing-policy.json#/statement",
            },
            "flag_resolved": {
                "_resolved": True,
                "value": True,
                "path": "case:missing-policy.json#/flag",
            },
        },
    )

    assert result["product_status"] != "succeeded"


def test_b3_other_subject_and_string_false_cannot_create_na(tmp_path):
    text = "Company-A office wall is blue. Published 2026-07-01."
    _seed_case(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    policy = blobs.put_bytes(json.dumps({
        "subject": "Company-B",
        "applicability": "no_external_financing",
        "flag": "false",
    }).encode("utf-8"))
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.add_import_record("case_provenance", "session:policy.json", policy.sha256)
    finally:
        case.close()

    result = _run(
        tmp_path, text, criterion_id=_frl_criterion(),
        na_proposal={
            "proposal": "not_applicable",
            "applicability_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/applicability",
            },
            "flag_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/flag",
            },
            "subject_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/subject",
            },
        },
    )

    assert result["product_status"] != "succeeded"


def test_b1_controlled_review_bound_to_current_quote_can_confirm(tmp_path):
    from kth_hybrid.audit import trace

    text = "Company-A identifies market demand. Published 2026-07-01."
    _seed_case(tmp_path, text)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        version = case.set_case_basis(**_basis())
        case.add_mapping_review(
            "REV-B1-OK",
            case_basis_version=version,
            claim_id="C-R1-4-B",
            criterion_id="CRL1-C1",
            quote_sha256=sha256_hex(text.encode("utf-8")),
            support_scope="仅支持来源陈述市场需求假设",
            decision="confirmed",
            reviewer="r1_4_offline_review",
            review_basis="已人工复核引文与 CRL1-C1 的窄关系",
        )
    finally:
        case.close()

    result = _run(tmp_path, text, mapping_review_id="REV-B1-OK")

    assert result["mapping_status"] == "confirmed"
    assert result["product_status"] == "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                     strict=False).ok
    finally:
        case.close()


def test_b1_review_for_another_quote_cannot_be_reused(tmp_path):
    text = "Company-A identifies market demand. Published 2026-07-01."
    _seed_case(tmp_path, text)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        version = case.set_case_basis(**_basis())
        case.add_mapping_review(
            "REV-B1-WRONG-QUOTE",
            case_basis_version=version,
            claim_id="C-R1-4-B",
            criterion_id="CRL1-C1",
            quote_sha256=sha256_hex(b"other quote"),
            support_scope="仅支持另一段引文",
            decision="confirmed",
            reviewer="r1_4_offline_review",
            review_basis="该记录故意绑定另一段引文",
        )
    finally:
        case.close()

    result = _run(tmp_path, text, mapping_review_id="REV-B1-WRONG-QUOTE")

    assert result["mapping_status"] != "confirmed"
    assert result["product_status"] != "succeeded"


def test_b3_current_subject_boolean_policy_is_legal_na_and_traceable(tmp_path):
    from kth_hybrid.audit import trace

    text = "Company-A office wall is blue. Published 2026-07-01."
    _seed_case(tmp_path, text)
    blobs = BlobStore(tmp_path / "blobs")
    policy = blobs.put_bytes(json.dumps({
        "subject": SUBJECT,
        "applicability": "no_external_financing",
        "flag": True,
    }).encode("utf-8"))
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.add_import_record("case_provenance", "session:policy.json", policy.sha256)
    finally:
        case.close()

    result = _run(
        tmp_path, text, criterion_id=_frl_criterion(),
        na_proposal={
            "proposal": "not_applicable",
            "applicability_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/applicability",
            },
            "flag_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/flag",
            },
            "subject_ref": {
                "kind": "field_reference",
                "path": "case:policy.json#/subject",
            },
        },
    )

    assert result["product_status"] == "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                     strict=False).ok
    finally:
        case.close()
