"""T06/R1.1：垂直切片 v2——真实判据入口、输入冻结、计数型模拟派发、CLI。

v2 变化：provider 必须先持久化响应；runner 校验有源CaseBasis与判据登记；
同ID异输入拒绝；真实样本期望按修复后规则如实翻转（BP=needs_review→不足，
公司报道=rejected→不足），合成正例证明正向通道。
"""

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
BASIS = {
    "subject_legal_name": SUBJECT, "subject_aliases": ["微玖"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": "合成CaseBasis（synthetic）：主体/截止来自登记依据",
}
APPROVED = {"CRL1-C1", "CRL2-C1"}

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
                    document_subject=SUBJECT,
                    document_subject_basis="合成：封面自识主体与评估主体一致",
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


def test_synthetic_qualified_slice_consumes_implemented_rule(case_dir):
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    assert result["qualification_status"] == "qualified"
    assert result["product_status"] == "succeeded"
    assert result["native_disposition"] is None  # 未调用原版，不冒称原生处置
    assert result["trace_ok"] is True
    assert "不是原生 met" in result["rationale"]


def test_unimplemented_criterion_in_slice_is_method_unsupported(case_dir):
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN",
        claim_spec=dict(_claim_spec(), claim_id="CLM-SYN-CRL2"),
        criterion=CRL2_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    assert result["product_status"] == "method_unsupported"
    assert result["trace_ok"] is True  # 链仍可追溯，但状态如实


def test_frozen_input_recompute_is_idempotent(case_dir):
    first = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    second = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    assert first["result_id"] == second["result_id"]
    assert second["replay_consistent"] is True
    assert second["input_digest"] == first["input_digest"]
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
            "retrieved_at='2026-08-28T02:43:19Z', document_subject=NULL, "
            "document_subject_basis=NULL WHERE source_id='SRC-SYN'"
        )
    case.close()
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN",
        claim_spec=dict(_claim_spec(), claim_id="CLM-SYN-LATE"),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    assert result["qualification_status"] == "rejected"
    assert result["product_status"] == "insufficient"
    assert result["trace_ok"] is True  # Gap 路径同样可追溯


def test_runner_requires_sourced_case_basis(case_dir):
    with pytest.raises(ValueError, match="subject_source_basis"):
        run_criterion_slice(
            case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
            criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
            case_basis={"subject_legal_name": SUBJECT, "subject_aliases": [],
                        "evidence_cutoff": "2026-08-27T03:02:29Z"},
            approved_criterion_ids=APPROVED)


def test_counting_simulated_provider_dispatches_exactly_once(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        provider = CountingSimulatedProvider(journal, blobs=blobs)
        out1 = provider.execute("mission-1", "input-1", lambda: b"response-1")
        assert provider.dispatch_count == 1
        assert out1 == sha256_hex(b"response-1")
        assert blobs.read_bytes(out1) == b"response-1"  # 响应可解析到字节
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
    blobs = BlobStore(tmp_path / "blobs")
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        provider = CountingSimulatedProvider(journal, blobs=blobs)
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
        case_basis=BASIS, approved_criterion_ids=APPROVED,
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


def test_cli_trace_broken_chain_returns_nonzero(case_dir, capsys):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion=CRL1_C1, dimension_levels_supported=[1, 2, 3, 4],
        case_basis=BASIS, approved_criterion_ids=APPROVED,
    )
    case = CaseStore(case_dir / "records.sqlite3")
    with case._conn:
        case._conn.execute(
            "DELETE FROM qualifications WHERE claim_id='CLM-SYN-CRL1'")
    case.close()
    code = cli.main(["trace", "--case-dir", str(case_dir),
                     "--result-id", "RESR::CLM-SYN-CRL1::CRL1-C1"])
    captured = capsys.readouterr()
    assert code == 2
    assert "链核验：失败" in captured.out


# ---- 真实 Case 门控复验（KTH_REAL_CASE_DIR_1 显式给定才运行；R1.1 新Case） ----

REAL_CASE = os.environ.get("KTH_REAL_CASE_DIR_1")


@pytest.mark.skipif(not REAL_CASE, reason="未设置 KTH_REAL_CASE_DIR_1（R1.1新Case）")
class TestRealCaseSlicesR11:
    """R1.1 真实切片（修复后规则下的诚实结果）：

    - BP：时间证据降为文件名候选级＋文档自识主体为苏州法人 → needs_review，
      CRL1-C1 如实不足（不再有 R1 的伪qualified）。
    - 公司报道：晚抓取无发布证明 → rejected → 不足（与 R1 相同的诚实负例）。
    复跑读取建库脚本落盘的权威 spec（real-slice-specs-r1_1.json），
    同输入必须得到相同输入摘要与相同结果（幂等）。
    """

    def _load_specs(self):
        from kth_hybrid.catalog import build_catalog_from_wheel

        catalog = build_catalog_from_wheel()
        crl = catalog["dimensions"]["CRL"]
        specs = json.loads(
            (Path(REAL_CASE) / "audit" / "real-slice-specs-r1_1.json")
            .read_text(encoding="utf-8"))
        return crl, specs

    def _run(self, spec, crl, specs):
        criterion = {"dimension": "CRL", **next(
            c for c in crl["registry"]["criteria"]
            if c["criterion_id"] == spec["criterion_id"])}
        return run_criterion_slice(
            Path(REAL_CASE), source_id=spec["source_id"],
            claim_spec=spec["claim_spec"], criterion=criterion,
            dimension_levels_supported=specs["dimension_levels_supported"],
            case_basis=specs["case_basis"],
            approved_criterion_ids=set(specs["approved_criterion_ids"]),
        )

    def test_real_bp_claim_honestly_needs_review(self):
        crl, specs = self._load_specs()
        bp_spec, cap_spec = specs["slices"]
        result = self._run(bp_spec, crl, specs)
        assert result["qualification_status"] == "needs_review", \
            "BP时间证据为文件名候选级且文档主体为苏州法人：不得qualified"
        assert result["product_status"] == "insufficient"
        assert result["native_disposition"] is None
        assert result["trace_ok"] is True
        # 幂等：与首次构建相同输入摘要与结果ID
        first = json.loads(
            (Path(REAL_CASE) / "audit" / "real-slices-r1_1.json")
            .read_text(encoding="utf-8"))["bp_slice"]
        assert result["input_digest"] == first["input_digest"]
        assert result["result_id"] == first["result_id"]

    def test_real_company_report_late_capture_is_insufficient(self):
        crl, specs = self._load_specs()
        _, cap_spec = specs["slices"]
        result = self._run(cap_spec, crl, specs)
        assert result["qualification_status"] == "rejected"
        assert result["product_status"] == "insufficient"
        assert result["trace_ok"] is True
        first = json.loads(
            (Path(REAL_CASE) / "audit" / "real-slices-r1_1.json")
            .read_text(encoding="utf-8"))["capture_slice"]
        assert result["input_digest"] == first["input_digest"]
        assert result["result_id"] == first["result_id"]
