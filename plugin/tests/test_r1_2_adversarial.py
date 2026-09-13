"""R1.2 残留修复反例（先于实现写入，须先在 84cb1fc 代码上失败）。

来源：`D:\\t\\kth-r11-review-20260908`（R1.1独立复验结论 + residual-observations）。
分组：A 依据可核验 / B 判据身份与映射 / C 冻结与trace / D 接管与统计。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.journal import Journal
from kth_hybrid.store import BlobStore, CaseStore

SUBJECT = "Company-A科技有限公司"
CUTOFF = "2026-08-27T03:02:29Z"
BASIS = {
    "subject_legal_name": SUBJECT, "subject_aliases": ["A公司"],
    "evidence_cutoff": CUTOFF,
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
        "status": "claimed"}, ensure_ascii=False),
}

DOC = (
    f"{SUBJECT}关注AR眼镜市场显示需求，计划推出相应产品。"
    "本段为受控合成原文，不含任何日期。"
).encode("utf-8")


# ---------- A. 依据必须可核验 ----------

def _qualify(blobs, data: bytes, tmp_path=None, **source_over):
    from kth_hybrid.qualification import qualify_claim

    case = None
    if tmp_path is not None:
        iref = blobs.put_bytes(json.dumps(
            {"subjects": [{"canonical_name_claimed":
                           BASIS["subject_legal_name"]}]},
            ensure_ascii=False).encode("utf-8"))
        case = CaseStore(tmp_path / "records.sqlite3")
        case.add_import_record("case_provenance",
                               "session:identity-plan.json", iref.sha256)
    ref = blobs.put_bytes(data)
    start, end = 0, len(SUBJECT.encode("utf-8"))
    excerpt = data[start:end]
    source = {
        "source_id": "SRC-A", "blob_sha256": ref.sha256, "byte_length": len(data),
        "capture_status": "attachment", "source_family": "owner_attachment",
        "retrieved_at": None, "published_at": None,
        "published_at_provenance": "附件无发布时间",
        "document_subject": None, "document_subject_basis": None,
    }
    source.update(source_over)
    claim = {
        "claim_id": "CLM-A", "source_id": "SRC-A", "locator_kind": "byte_range",
        "locator_start": start, "locator_end": end,
        "excerpt_sha256": sha256_hex(excerpt),
        "excerpt_text": excerpt.decode("utf-8", errors="replace"),
        "interpretation": "测试解释", "subject_scope": SUBJECT,
    }
    try:
        return qualify_claim(claim, source, blobs, BASIS, case=case)
    finally:
        if case is not None:
            case.close()


class TestR12A:
    def test_empty_document_subject_basis_not_first_party(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="",
            published_at="2026-07-01T00:00:00Z")
        assert outcome.identity_judgment.verdict != "ok", \
            "第一方依据为空不能给身份ok"

    def test_nonexistent_date_locator_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面自识",
            time_evidence={"kind": "document_self_date", "date": "2026-07-01",
                           "date_locator": "page 999, which does not exist"})
        assert outcome.time_judgment.verdict == "fail"
        assert outcome.status != "qualified"

    def test_locator_content_not_containing_date_rejected(self, tmp_path):
        # 定位真实存在但内容不含该日期 → 不能作为日期证明
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面自识",
            time_evidence={"kind": "document_self_date", "date": "2026-07-01",
                           "date_locator": {"kind": "byte_range", "start": 0,
                                            "end": len(DOC)}})
        assert outcome.time_judgment.verdict == "fail", \
            "定位内容不含日期仍放行=有字就算有依据"

    def test_real_locator_containing_date_accepted(self, tmp_path):
        # 真实定位且内容含日期 → 时间ok（正常真实定位能通过）
        doc = (f"{SUBJECT}关注AR眼镜市场。发布于2026年7月1日。受控合成原文。"
               ).encode("utf-8")
        pos = doc.decode("utf-8").find("2026年7月1日")
        start = len(doc.decode("utf-8")[:pos].encode("utf-8"))
        end = start + len("2026年7月1日".encode("utf-8"))
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(
            blobs, doc, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面自识",
            time_evidence={"kind": "document_self_date", "date": "2026-07-01",
                           "date_locator": {"kind": "byte_range", "start": start,
                                            "end": end}})
        assert outcome.time_judgment.verdict == "ok"
        assert outcome.status in ("qualified", "needs_review")

    def test_registered_at_without_real_record_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面",
            time_evidence={"kind": "registered_at", "date": "2026-07-01",
                           "basis": ""})
        assert outcome.time_judgment.verdict == "fail"
        assert outcome.status != "qualified"

    def test_registered_at_with_sealed_record_accepted(self, tmp_path):
        # 登记记录真实封存且**绑定当前原件**（document_sha256一致）→ ok
        blobs = BlobStore(tmp_path / "blobs")
        current_sha = sha256_hex(DOC)
        record = {"document_sha256": current_sha,
                  "attachments": [{"stored_path": "inputs/attachments/0000.bin",
                                   "copied_at": "2026-07-01T08:00:00Z"}]}
        rec_ref = blobs.put_bytes(
            json.dumps(record, ensure_ascii=False).encode("utf-8"))
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面",
            time_evidence={"kind": "registered_at",
                           "date": "2026-07-01T08:00:00Z",
                           "registration_proof": {
                               "blob_sha256": rec_ref.sha256,
                               "field": "attachments[0].copied_at",
                               "document_sha256": current_sha}})
        assert outcome.time_judgment.verdict == "ok"

    def test_registered_at_record_mismatch_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        record = {"copied_at": "2026-09-01T00:00:00Z"}  # 记录晚于截止
        rec_ref = blobs.put_bytes(
            json.dumps(record).encode("utf-8"))
        outcome = _qualify(
            blobs, DOC, tmp_path, document_subject=SUBJECT,
            document_subject_basis="封面",
            time_evidence={"kind": "registered_at",
                           "date": "2026-07-01T00:00:00Z",  # 声明与记录不符
                           "registration_proof": {
                               "blob_sha256": rec_ref.sha256,
                               "field": "copied_at"}})
        assert outcome.time_judgment.verdict == "fail"

    def test_case_basis_source_must_be_field_reference(self, tmp_path):
        from kth_hybrid.qualification import qualify_claim

        blobs = BlobStore(tmp_path / "blobs")
        ref = blobs.put_bytes(DOC)
        basis = {"subject_legal_name": SUBJECT, "subject_aliases": [],
                 "evidence_cutoff": CUTOFF,
                 "subject_source_basis": "一句无法核验的断言文字"}  # 非可解析引用
        with pytest.raises((ValueError, KeyError)):
            qualify_claim(
                {"claim_id": "x", "source_id": "s", "locator_kind": "byte_range",
                 "locator_start": 0, "locator_end": 9,
                 "excerpt_sha256": sha256_hex(DOC[:9]),
                 "excerpt_text": DOC[:9].decode("utf-8", "replace"),
                 "interpretation": "", "subject_scope": SUBJECT},
                {"source_id": "s", "blob_sha256": ref.sha256,
                 "byte_length": len(DOC), "capture_status": "attachment",
                 "source_family": "owner_attachment", "retrieved_at": None,
                 "published_at": None, "published_at_provenance": ""},
                blobs, basis)


# ---------- B. 判据身份与映射 ----------

def _catalog_criterion():
    from kth_hybrid.catalog import build_catalog_from_wheel

    catalog = build_catalog_from_wheel()
    crl = catalog["dimensions"]["CRL"]["registry"]["criteria"]
    return {"dimension": "CRL", **next(c for c in crl if c["criterion_id"] == "CRL1-C1")}


def _candidate(claim_id="CLM-B", uses=("third_party_reported_fact",)):
    return {
        "qualifications": [{
            "claim_id": claim_id, "status": "qualified",
            "allowed_uses": list(uses),
            "identity_judgment": {"verdict": "ok", "basis": "b"},
        }],
        "claims": {claim_id: {"claim_id": claim_id, "source_id": "SRC-1",
                              "subject_scope": "限定范围"}},
        "gap_refs": [],
    }


VIEW = {"dimension_levels_supported": [1, 2, 3, 4], "case_flags": {},
        "scope": "A", "approved_criterion_ids": {"CRL1-C1"},
        "catalog_criterion": None}  # 由各测试填


class TestR12B:
    def test_tampered_criterion_identity_rejected(self):
        from kth_hybrid.dimensions import evaluate_criterion

        canonical = _catalog_criterion()
        tampered = {**canonical, "dimension": "FRL", "level": 9,
                    "text": "Unauthorised replacement criterion"}
        view = {**VIEW, "catalog_criterion": canonical}
        result = evaluate_criterion(tampered, _candidate(), view)
        assert result.product_status in ("method_unsupported", "execution_failed")
        assert result.product_status != "succeeded"

    def test_unrelated_claim_cannot_map_to_crl1c1(self):
        # “办公室墙是蓝色”类无关主张：引文逐字核验虽过，但语义映射不成立
        # → 不得succeeded（映射留痕为 unmapped）
        from kth_hybrid.runner import run_criterion_slice

        doc = (f"{SUBJECT}的办公室墙壁是蓝色，装修风格现代。受控合成原文，"
               "不含市场表述。").encode("utf-8")
        text = doc.decode("utf-8")
        pos = text.find("墙壁是蓝色")
        quote = "墙壁是蓝色"
        quote_start = len(text[:pos].encode("utf-8"))
        quote_end = quote_start + len(quote.encode("utf-8"))
        excerpt_end = len((text.split("。")[0] + "。").encode("utf-8"))
        case_dir = Path(self._make_case("unrel", doc=doc))
        result = run_criterion_slice(
            case_dir, source_id="SRC-U", criterion_id="CRL1-C1",
            catalog=_wheel_catalog(),
            claim_spec={
                "claim_id": "CLM-WALL",
                "locator_kind": "byte_range", "start": 0, "end": excerpt_end,
                "interpretation": "公司办公室墙壁是蓝色（与市场假设无关）",
                "subject_scope": SUBJECT,
                "criterion_mapping": {"quote": quote, "start": quote_start,
                                      "end": quote_end},
            },
            case_basis=BASIS)
        assert result["mapping_status"] == "unmapped"
        assert result["product_status"] != "succeeded", \
            "无关主张（无市场语义）不得使CRL1-C1成功"
        assert result["product_status"] == "insufficient"

    def test_market_mapping_with_grounded_quote_accepted(self):
        from kth_hybrid.runner import run_criterion_slice

        doc = (f"{SUBJECT}的产品设计产能将响应300万-400万副AR眼镜市场显示需求。"
               "发布于2026年7月1日。受控合成原文。").encode("utf-8")
        text = doc.decode("utf-8")
        pos = text.find("300万-400万副AR眼镜市场显示需求")
        quote = "300万-400万副AR眼镜市场显示需求"
        quote_start = len(text[:pos].encode("utf-8"))
        quote_end = quote_start + len(quote.encode("utf-8"))
        # 摘录覆盖含主体名的完整句（身份判断需要正文提及主体）
        excerpt_end = len((text.split("。")[0] + "。").encode("utf-8"))
        case_dir = Path(self._make_case("mapped", doc=doc))
        review_id = _register_mapping_review(
            case_dir, "CLM-MARKET", "CRL1-C1", quote)
        result = run_criterion_slice(
            case_dir, source_id="SRC-U", criterion_id="CRL1-C1",
            catalog=_wheel_catalog(),
            claim_spec={
                "claim_id": "CLM-MARKET",
                "locator_kind": "byte_range", "start": 0, "end": excerpt_end,
                "interpretation": "第三方载明主体识别的市场需求假设（AR眼镜显示需求）。",
                "subject_scope": SUBJECT,
                "criterion_mapping": {"quote": quote, "start": quote_start,
                                      "end": quote_end},
                "semantic_confirmation": {"confirmed": True,
                                          "confirmator": "executor-r12",
                                          "review_basis": "来源陈述该市场需求假设"},
                "mapping_review_id": review_id,
            },
            case_basis=BASIS)
        assert result["qualification_status"] == "qualified"
        assert result["product_status"] == "succeeded"
        assert result["trace_ok"] is True

    def test_mapping_quote_not_in_sealed_bytes_rejected(self):
        # 编造引文（与封存区间不一致）→ 映射unmapped留痕，结果不得succeeded
        from kth_hybrid.runner import run_criterion_slice

        doc = (f"{SUBJECT}的产品设计产能将响应300万-400万副AR眼镜市场显示需求。"
               "发布于2026年7月1日。受控合成原文。").encode("utf-8")
        text = doc.decode("utf-8")
        excerpt_end = len((text.split("。")[0] + "。").encode("utf-8"))
        case_dir = Path(self._make_case("forged-quote", doc=doc))
        result = run_criterion_slice(
            case_dir, source_id="SRC-U", criterion_id="CRL1-C1",
            catalog=_wheel_catalog(),
            claim_spec={
                "claim_id": "CLM-FORGED",
                "locator_kind": "byte_range", "start": 0, "end": excerpt_end,
                "interpretation": "x",
                "subject_scope": SUBJECT,
                "criterion_mapping": {"quote": "编造的市场需求引文",
                                      "start": 0, "end": 20},
            },
            case_basis=BASIS)
        assert result["mapping_status"] == "unmapped"
        assert "引文与封存摘录区间不一致" in str(result) or \
            result["product_status"] != "succeeded"
        assert result["product_status"] == "insufficient"

    def test_runner_rejects_self_approved_unknown_criterion(self):
        from kth_hybrid.runner import run_criterion_slice

        case_dir = Path(self._make_case("forged-na"))
        with pytest.raises((ValueError, KeyError)):
            run_criterion_slice(
                case_dir, source_id="SRC-U", criterion_id="INVENTED-NA-ID",
                catalog=_wheel_catalog(),
                claim_spec={
                    "claim_id": "CLM-FNA",
                    "locator_kind": "byte_range", "start": 0, "end": 20,
                    "interpretation": "x", "subject_scope": SUBJECT,
                    "na_proposal": {"proposal": "not_applicable",
                                    "basis": "b", "case_flag_source": "s"},
                },
                case_basis=BASIS,
                case_flags={"explicit_no_external_financing": {
                    "value": True, "source": "合成融资策略声明"}})

    def test_na_requires_sourced_flag(self):
        # v4：N/A须结构化提案+预解析封存证据；本测试核验 kernels 层合同
        from kth_hybrid.kernels import check_na_legality

        frl4 = {"criterion_id": "FRL4-PITCH", "dimension": "FRL", "level": 4,
                "na_policy": "explicit_no_external_financing_only"}
        ok = {"proposal": "not_applicable",
              "applicability_ref": {"kind": "field_reference",
                                    "path": "case:p.json#/s"},
              "flag_ref": {"kind": "field_reference",
                           "path": "case:p.json#/f"},
              "subject_ref": {"kind": "field_reference",
                              "path": "case:p.json#/subject"},
              "applicability_resolved": {"_resolved": True,
                                         "value": "no_external_financing",
                                         "path": "case:p.json#/s"},
              "flag_resolved": {"_resolved": True, "value": True,
                                "path": "case:p.json#/f"},
              "subject_resolved": {"_resolved": True, "value": SUBJECT,
                                   "path": "case:p.json#/subject"},
              "relation_valid": True}
        legal, _ = check_na_legality(frl4, ok, SUBJECT)
        assert legal
        for bad in (
            {**ok, "applicability_resolved": {"_resolved": False}},
            {**ok, "flag_resolved": {"_resolved": False}},
            {**ok, "flag_resolved": {"_resolved": True, "value": False}},
            {"proposal": "not_applicable", "basis": "非空字符串",
             "case_flag_source": "非空字符串"},
        ):
            illegal, why = check_na_legality(frl4, bad, SUBJECT)
            assert not illegal, why

    @staticmethod
    def _make_case(name: str, doc: bytes | None = None) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp(prefix=f"r12-{name}-"))
        blobs = BlobStore(root / "blobs")
        case = CaseStore(root / "records.sqlite3")
        data = doc if doc is not None else (
            f"{SUBJECT}关注AR眼镜市场显示需求。发布于2026年7月1日。"
            "受控合成原文。").encode()
        ref = blobs.put_bytes(data)
        # 封存主体文档（v4：主体依据解析到封存字段值）
        iref = blobs.put_bytes(json.dumps(
            {"subjects": [{"canonical_name_claimed": SUBJECT}]},
            ensure_ascii=False).encode("utf-8"))
        case.add_import_record("case_provenance", "session:identity-plan.json",
                               iref.sha256)
        import_id = case.add_import_record("attachment", "synthetic", ref.sha256)
        case.add_source("SRC-U", ref.sha256, len(data), import_id=import_id,
                        source_family="news-media",
                        capture_status="raw_capture_validated")
        text = data.decode("utf-8")
        pos = text.find("2026年7月1日")
        if pos >= 0:
            ds = len(text[:pos].encode("utf-8"))
            de = ds + len("2026年7月1日".encode("utf-8"))
            case.append_time_evidence("SRC-U", {
                "kind": "document_self_date", "date": "2026-07-01",
                "date_locator": {"kind": "byte_range", "start": ds, "end": de},
                "basis": "合成正文日期"})
        case.close()
        return root


def _wheel_catalog():
    from kth_hybrid.catalog import build_catalog_from_wheel

    return build_catalog_from_wheel()


def _register_mapping_review(case_dir: Path, claim_id: str, criterion_id: str,
                             quote: str) -> str:
    review_id = f"REV-R12::{claim_id}::{criterion_id}"
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        current = case.get_case_basis()
        version = current["version"] if current is not None else case.set_case_basis(**BASIS)
        if case.get_mapping_review(review_id) is None:
            case.add_mapping_review(
                review_id,
                case_basis_version=version,
                claim_id=claim_id,
                criterion_id=criterion_id,
                quote_sha256=sha256_hex(quote.encode("utf-8")),
                support_scope="仅支持来源陈述市场需求假设",
                decision="confirmed",
                reviewer="r1_2_synthetic_offline_review",
                review_basis="受控合成复核记录",
            )
    finally:
        case.close()
    return review_id


# ---------- C. 冻结完整输入与trace ----------

class TestR12C:
    def _run_slice(self, case_dir, claim_id="CLM-FRZ", interpretation="解释一",
                   with_confirmation=True):
        from kth_hybrid.runner import run_criterion_slice

        blobs = BlobStore(case_dir / "blobs")
        case = CaseStore(case_dir / "records.sqlite3")
        source = case.fetch_one("sources", "source_id", "SRC-U")
        doc = blobs.read_bytes(source["blob_sha256"])
        case.close()
        text = doc.decode("utf-8")
        pos = text.find("300万-400万副AR眼镜市场显示需求")
        quote = "300万-400万副AR眼镜市场显示需求"
        q_start = len(text[:pos].encode("utf-8"))
        q_end = q_start + len(quote.encode("utf-8"))
        spec = {
            "claim_id": claim_id,
            "locator_kind": "byte_range", "start": 0,
            "end": len(text.split("。")[0].encode("utf-8")) + 3,
            "interpretation": interpretation,
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": quote, "start": q_start,
                                  "end": q_end},
        }
        if with_confirmation:
            spec["semantic_confirmation"] = {
                "confirmed": True, "confirmator": "executor-r12",
                "review_basis": "来源陈述该市场需求假设"}
            spec["mapping_review_id"] = _register_mapping_review(
                case_dir, claim_id, "CRL1-C1", quote)
        return run_criterion_slice(
            case_dir, source_id="SRC-U", criterion_id="CRL1-C1",
            catalog=_wheel_catalog(), claim_spec=spec, case_basis=BASIS)

    def test_digest_changes_when_criterion_tampered(self):
        # 完整判据身份进入摘要：同ID改维度/级别/文本 → 摘要必须不同
        from kth_hybrid.contracts import run_input_digest_v3

        canonical = _catalog_criterion()
        claim = {"source_id": "s", "locator_kind": "byte_range",
                 "locator_start": 0, "locator_end": 9, "locator_ref": None,
                 "excerpt_sha256": "0" * 64, "excerpt_text": "x",
                 "interpretation": "i", "subject_scope": "A"}
        base = {"catalog_sha256": "", "approved_ids": [],
                "rule_version": "v", "qualification_version": "v",
                "case_basis": BASIS, "case_flags": {}, "mapping": None}
        d1 = run_input_digest_v3({**base, "criterion": canonical}, claim)
        tampered = {**canonical, "dimension": "FRL", "level": 9, "text": "fake"}
        d2 = run_input_digest_v3({**base, "criterion": tampered}, claim)
        assert d1 != d2
        # 解释/CaseBasis 变化同样改变摘要
        d3 = run_input_digest_v3(
            {**base, "criterion": canonical}, {**claim, "interpretation": "改"})
        assert d3 != d1

    def test_case_basis_versioned_not_replaced(self, tmp_path):
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            case.set_case_basis(subject_legal_name=SUBJECT, subject_aliases=[],
                                evidence_cutoff=CUTOFF,
                                subject_source_basis=BASIS["subject_source_basis"])
            v1 = case.get_case_basis()
            case.set_case_basis(subject_legal_name="另一主体",
                                subject_aliases=[], evidence_cutoff=CUTOFF,
                                subject_source_basis=BASIS["subject_source_basis"])
            versions = case.get_case_basis_versions()
            assert len(versions) == 2  # 旧版本保留，未被抹去
            assert versions[0]["snapshot"]["subject_legal_name"] == SUBJECT
            assert v1["version"] == 1
        finally:
            case.close()

    def test_interpretation_tamper_detected_by_trace(self, tmp_path):
        # 改写保存的解释文本 → trace必须失败（冻结后改动可检测）
        from kth_hybrid.audit import TraceBroken, trace

        root = self._make_frozen_case(tmp_path)
        first = self._run_slice(root)
        assert first["trace_ok"] is True
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute(
                "UPDATE claims SET interpretation='被篡改的解释' "
                "WHERE claim_id='CLM-FRZ'")
        case.close()
        blobs = BlobStore(root / "blobs")
        case = CaseStore(root / "records.sqlite3")
        try:
            with pytest.raises(TraceBroken):
                trace(case, blobs, first["result_id"])
        finally:
            case.close()

    def test_case_basis_replacement_keeps_old_result_trace(self, tmp_path):
        # 替换CaseBasis后，旧结果仍绑定旧版本且trace按旧版本校验（不被最新行静默影响）
        root = self._make_frozen_case(tmp_path)
        first = self._run_slice(root)
        case = CaseStore(root / "records.sqlite3")
        case.set_case_basis(subject_legal_name="另一主体", subject_aliases=[],
                            evidence_cutoff=CUTOFF,
                            subject_source_basis=BASIS["subject_source_basis"])
        case.close()
        from kth_hybrid.audit import trace

        blobs = BlobStore(root / "blobs")
        case = CaseStore(root / "records.sqlite3")
        try:
            report = trace(case, blobs, first["result_id"], strict=False)
            assert report.ok, "旧结果应按其绑定的CaseBasis版本校验，不受新版本影响"
        finally:
            case.close()

    def test_trace_failed_result_not_published_as_succeeded(self, tmp_path):
        # R1.3 确定性版本：同一CLM-FRZ，发布后破坏其资格记录并重跑同输入——
        # 验证必须失败且不得把已发布成功翻转为失败发布之外的状态；
        # 独立连接检查落库状态。
        from kth_hybrid.runner import run_criterion_slice

        root = self._make_frozen_case(tmp_path)
        first = self._run_slice(root)  # 前置：正向候选实际发布成功
        assert first["product_status"] == "succeeded"
        assert first["trace_ok"] is True
        # 确定性故障注入：删除资格记录（验证必然失败）
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute(
                "DELETE FROM qualifications WHERE claim_id='CLM-FRZ'")
        case.close()
        # 重建资格（同输入重跑）后立刻再破坏并重跑：验证失败 → 不得发布成功
        self._rerun_after_tamper(root)

    def _rerun_after_tamper(self, root):
        from kth_hybrid import runner as runner_mod
        from kth_hybrid.runner import run_criterion_slice

        # 先恢复资格（重跑同输入会重建），再在验证前删除 → 确定性验证失败
        original = runner_mod._verify_candidate

        def verify_then_break(case, blobs, candidate_row):
            # 模拟"验证时刻"链路被外部破坏：资格记录在验证前被删除
            case._conn.execute("DELETE FROM qualifications")
            return original(case, blobs, candidate_row)

        # 使用新claim id避免与已发布结果冲突；验证在断链状态下必须失败
        blobs = BlobStore(root / "blobs")
        case = CaseStore(root / "records.sqlite3")
        src_row = case.fetch_one("sources", "source_id", "SRC-U")
        doc = blobs.read_bytes(src_row["blob_sha256"])
        text = doc.decode("utf-8")
        pos = text.find("300万-400万副AR眼镜市场显示需求")
        quote = "300万-400万副AR眼镜市场显示需求"
        q_start = len(text[:pos].encode("utf-8"))
        q_end = q_start + len(quote.encode("utf-8"))
        case.close()
        review_id = _register_mapping_review(
            root, "CLM-FRZ-FAILPATH", "CRL1-C1", quote)
        runner_mod._verify_candidate = verify_then_break
        try:
            result = run_criterion_slice(
                root, source_id="SRC-U", criterion_id="CRL1-C1",
                catalog=_wheel_catalog(),
                claim_spec={
                    "claim_id": "CLM-FRZ-FAILPATH",
                    "locator_kind": "byte_range", "start": 0,
                    "end": len(text.split("。")[0].encode("utf-8")) + 3,
                    "interpretation": "第三方载明市场需求假设。",
                    "subject_scope": SUBJECT,
                    "criterion_mapping": {"quote": quote, "start": q_start,
                                          "end": q_end},
                    "semantic_confirmation": {
                        "confirmed": True, "confirmator": "executor-r12",
                        "review_basis": "来源陈述该市场需求假设"},
                    "mapping_review_id": review_id,
                },
                case_basis=BASIS)
        finally:
            runner_mod._verify_candidate = original
        assert result["trace_ok"] is False
        assert result["product_status"] != "succeeded",             "确定性验证失败后不得发布成功"
        # 独立连接复查落库状态
        case = CaseStore(root / "records.sqlite3")
        try:
            row = case.fetch_one("criterion_results", "result_id",
                                 result["result_id"])
            assert row is not None and row["product_status"] != "succeeded"
        finally:
            case.close()

    @staticmethod
    def _make_frozen_case(tmp_path):
        root = tmp_path / "frozen-case"
        blobs = BlobStore(root / "blobs")
        case = CaseStore(root / "records.sqlite3")
        doc = (f"{SUBJECT}的产品将响应300万-400万副AR眼镜市场显示需求。"
               "发布于2026年7月1日。合成原文。").encode("utf-8")
        ref = blobs.put_bytes(doc)
        iref = blobs.put_bytes(json.dumps(
            {"subjects": [{"canonical_name_claimed": SUBJECT}]},
            ensure_ascii=False).encode("utf-8"))
        case.add_import_record("case_provenance", "session:identity-plan.json",
                               iref.sha256)
        import_id = case.add_import_record("attachment", "synthetic", ref.sha256)
        case.add_source("SRC-U", ref.sha256, len(doc), import_id=import_id,
                        source_family="news-media",
                        capture_status="raw_capture_validated")
        text = doc.decode("utf-8")
        pos = text.find("2026年7月1日")
        ds = len(text[:pos].encode("utf-8"))
        de = ds + len("2026年7月1日".encode("utf-8"))
        case.append_time_evidence("SRC-U", {
            "kind": "document_self_date", "date": "2026-07-01",
            "date_locator": {"kind": "byte_range", "start": ds, "end": de},
            "basis": "合成正文日期"})
        case.close()
        return root


# ---------- D. 接管attempt状态与统计 ----------


def _claim_then_hard_exit(db_path: Path, *, task_key: str, worker_id: str,
                          input_id: str) -> None:
    plugin_src = Path(__file__).resolve().parents[1] / "src"
    script = textwrap.dedent(f"""
        import os
        import sys
        sys.path.insert(0, {str(plugin_src)!r})
        from kth_hybrid.journal import Journal

        journal = Journal(sys.argv[1])
        journal.claim({task_key!r}, {worker_id!r}, {input_id!r})
        os._exit(0)
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(db_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr

class TestR12D:
    def test_takeover_attempt_advances_to_terminal(self, tmp_path):
        db_path = tmp_path / "journal.sqlite3"
        _claim_then_hard_exit(
            db_path, task_key="task", worker_id="dead-worker", input_id="input")
        j = Journal(db_path)
        try:
            takeover = j.takeover_stale_claim("task", "new-worker", "input",
                                              evidence="心跳超时（测试）")
            j.record_dispatch(takeover)
            j.commit(takeover, "out-ref")
            state = j.task_state("task")
            assert state["state"] == "succeeded"
            attempts = j.attempts("task")
            assert attempts[-1]["outcome"] == "succeeded", \
                "接管后的attempt必须推进到终态，不得停留在takeover"
            events = j.takeover_events("task")
            assert events and events[0]["worker_id"] == "new-worker", \
                "接管事件保留为审计记录"
        finally:
            j.close()

    def test_import_idempotent_no_duplicate_rows(self, tmp_path):
        # 重复导入不增加导入记录/依赖行/来源行（新证据数量不随重跑膨胀）
        from kth_hybrid.intake import import_capture

        root = tmp_path / "cap"
        (root / "frozen-capture").mkdir(parents=True)
        body = b"idempotent-import-body"
        (root / "frozen-capture" / "raw-body.bin").write_bytes(body)
        (root / "frozen-capture" / "transport.json").write_text("{}", encoding="utf-8")
        receipt = {"synthetic": True, "capture_status": "raw_capture_validated",
                   "failure_code": None, "response_status": 200,
                   "raw_body_sha256": sha256_hex(body),
                   "raw_body_size": len(body),
                   "retrieved_at": "2026-09-01T00:00:00Z",
                   "fixed_clock": "2026-09-01T00:00:00Z", "as_of_cut": CUTOFF,
                   "final_url": "https://www.example.gov.cn/x",
                   "provider_id": "p", "evidence_eligible": False,
                   "qualification_candidate_refs": []}
        (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        for dep in ("request.json", "transport-attempt.json",
                    "live-provider-result.json"):
            (root / dep).write_text("{}", encoding="utf-8")

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            import_capture(root, blobs, case)
            import_capture(root, blobs, case)  # 重复导入
            imports = [r for r in case.fetch_all("import_records")
                       if r["kind"] == "historical_capture"]
            assert len(imports) == 1
            deps = case.get_capture_dependencies(imports[0]["import_id"])
            assert len(deps) == 5  # 不重复
            sources = [s for s in case.fetch_all("sources")
                       if s["source_id"].startswith("CAP::")]
            assert len(sources) == 1
        finally:
            case.close()

    def test_dependency_counting_units_separated(self, tmp_path):
        # 依赖行数 / 逻辑依赖项（目录+文件名）/ 不同blob 三个口径分列
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            stats = case.dependency_stats()
            assert set(stats) >= {"dependency_rows", "logical_dependencies",
                                  "distinct_dependency_blobs"}
        finally:
            case.close()

    def test_gov_domain_maps_to_gov_family(self, tmp_path):
        from kth_hybrid.intake import _family_from_url

        assert _family_from_url(
            "https://www.sipac.gov.cn/kjzszx/jqhd/202602/x.shtml") == "gov-agency"
        assert _family_from_url("https://api3.cls.cn/share/article/1") == "news-media"
