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


def _excerpt_bytes_for_claim(blobs, claim_row, source_row):
    """取主张的封存摘录字节（byte_range 或投影文本），用于映射引文核验。"""
    if claim_row["locator_kind"] == "byte_range":
        data = blobs.read_bytes(source_row["blob_sha256"])
        start, end = claim_row["locator_start"], claim_row["locator_end"]
        if not (0 <= start < end <= len(data)):
            return None
        return data[start:end]
    try:
        locator_ref = json.loads(claim_row.get("locator_ref") or "{}")
    except (TypeError, ValueError):
        return None
    data = blobs.read_bytes(source_row["blob_sha256"])
    if claim_row["locator_kind"] == "pdf_page":
        from .intake import extract_pdf_pages

        for row in extract_pdf_pages(data).locators:
            if row["page"] == locator_ref.get("page"):
                return row["text"].encode("utf-8")
    if claim_row["locator_kind"] == "zip_member":
        import io
        import zipfile

        from .intake import extract_docx_paragraphs

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                member = archive.read(locator_ref["member"])
            for row in extract_docx_paragraphs(member).locators:
                if row["paragraph"] == locator_ref.get("paragraph"):
                    return row["text"].encode("utf-8")
        except Exception:
            return None
    return None


def verify_result_bindings(case, blobs, result_row):
    """R1.3-C 绑定核验（发布前与 trace 共用同一实现）。

    对候选/已存结果行核验：冻结输入摘要自洽、来源判断字段未被改动、
    所用时间证据修订快照未变、资格记录状态与内容一致、映射记录存在且
    引文绑定封存原文、CaseBasis 版本快照完整一致、合法 N/A 链。
    """
    from .contracts import (
        claim_content_digest,
        qualification_content_digest,
        run_input_digest_v3,
        sha256_hex,
    )

    rid = result_row["result_id"]
    report = TraceReport(
        result_id=rid,
        criterion_id=result_row.get("criterion_id", "?"),
        dimension=result_row.get("dimension", "?"),
        native_disposition=result_row.get("native_disposition"),
        product_status=result_row.get("product_status", "execution_failed"),
    )
    frozen_raw = result_row.get("frozen_inputs")
    stored_digest = result_row.get("input_digest")
    qual_refs = _load_json(result_row.get("qual_refs"))
    if not frozen_raw or not stored_digest:
        report.broken.append(f"结果 {rid} 缺少冻结输入/输入摘要")
        return report
    try:
        frozen = json.loads(frozen_raw)
    except (TypeError, ValueError) as exc:
        report.broken.append(f"结果 {rid} frozen_inputs 解析失败：{exc}")
        return report

    claim = None
    source = None
    for qual_id in qual_refs:
        qual = case.fetch_one("qualifications", "qual_id", qual_id)
        if qual:
            claim = case.fetch_one("claims", "claim_id", qual["claim_id"])
            if claim:
                source = case.fetch_one("sources", "source_id",
                                        claim["source_id"])
            break
    if claim is None:
        report.broken.append(f"结果 {rid} 无法经资格链解析到主张/来源")
        return report

    # 1) 冻结输入摘要自洽（frozen + 当前主张行 → digest）
    recomputed = run_input_digest_v3(frozen, claim)
    if recomputed != stored_digest:
        report.broken.append(
            f"结果 {rid} 完整输入摘要不一致（存档 {stored_digest[:12]}… vs 重算 "
            f"{recomputed[:12]}…）：冻结输入或主张内容在求值后被改动")
    # 2) 主张内容摘要（解释/主体/定位/摘录文本篡改可检）
    if claim.get("content_digest"):
        if claim_content_digest(claim) != claim["content_digest"]:
            report.broken.append(
                f"主张 {claim['claim_id']} 内容摘要不一致（冻结后被改动）")

    # 3) 来源判断字段未被改动（C3/C7）
    frozen_source = frozen.get("source_inputs") or {}
    if source is not None:
        # 发布前与读时同用这一段：原件可读/hash、登记长度、主张定位、
        # 摘录hash/文本都必须重新核验，不能由 trace 单独补查。
        try:
            source_bytes = blobs.read_bytes(source["blob_sha256"])
        except (OSError, StoreIntegrityError, KeyError) as exc:
            report.broken.append(
                f"来源 {source['source_id']} 封存原字节不可读/复核失败：{exc}")
            source_bytes = None
        if source_bytes is not None:
            if len(source_bytes) != source["byte_length"]:
                report.broken.append(f"来源 {source['source_id']} 字节长度不符")
            if claim["locator_kind"] == "byte_range":
                start, end = claim["locator_start"], claim["locator_end"]
                if not (isinstance(start, int) and isinstance(end, int)
                        and 0 <= start < end <= len(source_bytes)):
                    report.broken.append(
                        f"主张 {claim['claim_id']} 定位越界 [{start},{end})")
                else:
                    excerpt = source_bytes[start:end]
                    if sha256_hex(excerpt) != claim["excerpt_sha256"]:
                        report.broken.append(
                            f"主张 {claim['claim_id']} 摘录hash与封存原字节不符")
                    if (sha256_hex(
                            (claim.get("excerpt_text") or "").encode("utf-8"))
                            != claim["excerpt_sha256"]):
                        report.broken.append(
                            f"主张 {claim['claim_id']} 摘录文本与登记hash不符")
            else:
                projection_problem = _verify_projection(claim, source_bytes)
                if projection_problem:
                    report.broken.append(
                        f"主张 {claim['claim_id']} {projection_problem}")
        for field in ("published_at", "retrieved_at", "source_family",
                      "capture_status", "document_subject"):
            if source.get(field) != frozen_source.get(field):
                report.broken.append(
                    f"来源 {source['source_id']} 判断字段 {field} 与冻结值不符"
                    f"（当前 {source.get(field)!r} vs 冻结 "
                    f"{frozen_source.get(field)!r}）：发布后来源被改动")
        # 4) 所用时间证据修订快照未变；新修订不破坏旧结果
        rev = frozen_source.get("time_evidence_revision")
        snapshot = frozen_source.get("time_evidence_snapshot")
        if rev is not None:
            history = {e["revision"]: {k: v for k, v in e.items()
                                       if k not in ("revision", "created_at")}
                       for e in case.get_time_evidence_history(
                           source["source_id"])}
            if rev not in history:
                report.broken.append(
                    f"来源 {source['source_id']} 的时间证据修订 {rev} 缺失"
                    "（不可变版本被破坏）")
            elif history[rev] != snapshot:
                report.broken.append(
                    f"来源 {source['source_id']} 的时间证据修订 {rev} 内容被改动")
        if snapshot is None and source.get("time_evidence"):
            report.broken.append(
                f"来源 {source['source_id']} 存在列级时间证据但未冻结修订快照")

    # 5) 资格记录状态与内容（C4）
    if result_row.get("product_status") == "succeeded":
        for qual_id in qual_refs:
            qual = case.fetch_one("qualifications", "qual_id", qual_id)
            if qual is None:
                report.broken.append(f"资格 {qual_id} 引用断裂")
                continue
            if qual["status"] != "qualified":
                report.broken.append(
                    f"结果 {rid} 为 succeeded 但资格 {qual_id} 状态为 "
                    f"{qual['status']}（不再支持成功发布）")
            frozen_qd = frozen.get("qualification_digest")
            if frozen_qd and qualification_content_digest(qual) != frozen_qd:
                report.broken.append(
                    f"资格 {qual_id} 内容与冻结摘要不一致（判断依据被改动）")

    # 6) 映射记录存在且引文绑定封存原文（C5）
    mapping_rows = case.fetch_mappings(claim["claim_id"],
                                       result_row["criterion_id"])
    frozen_mapping = frozen.get("mapping") or {}
    if frozen_mapping.get("status") in ("confirmed", "candidate"):
        if not mapping_rows:
            report.broken.append(
                f"主张 {claim['claim_id']} 的判据映射记录缺失（被删除）")
        else:
            latest = mapping_rows[-1]
            if latest["quote_sha256"] != frozen_mapping.get("quote_sha256"):
                report.broken.append(
                    f"映射记录引文hash与冻结值不符（{latest['quote_sha256'][:12]}… "
                    f"vs {str(frozen_mapping.get('quote_sha256'))[:12]}…）")
            if result_row.get("product_status") == "succeeded" \
                    and frozen_mapping.get("status") != "confirmed":
                report.broken.append(
                    f"结果 {rid} 为 succeeded 但冻结映射状态为 "
                    f"{frozen_mapping.get('status')}（候选不构成支持关系）")
        if source is not None and frozen_mapping.get("quote_sha256"):
            excerpt = _excerpt_bytes_for_claim(blobs, claim, source)
            if excerpt is None:
                report.broken.append("无法取封存摘录以核验映射引文")
            else:
                qs = frozen_mapping.get("quote_start")
                qe = frozen_mapping.get("quote_end")
                if not (isinstance(qs, int) and isinstance(qe, int)
                        and 0 <= qs < qe <= len(excerpt)) \
                        or sha256_hex(excerpt[qs:qe]) \
                        != frozen_mapping["quote_sha256"]:
                    report.broken.append(
                        "映射引文不再逐字位于封存摘录（引文/原文绑定破坏）")
        confirmation = frozen_mapping.get("confirmation")
        if isinstance(confirmation, dict) and confirmation.get("_controlled") is True:
            review = case.get_mapping_review(confirmation.get("review_id"))
            if review is None:
                report.broken.append(
                    f"映射受控复核 {confirmation.get('review_id')!r} 缺失")
            else:
                for field in ("case_basis_version", "claim_id", "criterion_id",
                              "quote_sha256", "decision", "reviewer",
                              "review_basis", "support_scope"):
                    if review.get(field) != confirmation.get(field):
                        report.broken.append(
                            f"映射受控复核 {confirmation.get('review_id')} 的 {field}"
                            " 与冻结值不一致")
                        break

    # 7) CaseBasis 版本快照完整一致（C6：全字段，不只名称/截止）
    bound_version = result_row.get("case_basis_version")
    if bound_version is not None:
        snapshot_row = case.get_case_basis_version(bound_version)
        if snapshot_row is None:
            report.broken.append(
                f"结果 {rid} 绑定的 CaseBasis 版本 {bound_version} 快照缺失")
        else:
            stored_snapshot = {k: v for k, v in snapshot_row.items()
                               if k != "version"}
            frozen_basis = frozen.get("case_basis") or {}
            if stored_snapshot != frozen_basis:
                differing = sorted(
                    k for k in set(stored_snapshot) | set(frozen_basis)
                    if stored_snapshot.get(k) != frozen_basis.get(k))
                report.broken.append(
                    f"结果 {rid} 的 CaseBasis 版本 {bound_version} 快照与冻结输入"
                    f"不一致（差异字段：{differing}）")

    # 8) 主体依据不能只冻结路径字符串：解析到实际的 case_provenance 原件、
    # 字段和值后，重建的绑定必须与发布时相同。旧R1.3结果没有该字段，保留
    # 只读兼容；R1.4的新结果缺失或断裂时可见失败。
    bindings = frozen.get("case_provenance_bindings")
    if bindings is not None:
        subject_binding = bindings.get("subject") if isinstance(bindings, dict) else None
        if not isinstance(subject_binding, dict):
            report.broken.append(f"结果 {rid} 缺少主体封存证明绑定")
        else:
            try:
                from .qualification import resolve_case_field_reference_binding

                subject_ref = json.loads(
                    (frozen.get("case_basis") or {}).get("subject_source_basis") or "")
                _value, error, current_binding = resolve_case_field_reference_binding(
                    subject_ref, case, blobs,
                    expect_value=(frozen.get("case_basis") or {}).get(
                        "subject_legal_name"))
            except (TypeError, ValueError) as exc:
                error, current_binding = f"主体依据解析失败：{exc}", None
            if error or current_binding != subject_binding:
                report.broken.append(
                    f"主体封存证明闭包不一致：{error or '原件/字段/值绑定已变更'}")
        aliases_binding = bindings.get("aliases") if isinstance(bindings, dict) else None
        if aliases_binding is not None:
            try:
                from .qualification import resolve_case_basis_proof_bindings

                current_bindings, aliases_error = resolve_case_basis_proof_bindings(
                    frozen.get("case_basis") or {}, case, blobs)
            except (TypeError, ValueError) as exc:
                current_bindings, aliases_error = None, str(exc)
            if aliases_error or not isinstance(current_bindings, dict) \
                    or current_bindings.get("aliases") != aliases_binding:
                report.broken.append(
                    "主体别名封存证明闭包不一致："
                    f"{aliases_error or '原件/关系/字段/值绑定已变更'}")

    # 9) runner 实际消费的第一方归属、时区规则等关系证明也必须能重建。
    proof_bindings = frozen.get("proof_bindings")
    if isinstance(proof_bindings, dict):
        frozen_basis = frozen.get("case_basis") or {}
        document_binding = proof_bindings.get("document_subject")
        if document_binding is not None and source is not None:
            try:
                from .qualification import resolve_document_subject_proof_bindings

                current, document_error = resolve_document_subject_proof_bindings(
                    source, frozen_basis.get("subject_legal_name"), case, blobs)
            except (TypeError, ValueError) as exc:
                current, document_error = None, str(exc)
            if document_error or current != document_binding:
                report.broken.append(
                    "第一方归属证明闭包不一致："
                    f"{document_error or '关系/原件/字段/值绑定已变更'}")
        timezone_binding = proof_bindings.get("timezone")
        if timezone_binding is not None and source is not None:
            try:
                from .qualification import resolve_timezone_rule_binding

                frozen_time = (frozen.get("source_inputs") or {}).get(
                    "time_evidence_snapshot")
                current_source = {**source, "time_evidence": frozen_time}
                _tz, current, timezone_error = resolve_timezone_rule_binding(
                    frozen_time or {}, current_source, case, blobs)
            except (TypeError, ValueError) as exc:
                current, timezone_error = None, str(exc)
            if timezone_error or current != timezone_binding:
                report.broken.append(
                    "时区规则证明闭包不一致："
                    f"{timezone_error or '规则/适用域/关系绑定已变更'}")

    # 10) 合法 N/A 的政策字段必须仍能从封存对象重建，不能只相信冻结 JSON。
    na_proposal = frozen.get("na_proposal")
    if isinstance(na_proposal, dict) and na_proposal.get("proposal") == "not_applicable":
        from .qualification import resolve_case_field_reference_binding

        current_bindings = []
        for ref_key, resolved_key in (("applicability_ref", "applicability_resolved"),
                                      ("flag_ref", "flag_resolved"),
                                      ("subject_ref", "subject_resolved")):
            expected = na_proposal.get(resolved_key)
            if not isinstance(expected, dict) or not expected.get("_resolved"):
                continue
            _value, error, current = resolve_case_field_reference_binding(
                na_proposal.get(ref_key), case, blobs)
            actual = {"_resolved": error is None, "value": _value,
                      "path": (na_proposal.get(ref_key) or {}).get("path"),
                      "binding": current}
            if error or actual != expected:
                report.broken.append(
                    f"N/A 封存字段 {ref_key} 与冻结解析结果不一致")
            else:
                current_bindings.append(current)
        if len(current_bindings) == 3:
            from .qualification import _same_record_relation

            relation_ok, relation_error = _same_record_relation(*current_bindings)
            if na_proposal.get("relation_valid") is not True or not relation_ok:
                report.broken.append(
                    "N/A 政策关系证明闭包不一致："
                    f"{relation_error or na_proposal.get('relation_error') or '关系未冻结'}")

    # 11) 正向结果链（空链/非法N/A）
    na_raw = result_row.get("na_basis")
    na_valid = False
    if na_raw:
        try:
            na = json.loads(na_raw)
            na_valid = (isinstance(na, dict)
                        and str(na.get("basis") or "").strip()
                        and str(na.get("case_flag_source") or "").strip())
        except (TypeError, ValueError):
            na_valid = False
    if result_row.get("product_status") == "succeeded" and not qual_refs \
            and not na_valid:
        report.broken.append(
            f"结果 {rid} 为 succeeded 但资格引用为空且无合法 N/A 依据")
    if result_row.get("product_status") == "succeeded" and source is not None \
            and not na_valid:
        evidence_refs = _load_json(result_row.get("evidence_refs"))
        if set(evidence_refs) != {source["source_id"]}:
            report.broken.append(
                f"结果 {rid} 的 Evidence 集合与资格→主张→来源闭包不符："
                f"列出 {sorted(set(evidence_refs))}，闭包 {[source['source_id']]}")
    return report


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

    # R1.3-C: binding verification (shared with pre-publish verification)
    binding = verify_result_bindings(case, blobs, result)
    for item in binding.broken:
        if item not in report.broken:
            report.broken.append(item)

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
