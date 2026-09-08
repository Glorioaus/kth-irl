"""R1.4-A 回归：封存时间、登记、第一方归属与主体别名。"""

from __future__ import annotations

import json

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.qualification import qualify_claim
from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
CUTOFF = "2026-08-27T03:02:29Z"


def _identity(subject: str = SUBJECT, aliases: list[str] | None = None) -> bytes:
    return json.dumps({"subject": subject, "aliases": aliases or []}).encode("utf-8")


def _basis(*, aliases: list[str] | None = None, aliases_path: str | None = None) -> dict:
    source_basis = {
        "kind": "field_reference",
        "path": "case:identity.json#/subject",
        "status": "claimed",
    }
    if aliases_path is not None:
        source_basis["aliases_path"] = aliases_path
    return {
        "subject_legal_name": SUBJECT,
        "subject_aliases": aliases or [],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps(source_basis),
    }


def _qualify(tmp_path, text: str, *, basis: dict | None = None,
             identity_aliases: list[str] | None = None, **source_changes):
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        identity = blobs.put_bytes(_identity(aliases=identity_aliases))
        case.add_import_record("case_provenance", "session:identity.json",
                               identity.sha256)
        data = text.encode("utf-8")
        source_ref = blobs.put_bytes(data)
        source = {
            "source_id": "S",
            "blob_sha256": source_ref.sha256,
            "byte_length": len(data),
            "source_family": "news-media",
            "capture_status": "raw_capture_validated",
        }
        source.update(source_changes)
        claim = {
            "claim_id": "C",
            "source_id": "S",
            "locator_kind": "byte_range",
            "locator_start": 0,
            "locator_end": len(data),
            "excerpt_sha256": sha256_hex(data),
            "excerpt_text": text,
            "interpretation": "来源陈述的窄主张",
            "subject_scope": SUBJECT,
        }
        return qualify_claim(claim, source, blobs, basis or _basis(), case=case)
    finally:
        case.close()


def _dated_evidence(text: str, timestamp: str) -> dict:
    start = len(text[:text.index(timestamp)].encode("utf-8"))
    return {
        "kind": "document_self_date",
        "date": timestamp,
        "date_locator": {
            "kind": "byte_range",
            "start": start,
            "end": start + len(timestamp.encode("utf-8")),
        },
    }


@pytest.mark.parametrize(
    ("timestamp", "expected_status"),
    [
        ("2026-08-26T23:30:00-08:00", "rejected"),
        ("2026-08-27T09:00:00+08:00", "qualified"),
    ],
)
def test_a1_explicit_timezone_is_compared_as_real_instant(
        tmp_path, timestamp, expected_status):
    text = f"{SUBJECT} identifies market demand. Published {timestamp}."
    outcome = _qualify(
        tmp_path, text, time_evidence=_dated_evidence(text, timestamp))
    assert outcome.status == expected_status


def test_a1_zulu_and_pre_cutoff_date_precision_remain_legal(tmp_path):
    zulu = f"{SUBJECT} identifies market demand. Published 2026-08-27T03:02:29Z."
    zulu_outcome = _qualify(
        tmp_path / "zulu", zulu,
        time_evidence=_dated_evidence(zulu, "2026-08-27T03:02:29Z"))
    assert zulu_outcome.status == "qualified"

    dated = f"{SUBJECT} identifies market demand. Published 2026-08-26."
    date_outcome = _qualify(
        tmp_path / "date", dated,
        time_evidence=_dated_evidence(dated, "2026-08-26"))
    assert date_outcome.status == "qualified"


def test_a1_sourced_timezone_rule_converts_unzoned_clock_to_utc(tmp_path):
    timestamp = "2026-01-31 17:17:00"
    text = f"{SUBJECT} identifies market demand. Published {timestamp}."
    evidence = _dated_evidence(text, timestamp)
    evidence["date"] = "2026-01-31T17:17:00+08:00"
    evidence["timezone_rule"] = "+08:00"
    blobs = BlobStore(tmp_path / "blobs")
    rule = blobs.put_bytes(json.dumps({
        "timezone": "+08:00",
        "source_family": "news-media",
    }).encode("utf-8"))
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        case.add_import_record("case_provenance", "session:timezone-rules.json",
                               rule.sha256)
    finally:
        case.close()
    evidence["timezone_basis"] = {
        "kind": "field_reference",
        "path": "case:timezone-rules.json#/timezone",
    }
    evidence["timezone_scope_ref"] = {
        "kind": "field_reference",
        "path": "case:timezone-rules.json#/source_family",
    }

    outcome = _qualify(tmp_path, text, time_evidence=evidence)

    assert outcome.status == "qualified"
    assert "2026-01-31T09:17:00+00:00" in outcome.time_judgment.basis


def test_a3_outer_document_hash_cannot_override_sealed_registration(tmp_path):
    text = f"{SUBJECT} identifies market demand."
    blobs = BlobStore(tmp_path / "blobs")
    record = blobs.put_bytes(json.dumps({
        "document_sha256": "f" * 64,
        "registered_at": "2026-07-01T00:00:00Z",
    }).encode("utf-8"))
    outcome = _qualify(
        tmp_path, text,
        time_evidence={
            "kind": "registered_at",
            "date": "2026-07-01T00:00:00Z",
            "registration_proof": {
                "blob_sha256": record.sha256,
                "field": "registered_at",
                "document_sha256": sha256_hex(text.encode("utf-8")),
            },
        })
    assert outcome.status == "rejected"
    assert outcome.time_judgment.verdict == "fail"


def test_a4_competitor_mention_is_not_first_party_attribution(tmp_path):
    text = (
        "Company-B owns this report. Company-A is our competitor. "
        "Published 2026-07-01."
    )
    outcome = _qualify(
        tmp_path, text,
        source_family="owner_attachment",
        document_subject=SUBJECT,
        document_subject_basis={
            "kind": "byte_range",
            "start": 0,
            "end": len(text.encode("utf-8")),
        },
        time_evidence=_dated_evidence(text, "2026-07-01"),
    )
    assert outcome.identity_judgment.verdict != "ok"
    assert outcome.status != "qualified"


def test_a5_caller_alias_without_sealed_alias_source_is_not_accepted(tmp_path):
    text = "Company-B identifies market demand. Published 2026-07-01."
    outcome = _qualify(
        tmp_path, text,
        basis=_basis(aliases=["Company-B"]),
        identity_aliases=[],
        time_evidence=_dated_evidence(text, "2026-07-01"),
    )
    assert outcome.identity_judgment.verdict != "ok"
    assert outcome.status != "qualified"


def test_a5_alias_from_sealed_identity_source_remains_legal(tmp_path):
    text = "Company-A简称 identifies market demand. Published 2026-07-01."
    outcome = _qualify(
        tmp_path, text,
        basis=_basis(
            aliases=["Company-A简称"],
            aliases_path="case:identity.json#/aliases",
        ),
        identity_aliases=["Company-A简称"],
        time_evidence=_dated_evidence(text, "2026-07-01"),
    )
    assert outcome.status == "qualified"
