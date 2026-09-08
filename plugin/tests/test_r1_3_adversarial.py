"""R1.3 限定收口回归测试（先于实现写入，须先在 3a4f6b4 基线复现失败）。

来源：`D:\\t\\kth-r12-review-20260908`（R1.2独立复验结论 + residual_probes）。
分组：A 统一可核验依据 / B 受控窄映射与有源N/A / C 完整输入绑定与原子发布。
每类含合法正例、非法/无源/错对象反例与故障路径；不针对样本文字加特判。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.journal import Journal
from kth_hybrid.store import BlobStore, CaseStore

SUBJECT = "Company-A科技有限公司"
ALIAS = "A公司"
CUTOFF = "2026-08-27T03:02:29Z"

FIELD_REF_MISSING = json.dumps({
    "kind": "field_reference", "path": "case:missing.json#/not/exist",
    "status": "claimed"}, ensure_ascii=False)


def _subject_ref_doc(subject: str) -> bytes:
    return json.dumps({
        "subjects": [{"canonical_name_claimed": subject,
                      "status": "claimed"}]},
        ensure_ascii=False).encode("utf-8")


def _basis_with_sealed_subject(tmp_path, subject=SUBJECT) -> dict:
    """合法正例：主体引用解析到封存 identity 文档字段且值一致。"""
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    ref = blobs.put_bytes(_subject_ref_doc(subject))
    case.add_import_record("case_provenance", "session:identity-plan.json",
                           ref.sha256)
    case.close()
    return {
        "subject_legal_name": subject, "subject_aliases": [ALIAS],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
            "status": "claimed"}, ensure_ascii=False),
    }


# ---------- A：统一可核验依据 ----------

class TestR13A:
    def _probe(self, tmp_path, text: str, basis: dict, **source_over):
        from kth_hybrid.qualification import qualify_claim

        blobs = BlobStore(tmp_path / "blobs")
        data = text.encode("utf-8")
        ref = blobs.put_bytes(data)
        source = {
            "source_id": "S", "blob_sha256": ref.sha256,
            "byte_length": len(data), "capture_status": "raw_capture_validated",
            "source_family": "news-media", "retrieved_at": None,
            "published_at": None, "published_at_provenance": "",
        }
        source.update(source_over)
        excerpt = data[0:len(SUBJECT.encode("utf-8"))]
        claim = {
            "claim_id": "C", "source_id": "S", "locator_kind": "byte_range",
            "locator_start": 0, "locator_end": len(excerpt),
            "excerpt_sha256": sha256_hex(excerpt),
            "excerpt_text": excerpt.decode("utf-8", errors="replace"),
            "interpretation": "测试", "subject_scope": SUBJECT,
        }
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            document_basis = source.get("document_subject_basis")
            if isinstance(document_basis, dict) and \
                    document_basis.get("kind") == "case_field_reference":
                review = blobs.put_bytes(json.dumps({
                    "subject": source.get("document_subject"),
                    "document_sha256": source["blob_sha256"],
                }, ensure_ascii=False).encode("utf-8"))
                case.add_import_record(
                    "case_provenance", "session:document-subject-review.json",
                    review.sha256)
            return qualify_claim(claim, source, blobs, basis, case=case)
        finally:
            case.close()

    def test_a1_missing_subject_ref_and_unproved_published_at(self, tmp_path):
        # 主体引用指向不存在的封存文档 → 输入被拒（ValueError）；
        # published_at 自报过去时间无定位证明 → 时间不得ok
        basis = {"subject_legal_name": SUBJECT, "subject_aliases": [],
                 "evidence_cutoff": CUTOFF,
                 "subject_source_basis": FIELD_REF_MISSING}
        with pytest.raises(ValueError, match="主体来源依据不可核验"):
            self._probe(
                tmp_path,
                f"{SUBJECT} identifies demand. No date in this document.",
                basis, published_at="2026-07-01T00:00:00Z")
        # 同一来源，改用合法主体引用：自报published_at（无定位）必须时间fail
        basis2 = _basis_with_sealed_subject(tmp_path)
        outcome = self._probe(
            tmp_path, f"{SUBJECT} identifies demand. No date.", basis2,
            published_at="2026-07-01T00:00:00Z")
        assert outcome.time_judgment.verdict != "ok", \
            "自报published_at无定位证明不得过时间门"
        assert outcome.status != "qualified"

    def test_a1_positive_sealed_subject_ref(self, tmp_path):
        # 合法正例：封存主体引用+字段值一致；时间用结构化定位（正文含日期）
        basis = _basis_with_sealed_subject(tmp_path)
        text = f"{SUBJECT} identifies demand. Published 2026年7月1日."
        pos = text.find("2026年7月1日")
        ds = len(text[:pos].encode("utf-8"))
        de = ds + len("2026年7月1日".encode("utf-8"))
        outcome = self._probe(
            tmp_path, text, basis,
            time_evidence={"kind": "document_self_date", "date": "2026-07-01",
                           "date_locator": {"kind": "byte_range", "start": ds,
                                            "end": de}})
        assert outcome.status in ("qualified", "needs_review"), \
            "真实封存主体引用+真实定位日期的正例必须能通过"

    def test_a1_subject_ref_value_mismatch(self, tmp_path):
        basis = _basis_with_sealed_subject(tmp_path, subject="另一家公司")
        basis["subject_legal_name"] = SUBJECT  # 引用值与主体名不符
        with pytest.raises(ValueError, match="不一致|不可核验"):
            self._probe(
                tmp_path, f"{SUBJECT} content.", basis,
                time_evidence={"kind": "filename_derived_date",
                               "date": "2026-07-01", "basis": "候选"})

    def test_a2_fake_first_party_proof(self, tmp_path):
        # 文档归Company-B；自报Company-A第一方，依据为不可解析文字
        basis = _basis_with_sealed_subject(tmp_path)
        outcome = self._probe(
            tmp_path, f"{SUBJECT}内容。Company-B owns this document.", basis,
            source_family="owner_attachment", document_subject=SUBJECT,
            document_subject_basis="page 999 that does not exist",
            published_at=None,
            time_evidence={"kind": "filename_derived_date", "date": "2026-07-01",
                           "basis": "候选"})
        assert outcome.identity_judgment.verdict != "ok", \
            "任意非空描述不得证明第一方归属"
        assert outcome.status != "qualified"

    def test_a2_positive_structured_first_party_locator(self, tmp_path):
        # 合法正例：封存归属记录同时绑定主体字段和当前原件 hash。
        basis = _basis_with_sealed_subject(tmp_path)
        text = f"{SUBJECT}发布年度报告：关注AR眼镜市场。"
        pos = text.find(SUBJECT)
        s = len(text[:pos].encode("utf-8"))
        e = s + len(SUBJECT.encode("utf-8"))
        outcome = self._probe(
            tmp_path, text, basis,
            source_family="owner_attachment", capture_status="attachment",
            document_subject=SUBJECT,
            document_subject_basis={
                "kind": "case_field_reference",
                "path": "case:document-subject-review.json#/subject",
                "document_sha256_path":
                    "case:document-subject-review.json#/document_sha256",
            },
            retrieved_at=None,
            time_evidence={"kind": "filename_derived_date", "date": "2026-07-01",
                           "basis": "候选"})
        assert outcome.identity_judgment.verdict == "ok", \
            "封存归属记录绑定当前原件的第一方材料必须能通过"

    def test_a3_same_day_time_changed(self, tmp_path):
        # 正文发布2026-08-27T10:08:25Z；声明改为同日01:00:00Z——声明与定位内容不符
        basis = _basis_with_sealed_subject(tmp_path)
        text = f"{SUBJECT} identifies demand. Published 2026-08-27T10:08:25Z."
        outcome = self._probe(
            tmp_path, text, basis,
            time_evidence={"kind": "document_self_date",
                           "date": "2026-08-27T01:00:00Z",
                           "date_locator": {"kind": "byte_range", "start": 0,
                                            "end": len(text.encode())}})
        assert outcome.time_judgment.verdict == "fail"
        assert outcome.status != "qualified"

    def test_a3_date_only_same_day_as_cutoff_stays_unknown(self, tmp_path):
        # 只有日期且当天即截止（跨截止边界）→ 不确定，不凭空补时刻
        basis = _basis_with_sealed_subject(tmp_path)
        text = f"{SUBJECT} identifies demand. Published 2026-08-27."
        pos = text.find("2026-08-27")
        s = len(text[:pos].encode("utf-8"))
        e = s + len("2026-08-27".encode("utf-8"))
        outcome = self._probe(
            tmp_path, text, basis,
            time_evidence={"kind": "document_self_date", "date": "2026-08-27",
                           "date_locator": {"kind": "byte_range", "start": s,
                                            "end": e}})
        assert outcome.time_judgment.verdict in ("unknown", "fail")
        assert outcome.status != "qualified", "跨截止边界日期不得自动过门"

    def test_a4_registration_other_document(self, tmp_path):
        # 登记证明绑定另一份文档（document_sha256=64个f）证明当前原件 → 拒绝
        basis = _basis_with_sealed_subject(tmp_path)
        blobs = BlobStore(tmp_path / "blobs")
        record = blobs.put_bytes(json.dumps({
            "document_sha256": "f" * 64,
            "registered_at": "2026-07-01T00:00:00Z"}).encode())
        outcome = self._probe(
            tmp_path, f"{SUBJECT} identifies demand.", basis,
            time_evidence={"kind": "registered_at",
                           "date": "2026-07-01T00:00:00Z",
                           "registration_proof": {
                               "blob_sha256": record.sha256,
                               "field": "registered_at"}})
        assert outcome.time_judgment.verdict == "fail"
        assert outcome.status != "qualified"

    def test_a4_positive_registration_bound_to_current(self, tmp_path):
        basis = _basis_with_sealed_subject(tmp_path)
        text = f"{SUBJECT} identifies demand."
        blobs = BlobStore(tmp_path / "blobs")
        current_sha = sha256_hex(text.encode("utf-8"))
        record = blobs.put_bytes(json.dumps({
            "document_sha256": current_sha,
            "registered_at": "2026-07-01T00:00:00Z"}).encode())
        outcome = self._probe(
            tmp_path, text, basis,
            time_evidence={"kind": "registered_at",
                           "date": "2026-07-01T00:00:00Z",
                           "registration_proof": {
                               "blob_sha256": record.sha256,
                               "field": "registered_at",
                               "document_sha256": current_sha}})
        assert outcome.time_judgment.verdict == "ok"


# ---------- B：受控窄语义映射与有源N/A ----------

def _catalog():
    from kth_hybrid.catalog import build_catalog_from_wheel

    return build_catalog_from_wheel()


def _make_slice_case(root: Path, text: str, *, subject=SUBJECT,
                     with_time: bool = True) -> tuple[Path, dict]:
    """合成切片Case：v4合同下时间证明=正文内结构化定位（document_self_date）。"""
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    if with_time:
        text = text + " Published 2026年7月1日."
    data = text.encode("utf-8")
    ref = blobs.put_bytes(data)
    idb = _subject_ref_doc(subject)
    iref = blobs.put_bytes(idb)
    case.add_import_record("case_provenance", "session:identity-plan.json",
                           iref.sha256)
    case.add_source("S", ref.sha256, len(data), source_family="news-media",
                    capture_status="raw_capture_validated")
    if with_time:
        pos = text.find("2026年7月1日")
        ds = len(text[:pos].encode("utf-8"))
        de = ds + len("2026年7月1日".encode("utf-8"))
        case.append_time_evidence("S", {
            "kind": "document_self_date", "date": "2026-07-01",
            "date_locator": {"kind": "byte_range", "start": ds, "end": de},
            "basis": "合成正文日期"})
    case.close()
    return root, {
        "subject_legal_name": subject, "subject_aliases": [ALIAS],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
            "status": "claimed"}, ensure_ascii=False),
    }


def _run_slice(root, basis, text, *, claim_id="C", confirmation=None,
               criterion_id="CRL1-C1", na=None, flags=None, full_text=None):
    from kth_hybrid.runner import run_criterion_slice

    body = full_text if full_text is not None else text
    spec = {
        "claim_id": claim_id, "locator_kind": "byte_range", "start": 0,
        "end": len(body.encode("utf-8")),
        "interpretation": f"第三方载明{SUBJECT}识别的市场需求假设。",
        "subject_scope": SUBJECT,
        "criterion_mapping": {"quote": body, "start": 0,
                              "end": len(body.encode("utf-8"))},
    }
    if confirmation is not None:
        spec["semantic_confirmation"] = confirmation
    if na is not None:
        spec["na_proposal"] = na
    kwargs = {}
    if flags is not None:
        kwargs["case_flags"] = flags
    return run_criterion_slice(
        root, source_id="S", criterion_id=criterion_id, catalog=_catalog(),
        case_basis=basis, claim_spec=spec, **kwargs)


class TestR13B:
    def test_b1_negated_hypothesis_not_mapped_even_with_confirmation(
            self, tmp_path):
        # 明确否定假设的文字：即使调用方提交确认也不得成为支持关系
        text = f"{SUBJECT}尚未识别任何市场需求、问题或机会假设，当前只有办公室装修记录。"
        root, basis = _make_slice_case(tmp_path, text)
        full = text + " Published 2026年7月1日."
        result = _run_slice(
            root, basis, text, full_text=full,
            confirmation={"confirmed": True, "confirmator": "executor",
                          "review_basis": "复核记录（合成）"})
        assert result["mapping_status"] != "confirmed"
        assert result["product_status"] != "succeeded"
        assert result["product_status"] == "insufficient"

    def test_b1_positive_market_statement_with_confirmation(self, tmp_path):
        # 合法正例：真实市场假设陈述+留痕确认 → succeeded（非原生met）
        text = f"{SUBJECT}的产品设计产能将响应300万-400万副AR眼镜市场显示需求。"
        root, basis = _make_slice_case(tmp_path, text)
        full = text + " Published 2026年7月1日."
        result = _run_slice(
            root, basis, text, full_text=full,
            confirmation={"confirmed": True, "confirmator": "executor-r1_3",
                          "review_basis": "引文为来源陈述的市场需求假设"
                          "（范围：来源陈述该假设）"})
        assert result["mapping_status"] == "confirmed"
        assert result["product_status"] == "succeeded"
        assert result["native_disposition"] is None

    def test_b1_keyword_without_confirmation_stays_candidate(self, tmp_path):
        # 关键词命中但无确认记录 → 候选，不成功（不伪装已自动判定）
        text = f"{SUBJECT}的产品设计产能将响应AR眼镜市场显示需求。"
        root, basis = _make_slice_case(tmp_path, text)
        full = text + " Published 2026年7月1日."
        result = _run_slice(root, basis, text, full_text=full,
                            confirmation=None)
        assert result["mapping_status"] == "candidate"
        assert result["product_status"] != "succeeded"

    def test_b2_unsourced_na_rejected(self, tmp_path):
        # N/A依据/flag来源指向不存在的封存文件 → 不得合法N/A
        text = f"{SUBJECT} has a blue office wall."
        root, basis = _make_slice_case(tmp_path, text)
        frl = next(c["criterion_id"] for c in
                   _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
                   if c.get("na_policy") == "explicit_no_external_financing_only")
        full = text + " Published 2026年7月1日."
        result = _run_slice(
            root, basis, text, full_text=full, criterion_id=frl, claim_id="C-NA",
            na={"proposal": "not_applicable",
                "applicability_ref": {"kind": "field_reference",
                                      "path": "case:missing.json#/policy"},
                "flag_ref": {"kind": "field_reference",
                             "path": "case:missing.json#/financing"}},
            flags={"explicit_no_external_financing": {
                "value": True, "source": "missing.json#/financing"}})
        assert result["product_status"] != "succeeded"
        assert result["product_status"] in ("insufficient", "method_unsupported",
                                            "execution_failed")

    def test_b2_positive_sourced_na(self, tmp_path):
        # 合法正例：融资策略封存文档解析成功且声明不计划外部融资 → 合法N/A
        text = f"{SUBJECT} has a blue office wall."
        root, basis = _make_slice_case(tmp_path, text)
        blobs = BlobStore(root / "blobs")
        policy = json.dumps({
            "no_external_financing": {"declared": True,
                                      "statement": "公司决议不计划外部融资"}},
            ensure_ascii=False).encode("utf-8")
        pref = blobs.put_bytes(policy)
        case = CaseStore(root / "records.sqlite3")
        case.add_import_record("case_provenance", "session:financing-policy.json",
                               pref.sha256)
        case.close()
        frl = next(c["criterion_id"] for c in
                   _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
                   if c.get("na_policy") == "explicit_no_external_financing_only")
        full = text + " Published 2026年7月1日."
        result = _run_slice(
            root, basis, text, full_text=full, criterion_id=frl, claim_id="C-NA2",
            na={"proposal": "not_applicable",
                "applicability_ref": {
                    "kind": "field_reference",
                    "path": "case:financing-policy.json#/no_external_financing/statement"},
                "flag_ref": {
                    "kind": "field_reference",
                    "path": "case:financing-policy.json#/no_external_financing/declared"}})
        assert result["product_status"] == "succeeded", "有源N/A正例必须通过"
        assert result["native_disposition"] is None


# ---------- C：完整输入绑定与原子发布 ----------

GOV_TEXT = (f"{SUBJECT}的产品设计产能将响应300万-400万副AR眼镜市场显示需求。"
            "Published 2026年7月1日。")
_CONFIRM = {"confirmed": True, "confirmator": "executor-r1_3",
            "review_basis": "来源陈述该市场需求假设"}


def _positive_case(tmp_path, label="pos") -> tuple[Path, dict, str]:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix=f"r13-{label}-"))
    pos = GOV_TEXT.find("2026年7月1日")
    ds = len(GOV_TEXT[:pos].encode("utf-8"))
    de = ds + len("2026年7月1日".encode("utf-8"))
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    data = GOV_TEXT.encode("utf-8")
    ref = blobs.put_bytes(data)
    iref = blobs.put_bytes(_subject_ref_doc(SUBJECT))
    case.add_import_record("case_provenance", "session:identity-plan.json",
                           iref.sha256)
    case.add_source("S", ref.sha256, len(data), source_family="news-media",
                    capture_status="raw_capture_validated",
                    time_evidence=None)
    case.close()
    basis = {
        "subject_legal_name": SUBJECT, "subject_aliases": [ALIAS],
        "evidence_cutoff": CUTOFF,
        "subject_source_basis": json.dumps({
            "kind": "field_reference",
            "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
            "status": "claimed"}, ensure_ascii=False),
    }
    return root, basis, json.dumps(
        {"kind": "document_self_date", "date": "2026-07-01",
         "date_locator": {"kind": "byte_range", "start": ds, "end": de}},
        ensure_ascii=False)


def _seed_time_evidence(root, ev_json):
    case = CaseStore(root / "records.sqlite3")
    case.append_time_evidence("S", json.loads(ev_json))
    case.close()


class TestR13C:
    def test_c7_source_time_change_replay_rejected(self, tmp_path):
        # 同一来源发布时间从7月1日换到8月1日 → 同结果ID重放必须被拒
        from kth_hybrid.runner import run_criterion_slice

        root, basis, ev = _positive_case(tmp_path, "c7")
        _seed_time_evidence(root, ev)
        text = GOV_TEXT
        spec = {
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(text.encode("utf-8")),
            "interpretation": f"第三方载明{SUBJECT}市场需求假设。",
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": text, "start": 0,
                                  "end": len(text.encode("utf-8"))},
            "semantic_confirmation": _CONFIRM,
        }
        first = run_criterion_slice(
            root, source_id="S", criterion_id="CRL1-C1", catalog=_catalog(),
            case_basis=basis, claim_spec=spec)
        assert first["product_status"] == "succeeded"
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute(
                "UPDATE sources SET published_at='2026-08-01T00:00:00Z', "
                "published_at_provenance='被改' WHERE source_id='S'")
        case.close()
        with pytest.raises(RuntimeError, match="输入"):
            run_criterion_slice(
                root, source_id="S", criterion_id="CRL1-C1", catalog=_catalog(),
                case_basis=basis, claim_spec=spec)

    def test_c1_changed_na_proof_replay_rejected(self, tmp_path):
        from kth_hybrid.runner import run_criterion_slice

        text = f"{SUBJECT} blue wall."
        root, basis = _make_slice_case(tmp_path, text)
        blobs = BlobStore(root / "blobs")
        policy = json.dumps({"no_external_financing": {
            "declared": True, "statement": "决议不计划外部融资"}},
            ensure_ascii=False).encode()
        pref = blobs.put_bytes(policy)
        case = CaseStore(root / "records.sqlite3")
        case.add_import_record("case_provenance", "session:financing-policy.json",
                               pref.sha256)
        case.close()
        frl = next(c["criterion_id"] for c in
                   _catalog()["dimensions"]["FRL"]["registry"]["criteria"]
                   if c.get("na_policy") == "explicit_no_external_financing_only")
        full = text + " Published 2026年7月1日."
        spec = {
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(full.encode("utf-8")), "interpretation": "x",
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": full, "start": 0,
                                  "end": len(full.encode("utf-8"))},
            "na_proposal": {"proposal": "not_applicable",
                            "applicability_ref": {
                                "kind": "field_reference",
                                "path": "case:financing-policy.json#/no_external_financing/statement"},
                            "flag_ref": {
                                "kind": "field_reference",
                                "path": "case:financing-policy.json#/no_external_financing/declared"}},
        }
        first = run_criterion_slice(
            root, source_id="S", criterion_id=frl, catalog=_catalog(),
            case_basis=basis, claim_spec=spec)
        spec["na_proposal"]["applicability_ref"]["path"] = \
            "case:different-missing.json#/policy"
        with pytest.raises(RuntimeError, match="输入"):
            run_criterion_slice(
                root, source_id="S", criterion_id=frl, catalog=_catalog(),
                case_basis=basis, claim_spec=spec)

    def test_c2_crash_at_verification_no_succeeded_published(self, tmp_path):
        # 验证入口中断（发布前）：独立连接不得读到 succeeded
        from kth_hybrid import audit as audit_mod
        from kth_hybrid import runner as runner_mod
        from kth_hybrid.runner import run_criterion_slice

        root, basis, ev = _positive_case(tmp_path, "c2")
        _seed_time_evidence(root, ev)
        spec = {
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(GOV_TEXT.encode("utf-8")),
            "interpretation": f"第三方载明{SUBJECT}市场需求假设。",
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": GOV_TEXT, "start": 0,
                                  "end": len(GOV_TEXT.encode("utf-8"))},
            "semantic_confirmation": _CONFIRM,
        }
        original = runner_mod._verify_candidate
        def crash(*args, **kwargs):
            raise RuntimeError("审核注入：验证入口进程中断")
        runner_mod._verify_candidate = crash
        try:
            with pytest.raises(RuntimeError, match="验证入口"):
                run_criterion_slice(
                    root, source_id="S", criterion_id="CRL1-C1",
                    catalog=_catalog(), case_basis=basis, claim_spec=spec)
        finally:
            runner_mod._verify_candidate = original
        case = CaseStore(root / "records.sqlite3")
        try:
            results = case.fetch_all("criterion_results")
            assert all(r["product_status"] != "succeeded" for r in results), \
                "验证中断后不得存在已发布成功结果"
        finally:
            case.close()

    def test_c2_deterministic_trace_failure_not_published(self, tmp_path):
        # 确定性失败：验证返回broken → 落库为失败候选而非succeeded
        from kth_hybrid import runner as runner_mod
        from kth_hybrid.runner import run_criterion_slice

        root, basis, ev = _positive_case(tmp_path, "c2b")
        _seed_time_evidence(root, ev)
        spec = {
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(GOV_TEXT.encode("utf-8")),
            "interpretation": f"第三方载明{SUBJECT}市场需求假设。",
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": GOV_TEXT, "start": 0,
                                  "end": len(GOV_TEXT.encode("utf-8"))},
            "semantic_confirmation": _CONFIRM,
        }
        original = runner_mod._verify_candidate
        def broken(*args, **kwargs):
            class R:
                ok = False
                broken = ["审核注入：确定性追溯失败"]
            return R()
        runner_mod._verify_candidate = broken
        try:
            result = run_criterion_slice(
                root, source_id="S", criterion_id="CRL1-C1",
                catalog=_catalog(), case_basis=basis, claim_spec=spec)
        finally:
            runner_mod._verify_candidate = original
        assert result["trace_ok"] is False
        assert result["product_status"] != "succeeded"
        case = CaseStore(root / "records.sqlite3")
        try:
            row = case.fetch_one("criterion_results", "result_id",
                                 result["result_id"])
            assert row["product_status"] != "succeeded"
        finally:
            case.close()

    def _published_positive(self, tmp_path, label):
        from kth_hybrid.runner import run_criterion_slice

        root, basis, ev = _positive_case(tmp_path, label)
        _seed_time_evidence(root, ev)
        spec = {
            "claim_id": "C", "locator_kind": "byte_range", "start": 0,
            "end": len(GOV_TEXT.encode("utf-8")),
            "interpretation": f"第三方载明{SUBJECT}市场需求假设。",
            "subject_scope": SUBJECT,
            "criterion_mapping": {"quote": GOV_TEXT, "start": 0,
                                  "end": len(GOV_TEXT.encode("utf-8"))},
            "semantic_confirmation": _CONFIRM,
        }
        result = run_criterion_slice(
            root, source_id="S", criterion_id="CRL1-C1", catalog=_catalog(),
            case_basis=basis, claim_spec=spec)
        assert result["product_status"] == "succeeded"
        assert result["trace_ok"] is True
        return root

    def test_c3_source_date_tamper_breaks_trace(self, tmp_path):
        from kth_hybrid.audit import trace

        root = self._published_positive(tmp_path, "c3")
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute(
                "UPDATE sources SET published_at='2099-01-01T00:00:00Z' "
                "WHERE source_id='S'")
        blobs = BlobStore(root / "blobs")
        try:
            report = trace(case, blobs, "RESR::C::CRL1-C1", strict=False)
            assert not report.ok, "来源发布时间被篡改后trace必须失败"
        finally:
            case.close()

    def test_c4_qualification_tamper_breaks_trace(self, tmp_path):
        from kth_hybrid.audit import trace

        root = self._published_positive(tmp_path, "c4")
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute(
                "UPDATE qualifications SET status='rejected' WHERE claim_id='C'")
        blobs = BlobStore(root / "blobs")
        try:
            report = trace(case, blobs, "RESR::C::CRL1-C1", strict=False)
            assert not report.ok, "资格状态被改为rejected后trace必须失败"
        finally:
            case.close()

    def test_c5_mapping_deleted_breaks_trace(self, tmp_path):
        from kth_hybrid.audit import trace

        root = self._published_positive(tmp_path, "c5")
        case = CaseStore(root / "records.sqlite3")
        with case._conn:
            case._conn.execute("DELETE FROM claim_criterion_mappings")
        blobs = BlobStore(root / "blobs")
        try:
            report = trace(case, blobs, "RESR::C::CRL1-C1", strict=False)
            assert not report.ok, "映射记录被删除后trace必须失败"
        finally:
            case.close()

    def test_c6_basis_snapshot_tamper_breaks_trace(self, tmp_path):
        from kth_hybrid.audit import trace

        root = self._published_positive(tmp_path, "c6")
        case = CaseStore(root / "records.sqlite3")
        result = case.fetch_one("criterion_results", "result_id",
                                "RESR::C::CRL1-C1")
        version = result["case_basis_version"]
        snapshot = case.get_case_basis_version(version)
        snapshot.pop("version")
        snapshot["subject_aliases"] = ["无关主体"]
        snapshot["subject_source_basis"] = "已替换为无源声明"
        with case._conn:
            case._conn.execute(
                "UPDATE case_basis_versions SET snapshot_json=? WHERE version=?",
                (json.dumps(snapshot, ensure_ascii=False), version))
        blobs = BlobStore(root / "blobs")
        try:
            report = trace(case, blobs, "RESR::C::CRL1-C1", strict=False)
            assert not report.ok, "CaseBasis版本快照（含别名/来源依据）被改后trace必须失败"
        finally:
            case.close()

    def test_c_new_time_revision_does_not_break_old_result(self, tmp_path):
        # 版本更新不破坏旧判断：追加新时间证据修订，旧结果trace仍通过
        from kth_hybrid.audit import trace

        root = self._published_positive(tmp_path, "c-rev")
        case = CaseStore(root / "records.sqlite3")
        case.append_time_evidence("S", {
            "kind": "filename_derived_date", "date": "2026-07-02",
            "basis": "新增候选修订（不改变已判定输入）"})
        blobs = BlobStore(root / "blobs")
        try:
            report = trace(case, blobs, "RESR::C::CRL1-C1", strict=False)
            assert report.ok, "追加新时间证据版本不得破坏旧结果（旧版本不可变）"
        finally:
            case.close()


# ---------- 交付更正：幂等与运行历史区分 ----------

class TestR13Delivery:
    def test_case_provenance_import_idempotent(self, tmp_path):
        # 重跑建库不重复登记 case_provenance（业务对象幂等）
        blobs = BlobStore(tmp_path / "blobs")
        case = CaseStore(tmp_path / "records.sqlite3")
        ref = blobs.put_bytes(_subject_ref_doc(SUBJECT))
        for _ in range(2):
            existing = case.find_import("case_provenance",
                                        "session:identity-plan.json")
            if existing is None:
                case.add_import_record("case_provenance",
                                       "session:identity-plan.json", ref.sha256)
        rows = [r for r in case.fetch_all("import_records")
                if r["kind"] == "case_provenance"]
        case.close()
        assert len(rows) == 1

    def test_case_basis_changed_only_appends_new_version(self, tmp_path):
        # 相同内容重登不产生新版本（不是把重跑包装为新事件）；内容变化才追加
        basis = dict(subject_legal_name=SUBJECT, subject_aliases=[ALIAS],
                     evidence_cutoff=CUTOFF,
                     subject_source_basis=json.dumps({
                         "kind": "field_reference",
                         "path": "case:identity-plan.json#/x", "status": "claimed"},
                         ensure_ascii=False))
        case = CaseStore(tmp_path / "records.sqlite3")
        try:
            case.set_case_basis_if_changed(**basis)
            case.set_case_basis_if_changed(**basis)  # 重跑：内容相同
            assert len(case.get_case_basis_versions()) == 1
            case.set_case_basis_if_changed(**dict(
                basis, subject_aliases=["另一别名"]))  # 真实变化
            assert len(case.get_case_basis_versions()) == 2
        finally:
            case.close()
