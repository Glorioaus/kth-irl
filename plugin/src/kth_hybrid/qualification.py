"""资格判断：主张候选 → Qualification 或 Gap（一等对象）。

四类判断（每类必须给依据，不允许只填"valid"）：
- source：来源允许用途（空502/搜索摘要类来源不支持内容主张）
- identity：法定主体/作者/发布者匹配分开判断（行业综述≠主体自身能力证明）
- time：截止、事件、发布、抓取时间分开（晚抓取且无发布证明不提升资格）
- independence：来源族/转载关系（同正文hash不增加独立证据数）

候选中的 ``eligible=true`` 不能决定返回值。没有可靠规则依据的场景进入
``needs_review``，不得由模型自由写"已核实"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import sha256_hex
from .store import BlobStore, StoreIntegrityError

VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"

RULE_VERSION = "kth-hybrid.qualification.v1"


@dataclass(frozen=True)
class Judgment:
    verdict: str  # ok / fail / unknown
    basis: str


@dataclass
class QualificationOutcome:
    claim_id: str
    source_judgment: Judgment
    identity_judgment: Judgment
    time_judgment: Judgment
    independence_judgment: Judgment
    allowed_uses: list[str] = field(default_factory=list)
    cannot_prove: list[str] = field(default_factory=list)
    status: str = "needs_review"
    rule_version: str = RULE_VERSION
    review_attempt: str = ""

    @property
    def verdicts(self) -> list[Judgment]:
        return [self.source_judgment, self.identity_judgment, self.time_judgment,
                self.independence_judgment]


@dataclass
class GapOutcome:
    claim_id: str
    gap_type: str
    affected_criteria: list[str] = field(default_factory=list)
    pipeline_fault: bool = False
    investigation: str = ""
    unconfirmed: list[str] = field(default_factory=list)


def _default_policy() -> dict[str, Any]:
    return {
        # 仅可用于"来源发现"，正文不视为原文的来源族
        "discovery_only_families": {"search_summary", "aggregator_snippet"},
        # 主体第一方材料（Owner 附件/官方渠道）
        "first_party_families": {"owner_attachment", "company-official"},
        # 零字节失败采集状态
        "failed_capture_statuses": {"blocked"},
    }


def qualify_claim(claim: dict, source: dict, blobs: BlobStore, case_basis: dict,
                  source_policy: dict | None = None, *,
                  review_attempt: str = "",
                  same_body_sources: int = 1) -> QualificationOutcome | GapOutcome:
    """对单条主张候选做四类资格判断。

    ``claim``/``source`` 为 CaseStore 行（dict）。``same_body_sources`` 为与该来源
    共享正文 hash 的来源数（由调用方从盘点提供，默认 1）。
    """
    policy = _default_policy()
    if source_policy:
        policy.update(source_policy)
    subject = case_basis["subject_legal_name"]
    aliases = set(case_basis.get("subject_aliases", []))
    cutoff = case_basis["evidence_cutoff"]

    # ---- 定位核验（失败 → Gap，不伪装资格结论）----
    try:
        data = blobs.read_bytes(source["blob_sha256"])
    except (OSError, StoreIntegrityError) as exc:
        return GapOutcome(
            claim_id=claim["claim_id"], gap_type="original_bytes_unreadable",
            pipeline_fault=False,
            investigation=f"封存原字节不可读/复核失败：{exc}",
            unconfirmed=["原件是否被篡改或损坏"],
        )

    # ---- 1) 来源判断（空正文/失败采集先判定：保留失败类型，不可能有合法定位）----
    capture_status = (source.get("capture_status") or "").split("/")[0]
    family = source.get("source_family") or "unknown"
    allowed_uses: list[str] = []
    cannot_prove: list[str] = []
    if len(data) == 0 or capture_status in policy["failed_capture_statuses"] \
            or (source.get("capture_status") or "").endswith("empty_body"):
        empty_outcome = QualificationOutcome(
            claim_id=claim["claim_id"],
            source_judgment=Judgment(
                VERDICT_FAIL,
                f"失败采集（capture_status={source.get('capture_status')}，"
                f"正文 {len(data)} 字节），不构成证据来源",
            ),
            identity_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            time_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            independence_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            cannot_prove=["任何正向判据支持"],
            review_attempt=review_attempt,
        )
        empty_outcome.status = "rejected"
        return empty_outcome

    # ---- 定位核验（失败 → Gap，不伪装资格结论）----
    start, end = claim.get("locator_start"), claim.get("locator_end")
    locator_kind = claim.get("locator_kind")
    if locator_kind == "byte_range":
        if not (isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(data)):
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="invalid_locator",
                pipeline_fault=False,
                investigation=f"定位非法：kind={locator_kind} 区间=[{start},{end})，"
                              f"对象长度={len(data)}",
                unconfirmed=["主张定位是否被篡改"],
            )
        excerpt_bytes = data[start:end]
        if sha256_hex(excerpt_bytes) != claim["excerpt_sha256"]:
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="excerpt_hash_mismatch",
                pipeline_fault=False,
                investigation="摘录 hash 与封存原字节区间不一致",
                unconfirmed=["原文或摘录是否被篡改"],
            )
        excerpt_text = excerpt_bytes.decode("utf-8", errors="replace")
    elif locator_kind in ("pdf_page", "zip_member"):
        # 抽影定位：原件 hash + 页面/成员投影重核，不假装文本偏移是原字节偏移
        from .audit import _verify_projection

        problem = _verify_projection(claim, data)
        if problem:
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="projection_mismatch",
                pipeline_fault=False,
                investigation=f"抽取投影核验失败：{problem}",
                unconfirmed=["原文或投影是否被篡改"],
            )
        excerpt_text = claim.get("excerpt_text") or ""
    else:
        return GapOutcome(
            claim_id=claim["claim_id"], gap_type="invalid_locator",
            pipeline_fault=False,
            investigation=f"未知定位类型：{locator_kind}",
            unconfirmed=["主张定位是否被篡改"],
        )

    if family in policy["discovery_only_families"]:
        source_judgment = Judgment(
            VERDICT_FAIL,
            f"来源族 {family} 仅支持来源发现（搜索摘要类），正文不自动视为原文",
        )
        allowed_uses.append("source_discovery")
    else:
        source_judgment = Judgment(
            VERDICT_OK,
            f"来源族 {family}，capture_status={source.get('capture_status')}，"
            "正文与封存字节一致",
        )

    # ---- 2) 身份判断（第一方 / 第三方提及 / 未提及分开）----
    mentions_subject = subject in excerpt_text or any(
        alias in excerpt_text for alias in aliases
    )
    is_first_party = family in policy["first_party_families"]
    subject_scoped = subject[:2] in (claim.get("subject_scope") or "")
    if is_first_party:
        identity_judgment = Judgment(
            VERDICT_OK,
            f"第一方材料（{family}）：主体自身文件，支持'主体自述'类主张",
        )
        allowed_uses.append("company_self_statement")
        cannot_prove.append("自述内容的独立核实")
    elif mentions_subject:
        identity_judgment = Judgment(
            VERDICT_OK,
            f"第三方来源（{family}）正文明确提及主体'{subject}'；"
            "仅支持'该来源载明…'类主张，不支持主体自身能力证明",
        )
        allowed_uses.append("third_party_reported_fact")
    elif subject_scoped:
        identity_judgment = Judgment(
            VERDICT_FAIL,
            f"正文未提及主体（行业综述/通用内容），不能证明主体范围主张"
            f"（subject_scope={claim.get('subject_scope')}）",
        )
    else:
        identity_judgment = Judgment(
            VERDICT_UNKNOWN, "正文未提及主体且主张非主体范围，需人工核对主张范围"
        )

    # ---- 3) 时间判断（截止/事件/发布/抓取分开）----
    published_at = source.get("published_at")
    retrieved_at = source.get("retrieved_at")
    provenance = source.get("published_at_provenance") or ""
    time_evidence = source.get("time_evidence") or {}
    if published_at:
        if published_at <= cutoff:
            time_judgment = Judgment(
                VERDICT_OK, f"发布时间 {published_at} ≤ 截止 {cutoff}（{provenance}）"
            )
        else:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"发布时间 {published_at} 晚于截止 {cutoff}，不支持该时点正向结论",
            )
    elif time_evidence.get("kind") == "document_self_date":
        time_judgment = Judgment(
            VERDICT_OK,
            f"文档自述日期 {time_evidence.get('date')}（{time_evidence.get('basis')}），"
            f"早于截止 {cutoff}；登记时间 {time_evidence.get('registered_at', '未知')}。"
            "仅支持'截至自述日期文档如此载明'类主张",
        )
        allowed_uses.append("document_dated_statement")
        cannot_prove.append("自述日期之后文档是否变更")
    elif retrieved_at and retrieved_at > cutoff:
        time_judgment = Judgment(
            VERDICT_FAIL,
            f"抓取 {retrieved_at} 晚于截止 {cutoff} 且无发布时间证明"
            f"（{provenance or 'receipt 仅含抓取时钟'}），不提升资格",
        )
        cannot_prove.append("内容在证据截止前已存在")
    else:
        time_judgment = Judgment(
            VERDICT_UNKNOWN, f"发布时间未知（{provenance}），需补证后复审"
        )

    # ---- 4) 独立性判断（同hash不增加独立证据数）----
    if same_body_sources > 1:
        independence_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"同一正文 hash 出现于 {same_body_sources} 条捕获；"
            "转载/同源关系未审，不作为独立佐证",
        )
        cannot_prove.append("来源独立性（同hash组）")
    else:
        independence_judgment = Judgment(
            VERDICT_OK,
            "单一来源窄主张（不用于独立佐证计数；来源族独立性未做全面审查）",
        )
        cannot_prove.append("跨来源独立佐证")

    outcome = QualificationOutcome(
        claim_id=claim["claim_id"],
        source_judgment=source_judgment,
        identity_judgment=identity_judgment,
        time_judgment=time_judgment,
        independence_judgment=independence_judgment,
        allowed_uses=allowed_uses,
        cannot_prove=cannot_prove,
        review_attempt=review_attempt,
    )
    verdicts = [j.verdict for j in outcome.verdicts]
    if VERDICT_FAIL in verdicts:
        outcome.status = "rejected"
    elif VERDICT_UNKNOWN in verdicts:
        outcome.status = "needs_review"
    else:
        outcome.status = "qualified"
    return outcome
