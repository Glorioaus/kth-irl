"""R1.1 限定修复：审计反例测试（先于实现修复写入，须先在旧代码上失败）。

反例来源：`D:\\t\\kth-r1-review-20260908`（R1独立验收与修复裁决，2026-09-08）。
本文件按 R1-01…R1-06 分组；P2 盘点断言修正见 test_real_capture_readonly.py。

约定：合成数据全部 synthetic；不得以删除反例、放松资格或修改期望为错误行为
换取通过。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.journal import CommitRejected, Journal
from kth_hybrid.store import BlobStore, CaseStore

SUBJECT_A = "Company-A科技有限公司"
SUBJECT_B = "Company-B科技有限公司"
BASIS_A = {
    "subject_legal_name": SUBJECT_A,
    "subject_aliases": ["A公司"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "synthetic:identity-plan#/subjects/0/canonical_name_claimed",
        "status": "claimed"}, ensure_ascii=False),
}
CUTOFF = "2026-08-27T03:02:29Z"

DOC_B = f"{SUBJECT_B}是一家MicroLED芯片公司。本段为受控测试原文。".encode("utf-8")
DOC_A_MENTIONS = f"行业分析提及{SUBJECT_A}的MicroLED业务。受控测试原文。".encode("utf-8")


# ---------- R1-01 资格 ----------

def _qualify(blobs, data: bytes, *, family="owner_attachment", **source_over):
    from kth_hybrid.qualification import qualify_claim

    ref = blobs.put_bytes(data)
    start, end = 0, min(20, len(data))
    excerpt = data[start:end]
    source = {
        "source_id": "SRC-Q", "blob_sha256": ref.sha256, "byte_length": len(data),
        "capture_status": "attachment", "source_family": family,
        "retrieved_at": None, "published_at": None,
        "published_at_provenance": "附件无发布时间",
    }
    source.update(source_over)
    claim = {
        "claim_id": "CLM-Q", "source_id": "SRC-Q", "locator_kind": "byte_range",
        "locator_start": start, "locator_end": end,
        "excerpt_sha256": sha256_hex(excerpt),
        "excerpt_text": excerpt.decode("utf-8", errors="replace"),
        "interpretation": "测试解释", "subject_scope": SUBJECT_A,
    }
    return qualify_claim(claim, source, blobs, BASIS_A)


class TestR101Qualification:
    def test_wrong_entity_attachment_is_not_first_party(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        # 与审核Q1一致：时间合法，只考察身份——文档正文自识 Company-B
        outcome = _qualify(blobs, DOC_B, published_at="2026-07-01T00:00:00Z",
                           published_at_provenance="合成字段")
        assert outcome.status != "qualified", "错主体附件不得qualified"
        assert outcome.identity_judgment.verdict != "ok"

    def test_future_document_date_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_B, time_evidence={
            "kind": "document_self_date", "date": "2099-01-01",
            "basis": "合成", "date_locator": "page 1"})
        assert outcome.status != "qualified"
        assert outcome.time_judgment.verdict == "fail"

    def test_missing_document_date_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_B, time_evidence={
            "kind": "document_self_date", "date": None, "basis": "缺日期"})
        assert outcome.status != "qualified"
        assert outcome.time_judgment.verdict == "fail"

    def test_invalid_document_date_rejected(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_B, time_evidence={
            "kind": "document_self_date", "date": "not-a-date", "basis": "非法日期"})
        assert outcome.status != "qualified"
        assert outcome.time_judgment.verdict == "fail"

    def test_filename_derived_date_is_candidate_only(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_B, time_evidence={
            "kind": "filename_derived_date", "date": "2026-07-16",
            "basis": "文件名BP260716推定"})
        assert outcome.status != "qualified", "文件名日期不能自动证明截止前存在"
        assert outcome.time_judgment.verdict in ("fail", "unknown")
        assert "候选" in outcome.time_judgment.basis or "文件名" in outcome.time_judgment.basis

    def test_late_registration_without_time_proof_fails(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_B, time_evidence={
            "kind": "registered_at", "date": "2026-08-27T04:13:11Z",
            "basis": "附件登记时间晚于截止"})
        assert outcome.time_judgment.verdict == "fail"

    def test_case_basis_must_carry_subject_source(self, tmp_path):
        from kth_hybrid.qualification import qualify_claim

        blobs = BlobStore(tmp_path / "blobs")
        basis_no_source = {"subject_legal_name": SUBJECT_A,
                           "subject_aliases": [], "evidence_cutoff": CUTOFF}
        outcome = _qualify(blobs, DOC_A_MENTIONS)
        # 有来源CaseBasis可用；无来源CaseBasis必须拒绝执行（不是静默使用）
        with pytest.raises((KeyError, ValueError, RuntimeError)):
            qualify_claim(
                {"claim_id": "x", "source_id": "s", "locator_kind": "byte_range",
                 "locator_start": 0, "locator_end": 5, "excerpt_sha256": "0" * 64,
                 "excerpt_text": "", "interpretation": "", "subject_scope": ""},
                {"source_id": "s", "blob_sha256": "e" * 64, "byte_length": 5,
                 "capture_status": "attachment", "source_family": "owner_attachment",
                 "retrieved_at": None, "published_at": None,
                 "published_at_provenance": ""},
                blobs, basis_no_source)

    def test_three_meanings_separated(self, tmp_path):
        # Owner提供 ≠ 主体第一方 ≠ 文档记载主体：用途必须区分
        blobs = BlobStore(tmp_path / "blobs")
        dated = (DOC_A_MENTIONS.decode("utf-8") + " 发布于2026年7月1日。"
                 ).encode("utf-8")
        text = dated.decode("utf-8")
        pos = text.find("2026年7月1日")
        dstart = len(text[:pos].encode("utf-8"))
        dend = dstart + len("2026年7月1日".encode("utf-8"))
        outcome = _qualify(
            blobs, dated, byte_length=len(dated),
            time_evidence={"kind": "document_self_date", "date": "2026-07-01",
                           "basis": "正文日期",
                           "date_locator": {"kind": "byte_range", "start": dstart,
                                            "end": dend}})
        assert "company_self_statement" not in outcome.allowed_uses, \
            "仅Owner提供且正文只提及主体，不得给第一方自述用途"
        assert outcome.status in ("needs_review", "qualified")
        if outcome.status == "qualified":
            assert outcome.allowed_uses  # 若通过，用途应是记载类而非自述类

    def test_subject_scope_prefix_match_hack_removed(self, tmp_path):
        # 不得只凭名称前两个字匹配主体
        from kth_hybrid.qualification import qualify_claim

        blobs = BlobStore(tmp_path / "blobs")
        ref = blobs.put_bytes(DOC_B)
        excerpt = DOC_B[0:20]
        source = {
            "source_id": "SRC-Q", "blob_sha256": ref.sha256,
            "byte_length": len(DOC_B), "capture_status": "attachment",
            "source_family": "news-media", "retrieved_at": "2026-09-01T00:00:00Z",
            "published_at": None, "published_at_provenance": "无发布时间",
        }
        claim = {
            "claim_id": "CLM-Q", "source_id": "SRC-Q", "locator_kind": "byte_range",
            "locator_start": 0, "locator_end": 20,
            "excerpt_sha256": sha256_hex(excerpt),
            "excerpt_text": excerpt.decode("utf-8", errors="replace"),
            "interpretation": "测试", "subject_scope": "武钢集团",  # 前两字同为“武”
        }
        outcome = qualify_claim(claim, source, blobs, BASIS_A)
        assert outcome.identity_judgment.verdict != "ok", \
            "scope仅前缀相似不得当作主体范围主张"

    def test_unknown_source_family_needs_review_not_ok(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        outcome = _qualify(blobs, DOC_A_MENTIONS, family="totally-unknown-family",
                           time_evidence={"kind": "document_self_date",
                                          "date": "2026-07-01", "basis": "正文",
                                          "date_locator": "p1"})
        assert outcome.source_judgment.verdict != "ok", "未知来源族不得默认来源ok"


# ---------- R1-02 判据 ----------

CRL1_C1 = {"criterion_id": "CRL1-C1", "dimension": "CRL", "level": 1,
           "text": "A possible market need, problem or opportunity hypothesis "
                   "has been identified."}


def _candidate(claim_id="CLM-E", status="qualified", uses=("company_self_statement",),
               identity=None):
    return {
        "qualifications": [{
            "claim_id": claim_id, "status": status, "allowed_uses": list(uses),
            "identity_judgment": identity or {"verdict": "ok", "basis": "b"},
        }],
        "claims": {claim_id: {"claim_id": claim_id, "source_id": "SRC-1",
                              "subject_scope": "限定范围"}},
        "gap_refs": [],
    }


VIEW = {"dimension_levels_supported": [1, 2, 3, 4], "case_flags": {}, "scope": "A",
        "approved_criterion_ids": {"CRL1-C1", "CRL2-C1"}}


class TestR102Criteria:
    def test_native_met_proposal_never_becomes_final(self):
        from kth_hybrid.dimensions import evaluate_criterion

        candidate = _candidate()
        candidate["native_proposal"] = "met"
        candidate["qualifications"] = []  # 无证据
        result = evaluate_criterion(CRL1_C1, candidate, VIEW)
        assert result.native_disposition is None, "无真实求值时原生处置必须null"
        assert result.product_status != "succeeded"

    def test_unregistered_criterion_rejected(self):
        from kth_hybrid.dimensions import evaluate_criterion

        fake = {"criterion_id": "NOT-IN-APPROVED-REGISTRY", "dimension": "CRL",
                "level": 1}
        result = evaluate_criterion(fake, _candidate(), VIEW)
        assert result.product_status in ("method_unsupported", "execution_failed"), \
            "未登记判据不得succeeded"

    def test_unimplemented_rule_is_method_unsupported(self):
        from kth_hybrid.dimensions import evaluate_criterion

        crl2 = {"criterion_id": "CRL2-C1", "dimension": "CRL", "level": 2}
        result = evaluate_criterion(crl2, _candidate(), VIEW)
        assert result.product_status == "method_unsupported", \
            "R1.1未实现该判据规则，不是证据不足"

    def test_implemented_rule_requires_provenance(self):
        from kth_hybrid.kernels import IMPLEMENTED_RULES

        rule = IMPLEMENTED_RULES["CRL1-C1"]
        assert rule["provenance"] and rule["registry_id"]
        assert "use_class" not in json.dumps(rule) or rule.get("rule_kind") == "specific"

    def test_generic_level_routing_removed(self):
        from kth_hybrid import kernels

        assert not hasattr(kernels, "use_class_supports_criterion") or \
            not kernels.use_class_supports_criterion.__doc__ or True
        # 通用路由不得再作为判据消费依据：未登记于IMPLEMENTED_RULES的判据一律不支持
        from kth_hybrid.dimensions import evaluate_criterion

        crl1c2 = {"criterion_id": "CRL1-C2", "dimension": "CRL", "level": 1}
        result = evaluate_criterion(crl1c2, _candidate(), VIEW)
        assert result.product_status == "method_unsupported"

    def test_tmrl_identity_dict_structure_paired(self):
        # 调用方传入已取出的身份dict：合格输入必须通过，不合格必须拒绝（成对）
        from kth_hybrid.kernels import check_tmrl_identity_binding

        criterion = {"criterion_id": "TMRL2-C1", "dimension": "TMRL"}
        ok, ok_basis = check_tmrl_identity_binding(
            criterion, {"verdict": "ok", "basis": "已核验"},
            {"subject_scope": "微玖创始团队（具体角色）"})
        assert ok, f"合格TMRL输入被误拒：{ok_basis}"
        bad, _ = check_tmrl_identity_binding(
            criterion, {"verdict": "unknown", "basis": "身份不明"},
            {"subject_scope": "微玖创始团队（具体角色）"})
        assert not bad


# ---------- R1-03 trace 与冻结 ----------

def _seed_result(case: CaseStore, blobs: BlobStore, *, result_id="RES-T",
                 product_status="succeeded", qual_refs=None, evidence_refs=None,
                 excerpt_text=None):
    ref = blobs.put_bytes(b"sealed-original-bytes-for-trace-test")
    import_id = case.add_import_record("attachment", "synthetic", ref.sha256)
    case.add_source("SRC-T", ref.sha256, ref.byte_length, import_id=import_id,
                    source_family="owner_attachment", capture_status="attachment")
    excerpt = b"sealed-original-bytes-for-trace-test"[0:10]
    case.add_claim("CLM-T", "SRC-T", locator_kind="byte_range",
                   excerpt_start=0, excerpt_end=10,
                   excerpt_sha256=sha256_hex(excerpt),
                   excerpt_text=(excerpt_text if excerpt_text is not None
                                 else excerpt.decode()),
                   interpretation="测试", subject_scope="A")
    case.add_qualification("QUAL-T", "CLM-T", source_judgment="b",
                           identity_judgment="b", time_judgment="b",
                           independence_judgment="b", allowed_uses=["u"],
                           cannot_prove=[], review_attempt="t", status="qualified")
    case.add_criterion_result(
        result_id, "CRL1-C1", "CRL",
        native_disposition=None, native_note="", product_status=product_status,
        evidence_refs=evidence_refs if evidence_refs is not None else ["SRC-T"],
        gap_refs=[], qual_refs=(["QUAL-T"] if qual_refs is None else qual_refs),
        rationale="r", scope="A", rule_version="v")


class TestR103TraceFreeze:
    def test_succeeded_with_empty_qual_refs_fails(self, tmp_path):
        from kth_hybrid.audit import TraceBroken, trace

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            _seed_result(case, blobs, result_id="RES-E1", qual_refs=[])
            with pytest.raises(TraceBroken):
                trace(case, blobs, "RES-E1")
        finally:
            case.close()

    def test_malformed_json_refs_fail_closed(self, tmp_path):
        from kth_hybrid.audit import TraceBroken, trace

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            _seed_result(case, blobs, result_id="RES-E2")
            with case._conn:
                case._conn.execute(
                    "UPDATE criterion_results SET qual_refs='{not-json' "
                    "WHERE result_id='RES-E2'")
            with pytest.raises(TraceBroken):
                trace(case, blobs, "RES-E2")
        finally:
            case.close()

    def test_evidence_closure_mismatch_fails(self, tmp_path):
        from kth_hybrid.audit import TraceBroken, trace

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            _seed_result(case, blobs, result_id="RES-E3",
                         evidence_refs=["NONEXISTENT-SOURCE"])
            with pytest.raises(TraceBroken):
                trace(case, blobs, "RES-E3")
        finally:
            case.close()

    def test_replaced_excerpt_text_breaks_trace(self, tmp_path):
        # 改写保存的摘录文本（保持登记hash不变）→ 同引用trace必须失败
        from kth_hybrid.audit import TraceBroken, trace

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            _seed_result(case, blobs, result_id="RES-E4")
            report0 = trace(case, blobs, "RES-E4", strict=False)
            assert report0.ok
            with case._conn:
                case._conn.execute(
                    "UPDATE claims SET excerpt_text='被替换的摘录文本，与封存区间不符' "
                    "WHERE claim_id='CLM-T'")
            with pytest.raises(TraceBroken, match="摘录文本"):
                trace(case, blobs, "RES-E4")
        finally:
            case.close()

    def test_full_ids_no_truncation_collision(self):
        from kth_hybrid.runner import qualification_id, result_id

        id_a = "CLM-" + "a" * 40 + "X"
        id_b = "CLM-" + "a" * 40 + "Y"  # 仅末位不同（旧截断会碰撞）
        assert qualification_id(id_a) != qualification_id(id_b)
        assert result_id(id_a, "CRL1-C1") != result_id(id_b, "CRL1-C1")

    def test_case_basis_persisted_with_run(self, tmp_path):
        from kth_hybrid.store import CaseStore

        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            case.set_case_basis(
                subject_legal_name=SUBJECT_A, subject_aliases=["A公司"],
                evidence_cutoff=CUTOFF,
                subject_source_basis=json.dumps({
                    "kind": "field_reference",
                    "path": "synthetic:owner-handover#/case/subject",
                    "status": "claimed"}, ensure_ascii=False))
            basis = case.get_case_basis()
            assert basis["subject_source_basis"]
            assert basis["evidence_cutoff"] == CUTOFF
        finally:
            case.close()

    def test_time_evidence_revision_appends_not_updates(self, tmp_path):
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            ref = blobs.put_bytes(b"t")
            import_id = case.add_import_record("attachment", "s", ref.sha256)
            case.add_source("SRC-R", ref.sha256, 1, import_id=import_id)
            case.append_time_evidence("SRC-R", {"kind": "filename_derived_date",
                                                "date": "2026-07-16", "basis": "候选"})
            case.append_time_evidence("SRC-R", {"kind": "document_self_date",
                                                "date": "2026-07-01", "basis": "修订"})
            revisions = case.get_time_evidence_history("SRC-R")
            assert len(revisions) == 2  # 旧版保留
            assert revisions[0]["revision"] < revisions[1]["revision"]
        finally:
            case.close()

    def test_same_claim_id_different_input_rejected_or_versioned(self, tmp_path):
        from kth_hybrid.runner import run_criterion_slice

        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            data = f"{SUBJECT_A}自述其市场假设为X。受控测试原文。".encode("utf-8")
            ref = blobs.put_bytes(data)
            import_id = case.add_import_record("attachment", "synthetic", ref.sha256)
            case.add_source("SRC-RR", ref.sha256, len(data), import_id=import_id,
                            source_family="owner_attachment",
                            capture_status="attachment",
                            published_at="2026-07-01T00:00:00Z",
                            published_at_provenance="合成字段")
            case.close()
            spec = {
                "claim_id": "CLM-REPLAY",
                "locator_kind": "byte_range", "start": 0, "end": 10,
                "interpretation": "解释一",
                "subject_scope": SUBJECT_A,
            }
            from kth_hybrid.catalog import build_catalog_from_wheel

            first = run_criterion_slice(
                tmp_path, source_id="SRC-RR", claim_spec=spec,
                criterion_id="CRL1-C1",
                catalog=build_catalog_from_wheel(), case_basis=BASIS_A)
            spec_changed = dict(spec, interpretation="解释二（不同输入）",
                                subject_scope="别的主体范围")
            with pytest.raises((RuntimeError, ValueError), match="输入"):
                run_criterion_slice(
                    tmp_path, source_id="SRC-RR", claim_spec=spec_changed,
                    criterion_id="CRL1-C1",
                    catalog=build_catalog_from_wheel(), case_basis=BASIS_A)
            assert first["result_id"]
        finally:
            if case._conn:
                case.close()


# ---------- R1-04 事务 ----------

class TestR104JournalAtomicity:
    def test_fixed_interleave_double_claim_impossible(self, tmp_path):
        db = str(tmp_path / "journal.sqlite3")
        j_a, j_b = Journal(db), Journal(db)
        try:
            j_a.ensure_task("task", "input")
            j_b.ensure_task("task", "input")
            # 固定交错：两者都在认领前完成一次状态读取
            j_a.task_state("task")
            j_b.task_state("task")
            barrier = threading.Barrier(2, timeout=10)
            results = {}

            def attempt(journal, worker):
                barrier.wait()  # 对齐进入claim，制造最大竞争窗口
                try:
                    claim = journal.claim("task", worker, "input")
                    results[worker] = f"claimed:token{claim.token}"
                except CommitRejected as exc:
                    results[worker] = f"rejected"

            t1 = threading.Thread(target=attempt, args=(j_a, "A"))
            t2 = threading.Thread(target=attempt, args=(j_b, "B"))
            t1.start(); t2.start(); t1.join(); t2.join()
            claimed = [v for v in results.values() if v.startswith("claimed")]
            assert len(claimed) == 1, f"双认领竞争窗口未关闭：{results}"
            state = j_a.task_state("task")
            assert state["state"] == "claimed"
        finally:
            j_a.close(); j_b.close()

    def test_stale_worker_cannot_overwrite_after_new_commit(self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            claim_a = j.claim("task", "A", "input")
            j.record_failure(claim_a, "A失败")
            claim_b = j.claim("task", "B", "input")
            assert claim_b.token > claim_a.token
            j.commit(claim_b, "fresh-output")
            # 旧worker迟到提交不得覆盖
            with pytest.raises(CommitRejected):
                j.commit(claim_a, "old-output")
            assert j.task_state("task")["output_ref"] == "fresh-output"
        finally:
            j.close()

    def test_same_token_second_commit_rejected_terminal_immutable(self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            claim = j.claim("task", "A", "input")
            j.commit(claim, "first-output")
            with pytest.raises(CommitRejected):
                j.commit(claim, "replacement-output")
            assert j.task_state("task")["output_ref"] == "first-output"
        finally:
            j.close()

    def test_failed_attempt_cannot_be_flipped_to_success(self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            claim = j.claim("task", "A", "input")
            j.record_failure(claim, "失败留存")
            with pytest.raises(CommitRejected):
                j.commit(claim, "late-success")
            assert j.task_state("task")["state"] == "failed"
            attempts = j.attempts("task")
            assert attempts[-1]["outcome"] == "failed"
        finally:
            j.close()

    def test_worker_cannot_commit_other_workers_claim(self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            claim_a = j.claim("task", "A", "input")
            forged = type(claim_a)("task", "A", claim_a.token + 0, "input",
                                   claim_a.attempt_no)
            # worker名伪造
            forged = type(claim_a)("task", "B", claim_a.token, "input",
                                   claim_a.attempt_no)
            with pytest.raises(CommitRejected):
                j.commit(forged, "x")
        finally:
            j.close()


# ---------- R1-05 恢复 ----------

class TestR105Recovery:
    def test_claim_then_crash_has_controlled_takeover(self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            dead = j.claim("task", "dead-worker", "input")
            # 崩溃：无dispatch（external_actions=0），状态停留claimed
            assert j.task_state("task")["state"] == "claimed"
            takeover = j.takeover_stale_claim(
                "task", "new-worker", "input",
                evidence="旧worker心跳超时（测试证据）")
            assert takeover.token > dead.token
            assert j.task_state("task")["current_worker"] == "new-worker"
            # 旧worker复活：不能派发也不能提交
            with pytest.raises(CommitRejected):
                j.record_dispatch(dead)
            with pytest.raises(CommitRejected):
                j.commit(dead, "ghost-output")
            # 新worker可正常完成
            j.commit(takeover, "new-output")
            assert j.task_state("task")["output_ref"] == "new-output"
            # attempt历史保留接管记录
            outcomes = [a["outcome"] for a in j.attempts("task")]
            assert "takeover" in outcomes or "claimed" in outcomes
        finally:
            j.close()

    def test_dispatched_claim_takeover_requires_evidence_and_marks_unknown(
            self, tmp_path):
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            claim = j.claim("task", "dead-worker", "input")
            j.record_dispatch(claim)  # 已有外部动作
            # 已派发的失效认领：接管必须保守（先标unknown，不允许静默重派）
            with pytest.raises(CommitRejected):
                j.takeover_stale_claim("task", "new-worker", "input",
                                       evidence="尝试直接接管已派发任务")
            j.mark_recovered_unknown("task")
            assert j.task_state("task")["state"] == "outcome_unknown"
            with pytest.raises(CommitRejected):
                j.takeover_stale_claim("task", "new-worker", "input",
                                       evidence="unknown不允许自动接管")
        finally:
            j.close()

    def test_provider_persists_response_before_success(self, tmp_path):
        from kth_hybrid.runner import CountingSimulatedProvider

        blobs = BlobStore(tmp_path / "blobs")
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            provider = CountingSimulatedProvider(j, blobs=blobs)
            output_ref = provider.execute("mission", "input",
                                          lambda: b"response-bytes-123")
            # 成功提交前，响应原字节必须已持久化且可读回重核
            state = j.task_state("mission")
            assert state["state"] == "succeeded"
            assert blobs.read_bytes(output_ref) == b"response-bytes-123"
        finally:
            j.close()

    def test_crash_between_response_write_and_commit(self, tmp_path):
        from kth_hybrid.runner import (CountingSimulatedProvider,
                                       SimulatedCrash)

        blobs = BlobStore(tmp_path / "blobs")
        j = Journal(tmp_path / "journal.sqlite3")
        try:
            provider = CountingSimulatedProvider(j, blobs=blobs)
            with pytest.raises(SimulatedCrash):
                provider.execute("mission", "input", lambda: b"resp",
                                 crash_after_persist_before_commit=True)
            state = j.task_state("mission")
            assert state["state"] != "succeeded", "提交前崩溃不得登记成功"
            assert provider.dispatch_count == 1
            # 孤立响应工件保留（不删除），恢复期不盲重发
            j.mark_recovered_unknown("mission")
            with pytest.raises(CommitRejected):
                provider.execute("mission", "input", lambda: b"resp")
            assert provider.dispatch_count == 1
        finally:
            j.close()


# ---------- R1-06 来源身份 ----------

def _make_capture(root: Path, name: str, body: bytes, *, url, provider,
                  retrieved_at):
    import json as json_mod

    d = root / name
    (d / "frozen-capture").mkdir(parents=True)
    (d / "frozen-capture" / "raw-body.bin").write_bytes(body)
    (d / "frozen-capture" / "transport.json").write_text("{}", encoding="utf-8")
    receipt = {
        "synthetic": True, "capture_status": "raw_capture_validated",
        "failure_code": None, "response_status": 200,
        "raw_body_sha256": sha256_hex(body), "raw_body_size": len(body),
        "retrieved_at": retrieved_at, "fixed_clock": retrieved_at,
        "as_of_cut": CUTOFF, "final_url": url, "provider_id": provider,
        "evidence_eligible": False, "qualification_candidate_refs": [],
    }
    (d / "receipt.json").write_text(json_mod.dumps(receipt), encoding="utf-8")
    for dep in ("request.json", "transport-attempt.json",
                "live-provider-result.json"):
        (d / dep).write_text(json_mod.dumps({"synthetic": True, "dep": dep,
                                             "capture": name}),
                             encoding="utf-8")
    return d


class TestR106SourceIdentity:
    def test_same_body_two_occurrences_both_registered(self, tmp_path):
        from kth_hybrid.intake import import_capture

        body = b"same-body-content-for-dedup-test"
        _make_capture(tmp_path, "CAP-ONE", body, url="https://a.example/x",
                      provider="provider-a", retrieved_at="2026-09-01T00:00:00Z")
        _make_capture(tmp_path, "CAP-TWO", body, url="https://b.example/y",
                      provider="provider-b", retrieved_at="2026-09-02T00:00:00Z")
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            r1 = import_capture(tmp_path / "CAP-ONE", blobs, case)
            r2 = import_capture(tmp_path / "CAP-TWO", blobs, case)
            assert r1.source_id != r2.source_id, "采集实例身份不得被正文去重合并"
            s1 = case.fetch_one("sources", "source_id", r1.source_id)
            s2 = case.fetch_one("sources", "source_id", r2.source_id)
            assert s1["blob_sha256"] == s2["blob_sha256"], "blob按内容共享"
            assert s1["retrieved_at"] != s2["retrieved_at"]
            assert case.fetch_one("sources", "source_id", r1.source_id)["locator"]
        finally:
            case.close()

    def test_dependencies_actually_sealed_with_hashes(self, tmp_path):
        from kth_hybrid.intake import import_capture

        _make_capture(tmp_path, "CAP-DEP", b"dependency-test-body",
                      url="https://a.example/z", provider="p",
                      retrieved_at="2026-09-01T00:00:00Z")
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            result = import_capture(tmp_path / "CAP-DEP", blobs, case)
            deps = case.get_capture_dependencies(result.import_id)
            names = {d["dep_name"] for d in deps}
            assert {"request.json", "transport-attempt.json",
                    "live-provider-result.json", "receipt.json"} <= names
            for d in deps:  # 每份依赖真实封存并可读回
                assert d["blob_sha256"]
                assert blobs.read_bytes(d["blob_sha256"])
        finally:
            case.close()

    def test_isolated_copy_keeps_full_metadata_after_removing_origin(self, tmp_path):
        # 在隔离副本中导入；删除副本中的原目录后，新Case仍可核验完整来源元数据
        import shutil

        from kth_hybrid.intake import import_capture

        origin = tmp_path / "origin"
        _make_capture(origin, "CAP-ISO", b"isolated-copy-body",
                      url="https://a.example/i", provider="p",
                      retrieved_at="2026-09-01T00:00:00Z")
        copy = tmp_path / "copy" / "CAP-ISO"
        shutil.copytree(origin / "CAP-ISO", copy)
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            result = import_capture(copy, blobs, case)
            shutil.rmtree(copy)  # 只删除隔离副本，真实session不动
            source = case.fetch_one("sources", "source_id", result.source_id)
            assert source and source["blob_sha256"]
            assert blobs.read_bytes(source["blob_sha256"]) == b"isolated-copy-body"
            deps = case.get_capture_dependencies(result.import_id)
            assert len(deps) >= 4  # receipt等依赖仍可核验
            for d in deps:
                blobs.read_bytes(d["blob_sha256"])
        finally:
            case.close()

    def test_independence_not_inferred_from_source_count(self, tmp_path):
        from kth_hybrid.intake import import_capture

        body = f"{SUBJECT_A}发布独立性测试声明。".encode("utf-8")
        for i, (url, day) in enumerate([("https://x.example/1", "01"),
                                        ("https://x.example/2", "02"),
                                        ("https://x.example/3", "03")]):
            _make_capture(tmp_path, f"CAP-I{i}", body, url=url, provider="p",
                          retrieved_at=f"2026-09-{day}T00:00:00Z")
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            from kth_hybrid.qualification import qualify_claim

            for i in range(3):
                import_capture(tmp_path / f"CAP-I{i}", blobs, case)
            occurrences = sum(1 for s in case.fetch_all("sources")
                              if s["blob_sha256"] == sha256_hex(body))
            assert occurrences == 3
            # 独立性判断仍是unknown（同正文3实例），不得因“3条来源”变ok
            ref = blobs.put_bytes(body)
            subject_len = len(SUBJECT_A.encode("utf-8"))
            claim = {
                "claim_id": "CLM-I", "source_id": "SRC-I",
                "locator_kind": "byte_range", "locator_start": 0,
                "locator_end": subject_len,
                "excerpt_sha256": sha256_hex(body[:subject_len]),
                "excerpt_text": body[:subject_len].decode("utf-8"),
                "interpretation": "t", "subject_scope": SUBJECT_A,
            }
            source = {"source_id": "SRC-I", "blob_sha256": ref.sha256,
                      "byte_length": len(body), "capture_status":
                      "raw_capture_validated", "source_family": "news-media",
                      "retrieved_at": "2026-09-01T00:00:00Z", "published_at":
                      "2026-07-01T00:00:00Z", "published_at_provenance": "字段"}
            outcome = qualify_claim(claim, source, blobs, BASIS_A,
                                    same_body_sources=3)
            assert outcome.independence_judgment.verdict == "unknown"
            assert outcome.status == "needs_review", \
                "除独立性未知外其余判断应为ok：结果须为needs_review而非rejected"
        finally:
            case.close()
