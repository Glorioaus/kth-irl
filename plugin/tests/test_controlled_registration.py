"""受控登记与当前评估口径：配对合同测试（离线、零网络、零凭证）。

覆盖：主体/时间登记的机械上游再解析、declared_by 限制、hash 错绑、
规范化越界、单次登记；新旧截止的资格预检对照（旧截止不获益、新截止
仅在登记证据可核验时获益）；旧数据不变。
"""

from __future__ import annotations

import copy
import json

import pytest

from kth_hybrid.controlled_registration import (
    RegistrationRejected,
    declare_document_subject,
    register_time_evidence,
)
from kth_hybrid.qualification import build_qualification_input_view
from kth_hybrid.workflow import LocalWorkflow

SUBJECT_RAW = "微玖（苏州） 光电科技有限公司"
SUBJECT = "微玖（苏州）光电科技有限公司"
OLD_CUTOFF = "2026-08-27T03:02:29Z"
NEW_CUTOFF = "2026-09-16T12:19:20+08:00"
COPIED_AT = "2026-08-27T04:13:11Z"


@pytest.fixture()
def case(tmp_path):
    docx = pytest.importorskip("docx")
    value = LocalWorkflow(tmp_path / "case")
    identity = {"subject": SUBJECT}
    identity_blob = value.blobs.put_bytes(
        json.dumps(identity, ensure_ascii=False).encode("utf-8"))
    value.store.add_import_record(
        "case_provenance", "session:identity.json", identity_blob.sha256)
    value.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=["微玖光电"],
        evidence_cutoff=OLD_CUTOFF,
        subject_source_basis=json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    )
    path = tmp_path / "cover.docx"
    document = docx.Document()
    document.add_paragraph(
        f"商业计划书 {SUBJECT_RAW} 基于先进半导体显示技术")
    document.save(path)
    imported = value.import_attachments([path])[0]
    projection = next(item for item in value.project_sources(
        [imported["source_id"]]) if item["status"] == "projected")
    upstream = {
        "attachments": [
            {"sha256": imported["blob_sha256"], "copied_at": COPIED_AT},
        ],
    }
    upstream_blob = value.blobs.put_bytes(
        json.dumps(upstream, ensure_ascii=False).encode("utf-8"))
    frozen = value.blobs.read_bytes(imported["blob_sha256"])
    value.store.add_claim(
        "CLAIM::test-precheck", imported["source_id"],
        locator_kind="byte_range", excerpt_start=0, excerpt_end=len(frozen),
        excerpt_sha256=__import__("hashlib").sha256(frozen).hexdigest(),
        excerpt_text=projection["text"],
        interpretation="预检用候选", subject_scope=SUBJECT)
    yield {
        "workflow": value,
        "source_id": imported["source_id"],
        "projection": projection,
        "upstream_blob": upstream_blob.sha256,
        "blob_sha256": imported["blob_sha256"],
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
            "kind": "single_space_after_fullwidth_paren",
            "removed": " ",
            "basis": "全角右括号后的单个排版空格不构成名称差异",
        },
        "declared_by": "executor-controlled-observation",
        "observed_by": "执行层对封存投影的机械读取（测试）",
        "observed_at": "2026-09-16T12:30:00+08:00",
        "proof_scope": "文档自识主体字样与Case全称的规范化关联；"
                       "不证明文档由该公司制作/授权/提交，不证明技术陈述属实",
    }
    declaration.update(overrides)
    return declaration


def _precheck(item, cutoff: str):
    workflow = item["workflow"]
    source = workflow.store.fetch_one(
        "sources", "source_id", item["source_id"])
    claim = workflow.store.fetch_one(
        "claims", "claim_id", "CLAIM::test-precheck")
    basis = dict(workflow.store.get_case_basis())
    basis["evidence_cutoff"] = cutoff
    _view, outcome, errors = build_qualification_input_view(
        claim, source, None, basis, workflow.store, workflow.blobs,
        same_body_sources=1, review_attempt="precheck-test")
    assert not errors
    return outcome


def test_registration_and_new_basis_qualification_precheck(case):
    item = case
    subject_result = declare_document_subject(
        item["workflow"], source_id=item["source_id"],
        declaration=_declaration(item))
    assert subject_result["document_subject"] == SUBJECT
    time_result = register_time_evidence(
        item["workflow"], source_id=item["source_id"],
        upstream_blob_sha256=item["upstream_blob"],
        upstream_field="attachments[0].copied_at",
        upstream_document_field="attachments[0].sha256",
        event_note="测试收存事件")
    assert time_result["registered_date"] == COPIED_AT

    old = _precheck(item, OLD_CUTOFF)
    assert old.status == "rejected"
    assert "晚于截止" in old.time_judgment.basis
    assert old.identity_judgment.verdict == "ok"
    assert "company_self_statement" in old.allowed_uses

    new = _precheck(item, NEW_CUTOFF)
    assert new.status == "qualified"
    assert "company_self_statement" in new.allowed_uses
    assert new.time_judgment.verdict == "ok"
    assert any("发布" in item or "成文" in item for item in new.cannot_prove)


def test_declared_by_owner_is_rejected(case):
    item = case
    with pytest.raises(RegistrationRejected, match="不是Owner对文档归属的事实声明"):
        declare_document_subject(
            item["workflow"], source_id=item["source_id"],
            declaration=_declaration(item, declared_by="Owner"))


def test_document_hash_misbinding_is_rejected(case):
    item = case
    with pytest.raises(RegistrationRejected, match="原件hash与当前来源不一致"):
        declare_document_subject(
            item["workflow"], source_id=item["source_id"],
            declaration=_declaration(item, document_sha256="0" * 64))


def test_normalization_overreach_is_rejected(case):
    item = case
    with pytest.raises(RegistrationRejected, match="规范化依据不支持"):
        declare_document_subject(
            item["workflow"], source_id=item["source_id"],
            declaration=_declaration(item, normalization={
                "kind": "strip_all_whitespace", "removed": "*",
                "basis": "越界"}))
    with pytest.raises(RegistrationRejected, match="规范化无依据"):
        declare_document_subject(
            item["workflow"], source_id=item["source_id"],
            declaration=_declaration(
                item, document_subject_raw="微玖（苏州）光电科技有限公司",
                normalization={"kind": "single_space_after_fullwidth_paren",
                               "removed": " ", "basis": "原文已无空格"}))


def test_subject_registration_is_single_write(case):
    item = case
    declare_document_subject(
        item["workflow"], source_id=item["source_id"],
        declaration=_declaration(item))
    with pytest.raises(RegistrationRejected, match="单次登记，不接受改写"):
        declare_document_subject(
            item["workflow"], source_id=item["source_id"],
            declaration=_declaration(item))


def test_upstream_document_mismatch_is_rejected(case):
    item = case
    bad = {"attachments": [{"sha256": "0" * 64, "copied_at": COPIED_AT}]}
    blob = item["workflow"].blobs.put_bytes(
        json.dumps(bad, ensure_ascii=False).encode("utf-8"))
    with pytest.raises(RegistrationRejected, match="原件与当前来源不一致"):
        register_time_evidence(
            item["workflow"], source_id=item["source_id"],
            upstream_blob_sha256=blob.sha256,
            upstream_field="attachments[0].copied_at",
            upstream_document_field="attachments[0].sha256",
            event_note="错绑测试")


def test_upstream_field_parse_failure_is_rejected(case):
    item = case
    with pytest.raises(RegistrationRejected, match="不可解析"):
        register_time_evidence(
            item["workflow"], source_id=item["source_id"],
            upstream_blob_sha256=item["upstream_blob"],
            upstream_field="attachments[9].copied_at",
            upstream_document_field="attachments[0].sha256",
            event_note="路径测试")


def test_old_records_unchanged_after_registration(case):
    item = case
    workflow = item["workflow"]
    qualifications_before = [
        dict(row) for row in workflow.store.fetch_all("qualifications")]
    projections_before = [
        dict(row) for row in workflow.store._conn.execute("SELECT * FROM text_projections").fetchall()]
    declare_document_subject(
        workflow, source_id=item["source_id"], declaration=_declaration(item))
    register_time_evidence(
        workflow, source_id=item["source_id"],
        upstream_blob_sha256=item["upstream_blob"],
        upstream_field="attachments[0].copied_at",
        upstream_document_field="attachments[0].sha256",
        event_note="不变性测试")
    assert [dict(row) for row in workflow.store.fetch_all("qualifications")] \
        == qualifications_before
    assert [dict(row) for row in workflow.store._conn.execute("SELECT * FROM text_projections").fetchall()] \
        == projections_before
    basis = workflow.store.get_case_basis()
    assert basis["evidence_cutoff"] == OLD_CUTOFF
