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


def _load_json_strict(value: str) -> tuple[list, str | None]:
    """严格加载 JSON 列表：解析失败/非列表返回错误（失败关闭，不静默空数组）。"""
    if value is None:
        return [], "引用字段为空"
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        return [], f"引用字段非法 JSON：{exc}"
    if not isinstance(parsed, list):
        return [], f"引用字段不是列表：{type(parsed).__name__}"
    return parsed, None


def _verify_projection(claim: dict, blob_data: bytes) -> str | None:
    """按抽取投影核验 pdf_page / zip_member 定位的主张。

    使用与导入时相同的成熟解析库重抽，比对登记的文本 hash；
    失败返回中文问题描述，成功返回 None。
    """
    locator_kind = claim.get("locator_kind")
    try:
        locator_ref = json.loads(claim.get("locator_ref") or "{}")
    except (TypeError, ValueError):
        return f"定位引用（{locator_kind}）解析失败"
    try:
        if locator_kind == "pdf_page":
            from .intake import extract_pdf_pages

            projection = extract_pdf_pages(blob_data)
            page = locator_ref.get("page")
            page_rows = [p for p in projection.locators if p["page"] == page]
            if not page_rows:
                if any(str(page) in u for u in projection.unprocessed):
                    return f"第 {page} 页无文本层（登记时也未抽取成功）"
                return f"第 {page} 页不在抽取投影中"
            if page_rows[0]["text_sha256"] != claim["excerpt_sha256"]:
                return "页面投影文本 hash 与登记不一致（原文可能被篡改）"
            return None
        if locator_kind == "zip_member":
            import io
            import zipfile

            from .intake import extract_docx_paragraphs

            member = locator_ref.get("member")
            if not member:
                return "zip 成员定位缺 member"
            with zipfile.ZipFile(io.BytesIO(blob_data)) as archive:
                member_data = archive.read(member)
            projection = extract_docx_paragraphs(member_data)
            paragraph = locator_ref.get("paragraph")
            rows = [p for p in projection.locators if p["paragraph"] == paragraph]
            if not rows:
                return f"成员 {member} 第 {paragraph} 段不在抽取投影中"
            if rows[0]["text_sha256"] != claim["excerpt_sha256"]:
                return "段落投影文本 hash 与登记不一致（原文可能被篡改）"
            return None
        return f"未知定位类型 {locator_kind}"
    except Exception as exc:  # 投影重核的任何失败都可见，不吞
        return f"抽取投影重核失败：{type(exc).__name__}: {exc}"


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

    qual_refs, qual_err = _load_json_strict(result["qual_refs"])
    evidence_refs, ev_err = _load_json_strict(result["evidence_refs"])
    gap_refs, gap_err = _load_json_strict(result["gap_refs"])
    report = TraceReport(
        result_id=result_id,
        criterion_id=result["criterion_id"],
        dimension=result["dimension"],
        native_disposition=result["native_disposition"],
        product_status=result["product_status"],
        evidence_refs=evidence_refs,
        gap_refs=gap_refs,
    )
    for field_name, err in (("qual_refs", qual_err), ("evidence_refs", ev_err),
                            ("gap_refs", gap_err)):
        if err:
            report.broken.append(f"结果 {result_id} 的 {field_name} {err}")
    # 正向结果必须有非空资格链（失败关闭：空链/畸形链不得显示通过）；
    # 合法 N/A 例外：需 na_basis（basis+case_flag_source 均非空）可解析
    na_basis_raw = result["na_basis"] if "na_basis" in result.keys() else None
    na_basis_valid = False
    if na_basis_raw:
        try:
            na_basis = json.loads(na_basis_raw)
            na_basis_valid = (
                isinstance(na_basis, dict)
                and str(na_basis.get("basis") or "").strip()
                and str(na_basis.get("case_flag_source") or "").strip()
            )
        except (TypeError, ValueError):
            na_basis_valid = False
    if result["product_status"] == "succeeded" and not qual_err and not qual_refs \
            and not na_basis_valid:
        report.broken.append(
            f"结果 {result_id} 为 succeeded 但资格引用为空且无合法 N/A 依据"
            "（正向结果必须有非空、完整、一致的来源闭包）"
        )

    closure_sources: set[str] = set()

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

        # 冻结内容校验（R1.2-C）：主张行内容摘要重算比对——冻结后改动
        # 解释/主体/定位/摘录文本任意字段 → 可见失败
        stored_content_digest = claim["content_digest"] \
            if "content_digest" in claim.keys() else None
        if stored_content_digest:
            from .contracts import claim_content_digest

            recomputed = claim_content_digest(claim)
            if recomputed != stored_content_digest:
                report.broken.append(
                    f"主张 {claim['claim_id']} 内容摘要不一致（存档 "
                    f"{stored_content_digest[:12]}… vs 重算 {recomputed[:12]}…）："
                    "冻结后内容被改动"
                )
                continue

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
        closure_sources.add(source["source_id"])

        # 摘录核验：按定位类型分别重核 + 保存的摘录文本必须绑定同一 hash
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
        else:
            # pdf_page / zip_member：按抽取投影重核（不假装文本偏移是原字节偏移）
            problem = _verify_projection(claim, data)
            if problem:
                report.broken.append(f"主张 {claim['claim_id']} {problem}")
                continue
        # 摘录文本绑定：保存/展示的 excerpt_text 必须与登记 hash 对应，
        # 改写摘录文本后同一引用不得继续通过
        text_hash = sha256_hex((claim["excerpt_text"] or "").encode("utf-8"))
        if text_hash != claim["excerpt_sha256"]:
            report.broken.append(
                f"主张 {claim['claim_id']} 摘录文本与登记 hash 不符"
                f"（文本hash {text_hash[:12]}… ≠ 登记 "
                f"{claim['excerpt_sha256'][:12]}…）：摘录文本被改写或与封存内容不一致"
            )
            continue
        report.verified_excerpt_hashes.append(claim["excerpt_sha256"])

    for gap_id in report.gap_refs:
        gap = case.fetch_one("gaps", "gap_id", gap_id)
        edge = TraceEdge("criterion_result", result_id, "gap", gap_id, True)
        if gap is None:
            edge.ok = False
            edge.problem = "缺口引用断裂：gaps 中不存在"
            report.broken.append(f"缺口 {gap_id} 引用断裂")
        report.edges.append(edge)

    # 正向闭包一致性：结果列出的 evidence_refs 必须与资格→主张→来源闭包一致
    if result["product_status"] == "succeeded" and not ev_err and not na_basis_valid:
        if set(evidence_refs) != closure_sources:
            report.broken.append(
                f"结果 {result_id} 的 Evidence 集合与资格→主张→来源闭包不符："
                f"列出 {sorted(set(evidence_refs))}，闭包 {sorted(closure_sources)}"
            )

    # 冻结输入校验（R1.2-C）：frozen_inputs + 主张行重算完整输入摘要，
    # 与存档 input_digest 比对；CaseBasis 按结果绑定的版本快照校验
    frozen_raw = result["frozen_inputs"] \
        if "frozen_inputs" in result.keys() else None
    stored_input_digest = result["input_digest"] \
        if "input_digest" in result.keys() else None
    if frozen_raw and stored_input_digest:
        try:
            frozen = json.loads(frozen_raw)
            claim_for_digest = case.fetch_one("claims", "claim_id",
                                              _first_claim_id(qual_refs, case))
            if claim_for_digest is not None:
                from .contracts import run_input_digest_v3

                recomputed_run = run_input_digest_v3(frozen, claim_for_digest)
                if recomputed_run != stored_input_digest:
                    report.broken.append(
                        f"结果 {result_id} 完整输入摘要不一致（存档 "
                        f"{stored_input_digest[:12]}… vs 重算 "
                        f"{recomputed_run[:12]}…）：冻结输入（判据/CaseBasis/flags/"
                        "映射/主张内容）在求值后被改动"
                    )
                # CaseBasis 版本绑定：结果绑定的版本快照必须仍可读且未被改写
                bound_version = result["case_basis_version"] \
                    if "case_basis_version" in result.keys() else None
                if bound_version is not None:
                    snapshot = case.get_case_basis_version(bound_version)
                    if snapshot is None:
                        report.broken.append(
                            f"结果 {result_id} 绑定的 CaseBasis 版本 "
                            f"{bound_version} 快照缺失（版本不可变被破坏）"
                        )
                    elif frozen.get("case_basis", {}).get(
                            "subject_legal_name") != snapshot.get(
                            "subject_legal_name") \
                            or frozen.get("case_basis", {}).get(
                                "evidence_cutoff") != snapshot.get("evidence_cutoff"):
                        report.broken.append(
                            f"结果 {result_id} 的 frozen_inputs 与其绑定的 "
                            f"CaseBasis 版本 {bound_version} 快照不一致"
                        )
        except (TypeError, ValueError) as exc:
            report.broken.append(f"结果 {result_id} 的 frozen_inputs 解析失败：{exc}")

    if strict and report.broken:
        raise TraceBroken(report)
    return report


def _first_claim_id(qual_refs: list, case: CaseStore) -> str | None:
    for qual_id in qual_refs:
        qual = case.fetch_one("qualifications", "qual_id", qual_id)
        if qual:
            return qual["claim_id"]
    return None


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
