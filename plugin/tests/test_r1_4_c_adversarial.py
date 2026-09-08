"""R1.4-C 回归：有效输入、完整发布核验与主体证明闭包。

这些反例从独立审核目录移植到当前实现仓测试入口。测试本身断言
``kth_hybrid`` 从本工作树导入，避免误把审核快照当作修复后的实现。
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from kth_hybrid.store import BlobStore, CaseStore


SUBJECT = "Company-A"
ALIAS = "Company-A别名"
CUTOFF = "2026-08-27T03:02:29Z"
TEXT = "Company-A identifies market demand. Published 2026-07-01."
CONFIRMATION = {
    "confirmed": True,
    "confirmator": "r1_4-c-test-reviewer",
    "review_basis": "合成离线复核：来源陈述市场需求假设。",
}


def _catalog():
    from kth_hybrid.catalog import build_catalog_from_wheel

    return build_catalog_from_wheel()


def _basis(*, cutoff: str = CUTOFF, aliases: list[str] | None = None) -> dict:
    return {
        "subject_legal_name": SUBJECT,
        "subject_aliases": aliases if aliases is not None else [ALIAS],
        "evidence_cutoff": cutoff,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity.json#/subject",
            "status": "claimed",
        }),
    }


def _spec() -> dict:
    return {
        "claim_id": "C-R1-4",
        "locator_kind": "byte_range",
        "start": 0,
        "end": len(TEXT.encode("utf-8")),
        "interpretation": "第三方来源陈述 Company-A 已识别市场需求假设。",
        "subject_scope": SUBJECT,
        "criterion_mapping": {
            "quote": TEXT,
            "start": 0,
            "end": len(TEXT.encode("utf-8")),
        },
        "semantic_confirmation": dict(CONFIRMATION),
    }


def _seed_case(root: Path) -> None:
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        identity = blobs.put_bytes(json.dumps({"subject": SUBJECT}).encode("utf-8"))
        case.add_import_record("case_provenance", "session:identity.json",
                               identity.sha256)
        raw = TEXT.encode("utf-8")
        source = blobs.put_bytes(raw)
        case.add_source("S", source.sha256, len(raw), source_family="news-media",
                        capture_status="raw_capture_validated")
        start = len(TEXT[:TEXT.index("2026-07-01")].encode("utf-8"))
        case.append_time_evidence("S", {
            "kind": "document_self_date",
            "date": "2026-07-01",
            "date_locator": {
                "kind": "byte_range",
                "start": start,
                "end": start + len("2026-07-01"),
            },
        })
    finally:
        case.close()


def _run(root: Path, basis: dict):
    from kth_hybrid.runner import run_criterion_slice

    return run_criterion_slice(
        root,
        source_id="S",
        claim_spec=_spec(),
        criterion_id="CRL1-C1",
        catalog=_catalog(),
        case_basis=basis,
    )


def test_r1_4_c_loads_current_worktree_package():
    import kth_hybrid

    source_root = Path(__file__).resolve().parents[1] / "src"
    assert Path(kth_hybrid.__file__).resolve().is_relative_to(source_root)


def test_c1_replay_with_changed_case_basis_is_rejected(tmp_path):
    _seed_case(tmp_path)
    first = _run(tmp_path, _basis())
    assert first["product_status"] == "succeeded"

    with pytest.raises(RuntimeError, match="CaseBasis|有效依据|输入"):
        _run(tmp_path, _basis(cutoff="2099-01-01T00:00:00Z",
                              aliases=["Company-Z"]))


def test_c2_corrupted_source_before_publish_cannot_be_succeeded(tmp_path):
    _seed_case(tmp_path)
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute("UPDATE sources SET byte_length=1 WHERE source_id='S'")
    finally:
        case.close()

    result = _run(tmp_path, _basis())
    assert result["product_status"] != "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        row = case.fetch_one("criterion_results", "result_id", result["result_id"])
        assert row is not None
        assert row["product_status"] != "succeeded"
    finally:
        case.close()


def test_c3_concurrent_writer_is_blocked_during_verify_to_publish(tmp_path):
    from kth_hybrid import runner as runner_mod
    from kth_hybrid.audit import trace

    _seed_case(tmp_path)
    original = runner_mod._verify_candidate
    blocked: list[BaseException] = []

    def try_interleaved_write(case, blobs, candidate):
        report = original(case, blobs, candidate)
        other = sqlite3.connect(tmp_path / "records.sqlite3", timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as exc:
                other.execute("UPDATE qualifications SET status='rejected'")
                other.commit()
            blocked.append(exc.value)
        finally:
            other.close()
        return report

    runner_mod._verify_candidate = try_interleaved_write
    try:
        result = _run(tmp_path, _basis())
    finally:
        runner_mod._verify_candidate = original

    assert blocked, "验证到发布期间必须持有写入保护，另一连接不能改写资格"
    assert result["product_status"] == "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        assert trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                     strict=False).ok
    finally:
        case.close()


def test_c4_removing_subject_provenance_breaks_trace(tmp_path):
    from kth_hybrid.audit import trace

    _seed_case(tmp_path)
    result = _run(tmp_path, _basis())
    assert result["product_status"] == "succeeded"
    case = CaseStore(tmp_path / "records.sqlite3")
    try:
        with case._conn:
            case._conn.execute(
                "DELETE FROM import_records WHERE kind='case_provenance' "
                "AND origin_path='session:identity.json'"
            )
        report = trace(case, BlobStore(tmp_path / "blobs"), result["result_id"],
                       strict=False)
        assert not report.ok
        assert any("主体" in item or "证明" in item or "provenance" in item
                   for item in report.broken)
    finally:
        case.close()
