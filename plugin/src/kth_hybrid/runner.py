"""R1.1 运行编排 v2：判据切片、输入身份冻结、响应先落盘的计数型 Provider。

修复（验收 R1-03/R1-05）：
- 完整 ID（不截断 claim_id，避免碰撞）；同 claim ID 不同输入 → 拒绝执行
  （比较 ``claims.input_digest``，不只比较最终状态）。
- ``case_basis`` 必须带 ``subject_source_basis``；每次切片写入 runs 行
  （冻结输入摘要留痕）。
- ``CountingSimulatedProvider``：响应原字节**先持久化到 BlobStore 并读回
  重核**，然后才提交成功引用；支持在持久化后、提交前注入崩溃（孤立响应
  工件保留，恢复期不盲重发）。
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
    "subject_source_basis": (
        "Owner 交接书/开工指令钉定（LOCAL-CASE-e7edcba32c19969a317f4bca，"
        "session_hash a93e446f…75965，as_of_cut=2026-08-27T03:02:29Z）"
    ),
}


class SimulatedCrash(RuntimeError):
    """注入的模拟崩溃。"""


def qualification_id(claim_id: str) -> str:
    """完整资格 ID（不截断，防同前缀碰撞）。"""
    return f"QUALR::{claim_id}"


def result_id(claim_id: str, criterion_id: str) -> str:
    """完整结果 ID（不截断，防同前缀碰撞）。"""
    return f"RESR::{claim_id}::{criterion_id}"


def claim_input_digest(claim_spec: dict, source: dict, criterion: dict,
                       case_basis: dict) -> str:
    """冻结输入身份：解释/主体/来源/时点/判据/依据任一变化即不同摘要。"""
    payload = json.dumps({
        "source_id": source["source_id"],
        "blob_sha256": source["blob_sha256"],
        "locator_kind": claim_spec["locator_kind"],
        "locator": ({"start": claim_spec.get("start"), "end": claim_spec.get("end")}
                    if claim_spec["locator_kind"] == "byte_range"
                    else claim_spec.get("locator_ref")),
        "interpretation": claim_spec["interpretation"],
        "subject_scope": claim_spec["subject_scope"],
        "criterion_id": criterion["criterion_id"],
        "rule_scope": "kth-hybrid.kernels.r1-narrow.v2",
        "case_subject": case_basis["subject_legal_name"],
        "case_cutoff": case_basis["evidence_cutoff"],
        "case_basis_source": case_basis["subject_source_basis"],
        "source_time": {
            "published_at": source.get("published_at"),
            "retrieved_at": source.get("retrieved_at"),
            "time_evidence": source.get("time_evidence"),
            "document_subject": source.get("document_subject"),
        },
    }, ensure_ascii=False, sort_keys=True)
    return sha256_hex(payload.encode("utf-8"))


class CountingSimulatedProvider:
    """计数型本地模拟 Provider（simulated=true，非真实模型角色链）。

    v2：``blobs`` 必填——响应先 ``put_bytes`` 落盘并读回重核，再提交成功引用；
    ``output_ref`` 可解析到真实响应字节。
    """

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
        """恰好一次计数型派发：claim → dispatch落账 →（崩溃）→ 响应落盘重核
        →（崩溃）→ commit。"""
        claim = self.journal.claim(task_key, self.worker_id, input_id)
        self.journal.record_dispatch(claim)
        self.dispatch_count += 1
        if crash_after_dispatch:
            raise SimulatedCrash(f"任务 {task_key} 于派发后、响应持久前崩溃")
        response = response_factory()
        persisted = self.blobs.put_bytes(response)  # 原始响应先持久化
        if self.blobs.read_bytes(persisted) != response:  # 读回重核
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


def run_criterion_slice(case_dir: Path | str, *, source_id: str, claim_spec: dict,
                        criterion: dict, dimension_levels_supported: list[int],
                        case_basis: dict | None = None,
                        case_flags: dict | None = None,
                        approved_criterion_ids: set[str] | None = None,
                        review_attempt: str = "r1_1-runner") -> dict:
    """执行一条判据切片并落库（v2：输入身份冻结与同ID异输入拒绝）。"""
    case_dir = Path(case_dir)
    if not case_basis or not case_basis.get("subject_source_basis", "").strip():
        raise ValueError("case_basis 必须携带 subject_source_basis（有源主体/截止）")
    basis = case_basis
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        source = case.fetch_one("sources", "source_id", source_id)
        if source is None:
            raise KeyError(f"来源 {source_id} 不存在（先运行 census 导入）")
        # 时间证据与文档主体：读取追加版本表（最新修订）+ 列字段
        # digest 只取证据内容（kind/date/basis…），排除 revision/created_at 元数据
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

        locator_kind = claim_spec["locator_kind"]
        if locator_kind == "byte_range":
            data = blobs.read_bytes(source["blob_sha256"])
            start, end = claim_spec["start"], claim_spec["end"]
            excerpt = data[start:end]
            excerpt_sha256 = sha256_hex(excerpt)
            excerpt_text = claim_spec.get("excerpt_text") or excerpt.decode(
                "utf-8", errors="replace")
            locator_start, locator_end = start, end
            locator_ref = None
        else:
            row = _projection_excerpt(blobs, source, claim_spec["locator_ref"])
            excerpt_sha256 = row["text_sha256"]
            excerpt_text = row["text"]
            locator_start = locator_end = 0
            locator_ref = json.dumps(claim_spec["locator_ref"], ensure_ascii=False)

        claim_id = claim_spec["claim_id"]
        digest = claim_input_digest(claim_spec, source, criterion, basis)
        existing = case.fetch_one("claims", "claim_id", claim_id)
        if existing is not None:
            if existing["input_digest"] != digest:
                raise RuntimeError(
                    f"输入身份不一致：claim {claim_id} 已以不同输入登记"
                    f"（存档摘要 {existing['input_digest'][:12]}…，本次 "
                    f"{digest[:12]}…）。同ID不同解释/主体/来源/时点/规则必须"
                    f"新建claim版本，不得覆盖"
                )
        else:
            case.add_claim(
                claim_id, source_id, locator_kind=locator_kind,
                excerpt_start=locator_start or 0, excerpt_end=locator_end or 0,
                excerpt_sha256=excerpt_sha256, excerpt_text=excerpt_text,
                interpretation=claim_spec["interpretation"],
                subject_scope=claim_spec["subject_scope"],
                interpretation_attempt=review_attempt, locator_ref=locator_ref,
                input_digest=digest,
            )
        claim = case.fetch_one("claims", "claim_id", claim_id)

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
                    affected_criteria=[criterion["criterion_id"]],
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

        evidence_view = {
            "case_flags": case_flags or {},
            "dimension_levels_supported": list(dimension_levels_supported),
            "scope": claim_spec["subject_scope"],
            "approved_criterion_ids": (approved_criterion_ids
                                       if approved_criterion_ids is not None
                                       else {criterion["criterion_id"]}),
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
        rid = result_id(claim_id, criterion["criterion_id"])
        existing_result = case.fetch_one("criterion_results", "result_id", rid)
        if existing_result is None:
            case.add_criterion_result(
                rid, criterion["criterion_id"], criterion.get("dimension", ""),
                native_disposition=evaluation.native_disposition,
                native_note=evaluation.native_note,
                product_status=evaluation.product_status,
                evidence_refs=evaluation.evidence_refs, gap_refs=evaluation.gap_refs,
                qual_refs=qual_refs, rationale=evaluation.rationale,
                scope=evaluation.scope, rule_version=evaluation.rule_version,
            )
        elif existing_result["product_status"] != evaluation.product_status:
            raise RuntimeError(
                f"重复计算业务字段不一致：已存 {existing_result['product_status']}"
                f" vs 本次 {evaluation.product_status}（{rid}）"
            )
        case.new_run(input_digest=digest)  # 冻结输入运行留痕

        report = trace(case, blobs, rid, strict=False)
        replay_consistent = True
        previous_qual = case.fetch_one("qualifications", "qual_id",
                                       qualification_id(claim_id))
        if previous_qual is not None and not isinstance(outcome, GapOutcome):
            replay_consistent = previous_qual["status"] == outcome.status
        return {
            "result_id": rid,
            "claim_id": claim_id,
            "input_digest": digest,
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
