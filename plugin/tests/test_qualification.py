"""T05：资格判断——合格窄主张、错主体、未知时间、错来源、同源重复、空正文、
引用越界、伪造资格标签、搜索摘要类来源、候选eligible不决定返回值。"""

from __future__ import annotations

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.qualification import (
    GapOutcome,
    QualificationOutcome,
    qualify_claim,
)
from kth_hybrid.store import BlobStore

SUBJECT = "武汉微玖光电科技有限公司"
CUTOFF = "2026-08-27T03:02:29Z"
BASIS = {"subject_legal_name": SUBJECT, "subject_aliases": ["微玖"],
         "evidence_cutoff": CUTOFF}

REPORT = (
    f"{SUBJECT}于2026年7月对外宣布其MicroLED中试线完成工艺验证。"
    "本段为受控测试原文。"
).encode("utf-8")
INDUSTRY = "MicroLED行业整体处于中试阶段，多家厂商推进量产。本段为受控测试原文。".encode(
    "utf-8"
)


@pytest.fixture()
def blobs(tmp_path):
    return BlobStore(tmp_path / "blobs")


def _source(sha256: str, size: int, **overrides):
    base = {
        "source_id": "SRC-X", "blob_sha256": sha256, "byte_length": size,
        "capture_status": "raw_capture_validated", "source_family": "news-media",
        "retrieved_at": "2026-08-28T02:43:19Z", "published_at": None,
        "published_at_provenance": "历史捕获无发布时间证明",
    }
    base.update(overrides)
    return base


def _claim(cid: str, sha: str, text: str, start: int, end: int, **overrides):
    base = {
        "claim_id": cid, "source_id": "SRC-X", "locator_kind": "byte_range",
        "locator_start": start, "locator_end": end,
        "excerpt_sha256": sha, "excerpt_text": text,
        "interpretation": "测试解释", "subject_scope": SUBJECT,
    }
    base.update(overrides)
    return base


def _range(data: bytes, start: int, end: int):
    excerpt = data[start:end]
    return _claim("CLM-X", sha256_hex(excerpt), excerpt.decode("utf-8"), start, end)


def test_qualified_narrow_claim_with_publication_time(blobs):
    ref = blobs.put_bytes(REPORT)
    start, end = 0, len(SUBJECT.encode("utf-8"))
    excerpt = REPORT[start:end]
    claim = _claim("CLM-Q", sha256_hex(excerpt), excerpt.decode("utf-8"), start, end)
    source = _source(ref.sha256, len(REPORT), published_at="2026-07-20T00:00:00Z",
                    published_at_provenance="页面发布时间字段")
    outcome = qualify_claim(claim, source, blobs, BASIS, review_attempt="t05")
    assert isinstance(outcome, QualificationOutcome)
    assert outcome.status == "qualified"
    assert all(j.verdict == "ok" for j in outcome.verdicts)
    assert all(j.basis for j in outcome.verdicts)  # 依据非空
    assert outcome.allowed_uses == ["third_party_reported_fact"]


def test_wrong_time_late_retrieval_rejects(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    source = _source(ref.sha256, len(REPORT))  # 无发布时间，抓取晚于截止
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "rejected"
    assert outcome.time_judgment.verdict == "fail"
    assert "内容在证据截止前已存在" in outcome.cannot_prove


def test_first_party_self_dated_document_qualifies_with_limits(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    source = _source(
        ref.sha256, len(REPORT), source_family="owner_attachment",
        capture_status="attachment", retrieved_at=None,
        published_at_provenance="附件无抓取/发布时间（Owner 提供）",
        time_evidence={"kind": "document_self_date", "date": "2026-07-16",
                       "basis": "文档自述日期", "registered_at": "2026-08-27T04:13:11Z"},
    )
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "qualified"
    assert "company_self_statement" in outcome.allowed_uses
    assert "document_dated_statement" in outcome.allowed_uses
    assert any("独立核实" in c for c in outcome.cannot_prove)


def test_industry_review_cannot_prove_subject_claim(blobs):
    ref = blobs.put_bytes(INDUSTRY)
    claim = _range(INDUSTRY, 0, 11)  # "MicroLED行"（字节对齐区间），正文不含主体
    source = _source(ref.sha256, len(INDUSTRY), published_at="2026-07-01T00:00:00Z")
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "rejected"
    assert outcome.identity_judgment.verdict == "fail"
    assert "行业综述" in outcome.identity_judgment.basis


def test_empty_502_body_never_qualifies(blobs):
    ref = blobs.put_bytes(b"")
    claim = {
        "claim_id": "CLM-E", "source_id": "SRC-E", "locator_kind": "byte_range",
        "locator_start": 0, "locator_end": 1, "excerpt_sha256": "0" * 64,
        "excerpt_text": "", "interpretation": "", "subject_scope": SUBJECT,
    }
    source = _source(ref.sha256, 0, capture_status="blocked/empty_body")
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "rejected"
    assert outcome.source_judgment.verdict == "fail"


def test_search_summary_family_is_discovery_only(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    source = _source(ref.sha256, len(REPORT), source_family="search_summary",
                    published_at="2026-07-20T00:00:00Z")
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "rejected"
    assert "source_discovery" in outcome.allowed_uses  # 仅来源发现


def test_same_body_hash_group_not_independent(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    source = _source(ref.sha256, len(REPORT), published_at="2026-07-20T00:00:00Z")
    outcome = qualify_claim(claim, source, blobs, BASIS, same_body_sources=12)
    assert outcome.status == "needs_review"
    assert outcome.independence_judgment.verdict == "unknown"
    assert "来源独立性（同hash组）" in outcome.cannot_prove


def test_locator_out_of_range_yields_gap(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _claim("CLM-O", "0" * 64, "x", 100000, 200000)
    source = _source(ref.sha256, len(REPORT))
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert isinstance(outcome, GapOutcome)
    assert outcome.gap_type == "invalid_locator"


def test_excerpt_hash_mismatch_yields_gap(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _claim("CLM-H", "0" * 64, "x", 0, 10)
    source = _source(ref.sha256, len(REPORT))
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert isinstance(outcome, GapOutcome)
    assert outcome.gap_type == "excerpt_hash_mismatch"


def test_unreadable_blob_yields_gap(blobs):
    claim = _claim("CLM-U", "0" * 64, "x", 0, 10)
    source = _source("e" * 64, 10)  # 对象仓中不存在
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert isinstance(outcome, GapOutcome)
    assert outcome.gap_type == "original_bytes_unreadable"


def test_candidate_eligible_flag_does_not_decide(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    claim["eligible"] = True  # 伪造候选标签
    source = _source(ref.sha256, len(REPORT))  # 时间不合格
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "rejected"  # eligible=true 被四类判断推翻


def test_unknown_publication_time_needs_review(blobs):
    ref = blobs.put_bytes(REPORT)
    claim = _range(REPORT, 0, len(SUBJECT.encode("utf-8")))
    source = _source(ref.sha256, len(REPORT), retrieved_at=None,
                    published_at=None, published_at_provenance="发布时间未知")
    outcome = qualify_claim(claim, source, blobs, BASIS)
    assert outcome.status == "needs_review"
    assert outcome.time_judgment.verdict == "unknown"
