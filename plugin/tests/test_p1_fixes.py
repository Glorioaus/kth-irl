"""P1-A/P1-B 限定修复的独立同类反例（Astra 2026-09-16 复验矩阵）。

全部离线：store 双连接并发/旧余额反例、派发者零transport、跨附件记录
拼接、上游缺失/漂移后的读时失效。不触碰真实Case与真实Provider。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kth_hybrid.controlled_registration import (
    RegistrationRejected,
    declare_document_subject,
    register_time_evidence,
)
from kth_hybrid.qualification import (
    build_qualification_input_view,
    verify_time_registration_contract,
)
from kth_hybrid.store import BudgetRejected, BlobStore, CaseStore
from kth_hybrid.workflow import LocalWorkflow

SUBJECT_RAW = "微玖（苏州） 光电科技有限公司"
SUBJECT = "微玖（苏州）光电科技有限公司"
OLD_CUTOFF = "2026-08-27T03:02:29Z"
NEW_CUTOFF = "2026-09-16T12:19:20+08:00"


# ---- P1-A：事务内实况计算（Astra反例一/二 + 固定正反对照） ----

def test_two_connections_cannot_share_stale_balance(tmp_path):
    """上限100：两个连接先后各预留60，第二笔必须拒绝。"""
    db = tmp_path / "records.sqlite3"
    CaseStore(db).close()
    conn1 = CaseStore(db)
    conn2 = CaseStore(db)
    try:
        conn1.reserve_provider_usage(
            budget_scope="scope-x", task_key="provider-dispatch:A",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=60, output_reserved=60, historic_usage=[],
            request_cap=6, input_cap=100, output_cap=100)
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            conn2.reserve_provider_usage(
                budget_scope="scope-x", task_key="provider-dispatch:B",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=60, output_reserved=60, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
    finally:
        conn1.close()
        conn2.close()


def test_settled_actual_blocks_stale_known_caller(tmp_path):
    """第一笔结算actual=80后，持旧known=0的第二笔80仍必须拒绝。"""
    db = tmp_path / "records.sqlite3"
    CaseStore(db).close()
    conn1 = CaseStore(db)
    conn2 = CaseStore(db)
    try:
        conn1.reserve_provider_usage(
            budget_scope="scope-y", task_key="provider-dispatch:A",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=80, output_reserved=80, historic_usage=[],
            request_cap=6, input_cap=100, output_cap=100)
        conn1.settle_provider_usage(
            "provider-dispatch:A", status="succeeded", input_actual=80,
            output_actual=80, usage_status="known")
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            conn2.reserve_provider_usage(
                budget_scope="scope-y", task_key="provider-dispatch:B",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=80, output_reserved=80, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
    finally:
        conn1.close()
        conn2.close()


def test_legal_pairing_succeeds_and_settlement_is_immutable(tmp_path):
    """合法40+40成功；结算后追加大额拒绝；重复结算被拒（不重写历史）。"""
    store = CaseStore(tmp_path / "records.sqlite3")
    try:
        for index, amount in ((1, 40), (2, 40)):
            store.reserve_provider_usage(
                budget_scope="scope-z", task_key=f"provider-dispatch:{index}",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=amount, output_reserved=amount,
                historic_usage=[], request_cap=6, input_cap=100,
                output_cap=100)
        store.settle_provider_usage(
            "provider-dispatch:1", status="succeeded", input_actual=40,
            output_actual=40, usage_status="known")
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            store.reserve_provider_usage(
                budget_scope="scope-z", task_key="provider-dispatch:3",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=30, output_reserved=30, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
        with pytest.raises(BudgetRejected, match="结算条件更新失败"):
            store.settle_provider_usage(
                "provider-dispatch:1", status="failed", usage_status="unknown")
    finally:
        store.close()


def test_historic_unknown_without_per_item_bound_is_rejected(tmp_path):
    store = CaseStore(tmp_path / "records.sqlite3")
    try:
        with pytest.raises(BudgetRejected, match="无有依据的逐项保守上界"):
            store.reserve_provider_usage(
                budget_scope="scope-h", task_key="provider-dispatch:N",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=10, output_reserved=10,
                historic_usage=[{"task_key": "legacy-1",
                                 "usage_status": "unknown"}],
                request_cap=6, input_cap=100, output_cap=100)
        store.reserve_provider_usage(
            budget_scope="scope-h", task_key="provider-dispatch:K",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=10, output_reserved=10,
            historic_usage=[{"task_key": "legacy-1", "usage_status": "unknown",
                             "input_bound": 50, "output_bound": 50,
                             "evidence_source": "Owner批准的请求上界折算"}],
            request_cap=6, input_cap=100, output_cap=100)
    finally:
        store.close()


# ---- P1-B：同记录关系与读时上游实读 ----

@pytest.fixture()
def registered_case(tmp_path):
    docx = pytest.importorskip("docx")
    value = LocalWorkflow(tmp_path / "case")
    identity = {"subject": SUBJECT}
    identity_blob = value.blobs.put_bytes(
        json.dumps(identity, ensure_ascii=False).encode("utf-8"))
    value.store.add_import_record(
        "case_provenance", "session:identity.json", identity_blob.sha256)
    value.initialize_case(
        subject_legal_name=SUBJECT, subject_aliases=["微玖光电"],
        evidence_cutoff=OLD_CUTOFF,
        subject_source_basis=json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed"}, ensure_ascii=False))
    path = tmp_path / "cover.docx"
    document = docx.Document()
    document.add_paragraph(f"商业计划书 {SUBJECT_RAW} 先进半导体显示")
    document.save(path)
    imported = value.import_attachments([path])[0]
    projection = next(item for item in value.project_sources(
        [imported["source_id"]]) if item["status"] == "projected")
    upstream = {"attachments": [
        {"sha256": imported["blob_sha256"],
         "copied_at": "2026-08-27T04:13:11Z"},
        {"sha256": "0" * 64, "copied_at": "2026-08-01T00:00:00Z"},
    ]}
    upstream_blob = value.blobs.put_bytes(
        json.dumps(upstream, ensure_ascii=False).encode("utf-8"))
    frozen = value.blobs.read_bytes(imported["blob_sha256"])
    value.store.add_claim(
        "CLAIM::p1b", imported["source_id"], locator_kind="byte_range",
        excerpt_start=0, excerpt_end=len(frozen),
        excerpt_sha256=__import__("hashlib").sha256(frozen).hexdigest(),
        excerpt_text=projection["text"], interpretation="P1-B用",
        subject_scope=SUBJECT)
    yield {
        "workflow": value, "source_id": imported["source_id"],
        "projection": projection, "upstream_blob": upstream_blob.sha256,
        "blob_sha256": imported["blob_sha256"], "tmp_path": tmp_path,
    }
    value.close()


def _declaration(item, **overrides) -> dict:
    declaration = {
        "schema_version": "kth-hybrid.document-subject-declaration.v1",
        "document_sha256": item["blob_sha256"],
        "document_subject": SUBJECT,
        "document_subject_raw": SUBJECT_RAW,
        "raw_locator": copy.deepcopy(item["projection"]["locator"]),
        "raw_text_sha256": item["projection"]["text_sha256"],
        "normalization": {
            "kind": "single_space_after_fullwidth_paren", "removed": " ",
            "basis": "全角右括号后单个排版空格不构成名称差异"},
        "observed_by": "执行层机械读取（测试）",
        "observed_at": "2026-09-16T14:10:00+08:00",
        "proof_scope": "文档自识字样与Case全称的规范化关联；不证明归属与真实性",
    }
    declaration.update(overrides)
    return declaration


def _qualify(item, cutoff):
    workflow = item["workflow"]
    source = workflow.store.fetch_one(
        "sources", "source_id", item["source_id"])
    claim = workflow.store.fetch_one("claims", "claim_id", "CLAIM::p1b")
    basis = dict(workflow.store.get_case_basis())
    basis["evidence_cutoff"] = cutoff
    _view, outcome, errors = build_qualification_input_view(
        claim, source, None, basis, workflow.store, workflow.blobs,
        same_body_sources=2, review_attempt="p1b-test")
    return outcome, errors


def test_cross_attachment_record_splicing_is_rejected(registered_case):
    item = registered_case
    with pytest.raises(RegistrationRejected,
                       match="不属于同一上游附件记录"):
        register_time_evidence(
            item["workflow"], source_id=item["source_id"],
            upstream_blob_sha256=item["upstream_blob"],
            upstream_field="attachments[1].copied_at",
            upstream_document_field="attachments[0].sha256",
            event_note="跨记录拼接反例")


def test_upstream_deletion_invalidates_time_at_read(registered_case):
    item = registered_case
    declare_document_subject(
        item["workflow"], source_id=item["source_id"],
        declaration=_declaration(item))
    register_time_evidence(
        item["workflow"], source_id=item["source_id"],
        upstream_blob_sha256=item["upstream_blob"],
        upstream_field="attachments[0].copied_at",
        upstream_document_field="attachments[0].sha256",
        event_note="上游缺失反例")
    outcome, errors = _qualify(item, NEW_CUTOFF)
    assert outcome.time_judgment.verdict == "ok"
    # 删除上游封存blob文件：读取时必须实读上游并失效。
    upstream_file = (Path(item["workflow"].blobs.root) / "objects"
                     / item["upstream_blob"][:2] / item["upstream_blob"])
    upstream_file.unlink()
    outcome2, _errors2 = _qualify(item, NEW_CUTOFF)
    assert outcome2.time_judgment.verdict == "fail"
    assert "读时复验失败" in outcome2.time_judgment.basis


def test_upstream_value_drift_is_detected_by_verifier(registered_case):
    item = registered_case
    workflow = item["workflow"]
    declare_document_subject(
        workflow, source_id=item["source_id"], declaration=_declaration(item))
    result = register_time_evidence(
        workflow, source_id=item["source_id"],
        upstream_blob_sha256=item["upstream_blob"],
        upstream_field="attachments[0].copied_at",
        upstream_document_field="attachments[0].sha256",
        event_note="漂移反例")
    source = workflow.store.fetch_one(
        "sources", "source_id", item["source_id"])
    latest = workflow.store.latest_time_evidence(item["source_id"])
    assert verify_time_registration_contract(
        latest, source, workflow.blobs) is None
    # 篡改派生记录中的时间声明（自洽但与上游矛盾）：读时复验必须拒绝。
    raw = workflow.blobs.read_bytes(
        result["record_blob_sha256"]).decode("utf-8")
    doctored = json.loads(raw)
    doctored["recorded_value"] = "2026-01-01T00:00:00Z"
    doctored_blob = workflow.blobs.put_bytes(
        json.dumps(doctored, ensure_ascii=False).encode("utf-8"))
    doctored_evidence = copy.deepcopy(latest)
    doctored_evidence["registration_proof"]["blob_sha256"] = \
        doctored_blob.sha256
    error = verify_time_registration_contract(
        doctored_evidence, source, workflow.blobs)
    assert error == "登记时间与上游记录当前值不一致"


def test_old_cutoff_still_gains_nothing(registered_case):
    item = registered_case
    declare_document_subject(
        item["workflow"], source_id=item["source_id"],
        declaration=_declaration(item))
    register_time_evidence(
        item["workflow"], source_id=item["source_id"],
        upstream_blob_sha256=item["upstream_blob"],
        upstream_field="attachments[0].copied_at",
        upstream_document_field="attachments[0].sha256",
        event_note="旧截止对照")
    outcome, _errors = _qualify(item, OLD_CUTOFF)
    assert "晚于截止" in outcome.time_judgment.basis
    assert outcome.status == "rejected"
