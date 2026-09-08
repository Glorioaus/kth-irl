"""T06/R1.2：垂直切片 v3——可信判据解析、引文映射、冻结输入、计数型派发、CLI。

v3 变化：runner 以 criterion_id+catalog 从可信目录解析判据（拒自报/篡改）；
主张需带引文映射（逐字位于封存摘录＋语义过滤留痕）；CaseBasis/输入版本化；
trace 失败不得发布成功。真实样本门控指向 R1.2 新Case（KTH_REAL_CASE_DIR_2）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kth_hybrid import cli
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.journal import CommitRejected, Journal
from kth_hybrid.runner import (
    SimulatedCrash,
    CountingSimulatedProvider,
    resolve_criterion,
    run_criterion_slice,
)
from kth_hybrid.store import BlobStore, CaseStore

SUBJECT = "Company-A科技有限公司"
BASIS = {
    "subject_legal_name": SUBJECT, "subject_aliases": ["A公司"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
        "status": "claimed"}, ensure_ascii=False),
}

DOC = (
    f"{SUBJECT}关注AR眼镜市场显示需求。本段为受控测试原文（synthetic=true）。"
).encode("utf-8")
QUOTE = "AR眼镜市场显示需求"


DOC_V4 = (f"{SUBJECT}关注AR眼镜市场显示需求。发布于2026年7月16日。"
          "本段为受控测试原文（synthetic=true）。").encode("utf-8")
DATE_NEEDLE = "2026年7月16日"


def _date_range(doc: bytes) -> tuple[int, int]:
    text = doc.decode("utf-8")
    pos = text.find(DATE_NEEDLE)
    start = len(text[:pos].encode("utf-8"))
    return start, start + len(DATE_NEEDLE.encode("utf-8"))


@pytest.fixture()
def case_dir(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    ref = blobs.put_bytes(DOC_V4)
    iref = blobs.put_bytes(json.dumps(
        {"subjects": [{"canonical_name_claimed": SUBJECT}]},
        ensure_ascii=False).encode("utf-8"))
    case.add_import_record("case_provenance", "session:identity-plan.json",
                           iref.sha256)
    import_id = case.add_import_record("attachment", "synthetic", ref.sha256,
                                       note='{"synthetic": true}')
    case.add_source("SRC-SYN", ref.sha256, len(DOC_V4),
                    source_family="news-media",
                    capture_status="raw_capture_validated",
                    import_id=import_id)
    ds, de = _date_range(DOC_V4)
    case.append_time_evidence("SRC-SYN", {
        "kind": "document_self_date", "date": "2026-07-16",
        "date_locator": {"kind": "byte_range", "start": ds, "end": de},
        "basis": "合成正文日期"})
    basis_version = case.set_case_basis(**BASIS)
    case.add_mapping_review(
        "REV-SYN-CRL1",
        case_basis_version=basis_version,
        claim_id="CLM-SYN-CRL1",
        criterion_id="CRL1-C1",
        quote_sha256=sha256_hex(QUOTE.encode("utf-8")),
        support_scope="仅支持来源陈述市场需求假设",
        decision="confirmed",
        reviewer="synthetic-offline-review",
        review_basis="受控合成复核记录",
    )
    case.close()
    return tmp_path


def _mapping_spec(doc: bytes, quote: str) -> dict:
    text = doc.decode("utf-8")
    pos = text.find(quote)
    assert pos >= 0
    start = len(text[:pos].encode("utf-8"))
    return {"quote": quote, "start": start,
            "end": start + len(quote.encode("utf-8"))}


_CONFIRM = {"confirmed": True, "confirmator": "executor-r1_3",
            "review_basis": "来源陈述该市场需求假设（范围：来源陈述该假设）"}


def _claim_spec(doc: bytes = DOC_V4, claim_id="CLM-SYN-CRL1"):
    return {
        "claim_id": claim_id,
        "locator_kind": "byte_range", "start": 0, "end": len(doc),
        "interpretation": "第三方载明主体的市场需求假设（AR眼镜显示需求）。",
        "subject_scope": SUBJECT,
        "criterion_mapping": _mapping_spec(doc, QUOTE),
        "semantic_confirmation": _CONFIRM,
        "mapping_review_id": "REV-SYN-CRL1",
    }


def _catalog():
    from kth_hybrid.catalog import build_catalog_from_wheel

    return build_catalog_from_wheel()


def test_synthetic_qualified_slice_with_mapping(case_dir):
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    assert result["qualification_status"] == "qualified"
    assert result["mapping_status"] == "confirmed"
    assert result["product_status"] == "succeeded"
    assert result["native_disposition"] is None
    assert result["trace_ok"] is True
    assert "不是原生 met" in result["rationale"]


def test_runner_resolves_criterion_from_catalog(case_dir):
    canonical = resolve_criterion("CRL1-C1", _catalog())
    assert canonical["dimension"] == "CRL" and canonical["level"] == 1
    with pytest.raises(ValueError, match="不在可信 catalog"):
        resolve_criterion("INVENTED-ID", _catalog())


def test_unimplemented_criterion_in_slice_is_method_unsupported(case_dir):
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN",
        claim_spec=_claim_spec(claim_id="CLM-SYN-CRL2"),
        criterion_id="CRL2-C1", catalog=_catalog(), case_basis=BASIS)
    assert result["product_status"] == "method_unsupported"
    assert result["trace_ok"] is True


def test_frozen_input_recompute_is_idempotent(case_dir):
    first = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    second = run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    assert first["result_id"] == second["result_id"]
    assert second["input_digest"] == first["input_digest"]
    assert second["replay_consistent"] is True
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        assert len(case.fetch_all("sources")) == 1
        assert len(case.fetch_all("claims")) == 1
        assert len(case.fetch_all("criterion_results")) == 1
    finally:
        case.close()


def test_same_claim_id_different_interpretation_rejected(case_dir):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    with pytest.raises(RuntimeError, match="输入身份不一致"):
        run_criterion_slice(
            case_dir, source_id="SRC-SYN",
            claim_spec=dict(_claim_spec(), interpretation="不同解释（应拒绝）"),
            criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)


def test_late_retrieval_real_gap_path(case_dir):
    # v4：清除时间证据修订并把抓取时间改为晚于截止 → 时间fail→rejected
    case = CaseStore(case_dir / "records.sqlite3")
    with case._conn:
        case._conn.execute("DELETE FROM source_time_evidence")
        case._conn.execute(
            "UPDATE sources SET retrieved_at='2026-08-28T02:43:19Z' "
            "WHERE source_id='SRC-SYN'")
    case.close()
    result = run_criterion_slice(
        case_dir, source_id="SRC-SYN",
        claim_spec=_claim_spec(claim_id="CLM-SYN-LATE"),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    assert result["qualification_status"] == "rejected"
    assert result["product_status"] == "insufficient"
    assert result["trace_ok"] is True


def test_runner_requires_sourced_case_basis(case_dir):
    with pytest.raises(ValueError, match="subject_source_basis"):
        run_criterion_slice(
            case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
            criterion_id="CRL1-C1", catalog=_catalog(),
            case_basis={"subject_legal_name": SUBJECT, "subject_aliases": [],
                        "evidence_cutoff": "2026-08-27T03:02:29Z"})


def test_counting_simulated_provider_dispatches_exactly_once(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        provider = CountingSimulatedProvider(journal, blobs=blobs)
        out1 = provider.execute("mission-1", "input-1", lambda: b"response-1")
        assert provider.dispatch_count == 1
        assert blobs.read_bytes(out1) == b"response-1"
        assert journal.task_state("mission-1")["state"] == "succeeded"
        with pytest.raises(CommitRejected):
            provider.execute("mission-1", "input-1", lambda: b"response-1b")
        assert provider.dispatch_count == 1
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
        assert journal.task_state("mission-c")["state"] == "dispatch_recorded"
        journal.mark_recovered_unknown("mission-c")
        assert journal.task_state("mission-c")["state"] == "outcome_unknown"
        with pytest.raises(CommitRejected):
            provider.execute("mission-c", "input-1", lambda: b"resp")
        assert provider.dispatch_count == 1
    finally:
        journal.close()


def test_cli_inspect_and_trace(case_dir, capsys):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
    code = cli.main(["inspect", "--case-dir", str(case_dir)])
    captured = capsys.readouterr()
    assert code == 0
    assert "证据与判据核验，非正式评估报告" in captured.out
    assert "CRL1-C1" in captured.out

    code = cli.main(["trace", "--case-dir", str(case_dir),
                     "--result-id", "RESR::CLM-SYN-CRL1::CRL1-C1"])
    captured = capsys.readouterr()
    assert code == 0
    assert "链核验：通过" in captured.out


def test_cli_trace_broken_chain_returns_nonzero(case_dir, capsys):
    run_criterion_slice(
        case_dir, source_id="SRC-SYN", claim_spec=_claim_spec(),
        criterion_id="CRL1-C1", catalog=_catalog(), case_basis=BASIS)
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


# ---- 真实 Case 门控复验（KTH_REAL_CASE_DIR_2；R1.2 新Case weijiu-r1_2） ----

REAL_CASE = os.environ.get("KTH_REAL_CASE_DIR_3")


@pytest.mark.skipif(not REAL_CASE, reason="未设置 KTH_REAL_CASE_DIR_3（R1.3新Case）")
class TestRealCaseSlicesR13:
    """R1.2 真实切片期望（诚实规则下的结果）：

    - 政府页（8bd0fcdb）：正文自载发布时间（真实定位）＋第三方提及主体全名
      ＋市场需求引文映射＋留痕语义确认 → 期望 qualified 且 CRL1-C1 消费
      succeeded（真实正向；范围=来源陈述该假设）。
    - BP：第一方主体一致但时间证据为文件名候选级 → needs_review → 不足。
    - 公司报道：自载发布时间晚于截止7小时 → rejected → 不足。
    """

    def _load_specs(self):
        return json.loads(
            (Path(REAL_CASE) / "audit" / "real-slice-specs-r1_3.json")
            .read_text(encoding="utf-8"))

    def _run(self, spec, specs):
        claim_spec = dict(spec["claim_spec"])
        claim_spec.setdefault("semantic_confirmation",
                              specs.get("semantic_confirmation"))
        return run_criterion_slice(
            Path(REAL_CASE), source_id=spec["source_id"],
            claim_spec=claim_spec, criterion_id=spec["criterion_id"],
            catalog=_catalog(), case_basis=specs["case_basis"])

    def test_real_gov_claim_qualified_and_consumed(self):
        specs = self._load_specs()
        gov = next(s for s in specs["slices"]
                   if "GOV" in s["claim_spec"]["claim_id"])
        result = self._run(gov, specs)
        assert result["qualification_status"] == "qualified"
        assert result["mapping_status"] == "confirmed"
        assert result["product_status"] == "succeeded"
        assert result["trace_ok"] is True

    def test_real_bp_claim_honestly_needs_review(self):
        specs = self._load_specs()
        bp = next(s for s in specs["slices"]
                  if "BP" in s["claim_spec"]["claim_id"])
        result = self._run(bp, specs)
        assert result["qualification_status"] == "needs_review"
        assert result["product_status"] == "insufficient"
        assert result["trace_ok"] is True

    def test_real_company_report_late_capture_is_insufficient(self):
        specs = self._load_specs()
        cap = next(s for s in specs["slices"]
                   if "CAP" in s["claim_spec"]["claim_id"])
        result = self._run(cap, specs)
        assert result["qualification_status"] == "rejected"
        assert result["product_status"] == "insufficient"
        assert result["trace_ok"] is True
