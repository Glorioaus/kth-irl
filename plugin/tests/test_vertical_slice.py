"""T06：垂直切片——真实判据入口、重复一致性、Gap路径、计数型模拟派发、CLI。"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from kth_hybrid import cli
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.journal import CommitRejected, Journal
from kth_hybrid.runner import (
    SimulatedCrash,
    CountingSimulatedProvider,
    run_criterion_slice,
)
from kth_hybrid.store import BlobStore, CaseStore

SUBJECT = "武汉微玖光电科技有限公司"
BASIS = {"subject_legal_name": SUBJECT, "subject_aliases": ["微玖"],
         "evidence_cutoff": "2026-08-27T03:02:29Z"}

CRL1_C1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1,
           "text": "A possible market need, problem or opportunity hypothesis "
                   "has been identified."}
CRL2_C1 = {"criterion_id": "CRL2-C1", "dimension": "CRL", "level": 2,
           "text": "Some market research is performed, typically derived from "
                   "secondary sources."}

REPORT = (
    f"{SUBJECT}预期2030年全球AR眼镜出货超过2000万台，对应芯片市场150-200亿元。"
    "本段为受控测试原文（synthetic=true）。"
).encode("utf-8")


@pytest.fixture()
def case_dir(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    ref = blobs.put_bytes(REPORT)
    import_id = case.add_import_record("attachment", "synthetic", ref.sha256,
                                       note='{"synthetic": true}')
    case.add_source("SRC-SYN", ref.sha256, len(REPORT),
                    source_family="owner_attachment", capture_status="attachment",
                    published_at="2026-07-16T00:00:00Z",
                    published_at_provenance="合成：文档自述日期",
                    import_id=import_id)
    case.close()
    return tmp_path


def _claim_spec():
    start = 0
    end = len((f"{SUBJECT}预期2030年全球AR眼镜出货超过2000万台，对应芯片市场"
               "150-200亿元。").encode("utf-8"))
    return {
        "claim_id": "CLM-SYN-CRL1",
        "locator_kind": "byte_range",
        "start": start, "end": end,
        "interpretation": "公司自述其市场机会假设（AR眼镜芯片市场规模预期）。",
        "subject_scope": SUBJECT,
    }


def test_real_style_qualified_slice_consumes_criterion(case_dir):
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    assert result["qualification_status"] == "qualified"
    assert result["product_status"] == "succeeded"
    assert result["native_disposition"] is None  # 未调用原版，不冒称原生处置
    assert result["trace_ok"] is True
    assert "不是原生 met 判定" in result["rationale"]


def test_frozen_input_recompute_is_idempotent(case_dir):
    first = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    second = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    assert first["result_id"] == second["result_id"]
    assert second["replay_consistent"] is True
    assert second["product_status"] == first["product_status"]
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        assert len(case.fetch_all("sources")) == 1  # 不重复导入
        assert len(case.fetch_all("claims")) == 1
        assert len(case.fetch_all("criterion_results")) == 1
    finally:
        case.close()


def test_late_retrieval_real_gap_path(case_dir):
    # 改造为晚抓取无发布证明的第三方来源 → 资格拒绝 → 判据如实不足
    case = CaseStore(case_dir / "records.sqlite3")
    with case._conn:
        case._conn.execute(
            "UPDATE sources SET source_family='news-media', "
            "capture_status='raw_capture_validated', published_at=NULL, "
            "published_at_provenance='历史捕获无发布时间证明', "
            "retrieved_at='2026-08-28T02:43:19Z' WHERE source_id='SRC-SYN'"
        )
    case.close()
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL2_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    assert result["qualification_status"] == "rejected"
    assert result["product_status"] == "insufficient"
    assert result["trace_ok"] is True  # Gap 路径同样可追溯


def test_zip_member_locator_slice(case_dir, tmp_path):
    # 合成 zip 附件成员（docx）主张：zip_member 定位 + 投影核验
    import docx as docx_mod

    document = docx_mod.Document()
    document.add_paragraph(f"{SUBJECT}高管访谈纪要载明：公司自述聚焦Micro LED芯片。")
    buffer = io.BytesIO()
    document.save(buffer)
    docx_bytes = buffer.getvalue()
    zip_path = tmp_path / "interview.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("访谈/高管.docx", docx_bytes)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    zip_data = zip_path.read_bytes()
    zip_ref = blobs.put_bytes(zip_data)
    member_ref = blobs.put_bytes(docx_bytes)
    import_id = case.add_import_record("zip_attachment", "synthetic.zip",
                                       zip_ref.sha256, note='{"synthetic": true}')
    case.add_source("SRC-ZIP", member_ref.sha256, len(docx_bytes),
                    source_family="owner_attachment",
                    capture_status="attachment_zip_member",
                    locator="synthetic.zip!/访谈/高管.docx",
                    time_evidence={"kind": "document_self_date", "date": "2026-06-01",
                                   "basis": "合成：文档自述日期"},
                    import_id=import_id)
    # zip_member 定位核验读取的是 zip 整体，所以来源指向 zip 字节更合适：
    with case._conn:
        case._conn.execute(
            "UPDATE sources SET blob_sha256=?, byte_length=? WHERE source_id='SRC-ZIP'",
            (zip_ref.sha256, len(zip_data)),
        )
    case.close()

    result = run_criterion_slice(
        case_dir, source_id="SRC-ZIP",
        claim_spec={
            "claim_id": "CLM-ZIP-CRL1",
            "locator_kind": "zip_member",
            "locator_ref": {"member": "访谈/高管.docx", "paragraph": 1},
            "interpretation": "访谈纪要载明公司自述聚焦Micro LED芯片（窄主张）。",
            "subject_scope": SUBJECT,
        },
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    assert result["qualification_status"] == "qualified"
    assert result["product_status"] == "succeeded"
    assert result["trace_ok"] is True  # 投影 hash 重核通过


def test_counting_simulated_provider_dispatches_exactly_once(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        provider = CountingSimulatedProvider(journal)
        out1 = provider.execute("mission-1", "input-1", lambda: b"response-1")
        assert provider.dispatch_count == 1
        assert out1 == sha256_hex(b"response-1")
        assert journal.task_state("mission-1")["state"] == "succeeded"
        # 已完成任务不自动重派
        with pytest.raises(CommitRejected):
            provider.execute("mission-1", "input-1", lambda: b"response-1b")
        assert provider.dispatch_count == 1
        # 显式新任务 → 新计数
        provider.execute("mission-2", "input-1", lambda: b"response-2")
        assert provider.dispatch_count == 2
    finally:
        journal.close()


def test_crash_after_dispatch_is_outcome_unknown_no_blind_redispatch(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        provider = CountingSimulatedProvider(journal)
        with pytest.raises(SimulatedCrash):
            provider.execute("mission-c", "input-1", lambda: b"resp",
                             crash_after_dispatch=True)
        assert provider.dispatch_count == 1  # 恰好一次计数型派发
        # 崩溃后状态留在 dispatch_recorded（崩溃进程无法自我标记失败）
        assert journal.task_state("mission-c")["state"] == "dispatch_recorded"
        # 恢复期保守解释：无持久结果 → outcome_unknown
        journal.mark_recovered_unknown("mission-c")
        state = journal.task_state("mission-c")
        assert state["state"] == "outcome_unknown"
        assert state["external_actions"] == 1
        # 未知结果不自动重发
        with pytest.raises(CommitRejected):
            provider.execute("mission-c", "input-1", lambda: b"resp")
        assert provider.dispatch_count == 1
    finally:
        journal.close()


def test_cli_inspect_and_trace(case_dir, capsys):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    code = cli.main(["inspect", "--case-dir", str(case_dir)])
    captured = capsys.readouterr()
    assert code == 0
    assert "证据与判据核验，非正式评估报告" in captured.out
    assert "CRL1-C1" in captured.out
    assert "还有" in captured.out and "未完成主张提取" in captured.out

    code = cli.main(["trace", "--case-dir", str(case_dir),
                     "--result-id", "RESR::CLM-SYN-CRL1::CRL1-C1"])
    captured = capsys.readouterr()
    assert code == 0
    assert "链核验：通过" in captured.out
    assert "封存" in captured.out or "✓" in captured.out


def test_cli_trace_broken_chain_returns_nonzero(case_dir, capsys):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS,
    )
    case = CaseStore(case_dir / "records.sqlite3")
    with case._conn:
        case._conn.execute("DELETE FROM qualifications WHERE claim_id='CLM-SYN-CRL1'")
    case.close()
    code = cli.main(["trace", "--case-dir", str(case_dir),
                     "--result-id", "RESR::CLM-SYN-CRL1::CRL1-C1"])
    captured = capsys.readouterr()
    assert code == 2
    assert "链核验：失败" in captured.out


# ---- 真实 Case 门控复验（KTH_REAL_CASE_DIR 显式给定才运行） ----

REAL_CASE = os.environ.get("KTH_REAL_CASE_DIR")


@pytest.mark.skipif(not REAL_CASE, reason="未设置 KTH_REAL_CASE_DIR")
class TestRealCaseSlices:
    def test_real_bp_claim_consumes_crl1_c1(self):
        from kth_hybrid.catalog import build_catalog_from_wheel

        catalog = build_catalog_from_wheel()
        crl = catalog["dimensions"]["CRL"]
        crl1c1 = next(c for c in crl["registry"]["criteria"]
                      if c["criterion_id"] == "CRL1-C1")
        crl1c1 = {"dimension": "CRL", **crl1c1}
        case = CaseStore(Path(REAL_CASE) / "records.sqlite3")
        try:
            sources = case.fetch_all("sources")
        finally:
            case.close()
        bp = [s for s in sources if s["source_family"] == "owner_attachment"
              and s["byte_length"] > 1_000_000]
        assert bp, "BP 附件来源应已导入"
        result = run_criterion_slice(
            Path(REAL_CASE), source_id=bp[0]["source_id"],
            claim_spec={
                "claim_id": "CLM-REAL-BP-CRL1",
                "locator_kind": "pdf_page",
                "locator_ref": {"page": 4},
                "interpretation": "BP第4页载明公司市场机会假设：预期2030年全球AR眼镜"
                                  "出货超2000万台、对应Micro LED芯片市场150-200亿元"
                                  "（公司自述，第一方材料；文档自述主体为微玖（苏州），"
                                  "与评估主体武汉微玖的法人关系未核验）。",
                "subject_scope": SUBJECT,
            },
            criterion=crl1c1, dimension_levels_supported=crl["levels_supported"],
            case_basis=BASIS,
        )
        assert result["qualification_status"] == "qualified"
        assert result["product_status"] == "succeeded"
        assert result["trace_ok"] is True

    def test_real_company_report_late_capture_is_insufficient(self):
        from kth_hybrid.catalog import build_catalog_from_wheel

        catalog = build_catalog_from_wheel()
        crl = catalog["dimensions"]["CRL"]
        crl2c1 = {"dimension": "CRL", **next(
            c for c in crl["registry"]["criteria"] if c["criterion_id"] == "CRL2-C1")}
        case = CaseStore(Path(REAL_CASE) / "records.sqlite3")
        try:
            sources = case.fetch_all("sources")
        finally:
            case.close()
        cap = [s for s in sources
               if s["blob_sha256"].startswith("718e402e82d5fb1c")]
        assert cap, "公司报道捕获应已导入"
        result = run_criterion_slice(
            Path(REAL_CASE), source_id=cap[0]["source_id"],
            claim_spec={
                "claim_id": "CLM-REAL-CAP-CRL2",
                "locator_kind": "byte_range",
                "start": 33267, "end": 33542,
                "interpretation": "公司报道区间载明主体法定名称（第三方载明事实）。",
                "subject_scope": SUBJECT,
            },
            criterion=crl2c1, dimension_levels_supported=crl["levels_supported"],
            case_basis=BASIS,
        )
        # 真实晚抓取无发布证明 → 拒绝 → 判据如实不足（真实不足路径）
        assert result["qualification_status"] == "rejected"
        assert result["product_status"] == "insufficient"
        assert result["trace_ok"] is True
