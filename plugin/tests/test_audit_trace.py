"""T03：审计 trace——正常链、断链、篡改、越界、失败 attempt 保留。"""

from __future__ import annotations

import json

import pytest

from kth_hybrid.audit import TraceBroken, render_trace, trace
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore

ORIGINAL = (
    "武汉微玖光电科技有限公司（下称“微玖”）于2026年7月发布MicroLED量产线公告。"
    "本段为受控测试原文，用于验证trace核验行为。"
).encode("utf-8")


@pytest.fixture()
def stack(tmp_path):
    blobs = BlobStore(tmp_path / "objects")
    case = CaseStore(tmp_path / "case" / "records.sqlite3")
    ref = blobs.put_bytes(ORIGINAL)
    import_id = case.add_import_record("capture", "session/…/raw.html", ref.sha256)
    case.add_source("SRC-1", ref.sha256, len(ORIGINAL), media_type="text/html",
                    locator="session/research/…", retrieved_at="2026-08-28T02:43:19Z",
                    published_at=None, published_at_provenance="未知发布时间",
                    source_family="company-news", capture_status="raw_capture_validated",
                    import_id=import_id)
    start, end = 0, len("武汉微玖光电科技有限公司".encode("utf-8"))
    excerpt = ORIGINAL[start:end]
    case.add_claim(
        "CLM-1", "SRC-1", locator_kind="byte_range",
        excerpt_start=start, excerpt_end=end,
        excerpt_sha256=sha256_hex(excerpt), excerpt_text=excerpt.decode("utf-8"),
        interpretation="原文载明公司法定名称。窄主张：该发布主体自称为微玖。",
        subject_scope="微玖（法定主体识别）",
        interpretation_attempt="executor-r1-20260908",
    )
    case.add_qualification(
        "QUAL-1", "CLM-1",
        source_judgment="公司官方渠道报道，正文与receipt一致",
        identity_judgment="发布主体匹配请求的出版方；法定主体关系未经工商核验，按候选处理",
        time_judgment="抓取晚于截止（2026-08-28 > 2026-08-27），无发布时间证明",
        independence_judgment="与同族URL共享正文hash，独立来源族未确认",
        allowed_uses=["主体身份发现"],
        cannot_prove=["事件发生在证据截止前", "微玖自身量产能力"],
        review_attempt="executor-r1-20260908", status="needs_review",
    )
    case.add_gap(
        "GAP-1", "missing_publication_time", ["CRL2-C1"],
        pipeline_fault=False, investigation="receipt 无发布时间字段，未发现其他证明",
        unconfirmed=["发布时间是否早于证据截止"],
    )
    from kth_hybrid.contracts import (
        qualification_content_digest, run_input_digest_v3)

    basis_version = case.set_case_basis_if_changed(
        subject_legal_name="微玖（法定主体识别）", subject_aliases=["微玖"],
        evidence_cutoff="2026-08-27T03:02:29Z",
        subject_source_basis=json.dumps({
            "kind": "field_reference",
            "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
            "status": "claimed"}, ensure_ascii=False))
    claim_row = case.fetch_one("claims", "claim_id", "CLM-1")
    source_row = case.fetch_one("sources", "source_id", "SRC-1")
    qual_row = case.fetch_one("qualifications", "qual_id", "QUAL-1")
    frozen = {
        "criterion": {"criterion_id": "CRL2-C1", "dimension": "CRL",
                      "level": 2, "text": "t", "na_policy": None},
        "catalog_sha256": "", "approved_ids": ["CRL1-C1", "CRL2-C1"],
        "rule_version": "kth-hybrid.kernels.r1-narrow.v4",
        "qualification_version": "kth-hybrid.qualification.v4",
        "case_basis": {"subject_legal_name": "微玖（法定主体识别）",
                       "subject_aliases": ["微玖"],
                       "evidence_cutoff": "2026-08-27T03:02:29Z",
                       "subject_source_basis": json.dumps({
                           "kind": "field_reference",
                           "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
                           "status": "claimed"}, ensure_ascii=False),
                       "note": None},
        "case_basis_version": basis_version,
        "case_flags": {},
        "mapping": {"status": None, "quote_sha256": None,
                    "quote_start": 0, "quote_end": 0, "confirmation": None},
        "source_inputs": {
            "published_at": source_row.get("published_at"),
            "retrieved_at": source_row.get("retrieved_at"),
            "source_family": source_row.get("source_family"),
            "capture_status": source_row.get("capture_status"),
            "document_subject": source_row.get("document_subject"),
            "time_evidence_revision": None,
            "time_evidence_snapshot": None,
        },
        "qualification_digest": qualification_content_digest(qual_row),
        "na_proposal": None,
    }
    case.add_criterion_result(
        "RES-1", "CRL2-C1", "CRL",
        native_disposition=None, native_note="R1 未调用原版 vertical",
        product_status="insufficient",
        evidence_refs=["SRC-1"], gap_refs=["GAP-1"], qual_refs=["QUAL-1"],
        rationale="唯一候选主张时间资格不成立（晚抓取且无发布时间证明），如实不足。",
        scope="微玖（法定主体识别）", rule_version="kth-hybrid.qualification.v1",
        input_digest=run_input_digest_v3(frozen, claim_row),
        case_basis_version=basis_version, frozen_inputs=frozen, na_basis=None,
    )
    yield {"blobs": blobs, "case": case, "ref": ref}
    case.close()


def test_normal_chain_verifies_down_to_bytes(stack):
    report = trace(stack["case"], stack["blobs"], "RES-1")
    assert report.ok
    assert len(report.verified_excerpt_hashes) == 1
    assert report.native_disposition is None  # 未调用原版 → null，不冒称原生状态
    assert report.product_status == "insufficient"
    text = render_trace(report)
    assert "证据与判据核验，非正式评估报告" in text
    assert "通过" in text


def test_broken_qualification_reference_raises(stack):
    case = stack["case"]
    with case._conn:  # 直接删除资格模拟断链
        case._conn.execute("DELETE FROM qualifications WHERE qual_id='QUAL-1'")
    with pytest.raises(TraceBroken, match="引用断裂"):
        trace(case, stack["blobs"], "RES-1")


def test_tampered_excerpt_hash_raises(stack):
    case = stack["case"]
    with case._conn:
        case._conn.execute(
            "UPDATE claims SET excerpt_sha256=? WHERE claim_id='CLM-1'", ("0" * 64,)
        )
    with pytest.raises(TraceBroken, match="摘录 hash 不一致"):
        trace(case, stack["blobs"], "RES-1")


def test_tampered_original_bytes_raise(stack):
    case = stack["case"]
    blobs = stack["blobs"]
    target = blobs.root / "objects" / stack["ref"].sha256[:2] / stack["ref"].sha256
    target.write_bytes(ORIGINAL[:-4] + "篡改!!".encode("utf-8"))
    with pytest.raises(TraceBroken, match="不可读/复核失败"):
        trace(case, blobs, "RES-1")


def test_out_of_range_locator_raises(stack):
    case = stack["case"]
    with case._conn:
        case._conn.execute(
            "UPDATE claims SET locator_start=1000000, locator_end=2000000 "
            "WHERE claim_id='CLM-1'"
        )
    with pytest.raises(TraceBroken, match="定位越界"):
        trace(case, stack["blobs"], "RES-1")


def test_missing_result_is_visible(stack):
    with pytest.raises(TraceBroken, match="不存在"):
        trace(stack["case"], stack["blobs"], "RES-NOPE")


def test_broken_gap_reference_raises(stack):
    case = stack["case"]
    with case._conn:
        case._conn.execute("DELETE FROM gaps WHERE gap_id='GAP-1'")
    with pytest.raises(TraceBroken, match="缺口 GAP-1 引用断裂"):
        trace(case, stack["blobs"], "RES-1")


def test_non_strict_mode_returns_report_for_display(stack):
    case = stack["case"]
    with case._conn:
        case._conn.execute("DELETE FROM qualifications WHERE qual_id='QUAL-1'")
    report = trace(case, stack["blobs"], "RES-1", strict=False)
    assert not report.ok
    assert any("引用断裂" in b for b in report.broken)
