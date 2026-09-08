"""R1.2 运行编排 v3：可信判据解析、完整输入冻结、引文映射、追溯门。

修复（复验 P1-B/P1-C）：
- 判据由**可信 catalog**（批准 wheel 隔离探针提取）按 ID 解析出完整身份；
  runner 不接受调用方自报批准集合，也不接受同 ID 篡改 dimension/level/text。
- 输入摘要 v3 覆盖真正决定求值的完整合同：规范判据全文、catalog/规则版本、
  批准集合哈希、CaseBasis 快照（版本化不可变）、适用性 flags、资格/来源策略
  版本、主张内容（定位/摘录/解释/主体范围）与映射引文。
- 引文映射（kernels.interpret_claim_for_criterion）：引文必须逐字位于封存
  摘录区间＋保守语义过滤命中，全部留痕（claim_criterion_mappings 表）。
- 追溯门：评估为 succeeded 但 trace 失败时，落库为 execution_failed 失败
  候选（不发布成功、不转业务 NO）。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .audit import trace
from .contracts import claim_content_digest, run_input_digest_v3, sha256_hex
from .dimensions import CriterionEvaluation, evaluate_criterion
from .intake import extract_docx_paragraphs, extract_pdf_pages
from .journal import Journal
from .kernels import RULE_VERSION, interpret_claim_for_criterion
from .qualification import (
    GapOutcome,
    QualificationOutcome,
    qualify_claim,
    RULE_VERSION as QUALIFICATION_VERSION,
)
from .store import BlobStore, CaseStore

WEIJIU_CASE_BASIS = {
    "subject_legal_name": "微玖（苏州）光电科技有限公司",
    "subject_aliases": ["微玖", "微玖光电"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "session:research-foundation/identity-plan.json"
                "#/subjects/0/canonical_name_claimed",
        "status": "claimed",
        "note": "封存session身份计划声称的规范主体名（claimed，未工商核验）；"
                "与封存政府页面正文载明的法人名一致（R1.2有限核查）",
    }, ensure_ascii=False),
}


class SimulatedCrash(RuntimeError):
    """注入的模拟崩溃。"""


def qualification_id(claim_id: str) -> str:
    return f"QUALR::{claim_id}"


def result_id(claim_id: str, criterion_id: str) -> str:
    return f"RESR::{claim_id}::{criterion_id}"


def resolve_criterion(criterion_id: str, catalog: dict) -> dict:
    """从可信 catalog 解析判据完整身份（dimension/level/text/na_policy 原样）。"""
    dimension = criterion_id[:3] if len(criterion_id) > 3 and "-" in criterion_id else None
    for dim_name, info in catalog["dimensions"].items():
        for criterion in info["registry"].get("criteria", []):
            if criterion["criterion_id"] == criterion_id:
                resolved = {"dimension": dim_name, **criterion}
                resolved.setdefault("levels_supported",
                                    info["levels_supported"])
                return resolved
    raise ValueError(
        f"判据 {criterion_id} 不在可信 catalog（批准 wheel）中：不允许评估未登记"
        f"或调用方自报的判据"
    )


def approved_ids_from_catalog(catalog: dict) -> set[str]:
    return {c["criterion_id"] for info in catalog["dimensions"].values()
            for c in info["registry"].get("criteria", [])}


class CountingSimulatedProvider:
    """计数型本地模拟 Provider（simulated=true，非真实模型角色链）。"""

    simulated = True

    def __init__(self, journal: Journal, blobs: BlobStore | None = None,
                 worker_id: str = "simulated-provider"):
        if blobs is None:
            raise ValueError("v2 要求提供 BlobStore：响应必须先持久化再提交")
        self.journal = journal
        self.blobs = blobs
        self.worker_id = worker_id
        self.dispatch_count = 0

    def execute(self, task_key: str, input_id: str,
                response_factory: Callable[[], bytes], *,
                crash_after_dispatch: bool = False,
                crash_after_persist_before_commit: bool = False) -> str:
        claim = self.journal.claim(task_key, self.worker_id, input_id)
        self.journal.record_dispatch(claim)
        self.dispatch_count += 1
        if crash_after_dispatch:
            raise SimulatedCrash(f"任务 {task_key} 于派发后、响应持久前崩溃")
        response = response_factory()
        persisted = self.blobs.put_bytes(response)
        if self.blobs.read_bytes(persisted) != response:
            raise RuntimeError(f"任务 {task_key} 响应持久化复核失败")
        output_ref = persisted.sha256
        if crash_after_persist_before_commit:
            raise SimulatedCrash(
                f"任务 {task_key} 于响应持久化后、DB提交前崩溃（孤立响应工件保留）"
            )
        self.journal.commit(claim, output_ref)
        return output_ref


def _same_body_occurrence_count(case: CaseStore, blob_sha256: str) -> int:
    return sum(1 for row in case.fetch_all("sources")
               if row["blob_sha256"] == blob_sha256)


def _projection_excerpt(blobs: BlobStore, source: dict, locator_ref: dict):
    data = blobs.read_bytes(source["blob_sha256"])
    if "page" in locator_ref:
        projection = extract_pdf_pages(data)
        for row in projection.locators:
            if row["page"] == locator_ref["page"]:
                return row
        raise ValueError(f"第 {locator_ref['page']} 页无可用文本投影")
    if "member" in locator_ref:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member_data = archive.read(locator_ref["member"])
        projection = extract_docx_paragraphs(member_data)
        for row in projection.locators:
            if row["paragraph"] == locator_ref.get("paragraph"):
                return row
        raise ValueError(
            f"成员 {locator_ref['member']} 第 {locator_ref.get('paragraph')} 段无投影")
    raise ValueError(f"未知 locator_ref：{locator_ref}")


def _sealed_excerpt_bytes(blobs: BlobStore, source: dict, claim_spec: dict):
    """返回（封存摘录字节, 摘录hash, 摘录文本, locator字段）。"""
    locator_kind = claim_spec["locator_kind"]
    if locator_kind == "byte_range":
        data = blobs.read_bytes(source["blob_sha256"])
        start, end = claim_spec["start"], claim_spec["end"]
        if not (0 <= start < end <= len(data)):
            raise ValueError(f"主张区间非法 [{start},{end})（对象长度 {len(data)}）")
        excerpt = data[start:end]
        return (excerpt, sha256_hex(excerpt),
                claim_spec.get("excerpt_text")
                or excerpt.decode("utf-8", errors="replace"),
                start, end, None)
    row = _projection_excerpt(blobs, source, claim_spec["locator_ref"])
    return (row["text"].encode("utf-8"), row["text_sha256"], row["text"],
            None, None,
            json.dumps(claim_spec["locator_ref"], ensure_ascii=False))


def run_criterion_slice(case_dir: Path | str, *, source_id: str, claim_spec: dict,
                        criterion_id: str, catalog: dict,
                        case_basis: dict | None = None,
                        case_flags: dict | None = None,
                        review_attempt: str = "r1_2-runner") -> dict:
    """执行一条判据切片并落库（v3：可信解析＋完整冻结＋映射＋追溯门）。"""
    case_dir = Path(case_dir)
    if not case_basis or not case_basis.get("subject_source_basis"):
        raise ValueError("case_basis 必须携带 subject_source_basis（有源主体/截止）")
    basis = case_basis
    canonical = resolve_criterion(criterion_id, catalog)
    approved = approved_ids_from_catalog(catalog)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        source = case.fetch_one("sources", "source_id", source_id)
        if source is None:
            raise KeyError(f"来源 {source_id} 不存在（先运行 census 导入）")
        latest_time = case.latest_time_evidence(source_id)
        source = dict(source)
        if latest_time:
            source["time_evidence"] = {
                k: v for k, v in latest_time.items()
                if k not in ("revision", "created_at")
            }
        elif source.get("time_evidence"):
            try:
                source["time_evidence"] = json.loads(source["time_evidence"])
            except (TypeError, ValueError):
                source["time_evidence"] = {}

        excerpt, excerpt_sha256, excerpt_text, start, end, locator_ref = \
            _sealed_excerpt_bytes(blobs, source, claim_spec)
        if claim_spec.get("excerpt_text") and \
                claim_spec["locator_kind"] == "byte_range" and \
                claim_spec["excerpt_text"].encode("utf-8") != excerpt:
            raise ValueError("excerpt_text 与封存区间不一致（不接受自报摘录文本）")

        claim_id = claim_spec["claim_id"]
        claim_row = {
            "claim_id": claim_id, "source_id": source_id,
            "locator_kind": claim_spec["locator_kind"],
            "locator_start": start or 0, "locator_end": end or 0,
            "locator_ref": locator_ref,
            "excerpt_sha256": excerpt_sha256, "excerpt_text": excerpt_text,
            "interpretation": claim_spec["interpretation"],
            "subject_scope": claim_spec["subject_scope"],
        }
        content_digest = claim_content_digest(claim_row)
        existing = case.fetch_one("claims", "claim_id", claim_id)
        if existing is not None:
            stored_content = claim_content_digest(existing)
            if stored_content != content_digest:
                raise RuntimeError(
                    f"输入身份不一致：claim {claim_id} 已以不同输入登记"
                    f"（存档内容摘要 {stored_content[:12]}…，本次 "
                    f"{content_digest[:12]}…）。同ID不同解释/主体/来源/定位必须"
                    f"新建claim版本，不得覆盖"
                )
        else:
            case.add_claim(
                claim_id, source_id, locator_kind=claim_row["locator_kind"],
                excerpt_start=claim_row["locator_start"],
                excerpt_end=claim_row["locator_end"],
                excerpt_sha256=excerpt_sha256, excerpt_text=excerpt_text,
                interpretation=claim_row["interpretation"],
                subject_scope=claim_row["subject_scope"],
                interpretation_attempt=review_attempt, locator_ref=locator_ref,
                input_digest=content_digest, content_digest=content_digest,
            )
        claim = case.fetch_one("claims", "claim_id", claim_id)

        # 判据主张映射（R1.2-B）：引文逐字位于封存摘录＋语义过滤留痕
        mapping_spec = claim_spec.get("criterion_mapping")
        if mapping_spec is not None:
            mapping = interpret_claim_for_criterion(
                mapping_spec.get("quote") or "", criterion_id, excerpt,
                int(mapping_spec.get("start", -1)), int(mapping_spec.get("end", -1)))
        else:
            mapping = {"status": "unmapped", "filter_hits": [],
                       "quote_sha256": None,
                       "basis": "调用方未提供判据映射引文"}
        case.add_claim_mapping(
            claim_id, criterion_id,
            quote_start=int(mapping_spec.get("start", 0)) if mapping_spec else 0,
            quote_end=int(mapping_spec.get("end", 0)) if mapping_spec else 0,
            quote_sha256=mapping.get("quote_sha256") or "",
            filter_hits=mapping.get("filter_hits") or [],
            status=mapping.get("status", "unmapped"),
        )

        outcome = qualify_claim(
            claim, source, blobs, basis,
            same_body_sources=_same_body_occurrence_count(case, source["blob_sha256"]),
            review_attempt=review_attempt,
        )
        qual_refs: list[str] = []
        gap_refs: list[str] = []
        if isinstance(outcome, GapOutcome):
            gap_id = f"GAPR::{outcome.claim_id}"
            if case.fetch_one("gaps", "gap_id", gap_id) is None:
                case.add_gap(
                    gap_id, outcome.gap_type,
                    affected_criteria=[criterion_id],
                    pipeline_fault=outcome.pipeline_fault,
                    investigation=outcome.investigation,
                    unconfirmed=outcome.unconfirmed,
                )
            gap_refs.append(gap_id)
        else:
            qual_id = qualification_id(claim_id)
            if case.fetch_one("qualifications", "qual_id", qual_id) is None:
                case.add_qualification(
                    qual_id, outcome.claim_id,
                    source_judgment=outcome.source_judgment.basis,
                    identity_judgment=outcome.identity_judgment.basis,
                    time_judgment=outcome.time_judgment.basis,
                    independence_judgment=outcome.independence_judgment.basis,
                    allowed_uses=outcome.allowed_uses,
                    cannot_prove=outcome.cannot_prove,
                    review_attempt=outcome.review_attempt,
                    status=outcome.status,
                )
            qual_refs.append(qual_id)

        # CaseBasis 版本（存在则复用当前版本号；无则登记 v1）
        basis_row = case.get_case_basis()
        if basis_row is None:
            basis_version = case.set_case_basis(
                subject_legal_name=basis["subject_legal_name"],
                subject_aliases=basis.get("subject_aliases", []),
                evidence_cutoff=basis["evidence_cutoff"],
                subject_source_basis=basis["subject_source_basis"])
        else:
            basis_version = basis_row["version"]

        frozen_inputs = {
            "criterion": {k: canonical.get(k) for k in
                          ("criterion_id", "dimension", "level", "text",
                           "na_policy")},
            "catalog_sha256": catalog.get("wheel_sha256", ""),
            "approved_ids": sorted(approved),
            "rule_version": RULE_VERSION,
            "qualification_version": QUALIFICATION_VERSION,
            "case_basis": {k: basis.get(k) for k in
                           ("subject_legal_name", "subject_aliases",
                            "evidence_cutoff", "subject_source_basis")},
            "case_basis_version": basis_version,
            "case_flags": case_flags or {},
            "mapping": {
                "status": mapping.get("status"),
                "quote_sha256": mapping.get("quote_sha256"),
                "quote_start": (int(mapping_spec.get("start", 0))
                                if mapping_spec else 0),
                "quote_end": (int(mapping_spec.get("end", 0))
                              if mapping_spec else 0),
            },
        }
        digest = run_input_digest_v3(frozen_inputs, claim)

        evidence_view = {
            "case_flags": case_flags or {},
            "dimension_levels_supported": canonical.get("levels_supported")
            or catalog["dimensions"][canonical["dimension"]]["levels_supported"],
            "scope": claim_spec["subject_scope"],
            "approved_criterion_ids": approved,
            "catalog_criterion": canonical,
        }
        judgment_candidate = {
            "qualifications": [
                asdict(outcome) if isinstance(outcome, QualificationOutcome)
                else {"claim_id": outcome.claim_id, "status": "rejected",
                      "allowed_uses": [], "identity_judgment": {"verdict": "unknown"}}
            ],
            "claims": {claim_id: claim},
            "gap_refs": gap_refs,
            "na_proposal": claim_spec.get("na_proposal"),
            "native_proposal": claim_spec.get("native_proposal"),
            "criterion_mapping": mapping,
        }
        evaluation: CriterionEvaluation = evaluate_criterion(
            canonical, judgment_candidate, evidence_view
        )
        na_basis = None
        if evaluation.product_status == "succeeded" \
                and isinstance(claim_spec.get("na_proposal"), dict):
            na_basis = claim_spec["na_proposal"]

        rid = result_id(claim_id, criterion_id)
        existing_result = case.fetch_one("criterion_results", "result_id", rid)
        # 追溯门（R1.2-C）：succeeded 但追溯失败 → 改记 execution_failed 失败
        # 候选；同输入幂等重跑比对业务字段，不一致为可见失败
        if existing_result is None:
            case.add_criterion_result(
                rid, criterion_id, canonical["dimension"],
                native_disposition=evaluation.native_disposition,
                native_note=evaluation.native_note,
                product_status=evaluation.product_status,
                evidence_refs=evaluation.evidence_refs,
                gap_refs=evaluation.gap_refs,
                qual_refs=qual_refs, rationale=evaluation.rationale,
                scope=evaluation.scope, rule_version=evaluation.rule_version,
                input_digest=digest, case_basis_version=basis_version,
                frozen_inputs=frozen_inputs, na_basis=na_basis,
            )
        elif existing_result["input_digest"] != digest:
            raise RuntimeError(
                f"重复计算输入摘要不一致：{rid} 存档 "
                f"{existing_result['input_digest'][:12] if existing_result['input_digest'] else '无'}… "
                f"vs 本次 {digest[:12]}…（冻结输入被改动或换判据重放）"
            )
        elif existing_result["product_status"] != evaluation.product_status:
            raise RuntimeError(
                f"重复计算业务字段不一致：{rid} 已存 "
                f"{existing_result['product_status']} vs 本次 "
                f"{evaluation.product_status}"
            )
        report = trace(case, blobs, rid, strict=False)
        if existing_result is not None:
            published_status = existing_result["product_status"]
        else:
            published_status = evaluation.product_status
        if not report.ok and published_status == "succeeded":
            with case._conn:
                case._conn.execute(
                    "UPDATE criterion_results SET product_status='execution_failed', "
                    "rationale='追溯失败，不发布成功结果（保留为失败候选；"
                    "非业务NO）：' || ? WHERE result_id=?",
                    ("；".join(report.broken[:3]), rid))
            published_status = "execution_failed"

        case.new_run(input_digest=digest)
        replay_consistent = True
        previous_qual = case.fetch_one("qualifications", "qual_id",
                                       qualification_id(claim_id))
        if previous_qual is not None and not isinstance(outcome, GapOutcome):
            replay_consistent = previous_qual["status"] == outcome.status
        return {
            "result_id": rid,
            "claim_id": claim_id,
            "input_digest": digest,
            "content_digest": content_digest,
            "qualification_status": (
                outcome.status if isinstance(outcome, QualificationOutcome)
                else f"gap:{outcome.gap_type}"
            ),
            "mapping_status": mapping.get("status"),
            "product_status": published_status,
            "native_disposition": evaluation.native_disposition,
            "rationale": evaluation.rationale,
            "trace_ok": report.ok,
            "replay_consistent": replay_consistent,
        }
    finally:
        case.close()
