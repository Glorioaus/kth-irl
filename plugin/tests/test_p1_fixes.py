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


# ---- fix2：版本衔接（迁移漏算 / 孤儿行 / 输入上界政策 / 标记矩阵） ----

def _v11_ledger_seed(workflow, digest, task_key, actual=80):
    """直接构造v11形状账本行（无budget_scope），交由当前迁移补列。"""
    import sqlite3
    with sqlite3.connect(workflow.store.db_path if hasattr(
            workflow.store, "db_path") else workflow.case_dir
            / "records.sqlite3") as conn:
        conn.execute("DROP INDEX IF EXISTS idx_provider_usage_ledger_scope")
        conn.execute("ALTER TABLE provider_usage_ledger DROP COLUMN budget_scope")
        conn.execute("UPDATE meta SET value='kth-hybrid.store.v11' "
                     "WHERE key='schema_version'")
    with workflow.store._conn:
        workflow.store._conn.execute(
            "INSERT INTO provider_usage_ledger(task_key,authorization_digest,"
            "purpose,provider_id,status,input_reserved,output_reserved,"
            "input_actual,output_actual,usage_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_key, digest, "candidate_proposal", "glm", "succeeded",
             actual, actual, actual, actual, "known"))


def test_v11_consumption_after_migration_blocks_and_allows(tmp_path):
    """同授权摘要的v11消费迁移后：80拒绝、20可用；合法迁移不双算。"""
    import sqlite3
    from kth_hybrid.provider_gateway import ProviderDispatcher, authorization_digest
    auth = {"schema_version": "kth-hybrid.provider-authorization.v1",
            "material": {"original_sha256": "f" * 64},
            "purposes": ["candidate_proposal"]}
    digest = authorization_digest(auth)
    root = tmp_path / "legacy"
    workflow = LocalWorkflow(root)
    _v11_ledger_seed(workflow, digest, "provider-dispatch:OLD", actual=80)
    workflow.close()
    # 以当前代码重新打开触发迁移（budget_scope=authorization_digest）
    workflow = LocalWorkflow(root)
    try:
        dispatcher = object.__new__(ProviderDispatcher)
        dispatcher.workflow = workflow
        dispatcher.authorization = auth
        assert dispatcher._orphan_ledger_error() is None
        scope = dispatcher._budget_scope()
        lineage = sorted(dispatcher._budget_scopes() - {scope})
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            workflow.store.reserve_provider_usage(
                budget_scope=scope, lineage_scopes=lineage,
                task_key="provider-dispatch:NEW80",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=80, output_reserved=80, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
        workflow.store.reserve_provider_usage(
            budget_scope=scope, lineage_scopes=lineage,
            task_key="provider-dispatch:NEW20",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=20, output_reserved=20, historic_usage=[],
            request_cap=6, input_cap=100, output_cap=100)
        # 不双算：80+20=100 恰好占满，再申请1即拒。
        with pytest.raises(BudgetRejected, match="超限"):
            workflow.store.reserve_provider_usage(
                budget_scope=scope, lineage_scopes=lineage,
                task_key="provider-dispatch:NEW1",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=1, output_reserved=1, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
    finally:
        workflow.close()


def test_foreign_digest_rows_are_orphans_and_block(tmp_path):
    """他授权摘要的账本行：无lineage声明即孤儿阻断；声明后计入不双算。"""
    from kth_hybrid.provider_gateway import ProviderDispatcher, authorization_digest
    auth = {"schema_version": "kth-hybrid.provider-authorization.v1",
            "material": {"original_sha256": "f" * 64},
            "purposes": ["candidate_proposal"]}
    other = {"schema_version": "kth-hybrid.provider-authorization.v1",
             "material": {"original_sha256": "e" * 64},
             "purposes": ["candidate_proposal"]}
    foreign_digest = authorization_digest(other)
    root = tmp_path / "orphan"
    workflow = LocalWorkflow(root)
    _v11_ledger_seed(workflow, foreign_digest, "provider-dispatch:FOREIGN",
                     actual=80)
    workflow.close()
    workflow = LocalWorkflow(root)
    try:
        dispatcher = object.__new__(ProviderDispatcher)
        dispatcher.workflow = workflow
        dispatcher.authorization = auth
        error = dispatcher._orphan_ledger_error()
        assert error and "归属不明" in error and "FOREIGN" in error

        dispatcher.authorization = {**auth,
                                    "budget_scope_lineage": [foreign_digest]}
        assert dispatcher._orphan_ledger_error() is None
        scope = dispatcher._budget_scope()
        lineage = sorted(dispatcher._budget_scopes() - {scope})
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            workflow.store.reserve_provider_usage(
                budget_scope=scope, lineage_scopes=lineage,
                task_key="provider-dispatch:NEW80",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=80, output_reserved=80, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
    finally:
        workflow.close()


def test_authorization_revision_same_derived_scope_accumulates(tmp_path):
    """同预算授权修订（摘要变化、材料/用途不变）：v12行按稳定派生范围
    自动累计，不重置余额。"""
    from kth_hybrid.provider_gateway import (ProviderDispatcher,
                                              authorization_digest)
    base = {"schema_version": "kth-hybrid.provider-authorization.v1",
            "material": {"original_sha256": "f" * 64},
            "purposes": ["candidate_proposal"]}
    revised = {**base, "amendment": {"note": "endpoint修订"}}
    root = tmp_path / "revise"
    workflow = LocalWorkflow(root)
    try:
        d1 = object.__new__(ProviderDispatcher)
        d1.workflow = workflow
        d1.authorization = base
        scope = d1._budget_scope()
        workflow.store.reserve_provider_usage(
            budget_scope=scope, task_key="provider-dispatch:R1",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=60, output_reserved=60, historic_usage=[],
            request_cap=6, input_cap=100, output_cap=100,
            authorization_digest=authorization_digest(base))
        d2 = object.__new__(ProviderDispatcher)
        d2.workflow = workflow
        d2.authorization = revised
        assert d2._budget_scope() == scope
        with pytest.raises(BudgetRejected, match="累计输入token超限"):
            workflow.store.reserve_provider_usage(
                budget_scope=d2._budget_scope(),
                lineage_scopes=sorted(d2._budget_scopes()
                                      - {d2._budget_scope()}),
                task_key="provider-dispatch:R2",
                purpose="candidate_proposal", provider_id="glm",
                input_reserved=60, output_reserved=60, historic_usage=[],
                request_cap=6, input_cap=100, output_cap=100)
    finally:
        workflow.close()


def test_marker_schema_matrix_no_downgrade(registered_case):
    """标记v1+schema缺失拒；record schema未知拒；真旧记录走旧路径。"""
    from kth_hybrid.qualification import classify_time_registration
    item = registered_case
    blobs = item["workflow"].blobs
    plain = item["workflow"].blobs.put_bytes(
        b'{"document_sha256": "' + item["blob_sha256"].encode() + b'"}')
    legacy = {"kind": "registered_at", "date": "2026-08-27T04:13:11Z",
              "basis": "真旧记录",
              "registration_proof": {"blob_sha256": plain.sha256}}
    mode, error = classify_time_registration(legacy, blobs)
    assert (mode, error) == ("legacy", None)
    marked = dict(legacy,
                  registration_contract="kth-hybrid.time-registration.v1")
    mode2, error2 = classify_time_registration(marked, blobs)
    assert mode2 == "reject" and "schema缺失或不一致" in error2
    weird = item["workflow"].blobs.put_bytes(
        b'{"schema_version": "kth-hybrid.time-registration.v999", '
        b'"document_sha256": "x"}')
    unknown = dict(legacy, registration_proof={"blob_sha256": weird.sha256})
    mode3, error3 = classify_time_registration(unknown, blobs)
    assert mode3 == "reject" and "v999" in error3
