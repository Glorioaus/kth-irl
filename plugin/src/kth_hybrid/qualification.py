"""资格判断 v2：主张候选 → Qualification 或 Gap（一等对象）。

R1.1 修复（验收 R1-01）：
- **三种含义分离**：``Owner提供的材料``（family=owner_attachment，只说明提供方）
  ≠ ``某主体的第一方材料``（需 ``document_subject`` 与评估主体一致且有依据）
  ≠ ``文档记载了某主体``（正文提及，仅支持"该文档载明…"类主张）。
- **主体来自有源 CaseBasis**：``subject_source_basis`` 必填；未核实的实体关系
  不默认相同；scope 匹配用完整名称/别名，不用前缀。
- **时间严格化**：ISO 日期解析（拒绝字符串比较）；``document_self_date`` 必须
  有合法日期＋定位依据＋不晚于截止；``filename_derived_date`` 仅候选（永不据其
  qualified）；``registered_at`` 晚于截止即不证明截止前存在；未来/缺失/非法
  日期一律 fail。
- **来源族白名单**：未知族不默认 ok。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .contracts import sha256_hex
from .store import BlobStore, StoreIntegrityError

VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"

RULE_VERSION = "kth-hybrid.qualification.v2"

# 已知来源族白名单（未知族 → 来源判断 unknown，不得默认放行）
KNOWN_FAMILIES = {
    "owner_attachment",          # Owner 提供（提供方事实，不代表主体归属）
    "company-official",          # 主体官方渠道
    "news-media",                # 第三方新闻/报道
    "public_original_page",      # 原版采集 provider id 之一
    "patent-office",             # 专利局
}


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


# ---- 日期解析（严格；不做字符串比较） ----

def parse_iso_datetime(value: Any) -> datetime | None:
    """解析 ISO 日期/时间（含 Z）；无效返回 None。统一为 aware UTC。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _default_policy() -> dict[str, Any]:
    return {
        "discovery_only_families": {"search_summary", "aggregator_snippet"},
        "first_party_families": {"owner_attachment", "company-official"},
        "failed_capture_statuses": {"blocked"},
    }


def qualify_claim(claim: dict, source: dict, blobs: BlobStore, case_basis: dict,
                  source_policy: dict | None = None, *,
                  review_attempt: str = "",
                  same_body_sources: int = 1) -> QualificationOutcome | GapOutcome:
    """对单条主张候选做四类资格判断。

    ``case_basis`` 必须含 ``subject_source_basis``（主体与截止的登记依据）；
    ``source`` 可携带 ``document_subject``/``document_subject_basis``（文档自识
    主体及其依据）与 ``time_evidence``（结构化时间证据）。
    """
    if not case_basis.get("subject_source_basis", "").strip():
        raise ValueError(
            "case_basis 缺少 subject_source_basis：主体/截止必须有登记依据，"
            "不接受无源硬编码"
        )
    policy = _default_policy()
    if source_policy:
        policy.update(source_policy)
    subject = case_basis["subject_legal_name"]
    aliases = set(case_basis.get("subject_aliases", []))
    cutoff_dt = parse_iso_datetime(case_basis["evidence_cutoff"])
    if cutoff_dt is None:
        raise ValueError(
            f"case_basis.evidence_cutoff 非法：{case_basis.get('evidence_cutoff')!r}"
        )

    # ---- 原字节与定位核验（失败 → Gap）----
    try:
        data = blobs.read_bytes(source["blob_sha256"])
    except (OSError, StoreIntegrityError, KeyError) as exc:
        return GapOutcome(
            claim_id=claim["claim_id"], gap_type="original_bytes_unreadable",
            investigation=f"封存原字节不可读/复核失败：{exc}",
            unconfirmed=["原件是否被篡改或损坏"],
        )

    capture_status_raw = source.get("capture_status") or ""
    capture_status = capture_status_raw.split("/")[0]
    family = source.get("source_family") or "unknown"
    allowed_uses: list[str] = []
    cannot_prove: list[str] = []

    # ---- 1) 来源判断（空正文/失败采集/白名单外族）----
    if len(data) == 0 or capture_status in policy["failed_capture_statuses"] \
            or capture_status_raw.endswith("empty_body"):
        outcome = QualificationOutcome(
            claim_id=claim["claim_id"],
            source_judgment=Judgment(
                VERDICT_FAIL,
                f"失败采集（capture_status={capture_status_raw}，"
                f"正文 {len(data)} 字节），不构成证据来源",
            ),
            identity_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            time_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            independence_judgment=Judgment(VERDICT_UNKNOWN, "无正文可判"),
            cannot_prove=["任何正向判据支持"],
            review_attempt=review_attempt,
        )
        outcome.status = "rejected"
        return outcome

    locator_kind = claim.get("locator_kind")
    start, end = claim.get("locator_start"), claim.get("locator_end")
    if locator_kind == "byte_range":
        if not (isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(data)):
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="invalid_locator",
                investigation=f"定位非法：kind={locator_kind} 区间=[{start},{end})，"
                              f"对象长度={len(data)}",
                unconfirmed=["主张定位是否被篡改"],
            )
        excerpt_bytes = data[start:end]
        if sha256_hex(excerpt_bytes) != claim["excerpt_sha256"]:
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="excerpt_hash_mismatch",
                investigation="摘录 hash 与封存原字节区间不一致",
                unconfirmed=["原文或摘录是否被篡改"],
            )
        excerpt_text = excerpt_bytes.decode("utf-8", errors="replace")
    elif locator_kind in ("pdf_page", "zip_member"):
        from .audit import _verify_projection

        problem = _verify_projection(claim, data)
        if problem:
            return GapOutcome(
                claim_id=claim["claim_id"], gap_type="projection_mismatch",
                investigation=f"抽取投影核验失败：{problem}",
                unconfirmed=["原文或投影是否被篡改"],
            )
        excerpt_text = claim.get("excerpt_text") or ""
    else:
        return GapOutcome(
            claim_id=claim["claim_id"], gap_type="invalid_locator",
            investigation=f"未知定位类型：{locator_kind}",
            unconfirmed=["主张定位是否被篡改"],
        )

    if family in policy["discovery_only_families"]:
        source_judgment = Judgment(
            VERDICT_FAIL,
            f"来源族 {family} 仅支持来源发现（搜索摘要类），正文不自动视为原文",
        )
        allowed_uses.append("source_discovery")
    elif family not in KNOWN_FAMILIES:
        source_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"来源族 {family!r} 不在已知白名单 {sorted(KNOWN_FAMILIES)}，"
            "需人工核定该族的允许用途",
        )
    else:
        source_judgment = Judgment(
            VERDICT_OK,
            f"已知来源族 {family}，capture_status={capture_status_raw}，"
            "正文与封存字节一致",
        )

    # ---- 2) 身份判断（三含义分离）----
    mentions_subject = subject in excerpt_text or any(
        alias in excerpt_text for alias in aliases
    )
    document_subject = (source.get("document_subject") or "").strip()
    document_subject_basis = (source.get("document_subject_basis") or "").strip()
    subject_names = {subject, *aliases}
    scope = (claim.get("subject_scope") or "").strip()
    scope_matches_subject = any(name in scope for name in subject_names) and scope

    if family in policy["first_party_families"]:
        if document_subject and document_subject in subject_names:
            identity_judgment = Judgment(
                VERDICT_OK,
                f"第一方材料：文档自识主体'{document_subject}'"
                f"（依据：{document_subject_basis or '未登记'}）与评估主体一致，"
                "支持'主体自述'类主张",
            )
            allowed_uses.append("company_self_statement")
            cannot_prove.append("自述内容的独立核实")
        elif document_subject:
            identity_judgment = Judgment(
                VERDICT_UNKNOWN,
                f"Owner提供材料，但文档自识主体'{document_subject}'"
                f"（依据：{document_subject_basis or '未登记'}）≠评估主体"
                f"'{subject}'；两实体关系未核验，不得按第一方自述使用",
            )
            cannot_prove.append(
                f"文档主体（{document_subject}）与评估主体（{subject}）的实体关系")
            if mentions_subject:
                identity_judgment = Judgment(
                    VERDICT_UNKNOWN,
                    f"Owner提供材料且正文提及评估主体'{subject}'，但文档自识主体"
                    f"为'{document_subject}'（实体关系未核验）；仅可作'文档记载'"
                    f"类候选，需人工确认主体关系",
                )
        else:
            identity_judgment = Judgment(
                VERDICT_UNKNOWN,
                "Owner提供材料，但未登记文档自识主体（document_subject）；"
                "不能仅凭提供方式认定第一方，需人工核定",
            )
            cannot_prove.append("文档归属主体")
    elif mentions_subject:
        identity_judgment = Judgment(
            VERDICT_OK,
            f"第三方来源（{family}）正文明确提及主体'{subject}'；"
            "仅支持'该来源载明…'类主张，不支持主体自身能力证明",
        )
        allowed_uses.append("third_party_reported_fact")
    elif scope_matches_subject:
        identity_judgment = Judgment(
            VERDICT_FAIL,
            f"正文未提及主体（行业综述/通用内容），不能证明主体范围主张"
            f"（subject_scope={scope}）",
        )
    else:
        identity_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"正文未提及主体且主张范围（{scope or '空'}）未对应评估主体，"
            "需人工核对主张范围",
        )

    # ---- 3) 时间判断（严格解析；四类时间分列）----
    time_evidence = source.get("time_evidence") or {}
    published_at = source.get("published_at")
    retrieved_at = source.get("retrieved_at")

    def _dt(value) -> datetime | None:
        return parse_iso_datetime(value)

    if published_at is not None:
        published_dt = _dt(published_at)
        if published_dt is None:
            time_judgment = Judgment(
                VERDICT_FAIL, f"发布时间无法解析：{published_at!r}")
        elif published_dt > cutoff_dt:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"发布时间 {published_at} 晚于截止 {case_basis['evidence_cutoff']}，"
                "不支持该时点正向结论",
            )
        else:
            time_judgment = Judgment(
                VERDICT_OK,
                f"发布时间 {published_at} ≤ 截止（"
                f"{source.get('published_at_provenance') or '无来源说明'}）",
            )
    elif time_evidence.get("kind") == "document_self_date":
        doc_dt = _dt(time_evidence.get("date"))
        locator_proof = time_evidence.get("date_locator")
        if doc_dt is None:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"文档自述日期缺失或非法：{time_evidence.get('date')!r}",
            )
        elif not locator_proof:
            time_judgment = Judgment(
                VERDICT_FAIL,
                "文档自述日期缺少定位依据（date_locator），不能作为时点证明",
            )
        elif doc_dt > cutoff_dt:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"文档自述日期 {time_evidence.get('date')} 晚于截止 "
                f"{case_basis['evidence_cutoff']}",
            )
        else:
            time_judgment = Judgment(
                VERDICT_OK,
                f"文档自述日期 {time_evidence.get('date')}（定位：{locator_proof}；"
                f"{time_evidence.get('basis', '')}）≤ 截止。"
                "仅支持'截至自述日期文档如此载明'类主张",
            )
            allowed_uses.append("document_dated_statement")
            cannot_prove.append("自述日期之后文档是否变更")
    elif time_evidence.get("kind") == "filename_derived_date":
        time_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"文件名推定日期 {time_evidence.get('date')!r}（"
            f"{time_evidence.get('basis', '')}）仅为候选，不构成正文发布日期，"
            "不能证明截止前存在",
        )
        cannot_prove.append("文档在证据截止前已存在")
    elif time_evidence.get("kind") == "registered_at":
        reg_dt = _dt(time_evidence.get("date"))
        if reg_dt is not None and reg_dt <= cutoff_dt:
            time_judgment = Judgment(
                VERDICT_OK,
                f"登记时间 {time_evidence.get('date')} ≤ 截止，"
                f"以登记时间为入池时点证明（{time_evidence.get('basis', '')}）",
            )
        else:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"登记时间 {time_evidence.get('date')} 晚于截止"
                f"{case_basis['evidence_cutoff']}，不能证明截止前存在",
            )
        cannot_prove.append("文档发布/成文时间本身")
    elif retrieved_at is not None:
        retrieved_dt = _dt(retrieved_at)
        if retrieved_dt is None:
            time_judgment = Judgment(VERDICT_FAIL,
                                     f"抓取时间无法解析：{retrieved_at!r}")
        elif retrieved_dt > cutoff_dt:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"抓取 {retrieved_at} 晚于截止 {case_basis['evidence_cutoff']} "
                f"且无发布时间证明（"
                f"{source.get('published_at_provenance') or 'receipt 仅含抓取时钟'}），"
                "不提升资格",
            )
            cannot_prove.append("内容在证据截止前已存在")
        else:
            time_judgment = Judgment(
                VERDICT_OK,
                f"抓取时间 {retrieved_at} ≤ 截止（以抓取为入池时点；发布时间仍未证）",
            )
            cannot_prove.append("内容发布时间早于抓取")
    else:
        time_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"发布/成文时间未知（{source.get('published_at_provenance') or '无'}），"
            "需补证后复审",
        )

    # ---- 4) 独立性判断 ----
    if same_body_sources > 1:
        independence_judgment = Judgment(
            VERDICT_UNKNOWN,
            f"同一正文 hash 出现于 {same_body_sources} 条采集实例；"
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
