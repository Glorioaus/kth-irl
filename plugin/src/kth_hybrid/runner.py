"""R1 运行编排：判据切片执行、重复计算一致性与计数型模拟 Provider。

- ``run_criterion_slice``：真实/合成来源 → 主张 → 资格 → 判据消费 → 落库。
- ``CountingSimulatedProvider``：模拟 Provider 必须实际经过一次计数型派发
  （先落账 dispatch 再行动），不得以零调用证明不重复；崩溃窗口产生
  outcome_unknown，不自动重发；显式新任务是新的计数。
- 同一冻结输入重复计算：业务字段一致，不重复导入、无 Provider 调用。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .audit import trace
from .contracts import sha256_hex
from .dimensions import CriterionEvaluation, evaluate_criterion
from .intake import extract_docx_paragraphs, extract_pdf_pages
from .journal import Journal
from .qualification import GapOutcome, QualificationOutcome, qualify_claim
from .store import BlobStore, CaseStore

WEIJIU_CASE_BASIS = {
    "subject_legal_name": "武汉微玖光电科技有限公司",
    "subject_aliases": ["微玖", "微玖光电"],
    "evidence_cutoff": "2026-08-27T03:02:29Z",
}


class SimulatedCrash(RuntimeError):
    """注入的模拟崩溃（dispatch 已落账、响应未持久）。"""


class CountingSimulatedProvider:
    """计数型本地模拟 Provider（simulated=true，非真实模型角色链）。"""

    simulated = True

    def __init__(self, journal: Journal, worker_id: str = "simulated-provider"):
        self.journal = journal
        self.worker_id = worker_id
        self.dispatch_count = 0

    def execute(self, task_key: str, input_id: str,
                response_factory: Callable[[], bytes], *,
                crash_after_dispatch: bool = False) -> str:
        """恰好一次计数型派发：claim → dispatch 落账 → （可选崩溃）→ 响应 → commit。"""
        claim = self.journal.claim(task_key, self.worker_id, input_id)
        self.journal.record_dispatch(claim)  # 外部动作先落账
        self.dispatch_count += 1
        if crash_after_dispatch:
            raise SimulatedCrash(f"任务 {task_key} 于派发后、响应持久前崩溃")
        response = response_factory()
        output_ref = sha256_hex(response)
        self.journal.commit(claim, output_ref)
        return output_ref


def _same_body_source_count(case: CaseStore, blob_sha256: str) -> int:
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
        raise ValueError(f"成员 {locator_ref['member']} 第 {locator_ref.get('paragraph')} 段无投影")
    raise ValueError(f"未知 locator_ref：{locator_ref}")


def run_criterion_slice(case_dir: Path | str, *, source_id: str, claim_spec: dict,
                        criterion: dict, dimension_levels_supported: list[int],
                        case_basis: dict | None = None,
                        case_flags: dict | None = None,
                        review_attempt: str = "r1-runner") -> dict:
    """执行一条判据切片并落库；返回结果引用（供 trace/inspect）。

    ``claim_spec``：{"claim_id", "locator_kind", "start"/"end"（byte_range）或
    "locator_ref"（pdf_page/zip_member）, "interpretation", "subject_scope",
    "excerpt_text"（byte_range 时可选，默认取自封存字节）}。
    """
    case_dir = Path(case_dir)
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        basis = case_basis or WEIJIU_CASE_BASIS
        source = case.fetch_one("sources", "source_id", source_id)
        if source is None:
            raise KeyError(f"来源 {source_id} 不存在（先运行 census 导入）")
        if source.get("time_evidence"):
            source = {**source,
                      "time_evidence": json.loads(source["time_evidence"])}

        locator_kind = claim_spec["locator_kind"]
        if locator_kind == "byte_range":
            data = blobs.read_bytes(source["blob_sha256"])
            start, end = claim_spec["start"], claim_spec["end"]
            excerpt = data[start:end]
            excerpt_sha256 = sha256_hex(excerpt)
            excerpt_text = claim_spec.get("excerpt_text") or excerpt.decode(
                "utf-8", errors="replace"
            )
            locator_start, locator_end, locator_ref = start, end, None
        else:
            row = _projection_excerpt(blobs, source, claim_spec["locator_ref"])
            excerpt_sha256 = row["text_sha256"]
            excerpt_text = row["text"]
            locator_start = locator_end = None
            locator_ref = json.dumps(claim_spec["locator_ref"], ensure_ascii=False)

        claim_id = claim_spec["claim_id"]
        existing = case.fetch_one("claims", "claim_id", claim_id)
        if existing is None:
            case.add_claim(
                claim_id, source_id, locator_kind=locator_kind,
                excerpt_start=locator_start or 0, excerpt_end=locator_end or 0,
                excerpt_sha256=excerpt_sha256, excerpt_text=excerpt_text,
                interpretation=claim_spec["interpretation"],
                subject_scope=claim_spec["subject_scope"],
                interpretation_attempt=review_attempt, locator_ref=locator_ref,
            )
        claim = case.fetch_one("claims", "claim_id", claim_id)

        # 重复计算一致性：同一冻结输入再跑，业务字段必须一致
        previous_qual = case.fetch_one("qualifications", "claim_id", claim_id)

        outcome = qualify_claim(
            claim, source, blobs, basis,
            same_body_sources=_same_body_source_count(case, source["blob_sha256"]),
            review_attempt=review_attempt,
        )
        qual_refs: list[str] = []
        gap_refs: list[str] = []
        if isinstance(outcome, GapOutcome):
            gap_id = f"GAPR::{outcome.claim_id[:24]}"
            if case.fetch_one("gaps", "gap_id", gap_id) is None:
                case.add_gap(
                    gap_id, outcome.gap_type,
                    affected_criteria=[criterion["criterion_id"]],
                    pipeline_fault=outcome.pipeline_fault,
                    investigation=outcome.investigation,
                    unconfirmed=outcome.unconfirmed,
                )
            gap_refs.append(gap_id)
        else:
            qual_id = f"QUALR::{outcome.claim_id[:24]}"
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

        evidence_view = {
            "case_flags": case_flags or {},
            "dimension_levels_supported": list(dimension_levels_supported),
            "scope": claim_spec["subject_scope"],
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
        }
        evaluation: CriterionEvaluation = evaluate_criterion(
            criterion, judgment_candidate, evidence_view
        )
        result_id = f"RESR::{claim_id[:20]}::{criterion['criterion_id']}"
        existing_result = case.fetch_one("criterion_results", "result_id", result_id)
        if existing_result is None:
            case.add_criterion_result(
                result_id, criterion["criterion_id"], criterion.get("dimension", ""),
                native_disposition=evaluation.native_disposition,
                native_note=evaluation.native_note,
                product_status=evaluation.product_status,
                evidence_refs=evaluation.evidence_refs, gap_refs=evaluation.gap_refs,
                qual_refs=qual_refs, rationale=evaluation.rationale,
                scope=evaluation.scope, rule_version=evaluation.rule_version,
            )
        else:
            # 同一冻结输入重复计算：业务字段必须一致，不一致是可见失败
            if existing_result["product_status"] != evaluation.product_status:
                raise RuntimeError(
                    f"重复计算业务字段不一致：已存 {existing_result['product_status']}"
                    f" vs 本次 {evaluation.product_status}（{result_id}）"
                )

        report = trace(case, blobs, result_id, strict=False)
        # 重复计算一致性校验（previous_qual 存在时比对业务状态）
        replay_consistent = True
        if previous_qual is not None and not isinstance(outcome, GapOutcome):
            replay_consistent = (
                previous_qual["status"] == outcome.status
            )
        return {
            "result_id": result_id,
            "claim_id": claim_id,
            "qualification_status": (
                outcome.status if isinstance(outcome, QualificationOutcome)
                else f"gap:{outcome.gap_type}"
            ),
            "product_status": evaluation.product_status,
            "native_disposition": evaluation.native_disposition,
            "rationale": evaluation.rationale,
            "trace_ok": report.ok,
            "replay_consistent": replay_consistent,
        }
    finally:
        case.close()
