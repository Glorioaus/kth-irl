"""R1.3 运行编排 v4：预解析封存证据、确认映射、完整冻结、原子发布。

修复（R1.3 A/B/C）：
- 主体依据/N-A 适用性与 flag 引用由 runner **预解析**到封存原件字段值后
  才进入资格/判据判断（qualification 侧同核）。
- 映射=关键词候选 + ``confirm_mapping`` 留痕确认（否定门控）；candidate
  不构成支持关系。
- 冻结输入 v4 补全实际参与判断的全部内容：来源判断字段＋所用时间证据
  **修订快照**、资格内容摘要、映射＋确认、N/A 提案全文（含引用与解析值）。
- **原子发布**：先验证（``_verify_candidate``，含全部绑定核验）后单条
  INSERT；验证失败/中断时不发布成功（留失败候选），绝不"先提交succeeded
  再trace再改回"。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .contracts import (
    claim_content_digest,
    qualification_content_digest,
    run_input_digest_v3,
    sha256_hex,
)
from .dimensions import CriterionEvaluation, evaluate_criterion
from .intake import extract_docx_paragraphs, extract_pdf_pages
from .journal import Journal
from .kernels import (
    RULE_VERSION,
    confirm_mapping,
    interpret_claim_for_criterion,
)
from .qualification import (
    GapOutcome,
    QualificationOutcome,
    qualify_claim,
    RULE_VERSION as QUALIFICATION_VERSION,
    _resolve_case_field_reference,
)
from .store import BlobStore, CaseStore

WEIJIU_CASE_BASIS = {
    "subject_legal_name": "微玖（苏州）光电科技有限公司",
    "subject_aliases": ["微玖", "微玖光电"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
    "subject_source_basis": json.dumps({
        "kind": "field_reference",
        "path": "case:identity-plan.json#/subjects/0/canonical_name_claimed",
        "status": "claimed",
        "note": "封存session身份计划声称的规范主体名（claimed，未工商核验）；"
                "identity-plan.json 已封存于本Case blobs",
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
    for dim_name, info in catalog["dimensions"].items():
        for criterion in info["registry"].get("criteria", []):
            if criterion["criterion_id"] == criterion_id:
                resolved = {"dimension": dim_name, **criterion}
                resolved.setdefault("levels_supported", info["levels_supported"])
                return resolved
    raise ValueError(
        f"判据 {criterion_id} 不在可信 catalog（批准 wheel）中：不允许评估未登记"
        f"或调用方自报的判据"
    )


def approved_ids_from_catalog(catalog: dict) -> set[str]:
    return {c["criterion_id"] for info in catalog["dimensions"].values()
            for c in info["registry"].get("criteria", [])}


def _resolve_na_proposal(na_proposal, case, blobs):
    """预解析 N/A 适用性与 flag 引用到封存对象字段值（R1.3-B）。"""
    if not isinstance(na_proposal, dict):
        return na_proposal
    out = dict(na_proposal)
    for ref_key, res_key in (("applicability_ref", "applicability_resolved"),
                             ("flag_ref", "flag_resolved")):
        ref = out.get(ref_key)
        if ref is None:
            continue
        value, err = _resolve_case_field_reference(ref, case, blobs)
        if err:
            out[res_key] = {"_resolved": False, "error": err,
                            "path": ref.get("path") if isinstance(ref, dict) else None}
        else:
            out[res_key] = {"_resolved": True, "value": value,
                            "path": ref.get("path")}
    return out


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
    """返回（封存摘录字节, 摘录hash, 摘录文本, 定位字段）。"""
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


def _verify_candidate(case: CaseStore, blobs: BlobStore,
                      candidate_row: dict):
    """发布前验证（R1.3-C）：对候选结果执行与 trace 相同的绑定核验。

    返回 audit.TraceReport（ok=True 方可发布成功）。验证中断（异常）时
    调用方不得执行 INSERT——不发布未核验的成功。
    """
    from .audit import verify_result_bindings

    return verify_result_bindings(case, blobs, candidate_row)


def run_criterion_slice(case_dir: Path | str, *, source_id: str, claim_spec: dict,
                        criterion_id: str, catalog: dict,
                        case_basis: dict | None = None,
                        case_flags: dict | None = None,
                        review_attempt: str = "r1_3-runner") -> dict:
    """执行一条判据切片并落库（v4：预解析＋确认映射＋完整冻结＋原子发布）。"""
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
        # 所用时间证据=最新修订；记录修订号与内容快照（冻结不可变版本）
        latest_time = case.latest_time_evidence(source_id)
        time_revision = latest_time["revision"] if latest_time else None
        time_snapshot = ({k: v for k, v in latest_time.items()
                          if k not in ("revision", "created_at")}
                         if latest_time else None)
        source = dict(source)
        if time_snapshot is not None:
            source["time_evidence"] = time_snapshot
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

        # 映射：关键词候选 → 留痕确认（否定门控）
        mapping_spec = claim_spec.get("criterion_mapping")
        if mapping_spec is not None:
            candidate_mapping = interpret_claim_for_criterion(
                mapping_spec.get("quote") or "", criterion_id, excerpt,
                int(mapping_spec.get("start", -1)),
                int(mapping_spec.get("end", -1)))
        else:
            candidate_mapping = {"status": "unmapped", "filter_hits": [],
                                 "quote_sha256": None,
                                 "basis": "调用方未提供判据映射引文"}
        mapping = confirm_mapping(
            candidate_mapping, claim_spec.get("semantic_confirmation"),
            interpretation=claim_spec.get("interpretation") or "")
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
            review_attempt=review_attempt, case=case,
        )
        qual_refs: list[str] = []
        gap_refs: list[str] = []
        qual_digest = ""
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
            qual_row = case.fetch_one("qualifications", "qual_id", qual_id)
            qual_digest = qualification_content_digest(qual_row)
            qual_refs.append(qual_id)

        basis_row = case.get_case_basis()
        if basis_row is None:
            basis_version = case.set_case_basis(
                subject_legal_name=basis["subject_legal_name"],
                subject_aliases=basis.get("subject_aliases", []),
                evidence_cutoff=basis["evidence_cutoff"],
                subject_source_basis=basis["subject_source_basis"])
        else:
            basis_version = basis_row["version"]
        # 冻结的case_basis取**存储的版本快照**（与绑定版本完全一致，含note）
        basis_snapshot = {k: v for k, v in
                          case.get_case_basis_version(basis_version).items()
                          if k != "version"}

        # N/A 预解析（封存对象字段值）
        na_proposal = _resolve_na_proposal(
            claim_spec.get("na_proposal"), case, blobs)

        frozen_inputs = {
            "criterion": {k: canonical.get(k) for k in
                          ("criterion_id", "dimension", "level", "text",
                           "na_policy")},
            "catalog_sha256": catalog.get("wheel_sha256", ""),
            "approved_ids": sorted(approved),
            "rule_version": RULE_VERSION,
            "qualification_version": QUALIFICATION_VERSION,
            "case_basis": basis_snapshot,
            "case_basis_version": basis_version,
            "case_flags": case_flags or {},
            "mapping": {
                "status": mapping.get("status"),
                "quote_sha256": mapping.get("quote_sha256"),
                "quote_start": (int(mapping_spec.get("start", 0))
                                if mapping_spec else 0),
                "quote_end": (int(mapping_spec.get("end", 0))
                              if mapping_spec else 0),
                "confirmation": mapping.get("confirmation"),
            },
            "source_inputs": {
                "published_at": source.get("published_at"),
                "retrieved_at": source.get("retrieved_at"),
                "source_family": source.get("source_family"),
                "capture_status": source.get("capture_status"),
                "document_subject": source.get("document_subject"),
                "time_evidence_revision": time_revision,
                "time_evidence_snapshot": time_snapshot,
            },
            "qualification_digest": qual_digest,
            "na_proposal": na_proposal,
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
            "na_proposal": na_proposal,
            "native_proposal": claim_spec.get("native_proposal"),
            "criterion_mapping": mapping,
        }
        evaluation: CriterionEvaluation = evaluate_criterion(
            canonical, judgment_candidate, evidence_view
        )

        rid = result_id(claim_id, criterion_id)
        existing_result = case.fetch_one("criterion_results", "result_id", rid)
        if existing_result is not None:
            if existing_result["input_digest"] != digest:
                raise RuntimeError(
                    f"重复计算输入摘要不一致：{rid} 存档 "
                    f"{(existing_result['input_digest'] or '无')[:12]}… vs 本次 "
                    f"{digest[:12]}…（冻结输入被改动或换依据重放：时间/N-A依据/"
                    "映射确认/来源字段任一变化即不同输入）"
                )
            if existing_result["product_status"] != evaluation.product_status:
                raise RuntimeError(
                    f"重复计算业务字段不一致：{rid} 已存 "
                    f"{existing_result['product_status']} vs 本次 "
                    f"{evaluation.product_status}"
                )
            published_status = existing_result["product_status"]
        else:
            published_status = evaluation.product_status

        na_basis = None
        if evaluation.product_status == "succeeded" and na_proposal:
            na_basis = {
                "basis": str(((na_proposal.get("applicability_resolved")
                               or {}).get("value")) or ""),
                "case_flag_source": (na_proposal.get("flag_resolved") or {}
                                     ).get("path"),
            }

        candidate_row = {
            "result_id": rid, "criterion_id": criterion_id,
            "dimension": canonical["dimension"],
            "native_disposition": evaluation.native_disposition,
            "native_note": evaluation.native_note,
            "product_status": published_status,
            "evidence_refs": json.dumps(evaluation.evidence_refs,
                                        ensure_ascii=False),
            "gap_refs": json.dumps(evaluation.gap_refs, ensure_ascii=False),
            "qual_refs": json.dumps(qual_refs, ensure_ascii=False),
            "rationale": evaluation.rationale,
            "scope": evaluation.scope, "rule_version": evaluation.rule_version,
            "input_digest": digest, "case_basis_version": basis_version,
            "frozen_inputs": json.dumps(frozen_inputs, ensure_ascii=False,
                                        sort_keys=True),
            "na_basis": json.dumps(na_basis, ensure_ascii=False) if na_basis
            else None,
        }
        # 原子发布（R1.3-C）：先完整验证，后单条 INSERT；验证失败/中断
        # 不发布成功（不先提交succeeded再trace再改回）
        report = _verify_candidate(case, blobs, candidate_row)
        if existing_result is None:
            if report.ok:
                case.add_criterion_result(
                    rid, criterion_id, canonical["dimension"],
                    native_disposition=evaluation.native_disposition,
                    native_note=evaluation.native_note,
                    product_status=evaluation.product_status,
                    evidence_refs=evaluation.evidence_refs,
                    gap_refs=evaluation.gap_refs, qual_refs=qual_refs,
                    rationale=evaluation.rationale, scope=evaluation.scope,
                    rule_version=evaluation.rule_version, input_digest=digest,
                    case_basis_version=basis_version,
                    frozen_inputs=frozen_inputs, na_basis=na_basis)
                published_status = evaluation.product_status
            else:
                failure_rationale = (
                    "发布前验证失败，保留为失败候选（非业务NO；验证未通过不得"
                    "发布成功）：" + "；".join(report.broken[:3]))
                case.add_criterion_result(
                    rid, criterion_id, canonical["dimension"],
                    native_disposition=evaluation.native_disposition,
                    native_note=evaluation.native_note,
                    product_status="execution_failed",
                    evidence_refs=evaluation.evidence_refs,
                    gap_refs=evaluation.gap_refs, qual_refs=qual_refs,
                    rationale=failure_rationale, scope=evaluation.scope,
                    rule_version=evaluation.rule_version, input_digest=digest,
                    case_basis_version=basis_version,
                    frozen_inputs=frozen_inputs, na_basis=na_basis)
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
