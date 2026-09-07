"""审计 trace：从判据结果反向解析到封存原字节，读时重核。

最小审计链（business-contract-v1 §4）：

    CriterionResult → Qualification → Claim → Source → 封存原字节 + 精确定位

每条边存实际引用；``trace()`` 在读取时重新核验：
- 引用存在（断开的资格/主张/来源引用 → 可见失败）；
- 摘录 hash 与封存原件的字节区间一致（篡改/截断 → 可见失败）；
- 定位区间合法（越界 → 可见失败）；
- 缺口路径到达真实 Gap。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .contracts import is_sha256_hex, sha256_hex
from .store import BlobStore, CaseStore, StoreIntegrityError


@dataclass
class TraceEdge:
    from_kind: str
    from_id: str
    to_kind: str
    to_id: str
    ok: bool
    problem: str | None = None


@dataclass
class TraceReport:
    result_id: str
    criterion_id: str
    dimension: str
    native_disposition: str | None
    product_status: str
    evidence_refs: list[str] = field(default_factory=list)
    gap_refs: list[str] = field(default_factory=list)
    edges: list[TraceEdge] = field(default_factory=list)
    verified_excerpt_hashes: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.broken


class TraceBroken(RuntimeError):
    """链不完整或核验失败：篡改、断链、越界定位等。"""

    def __init__(self, report: TraceReport):
        self.report = report
        super().__init__(
            "trace 核验失败：" + "; ".join(report.broken) if report.broken else "trace 核验失败"
        )


def _load_json(value: str) -> list:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def trace(case: CaseStore, blobs: BlobStore, result_id: str,
          *, strict: bool = True) -> TraceReport:
    """反向解析并核验一条判据结果的证据链。

    ``strict=True`` 时任何断裂抛 :class:`TraceBroken`（调用方必须可见失败）；
    ``strict=False`` 仅返回报告（inspect 展示用）。
    """
    result = case.fetch_one("criterion_results", "result_id", result_id)
    if result is None:
        report = TraceReport(result_id=result_id, criterion_id="?", dimension="?",
                             native_disposition=None, product_status="execution_failed")
        report.broken.append(f"判据结果 {result_id} 不存在")
        if strict:
            raise TraceBroken(report)
        return report

    report = TraceReport(
        result_id=result_id,
        criterion_id=result["criterion_id"],
        dimension=result["dimension"],
        native_disposition=result["native_disposition"],
        product_status=result["product_status"],
        evidence_refs=_load_json(result["evidence_refs"]),
        gap_refs=_load_json(result["gap_refs"]),
    )
    qual_refs = _load_json(result["qual_refs"])

    for qual_id in qual_refs:
        qual = case.fetch_one("qualifications", "qual_id", qual_id)
        edge = TraceEdge("criterion_result", result_id, "qualification", qual_id, True)
        if qual is None:
            edge.ok = False
            edge.problem = "资格引用断裂：qualifications 中不存在"
            report.broken.append(f"资格 {qual_id} 引用断裂")
            report.edges.append(edge)
            continue
        report.edges.append(edge)

        claim = case.fetch_one("claims", "claim_id", qual["claim_id"])
        edge_c = TraceEdge("qualification", qual_id, "claim", qual["claim_id"], True)
        if claim is None:
            edge_c.ok = False
            edge_c.problem = "主张引用断裂：claims 中不存在"
            report.broken.append(f"主张 {qual['claim_id']} 引用断裂（来自资格 {qual_id}）")
            report.edges.append(edge_c)
            continue
        report.edges.append(edge_c)

        source = case.fetch_one("sources", "source_id", claim["source_id"])
        edge_s = TraceEdge("claim", claim["claim_id"], "source", claim["source_id"], True)
        if source is None:
            edge_s.ok = False
            edge_s.problem = "来源引用断裂：sources 中不存在"
            report.broken.append(f"来源 {claim['source_id']} 引用断裂（来自主张 {claim['claim_id']}）")
            report.edges.append(edge_s)
            continue
        report.edges.append(edge_s)

        edge_b = TraceEdge("source", source["source_id"], "blob", source["blob_sha256"],
                           True)
        if not is_sha256_hex(source["blob_sha256"]):
            edge_b.ok = False
            edge_b.problem = "blob id 非法"
            report.broken.append(f"来源 {source['source_id']} 的 blob id 非法")
            report.edges.append(edge_b)
            continue
        try:
            data = blobs.read_bytes(source["blob_sha256"])
        except (OSError, StoreIntegrityError) as exc:
            edge_b.ok = False
            edge_b.problem = f"封存原字节不可读或复核失败：{exc}"
            report.broken.append(
                f"来源 {source['source_id']} 封存原字节 {source['blob_sha256'][:12]}… "
                f"不可读/复核失败：{exc}"
            )
            report.edges.append(edge_b)
            continue
        if len(data) != source["byte_length"]:
            edge_b.ok = False
            edge_b.problem = "字节长度与登记不符"
            report.broken.append(f"来源 {source['source_id']} 字节长度不符")
            report.edges.append(edge_b)
            continue
        report.edges.append(edge_b)

        # 摘录核验：byte_range 主张必须与封存字节区间一致
        if claim["locator_kind"] == "byte_range":
            start, end = claim["locator_start"], claim["locator_end"]
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(data)):
                report.broken.append(
                    f"主张 {claim['claim_id']} 定位越界 [{start},{end})，"
                    f"对象长度 {len(data)}"
                )
                continue
            excerpt = data[start:end]
            actual = sha256_hex(excerpt)
            if actual != claim["excerpt_sha256"]:
                report.broken.append(
                    f"主张 {claim['claim_id']} 摘录 hash 不一致："
                    f"登记 {claim['excerpt_sha256'][:12]}…，实际 {actual[:12]}…"
                )
                continue
            report.verified_excerpt_hashes.append(actual)

    for gap_id in report.gap_refs:
        gap = case.fetch_one("gaps", "gap_id", gap_id)
        edge = TraceEdge("criterion_result", result_id, "gap", gap_id, True)
        if gap is None:
            edge.ok = False
            edge.problem = "缺口引用断裂：gaps 中不存在"
            report.broken.append(f"缺口 {gap_id} 引用断裂")
        report.edges.append(edge)

    if strict and report.broken:
        raise TraceBroken(report)
    return report


def render_trace(report: TraceReport) -> str:
    """中文渲染（inspect/trace CLI 共用；工程引用进审计输出）。"""
    lines = [
        "【证据与判据核验，非正式评估报告】",
        f"判据结果：{report.result_id}",
        f"维度/判据：{report.dimension} / {report.criterion_id}",
        f"原生处置：{report.native_disposition if report.native_disposition else 'null（未调用原版或无对应状态）'}",
        f"产品状态：{report.product_status}",
        f"链核验：{'通过' if report.ok else '失败'}",
    ]
    for edge in report.edges:
        mark = "✓" if edge.ok else "✗"
        suffix = f"（{edge.problem}）" if edge.problem else ""
        lines.append(f"  {mark} {edge.from_kind}:{edge.from_id} → {edge.to_kind}:{edge.to_id}{suffix}")
    if report.broken:
        lines.append("断裂明细：")
        lines.extend(f"  - {item}" for item in report.broken)
    if report.verified_excerpt_hashes:
        lines.append(
            f"已重核摘录 hash：{len(report.verified_excerpt_hashes)} 条"
            "（与封存原字节逐段一致）"
        )
    return "\n".join(lines)
