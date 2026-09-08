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

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .contracts import sha256_hex
from .store import BlobStore, StoreIntegrityError

VERDICT_OK = "ok"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"

# ---- 依据核验（R1.2-A：引用的材料必须真实存在、定位可解析、内容支持该字段）----

def _date_text_variants(value) -> list[str]:
    """由日期生成在正文中可能出现的文本形式（ISO/本地化/带时刻）。"""
    dt = parse_iso_datetime(value)
    if dt is None:
        return []
    y, m, d = dt.year, dt.month, dt.day
    variants = [
        f"{y:04d}-{m:02d}-{d:02d}", f"{y}-{m}-{d}", f"{y:04d}/{m:02d}/{d:02d}",
        f"{y}年{m}月{d}日",
    ]
    if (dt.hour, dt.minute, dt.second) != (0, 0, 0):
        for sep in (" ", "T"):
            variants.append(f"{y:04d}-{m:02d}-{d:02d}{sep}{dt.hour:02d}:{dt.minute:02d}")
            variants.append(
                f"{y:04d}-{m:02d}-{d:02d}{sep}{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
    return variants


def _resolve_locator_text(locator, data: bytes, blobs=None) -> tuple[str | None, str | None]:
    """解析日期/主体证明定位到实际文本。

    返回 (text, error)。byte_range：区间合法并解码；pdf_page：抽取投影页文本。
    字符串型 locator（不可解析的任意文字）→ 错误。
    """
    if isinstance(locator, str) or locator is None:
        return None, f"定位不可解析（期望结构化 {{kind,start,end}}/{{kind,page}}，" \
                     f"得到 {locator!r}）"
    if not isinstance(locator, dict) or "kind" not in locator:
        return None, f"定位结构非法：{locator!r}"
    if locator["kind"] == "byte_range":
        start, end = locator.get("start"), locator.get("end")
        if not (isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(data)):
            return None, f"byte_range [{start},{end}) 越界（对象长度 {len(data)}）"
        return data[start:end].decode("utf-8", errors="replace"), None
    if locator["kind"] == "pdf_page":
        try:
            from .intake import extract_pdf_pages

            projection = extract_pdf_pages(data)
        except Exception as exc:  # 抽取失败必须可见
            return None, f"PDF 投影抽取失败：{type(exc).__name__}: {exc}"
        page = locator.get("page")
        rows = [p for p in projection.locators if p["page"] == page]
        if not rows:
            return None, f"第 {page} 页不在抽取投影中（不存在或无文本层）"
        return rows[0]["text"], None
    return None, f"未知定位类型：{locator.get('kind')!r}"


def _resolve_registration_proof(proof, blobs) -> tuple[datetime | None, str | None]:
    """核验登记时间证明：封存记录可读、字段可解析且与声明一致。

    proof: {"blob_sha256": ..., "field": "a.b[0].c"}；返回 (记录中的时间, 错误)。
    """
    if not isinstance(proof, dict) or not proof.get("blob_sha256") \
            or not proof.get("field"):
        return None, "登记证明缺少 blob_sha256/field（不能凭空声明登记时间）"
    try:
        raw = blobs.read_bytes(proof["blob_sha256"])
    except (OSError, StoreIntegrityError, KeyError) as exc:
        return None, f"登记记录不可读：{exc}"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"登记记录解析失败：{exc}"
    node = record
    for token in re.split(r"\.|(\[|\])", proof["field"]):
        if token in (None, "", "[", "]"):
            continue
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None, f"登记记录字段路径失败于 {token!r}"
        elif isinstance(node, dict):
            if token not in node:
                return None, f"登记记录无字段 {token!r}"
            node = node[token]
        else:
            return None, f"登记记录字段路径失败于 {token!r}"
    if not isinstance(node, str):
        return None, "登记字段不是字符串时间"
    parsed = parse_iso_datetime(node)
    if parsed is None:
        return None, f"登记字段值不可解析为时间：{node!r}"
    return parsed, None


def _registration_binding_error(proof, source, blobs) -> str | None:
    """R1.3-A：登记证明必须绑定当前原件。

    接受两种绑定：proof.document_sha256 或登记记录自身 document_sha256，
    两者取其一时必须等于当前来源 blob；都没有绑定时拒绝（不能借用他人记录）。
    """
    if not isinstance(proof, dict) or not proof.get("blob_sha256"):
        return "登记证明缺少 blob_sha256"
    claimed = proof.get("document_sha256")
    if claimed is not None:
        if claimed != source["blob_sha256"]:
            return (f"登记证明绑定另一份文档（{claimed[:12]}… ≠ 当前原件 "
                    f"{source['blob_sha256'][:12]}…），不能借用")
        return None
    try:
        raw = blobs.read_bytes(proof["blob_sha256"])
        record = json.loads(raw.decode("utf-8"))
    except (OSError, StoreIntegrityError, KeyError, UnicodeDecodeError,
            ValueError) as exc:
        return f"登记记录不可读/解析失败：{exc}"
    if isinstance(record, dict) and "document_sha256" in record:
        if record["document_sha256"] != source["blob_sha256"]:
            return (f"登记记录绑定的文档（{str(record['document_sha256'])[:12]}…）"
                    f"≠ 当前原件（{source['blob_sha256'][:12]}…），不能借用")
        return None
    return "登记证明未绑定当前原件（缺少 document_sha256 绑定）"


def resolve_case_field_reference_binding(
        ref: dict, case, blobs: BlobStore, expect_value=None
) -> tuple[object, str | None, dict | None]:
    """解析 ``case:<file>#<json/path>`` 字段引用到封存原件的具体字段值。

    R1.3-A：JSON 形状不等于引用真实存在——必须找到对应 case_provenance 导入
    的封存 blob、解析 JSON、走到字段；``expect_value`` 给定时还须值一致。
    返回 ``(值, 错误, 封存对象/字段绑定)``。绑定用于让发布结果能重核
    实际消费的 provenance 对象，而不是只冻结调用方路径字符串。
    """
    if not isinstance(ref, dict) or ref.get("kind") != "field_reference"             or not ref.get("path"):
        return None, "引用必须是 {kind:'field_reference', path}", None
    raw_path = ref["path"]
    if not raw_path.startswith("case:") or "#" not in raw_path:
        return None, f"引用路径必须为 case:<file>#<json/path>，得到 {raw_path!r}", None
    file_part, field_path = raw_path[len("case:"):].split("#", 1)
    file_part = file_part.strip("/")
    if case is None:
        return None, "无可核验的Case库（引用无法解析到封存对象）", None
    record = None
    for row in case.fetch_all("import_records"):
        if row["kind"] == "case_provenance" and row["origin_path"].endswith(file_part):
            record = row
            break
    if record is None:
        return None, f"未找到封存的 {file_part}（case_provenance）", None
    try:
        raw = blobs.read_bytes(record["origin_sha256"])
    except (OSError, StoreIntegrityError, KeyError) as exc:
        return None, f"封存原件不可读：{exc}", None
    try:
        node = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"封存原件解析失败：{exc}", None
    for token in [t for t in re.split(r"/|\.|(\[|\])", field_path) if t]:
        token = token.strip("/")
        if token in ("[", "]", ""):
            continue
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None, f"字段路径失败于 {token!r}", None
        elif isinstance(node, dict):
            if token not in node:
                return None, f"封存原件无字段 {token!r}", None
            node = node[token]
        else:
            return None, f"字段路径失败于 {token!r}", None
    if expect_value is not None and node != expect_value:
        return None, (f"引用字段值 {node!r} 与期望值 {expect_value!r} 不一致"), None
    binding = {
        "path": raw_path,
        "origin_path": record["origin_path"],
        "origin_sha256": record["origin_sha256"],
        "field_path": field_path,
        "value_sha256": sha256_hex(
            json.dumps(node, ensure_ascii=False, sort_keys=True).encode("utf-8")),
    }
    return node, None, binding


def _resolve_case_field_reference(ref: dict, case, blobs: BlobStore,
                                  expect_value=None) -> tuple[object, str | None]:
    """兼容既有调用：只返回已解析字段值与错误。"""
    value, error, _binding = resolve_case_field_reference_binding(
        ref, case, blobs, expect_value=expect_value)
    return value, error


def _extract_datetime_from_text(text: str) -> list[tuple[datetime, bool]]:
    """从定位文本提取实际时间值。返回 [(datetime, 含时刻精度)]。

    R1.3-A：证据是定位文本本身；只有日期不凭空补时刻。
    """
    found = []
    for m in re.finditer(
            r"(\d{4})-(\d{1,2})-(\d{1,2})"
            r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        has_time = m.group(4) is not None
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            found.append((datetime(y, mo, d, hh, mi, ss,
                                   tzinfo=timezone.utc), has_time))
        except ValueError:
            continue
    for m in re.finditer(
            r"(\d{4})年(\d{1,2})月(\d{1,2})日"
            r"(?:\s*(\d{1,2})时(\d{1,2})分(?:(\d{1,2})秒)?)?", text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        has_time = m.group(4) is not None
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            found.append((datetime(y, mo, d, hh, mi, ss,
                                   tzinfo=timezone.utc), has_time))
        except ValueError:
            continue
    return found


def _parse_subject_source_basis(raw) -> dict:
    """主体来源依据必须是可解析的字段引用（R1.2-A：不接受一句断言文字）。"""
    if isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"subject_source_basis 必须是可解析的字段引用 JSON：{exc}"
            ) from exc
    if parsed.get("kind") != "field_reference" or not parsed.get("path") \
            or not parsed.get("status"):
        raise ValueError(
            "subject_source_basis 必须为 {kind:'field_reference', path, status}"
            "（主体依据须可核验到具体字段，不接受断言文字）"
        )
    return parsed


RULE_VERSION = "kth-hybrid.qualification.v4"

# 已知来源族白名单（未知族 → 来源判断 unknown，不得默认放行）
KNOWN_FAMILIES = {
    "owner_attachment",          # Owner 提供（提供方事实，不代表主体归属）
    "company-official",          # 主体官方渠道
    "news-media",                # 第三方新闻/报道
    "gov-agency",                # 政府机构站点（第三方权威，R1.2 按域名分类）
    "academic",                  # 学术站点
    "public_original_page",      # 原版采集 provider id 之一（历史兼容）
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
                  same_body_sources: int = 1,
                  case=None) -> QualificationOutcome | GapOutcome:
    """对单条主张候选做四类资格判断。

    ``case_basis`` 必须含 ``subject_source_basis``（主体与截止的登记依据）；
    ``source`` 可携带 ``document_subject``/``document_subject_basis``（文档自识
    主体及其依据）与 ``time_evidence``（结构化时间证据）。
    """
    if not case_basis.get("subject_source_basis"):
        raise ValueError(
            "case_basis 缺少 subject_source_basis：主体/截止必须有登记依据，"
            "不接受无源硬编码"
        )
    subject_basis_ref = _parse_subject_source_basis(
        case_basis["subject_source_basis"])
    _subject_ref_value, subject_ref_err = _resolve_case_field_reference(
        subject_basis_ref, case, blobs, expect_value=case_basis["subject_legal_name"])
    if subject_ref_err is not None:
        raise ValueError(
            f"主体来源依据不可核验：{subject_ref_err}（引用必须解析到封存原件"
            f"的具体字段且值与评估主体一致）"
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
    document_subject_basis = source.get("document_subject_basis")
    subject_names = {subject, *aliases}
    scope = (claim.get("subject_scope") or "").strip()
    scope_matches_subject = any(name in scope for name in subject_names) and scope

    # R1.3-A：第一方归属需要真实文档定位且定位内容支持归属（含主体名）；
    # 任意非空字符串不构成依据
    first_party_proof = None  # (ok, basis_text)
    if family in policy["first_party_families"] and document_subject \
            and document_subject in subject_names:
        if isinstance(document_subject_basis, dict):
            locator_text, locator_err = _resolve_locator_text(
                document_subject_basis, data)
            if locator_err:
                first_party_proof = (
                    False, f"第一方归属定位不可解析：{locator_err}")
            elif document_subject not in locator_text:
                first_party_proof = (
                    False,
                    f"第一方归属定位内容不含文档主体'{document_subject}'，"
                    "不支持归属主张")
            else:
                first_party_proof = (
                    True,
                    f"文档自识主体'{document_subject}'（定位解析成功且内容含该"
                    f"主体名：{json.dumps(document_subject_basis, ensure_ascii=False)}）"
                    "与评估主体一致")
        else:
            first_party_proof = (
                False,
                f"第一方依据必须为结构化定位（byte_range/pdf_page），得到"
                f"{document_subject_basis!r}")

    if family in policy["first_party_families"]:
        if first_party_proof and first_party_proof[0]:
            identity_judgment = Judgment(
                VERDICT_OK,
                f"第一方材料：{first_party_proof[1]}，支持'主体自述'类主张",
            )
            allowed_uses.append("company_self_statement")
            cannot_prove.append("自述内容的独立核实")
        elif first_party_proof and not first_party_proof[0]:
            identity_judgment = Judgment(
                VERDICT_UNKNOWN,
                f"文档自识主体与评估主体一致，但归属证明不成立："
                f"{first_party_proof[1]}",
            )
            cannot_prove.append("文档归属主体依据")
        elif document_subject:
            identity_judgment = Judgment(
                VERDICT_UNKNOWN,
                f"Owner提供材料，但文档自识主体'{document_subject}'"
                f"≠评估主体'{subject}'；两实体关系未核验，不得按第一方自述使用",
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

    # ---- 3) 时间判断（R1.3-A 统一证明标准：从定位文本提取实际时间值比对，
    #      精度/时区不足不自动补出有利于过门的时刻）----
    time_evidence = source.get("time_evidence") or {}
    published_at = source.get("published_at")
    retrieved_at = source.get("retrieved_at")

    def _dt(value) -> datetime | None:
        return parse_iso_datetime(value)

    def _judged_from_located_text(declared, locator, label) -> Judgment | None:
        """统一时间证明：声明必须与定位文本提取的实际时间一致，并按精度比对截止。

        返回 Judgment 或 None（表示该路径无定位可用，交由调用方处理）。
        """
        declared_dt = _dt(declared)
        if declared_dt is None:
            return Judgment(VERDICT_FAIL, f"{label}声明缺失或非法：{declared!r}")
        locator_text, locator_err = _resolve_locator_text(locator, data)
        if locator_err:
            return Judgment(
                VERDICT_FAIL,
                f"{label}证明定位不可解析/不存在：{locator_err}")
        extracted = _extract_datetime_from_text(locator_text)
        if not extracted:
            return Judgment(
                VERDICT_FAIL,
                f"{label}定位内容不含可提取的时间值（文本片段："
                f"{locator_text[:60]!r}）")
        # 声明与提取值比对（按声明精度；钟面时间按声明时区解释）
        declared_raw = declared if isinstance(declared, str) else str(declared)
        try:
            wall = datetime.fromisoformat(
                declared_raw.replace("Z", "+00:00"))
        except ValueError:
            wall = declared_dt
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=timezone.utc)
        else:
            wall = wall.astimezone(timezone(wall.utcoffset()))  # 声明时区钟面
        declared_has_time = bool(re.search(
            r"T\d{1,2}:\d{2}| \d{1,2}:\d{2}|\d{1,2}时\d{1,2}分",
            declared_raw))
        matched = None
        for value, has_time in extracted:
            same_date = (value.year, value.month, value.day) == (
                wall.year, wall.month, wall.day)
            if not same_date:
                continue
            if declared_has_time:
                if has_time and (value.hour, value.minute) == (
                        wall.hour, wall.minute):
                    matched = (value, has_time)
                    break
            else:
                matched = (value, has_time)
                break
        if matched is None:
            return Judgment(
                VERDICT_FAIL,
                f"{label}声明 {declared!r} 与定位文本提取的实际时间不符"
                f"（提取：{[v.isoformat() for v, _ in extracted[:3]]}）")
        actual, actual_has_time = matched
        if actual > cutoff_dt:
            return Judgment(
                VERDICT_FAIL,
                f"{label}实际时间 {actual.isoformat()}（自定位文本提取）晚于截止"
                f" {case_basis['evidence_cutoff']}")
        if not actual_has_time and (
                (actual.year, actual.month, actual.day)
                == (cutoff_dt.year, cutoff_dt.month, cutoff_dt.day)):
            return Judgment(
                VERDICT_UNKNOWN,
                f"{label}定位文本仅有日期（{actual.date().isoformat()}）且当天即"
                "截止：跨截止边界，不凭空补时刻，保持不确定")
        return Judgment(
            VERDICT_OK,
            f"{label}实际时间 {actual.isoformat()}（自定位文本提取，声明一致）"
            f"{'≤' if actual_has_time else '（日期级）≤'} 截止")

    if published_at is not None:
        published_locator = source.get("published_at_locator")
        if published_locator is None:
            time_judgment = Judgment(
                VERDICT_FAIL,
                "published_at 无定位证明（published_at_locator）：不接受自报"
                "过去时间；须提供正文内可解析定位",
            )
        else:
            judged = _judged_from_located_text(
                published_at, published_locator, "发布时间")
            time_judgment = judged if judged else Judgment(
                VERDICT_FAIL, "发布时间证明缺失")
    elif time_evidence.get("kind") == "document_self_date":
        judged = _judged_from_located_text(
            time_evidence.get("date"), time_evidence.get("date_locator"),
            "文档自述日期")
        if judged is None:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"文档自述日期证明缺失：{time_evidence.get('date')!r}")
        else:
            time_judgment = judged
            if judged.verdict == VERDICT_OK:
                time_judgment = Judgment(
                    VERDICT_OK,
                    judged.basis + f"（{time_evidence.get('basis', '')}）。"
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
        # R1.3-A：登记证明必须绑定**当前**原件；时间与封存记录一致且不晚于截止
        reg_dt = _dt(time_evidence.get("date"))
        proof = time_evidence.get("registration_proof") or {}
        binding_err = _registration_binding_error(proof, source, blobs)
        record_dt, proof_err = _resolve_registration_proof(proof, blobs)
        claimed_dt = reg_dt
        if binding_err:
            time_judgment = Judgment(
                VERDICT_FAIL, f"登记证明绑定核验失败：{binding_err}")
        elif reg_dt is None:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"登记时间缺失或非法：{time_evidence.get('date')!r}",
            )
        elif proof_err:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"登记证明不可核验：{proof_err}（不接受无记录的登记声明）",
            )
        elif record_dt != claimed_dt:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"登记声明与封存记录不符：声明 {time_evidence.get('date')}，"
                f"记录 {record_dt.isoformat()}",
            )
        elif claimed_dt > cutoff_dt:
            time_judgment = Judgment(
                VERDICT_FAIL,
                f"登记时间 {time_evidence.get('date')} 晚于截止"
                f"{case_basis['evidence_cutoff']}，不能证明截止前存在",
            )
        else:
            time_judgment = Judgment(
                VERDICT_OK,
                f"登记时间 {time_evidence.get('date')} 与封存记录一致（绑定当前"
                f"原件）且 ≤ 截止，以登记记录为入池时点证明"
                f"（{time_evidence.get('basis', '')}）",
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
