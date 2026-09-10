"""素材导入：真实附件与历史捕获 → 原件仓 + 诚实 ImportRecord。

纪律（开工指令 §4 唯一素材导入例外）：
- 只按 M0 清单读取两附件与 80 条捕获及其 request/transport/receipt 依赖；
  复制到新 Case，原文与旧 receipt **字节不改**；
- 旧 qualification 仅作候选登记，不继承权威；
- 空 502 与 abandonment 保留失败类型，永不生成支持 met 的 Evidence；
- zip 成员防路径逃逸、重名、压缩炸弹，逐成员保留 hash；
- PDF/docx 抽取用本地已验证解析库（PyPDF2 3.0.1 / python-docx 1.2.0），
  保存页码/段落定位、工具版本与派生文本 hash；不手写解析器。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import sha256_hex
from .store import BlobStore, CaseStore

# 本地已验证的解析库版本（R1 实测可用；pypdf 不在环境内，不假装使用）
PDF_TOOL = "PyPDF2 3.0.1"
DOCX_TOOL = "python-docx 1.2.0"
TEXT_TOOL = "utf-8(stdlib)"
SUPPORTED_ATTACHMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md"})
DOCX_MAX_MEMBERS = 2048
DOCX_MAX_TOTAL_UNCOMPRESSED = 128 * 1024 * 1024
DOCX_MAX_MEMBER_RATIO = 500

# zip 安全边界（防压缩炸弹）
ZIP_MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
ZIP_MAX_MEMBER_RATIO = 500
ZIP_MAX_MEMBERS = 1000


class IntakeRejected(RuntimeError):
    """导入被拒：身份不符、zip 路径逃逸/重名/炸弹等。"""


@dataclass
class ImportOutcome:
    kind: str
    origin: str
    blob_sha256: str | None = None
    byte_length: int | None = None
    import_id: int | None = None
    source_id: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AttachmentInspection:
    """完整读取后的有限格式检查结果；尚未登记任何业务对象。"""

    media_type: str
    status: str
    tool: str
    projection: TextProjection | None = None
    error: str | None = None


def import_attachment(path: Path | str, blobs: BlobStore, case: CaseStore, *,
                      declared_sha256: str | None = None,
                      declared_size: int | None = None,
                      original_filename: str | None = None,
                      media_type: str | None = None) -> ImportOutcome:
    """导入单个附件原件：核验声明身份后封存。不改变来源文件。"""
    path = Path(path)
    data = path.read_bytes()
    actual = sha256_hex(data)
    if declared_sha256 and declared_sha256 != actual:
        raise IntakeRejected(
            f"附件身份不符：声明 {declared_sha256}，实际 {actual}（{path}）"
        )
    if declared_size is not None and declared_size != len(data):
        raise IntakeRejected(
            f"附件大小不符：声明 {declared_size}，实际 {len(data)}（{path}）"
        )
    ref = blobs.put_bytes(data)
    import_id = case.add_import_record(
        "attachment", str(path), actual,
        note=json.dumps({"original_filename": original_filename,
                         "media_type": media_type}, ensure_ascii=False),
    )
    source_id = f"ATT::{actual[:16]}"
    if case.fetch_one("sources", "source_id", source_id) is None:
        case.add_source(
            source_id, ref.sha256, ref.byte_length, media_type=media_type,
            locator=original_filename, retrieved_at=None, published_at=None,
            published_at_provenance="附件无抓取/发布时间（Owner 提供）",
            source_family="owner_attachment", capture_status="attachment",
            import_id=import_id,
        )
    return ImportOutcome("attachment", str(path), ref.sha256, ref.byte_length,
                         import_id, source_id)


def _zip_member_display_name(info: zipfile.ZipInfo) -> str:
    """GBK 文件名兼容：未置 UTF-8 标志位的历史 zip 按 cp437→GBK 重解码。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def import_zip_attachment(path: Path | str, blobs: BlobStore, case: CaseStore, *,
                          declared_sha256: str | None = None,
                          original_filename: str | None = None) -> ImportOutcome:
    """导入 zip 附件：逐成员安全校验、封存、登记。"""
    path = Path(path)
    data = path.read_bytes()
    actual = sha256_hex(data)
    if declared_sha256 and declared_sha256 != actual:
        raise IntakeRejected(f"zip 附件身份不符：声明 {declared_sha256}，实际 {actual}")
    outcome = ImportOutcome("zip_attachment", str(path), actual, len(data))
    outcome.import_id = case.add_import_record(
        "zip_attachment", str(path), actual,
        note=json.dumps({"original_filename": original_filename,
                         "tool": "zipfile(stdlib)"}, ensure_ascii=False),
    )
    ref = blobs.put_bytes(data)

    seen_names: set[str] = set()
    total = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > ZIP_MAX_MEMBERS:
            raise IntakeRejected(f"zip 成员数超界：{len(infos)}")
        members: list[dict] = []
        for info in infos:
            display = _zip_member_display_name(info)
            normalized = display.replace("\\", "/")
            parts = [p for p in normalized.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts) or ":" in normalized[:2]:
                raise IntakeRejected(f"zip 成员路径逃逸：{display!r}")
            if info.is_dir():
                members.append({"name": display, "is_dir": True})
                continue
            if display in seen_names:
                raise IntakeRejected(f"zip 成员重名：{display!r}")
            seen_names.add(display)
            if info.file_size and info.compress_size:
                if info.file_size / max(info.compress_size, 1) > ZIP_MAX_MEMBER_RATIO:
                    raise IntakeRejected(
                        f"zip 成员疑似压缩炸弹（压缩比 "
                        f"{info.file_size}/{info.compress_size}）：{display!r}"
                    )
            total += info.file_size
            if total > ZIP_MAX_TOTAL_UNCOMPRESSED:
                raise IntakeRejected("zip 解压总量超界")
            member_data = archive.read(info)
            member_ref = blobs.put_bytes(member_data)
            members.append({
                "name": display, "is_dir": False,
                "sha256": member_ref.sha256, "size": member_ref.byte_length,
            })
            # 每个非目录成员登记为独立 Source（locator 指向 zip 成员）
            source_id = f"ZIPMEM::{member_ref.sha256[:16]}"
            if case.fetch_one("sources", "source_id", source_id) is None:
                case.add_source(
                    source_id, member_ref.sha256, member_ref.byte_length,
                    media_type=_guess_media(display),
                    locator=f"{original_filename or path.name}!/{display}",
                    retrieved_at=None, published_at=None,
                    published_at_provenance="zip 成员无独立抓取/发布时间",
                    source_family="owner_attachment",
                    capture_status="attachment_zip_member",
                    import_id=outcome.import_id,
                )
    outcome.source_id = f"ATT::{ref.sha256[:16]}"
    outcome.notes.append(f"成员 {len(members)} 项（含目录）")
    return outcome


def _guess_media(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".html", ".htm")):
        return "text/html"
    return "application/octet-stream"


# ---- 历史捕获导入 ----

_CAPTURE_DEPENDENCIES = (
    "request.json",
    "transport-attempt.json",
    "live-provider-result.json",
    "receipt.json",
)


def _family_from_url(url: str | None) -> str:
    """按内容来源域分类来源族（采集器provider名不是内容来源族）。

    R1.2-R1-06/R1.2-A：政府域名 → gov-agency（第三方权威）；其余网页捕获按
    news-media（第三方媒体/页面）。无法解析时返回 unknown（资格层将转
    needs_review，不默认放行）。
    """
    if not url or "://" not in url:
        return "unknown"
    try:
        host = url.split("://", 1)[1].split("/", 1)[0].lower()
    except (ValueError, IndexError):
        return "unknown"
    if ".gov.cn" in host or host.endswith(".gov"):
        return "gov-agency"
    if any(host.endswith(tld) for tld in (".edu.cn", ".edu")):
        return "academic"
    return "news-media"


@dataclass
class CaptureImport:
    dir_name: str
    readable: bool = True
    error: str | None = None
    raw_body_sha256: str | None = None
    raw_body_size: int | None = None
    receipt_sha256: str | None = None
    declared_raw_sha256: str | None = None
    declared_raw_size: int | None = None
    hash_matches_declaration: bool | None = None
    capture_status: str | None = None
    response_status: str | None = None
    failure_code: str | None = None
    retrieved_at: str | None = None
    fixed_clock: str | None = None
    as_of_cut: str | None = None
    final_url: str | None = None
    provider_id: str | None = None
    evidence_eligible: bool | None = None
    qualification_candidate_count: int = 0
    missing_dependencies: list[str] = field(default_factory=list)
    source_id: str | None = None
    import_id: int | None = None
    nonempty: bool = False


def import_capture(capture_dir: Path | str, blobs: BlobStore | None,
                   case: CaseStore | None, *,
                   session_root: Path | str | None = None) -> CaptureImport:
    """导入一条历史捕获：实读正文与全部依赖；失败类型保留。

    ``blobs/case`` 为 None 时仅盘点（只读），不写入。
    """
    capture_dir = Path(capture_dir)
    result = CaptureImport(dir_name=capture_dir.name)
    try:
        if not capture_dir.is_dir():
            result.readable = False
            result.error = "目录不存在"
            return result
        raw_path = capture_dir / "frozen-capture" / "raw-body.bin"
        receipt_path = capture_dir / "receipt.json"
        for dep in _CAPTURE_DEPENDENCIES:
            if not (capture_dir / dep).exists():
                result.missing_dependencies.append(dep)
        if not raw_path.exists() or not receipt_path.exists():
            result.missing_dependencies.append(
                "frozen-capture/raw-body.bin" if not raw_path.exists()
                else "receipt.json"
            )
            if result.missing_dependencies:
                result.readable = False
                result.error = f"依赖缺失：{result.missing_dependencies}"
                return result
        raw_bytes = raw_path.read_bytes()
        receipt_bytes = receipt_path.read_bytes()
    except PermissionError as exc:
        result.readable = False
        result.error = f"PermissionError winerror={getattr(exc, 'winerror', None)}"
        return result
    except OSError as exc:
        result.readable = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.raw_body_sha256 = sha256_hex(raw_bytes)
    result.raw_body_size = len(raw_bytes)
    result.receipt_sha256 = sha256_hex(receipt_bytes)
    result.nonempty = len(raw_bytes) > 0
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        result.readable = False
        result.error = f"receipt 解析失败：{exc}"
        return result
    result.declared_raw_sha256 = receipt.get("raw_body_sha256")
    result.declared_raw_size = receipt.get("raw_body_size")
    result.hash_matches_declaration = (
        result.declared_raw_sha256 == result.raw_body_sha256
        and result.declared_raw_size == result.raw_body_size
    )
    result.capture_status = receipt.get("capture_status")
    result.response_status = str(receipt.get("response_status"))
    result.failure_code = receipt.get("failure_code")
    result.retrieved_at = receipt.get("retrieved_at")
    result.fixed_clock = receipt.get("fixed_clock")
    result.as_of_cut = receipt.get("as_of_cut")
    result.final_url = receipt.get("final_url")
    result.provider_id = receipt.get("provider_id")
    result.evidence_eligible = receipt.get("evidence_eligible")
    result.qualification_candidate_count = len(
        receipt.get("qualification_candidate_refs") or []
    )

    if blobs is not None and case is not None:
        # 导入幂等（R1.2-D）：同来源路径的重跑复用已有导入记录，不重复登记
        existing_import = case.find_import("historical_capture", str(capture_dir))
        if existing_import is not None:
            result.import_id = existing_import["import_id"]
            result.source_id = f"CAP::{capture_dir.name}"
            return result
        raw_ref = blobs.put_bytes(raw_bytes)
        receipt_ref = blobs.put_bytes(receipt_bytes)
        # 逐份封存采集依赖（R1-06）：request/transport-attempt/live-provider-result/
        # receipt + frozen-capture/transport.json——真实 blob 引用，不是文件名清单
        import_id = case.add_import_record(
            "historical_capture", str(capture_dir), result.raw_body_sha256,
            note=json.dumps({
                "receipt_blob": receipt_ref.sha256,
                "session_root": str(session_root) if session_root else None,
            }, ensure_ascii=False),
        )
        for dep_name in (*_CAPTURE_DEPENDENCIES, "frozen-capture/transport.json"):
            dep_path = capture_dir / dep_name
            if dep_path.exists():
                dep_bytes = dep_path.read_bytes()
                dep_ref = blobs.put_bytes(dep_bytes)
                case.add_capture_dependency(import_id, dep_name, dep_ref.sha256,
                                            dep_ref.byte_length)
        # 采集实例身份（R1-06）：按真实采集目录（=采集身份）独立登记 Source，
        # 同正文的不同采集共享同一 blob，不再被正文去重合并；
        # 来源族按内容来源域分类（gov.cn → gov-agency），provider 记录于依赖封存
        source_id = f"CAP::{capture_dir.name}"
        if case.fetch_one("sources", "source_id", source_id) is None:
            case.add_source(
                source_id, raw_ref.sha256, raw_ref.byte_length,
                media_type=_guess_media(result.final_url or ""),
                locator=str(capture_dir),
                retrieved_at=result.retrieved_at,
                published_at=None,
                published_at_provenance=(
                    "历史捕获无发布时间证明（receipt 仅含抓取时钟）"
                ),
                source_family=_family_from_url(result.final_url),
                capture_status=(
                    result.capture_status or "unknown"
                ) + ("/empty_body" if not result.nonempty else ""),
                import_id=import_id,
            )
        result.source_id = source_id
        result.import_id = import_id
    return result


@dataclass
class AbandonmentImport:
    dir_name: str
    readable: bool
    error: str | None = None
    files: list[str] = field(default_factory=list)
    abandonment_sha256: str | None = None
    import_id: int | None = None


def import_abandonment(dir_path: Path | str, blobs: BlobStore | None,
                       case: CaseStore | None) -> AbandonmentImport:
    """导入显式放弃的失败传输 action：保留失败类型，不生成 Source。"""
    dir_path = Path(dir_path)
    result = AbandonmentImport(dir_name=dir_path.name, readable=True)
    try:
        result.files = sorted(p.name for p in dir_path.iterdir())
        abandonment = dir_path / "abandonment.json"
        result.abandonment_sha256 = sha256_hex(abandonment.read_bytes())
    except (PermissionError, OSError) as exc:
        result.readable = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    if blobs is not None and case is not None:
        for name in result.files:
            blobs.put_bytes((dir_path / name).read_bytes())
        result.import_id = case.add_import_record(
            "abandoned_transport", str(dir_path), result.abandonment_sha256,
            note="显式放弃的失败传输（无 frozen-capture）；诚实失败证据，非 Evidence",
        )
    return result


# ---- 文本抽取（成熟解析库；记录工具版本与定位） ----

@dataclass
class TextProjection:
    tool: str
    locator_kind: str  # pdf_page / docx_paragraph / zip_member
    locators: list[dict] = field(default_factory=list)  # 每段/页定位与文本hash
    unprocessed: list[str] = field(default_factory=list)  # 如无文本层的页


def extract_pdf_pages(data: bytes) -> TextProjection:
    """按页抽取 PDF 文本：页码定位 + 派生文本 hash；无文本层的页明确标记。"""
    import io

    import PyPDF2

    projection = TextProjection(tool=PDF_TOOL, locator_kind="pdf_page")
    reader = PyPDF2.PdfReader(io.BytesIO(data))
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # 单页失败不吞：登记为未处理
            projection.unprocessed.append(f"page {page_no}: {type(exc).__name__}: {exc}")
            continue
        if not text.strip():
            projection.unprocessed.append(f"page {page_no}: 无文本层（可能为扫描图像）")
            continue
        encoded = text.encode("utf-8")
        projection.locators.append({
            "page": page_no, "text_sha256": sha256_hex(encoded),
            "char_count": len(text), "text": text,
        })
    return projection


def extract_docx_paragraphs(data: bytes) -> TextProjection:
    """按段落抽取 docx 文本：段落序号定位 + 派生文本 hash。"""
    import io

    import docx

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > DOCX_MAX_MEMBERS:
            raise IntakeRejected(
                f"DOCX成员数超过上限：{len(infos)}>{DOCX_MAX_MEMBERS}")
        total = 0
        for info in infos:
            normalized = info.filename.replace("\\", "/")
            parts = [part for part in normalized.split("/")
                     if part not in {"", "."}]
            if any(part == ".." for part in parts) or ":" in normalized[:2]:
                raise IntakeRejected(f"DOCX成员路径逃逸：{info.filename!r}")
            total += info.file_size
            if total > DOCX_MAX_TOTAL_UNCOMPRESSED:
                raise IntakeRejected(
                    "DOCX解压总量超过上限："
                    f"{total}>{DOCX_MAX_TOTAL_UNCOMPRESSED}")
            if info.file_size \
                    and info.file_size / max(info.compress_size, 1) > \
                    DOCX_MAX_MEMBER_RATIO:
                raise IntakeRejected(
                    f"DOCX成员压缩比超过上限：{info.filename!r}")
    projection = TextProjection(tool=DOCX_TOOL, locator_kind="docx_paragraph")
    document = docx.Document(io.BytesIO(data))
    for para_no, para in enumerate(document.paragraphs, start=1):
        text = para.text or ""
        if not text.strip():
            continue
        encoded = text.encode("utf-8")
        projection.locators.append({
            "paragraph": para_no, "text_sha256": sha256_hex(encoded),
            "char_count": len(text), "text": text,
        })
    return projection


def inspect_attachment(name: str, data: bytes) -> AttachmentInspection:
    """在登记前完整验证一个明确支持的附件格式。

    不支持格式与损坏文件返回诚实状态；调用方仍可封存原件，但不得据此创建
    Source 或把解析问题解释为业务证据不足。
    """
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_ATTACHMENT_SUFFIXES:
        return AttachmentInspection(
            media_type="application/octet-stream", status="unsupported",
            tool="none", error=f"不支持的附件格式：{suffix or '<无扩展名>'}")
    if suffix == ".pdf":
        try:
            projection = extract_pdf_pages(data)
        except Exception as exc:
            return AttachmentInspection(
                media_type="application/pdf", status="failed", tool=PDF_TOOL,
                error=f"PDF解析失败：{type(exc).__name__}: {exc}")
        return AttachmentInspection(
            media_type="application/pdf", status="saved", tool=PDF_TOOL,
            projection=projection)
    if suffix == ".docx":
        try:
            projection = extract_docx_paragraphs(data)
        except Exception as exc:
            return AttachmentInspection(
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"),
                status="failed", tool=DOCX_TOOL,
                error=f"DOCX解析失败：{type(exc).__name__}: {exc}")
        return AttachmentInspection(
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"),
            status="saved", tool=DOCX_TOOL, projection=projection)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return AttachmentInspection(
            media_type="text/plain", status="failed", tool=TEXT_TOOL,
            error=f"UTF-8文本解析失败：{exc}")
    projection = TextProjection(tool=TEXT_TOOL, locator_kind="byte_range")
    if text.strip():
        projection.locators.append({
            "start": 0, "end": len(data), "text": text,
            "text_sha256": sha256_hex(data), "char_count": len(text),
        })
    else:
        projection.unprocessed.append("文本为空")
    return AttachmentInspection(
        media_type="text/markdown" if suffix == ".md" else "text/plain",
        status="saved", tool=TEXT_TOOL, projection=projection)
