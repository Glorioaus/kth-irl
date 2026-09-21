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

import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .agent_host import digest
from .contracts import sha256_hex
from .store import BlobStore, CaseStore

# 本地已验证的解析库版本（R1 实测可用；pypdf 不在环境内，不假装使用）
PDF_TOOL = "PyPDF2 3.0.1"
DOCX_TOOL = "python-docx 1.2.0"
DOCX_STRUCTURE_TOOL = DOCX_TOOL + "; ag1-docx-structure.v3"
TEXT_TOOL = "utf-8(stdlib)"
SUPPORTED_ATTACHMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md"})
DOCX_MAX_MEMBERS = 2048
DOCX_MAX_TOTAL_UNCOMPRESSED = 128 * 1024 * 1024
DOCX_MAX_MEMBER_RATIO = 500

# zip 安全边界（防压缩炸弹）
ZIP_MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
ZIP_MAX_MEMBER_RATIO = 500
ZIP_MAX_MEMBERS = 1000
PRODUCT_MAX_INPUTS = 256
PRODUCT_MAX_INPUT_BYTES = 64 * 1024 * 1024
PRODUCT_MAX_TOTAL_BYTES = 256 * 1024 * 1024
PRODUCT_MAX_SEGMENT_CHARS = 32768
_COVERAGE_COLLECTIONS = ("entries", "segments", "unprocessed")
_COVERAGE_PART_MAX_BYTES = 4 * 1024 * 1024


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


def extract_docx_paragraphs(data: bytes, *,
                            include_structure: bool = False) -> TextProjection:
    """默认保留旧段落合同；结构模式包含实际正文及页眉页脚部件。"""
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
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn

    projection = TextProjection(
        tool=DOCX_STRUCTURE_TOOL if include_structure else DOCX_TOOL,
        locator_kind="docx_paragraph")
    document = docx.Document(io.BytesIO(data))
    def add_text(text, location):
        projection.locators.append({
            **location, "text_sha256": sha256_hex(text.encode("utf-8")),
            "char_count": len(text), "text": text,
        })

    if not include_structure:
        for para_no, para in enumerate(document.paragraphs, start=1):
            if (para.text or "").strip():
                add_text(para.text, {"paragraph": para_no})
        return projection

    unprocessed_nodes = set()

    def note_unprocessed(node, member):
        position = f"{member} {node.getroottree().getpath(node)}"
        if position not in unprocessed_nodes:
            unprocessed_nodes.add(position)
            projection.unprocessed.append(f"DOCX未处理内容：{position}")

    unsupported = {qn(tag) for tag in (
        "w:sdt", "w:txbxContent", "w:ins", "w:del", "w:fldSimple",
        "w:drawing", "w:pict", "w:object", "w:altChunk")}
    non_content = {qn(tag) for tag in (
        "w:bookmarkStart", "w:bookmarkEnd", "w:proofErr", "w:permStart",
        "w:permEnd", "w:commentRangeStart", "w:commentRangeEnd",
        "w:pPr", "w:rPr", "w:tblPr", "w:tblGrid", "w:trPr", "w:tcPr",
        "w:sectPr")}
    inline_containers = {qn(tag) for tag in (
        "w:r", "w:hyperlink", "w:smartTag", "w:customXml")}
    text_nodes = {qn("w:t"), qn("w:delText"), qn("w:instrText")}

    def collect_inline(node, member):
        if node.tag in unsupported:
            note_unprocessed(node, member)
            return []
        if node.tag in non_content:
            return []
        if node.tag in text_nodes:
            return [node.text or ""]
        if node.tag == qn("w:tab"):
            return ["\t"]
        if node.tag in {qn("w:br"), qn("w:cr")}:
            return ["\n"]
        if node.tag not in inline_containers:
            note_unprocessed(node, member)
            return []
        fragments = []
        for child in node.iterchildren():
            fragments.extend(collect_inline(child, member))
        return fragments

    def emit_paragraph(node, base, member, paragraph_no, *, in_cell):
        fragments = []
        for child in node.iterchildren():
            if child.tag in non_content:
                continue
            if child.tag in unsupported:
                note_unprocessed(child, member)
                continue
            fragments.extend(collect_inline(child, member))
        location = dict(base)
        if in_cell:
            if paragraph_no > 1:
                location["cell_paragraph"] = paragraph_no
        else:
            location["paragraph"] = paragraph_no
        if not fragments:
            if not any(
                    f"{member} {node.getroottree().getpath(node)}" == value
                    for value in unprocessed_nodes):
                add_text("", location)
            return
        for span_no, text in enumerate(fragments, start=1):
            if not text:
                continue
            span_location = dict(location)
            if len(fragments) > 1:
                span_location["inline_span"] = span_no
            add_text(text, span_location)

    def walk_container(element, base, member, ancestors=(), *, in_cell=False):
        paragraph_no = table_no = 0
        for child in element.iterchildren():
            if child.tag == qn("w:p"):
                paragraph_no += 1
                emit_paragraph(child, base, member, paragraph_no,
                               in_cell=in_cell)
            elif child.tag == qn("w:tbl"):
                table_no += 1
                walk_table(child, base, member, table_no, ancestors)
            elif child.tag in non_content:
                continue
            else:
                note_unprocessed(child, member)

    def walk_table(table, base, member, table_no, ancestors):
        row_no = 0
        for row in table.iterchildren():
            if row.tag in non_content:
                continue
            if row.tag != qn("w:tr"):
                note_unprocessed(row, member)
                continue
            row_no += 1
            column_no = 0
            for cell in row.iterchildren():
                if cell.tag in non_content:
                    continue
                if cell.tag != qn("w:tc"):
                    note_unprocessed(cell, member)
                    continue
                column_no += 1
                cell_path = {
                    **base, "table": table_no, "row": row_no,
                    "column": column_no,
                }
                if ancestors:
                    cell_path["parent_cells"] = [dict(item) for item in ancestors]
                walk_container(
                    cell, cell_path, member, (*ancestors, cell_path),
                    in_cell=True)

    def add_story(element, base, member):
        walk_container(element, base, member)

    add_story(document.element.body, {}, "word/document.xml")
    # 遍历已封存部件，不调用可能创建默认页眉定义的section.header API。
    parts = list(document.part.package.parts)
    for story in ("header", "footer"):
        for part in sorted(parts, key=lambda item: str(item.partname)):
            if part.content_type != (
                    f"application/vnd.openxmlformats-officedocument.wordprocessingml.{story}+xml"):
                continue
            member = str(part.partname).lstrip("/")
            element = parse_xml(part.blob)
            add_story(element, {"member": member, "story": story}, member)
    additional_story_types = {
        f"application/vnd.openxmlformats-officedocument.wordprocessingml.{story}+xml"
        for story in ("footnotes", "endnotes", "comments")
    }
    for part in parts:
        member = str(part.partname).lstrip("/")
        if part.content_type in additional_story_types:
            projection.unprocessed.append(f"DOCX未处理附加内容区：{member}")
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
            projection = extract_docx_paragraphs(data, include_structure=True)
        except Exception as exc:
            return AttachmentInspection(
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"),
                status="failed", tool=DOCX_STRUCTURE_TOOL,
                error=f"DOCX解析失败：{type(exc).__name__}: {exc}")
        return AttachmentInspection(
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"),
            status="saved", tool=DOCX_STRUCTURE_TOOL, projection=projection)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return AttachmentInspection(
            media_type="text/plain", status="failed", tool=TEXT_TOOL,
            error=f"UTF-8文本解析失败：{exc}")
    projection = TextProjection(tool=TEXT_TOOL, locator_kind="byte_range")
    offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        encoded = line.encode("utf-8")
        projection.locators.append({
            "line": line_no, "start": offset, "end": offset + len(encoded),
            "text": line, "text_sha256": sha256_hex(encoded),
            "char_count": len(line),
        })
        offset += len(encoded)
    if not text:
        projection.unprocessed.append("文本为空")
    return AttachmentInspection(
        media_type="text/markdown" if suffix == ".md" else "text/plain",
        status="saved", tool=TEXT_TOOL, projection=projection)


# ---- AG1 产品材料：冻结输入与完整覆盖，不登记 Evidence 或资格 ----

def freeze_product_inputs(paths: list[str], blobs: BlobStore) -> list[dict]:
    """只读冻结显式原件；失败及超限条目仍占接收分母。"""
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise IntakeRejected("输入必须是显式路径字符串列表")
    output = []
    total = 0
    for index, origin in enumerate(paths):
        path = Path(origin)
        item = {
            "input_index": index, "origin_path": origin,
            "original_filename": path.name, "blob_sha256": None,
            "byte_length": None, "status": "failed", "error": None,
        }
        output.append(item)
        if len(paths) > PRODUCT_MAX_INPUTS:
            item.update(status="rejected", error="输入件数超过256上限")
            continue
        if not path.is_absolute():
            item.update(status="rejected", error="输入路径必须为绝对路径")
            continue
        try:
            before = path.stat()
            item["byte_length"] = before.st_size
            if not stat.S_ISREG(before.st_mode):
                raise IntakeRejected("输入不是普通文件")
            if before.st_size > PRODUCT_MAX_INPUT_BYTES:
                item.update(status="rejected", error="单文件超过64MiB上限")
                continue
            if total + before.st_size > PRODUCT_MAX_TOTAL_BYTES:
                item.update(status="rejected", error="输入总量超过256MiB上限")
                continue
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                data = handle.read(PRODUCT_MAX_INPUT_BYTES + 1)
                finished = os.fstat(handle.fileno())
            after = path.stat()
            identities = {
                (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
                for s in (before, opened, finished, after)
            }
            # Windows路径stat与句柄fstat的ctime语义不同，各自前后核对。
            if len(identities) != 1 or len(data) != before.st_size \
                    or before.st_ctime_ns != after.st_ctime_ns \
                    or opened.st_ctime_ns != finished.st_ctime_ns:
                raise IntakeRejected("原件读取期间身份或长度改变，未冻结")
        except (OSError, ValueError, IntakeRejected) as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
            continue
        # 原件读取错误可保留为终态；封存失败须停止，不伪装成输入缺料。
        ref = blobs.put_bytes(data)
        total += ref.byte_length
        item.update(blob_sha256=ref.sha256, byte_length=ref.byte_length,
                    status="frozen")
    return output


def _product_zip_error(infos: list[zipfile.ZipInfo]) -> str | None:
    """预检全部中央目录；不解压到任何文件系统路径。"""
    if len(infos) > ZIP_MAX_MEMBERS:
        return "ZIP成员数超过1000上限"
    if sum(info.file_size for info in infos) > ZIP_MAX_TOTAL_UNCOMPRESSED:
        return "ZIP展开总量超过256MiB上限"
    seen = set()
    for info in infos:
        name = _zip_member_display_name(info).replace("\\", "/")
        parts = name.split("/")
        if name.startswith("/") or ".." in parts or ":" in name \
                or "\x00" in info.orig_filename:
            return f"ZIP成员路径逃逸：{name!r}"
        normalized = "/".join(p for p in parts if p not in {"", "."}).casefold()
        if not normalized or normalized in seen:
            return f"ZIP成员重名或空路径：{name!r}"
        seen.add(normalized)
        if stat.S_ISLNK(info.external_attr >> 16):
            return f"ZIP符号链接不支持：{name!r}"
        if info.file_size / max(info.compress_size, 1) > ZIP_MAX_MEMBER_RATIO:
            return f"ZIP成员压缩比超过500上限：{name!r}"
    return None


def _product_source(workflow, run_id, entry, name, data, origin, *,
                    media_type, persist=True):
    source_id = f"PRODUCT::{entry['material_id']}"
    note = json.dumps({
        "run_id": run_id, "material_id": entry["material_id"],
        "locator": entry["locator"],
    }, ensure_ascii=False, sort_keys=True)
    existing = workflow.store.fetch_one("sources", "source_id", source_id)
    if persist:
        import_id = workflow.store.ensure_import_record(
            "product_attachment", origin, entry["blob_sha256"], note=note)
    else:
        if existing is None:
            raise IntakeRejected("coverage原件来源缺失，禁止读取时补造")
        import_id = existing["import_id"]
        audit = workflow.store.fetch_one("import_records", "import_id", import_id)
        if audit is None or any(audit[key] != value for key, value in {
                "kind": "product_attachment", "origin_path": origin,
                "origin_sha256": entry["blob_sha256"], "note": note}.items()):
            raise IntakeRejected("coverage原件导入身份或归属不符")
    fields = {
        "blob_sha256": entry["blob_sha256"], "byte_length": len(data),
        "locator": name, "import_id": import_id, "media_type": media_type,
    }
    if existing is None:
        workflow.store.add_source(
            source_id, fields["blob_sha256"], fields["byte_length"],
            locator=name, import_id=import_id, media_type=media_type,
            published_at=None, retrieved_at=None,
            published_at_provenance="用户附件，无独立主体或时间证明",
            source_family="owner_attachment", capture_status="product_attachment")
    elif any(existing[key] != value for key, value in fields.items()):
        raise IntakeRejected("材料source身份冲突")
    entry["source_id"] = source_id


def _read_original_projections(workflow, source_id, blob_sha256, inspection):
    """只读从原件解析结果重建既有投影身份，不信任派生文本自身的hash。"""
    records = [
        ({key: value for key, value in locator.items()
          if key not in {"text", "text_sha256", "char_count"}},
         locator["text"], "projected", None)
        for locator in inspection.projection.locators
    ]
    records.extend(
        ({"unprocessed_index": index}, None, "unprocessed", error)
        for index, error in enumerate(inspection.projection.unprocessed, start=1))
    if not records:
        records = [({"unprocessed_index": 1}, None, "unprocessed", "附件未产生可用文本片段")]
    saved = {row["projection_id"]: row
             for row in workflow.store.fetch_text_projections(source_id)}
    saved_tools = {row["tool"] for row in saved.values()}
    if saved_tools and any(tool != inspection.tool for tool in saved_tools):
        if (inspection.tool == DOCX_STRUCTURE_TOOL
                and any(tool == DOCX_TOOL + "; ag1-docx-structure.v2"
                        for tool in saved_tools)):
            raise IntakeRejected(
                "legacy_extraction_contract: 旧DOCX结构投影不能由新遍历器静默升级")
        raise IntakeRejected("coverage原件投影工具版本变化，需要显式新run")
    output, verified_text = [], set()
    for locator, text, status, error in records:
        raw = text.encode("utf-8") if text is not None else None
        text_sha = sha256_hex(raw) if raw is not None else None
        identity = {
            "source_id": source_id, "source_blob_sha256": blob_sha256,
            "locator": locator, "text_sha256": text_sha,
            "tool": inspection.tool, "status": status, "error": error,
        }
        projection_id = "PROJ::" + digest(identity)
        expected = {
            **identity, "projection_id": projection_id,
            "text_blob_sha256": text_sha,
            "char_count": len(text) if text is not None else None,
        }
        stored = saved.pop(projection_id, None)
        if stored is None or any(stored[key] != value for key, value in expected.items()):
            raise IntakeRejected("coverage原件投影缺失/不符或提取版本变化，需要显式新run")
        if raw is not None and text_sha not in verified_text:
            if workflow.blobs.read_bytes(text_sha) != raw:
                raise IntakeRejected("coverage原件与投影正文不一致")
            verified_text.add(text_sha)
        output.append(expected)
    if saved:
        raise IntakeRejected("coverage原件投影分母与实际解析不一致")
    return output


def product_coverage_digest(coverage_without_digest: dict) -> str:
    """本地完整覆盖的流式身份；不改变宿主消息的4MiB限制。"""
    if not isinstance(coverage_without_digest, dict):
        raise IntakeRejected("coverage摘要输入必须为对象")
    hasher = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(coverage_without_digest):
        hasher.update(chunk.encode("utf-8"))
    return hasher.hexdigest()


def _coverage_json_size(value) -> int:
    encoder = json.JSONEncoder(
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sum(len(chunk.encode("utf-8")) for chunk in encoder.iterencode(value))


def _check_product_coverage(workflow, coverage, inputs):
    if coverage["coverage_digest"] != product_coverage_digest({
            key: value for key, value in coverage.items() if key != "coverage_digest"}):
        raise IntakeRejected("coverage摘要身份不符")
    rebuilt = _build_product_coverage(workflow, coverage["run_id"], inputs, persist=False)
    if rebuilt["coverage_digest"] != coverage["coverage_digest"]:
        raise IntakeRejected("coverage与原件重建的投影、派生片段或完整分母不一致")
    return coverage


def _coverage_part_metadata(coverage, collection, part_index, start, count):
    return {
        "schema_version": "ag1.coverage.part.v1",
        "run_id": coverage["run_id"], "inputs_digest": coverage["inputs_digest"],
        "coverage_digest": coverage["coverage_digest"],
        "collection": collection, "part_index": part_index,
        "start": start, "count": count,
    }


def _seal_product_coverage(workflow, coverage):
    parts = []

    def save_part(collection, start, items):
        part_index = len(parts)
        part = {
            **_coverage_part_metadata(
                coverage, collection, part_index, start, len(items)),
            "items": items,
        }
        part["part_digest"] = digest(part)
        byte_length = _coverage_json_size(part)
        if byte_length >= _COVERAGE_PART_MAX_BYTES:
            raise IntakeRejected("coverage分片连同元数据超过字节上限")
        part_id = (f"COVERAGE-PART::{coverage['run_id']}::"
                   f"{coverage['coverage_digest']}::{part_index}")
        workflow.store.put_product_object(
            part_id, run_id=coverage["run_id"], kind="coverage_part", body=part)
        parts.append({
            "part_id": part_id, "collection": collection, "part_index": part_index,
            "start": start, "count": len(items), "byte_length": byte_length,
            "part_digest": part["part_digest"],
        })

    for collection in _COVERAGE_COLLECTIONS:
        items, item_bytes, start = [], 0, 0
        for item in coverage[collection]:
            item_size = _coverage_json_size(item)
            # 空数组的规范编码再加成员及逗号，精确计入数量位数和摘要字段。
            def prospective_size():
                envelope = {
                    **_coverage_part_metadata(
                        coverage, collection, len(parts), start, len(items) + 1),
                    "items": [], "part_digest": "0" * 64,
                }
                return _coverage_json_size(envelope) + item_bytes + item_size + len(items)

            if prospective_size() >= _COVERAGE_PART_MAX_BYTES and items:
                save_part(collection, start, items)
                start += len(items)
                items, item_bytes = [], 0
            if prospective_size() >= _COVERAGE_PART_MAX_BYTES:
                raise IntakeRejected("coverage单项连同元数据超过分片上限")
            items.append(item)
            item_bytes += item_size
        if items:
            save_part(collection, start, items)
    index = {
        "schema_version": "ag1.coverage.index.v1",
        "run_id": coverage["run_id"], "inputs_digest": coverage["inputs_digest"],
        "received_count": coverage["received_count"],
        "coverage_digest": coverage["coverage_digest"],
        "counts": {key: len(coverage[key]) for key in _COVERAGE_COLLECTIONS},
        "parts": parts,
    }
    index["index_digest"] = digest(index)
    if _coverage_json_size(index) >= _COVERAGE_PART_MAX_BYTES:
        raise IntakeRejected("coverage根索引超过字节上限")
    # 分片先逐个不可变封存；根索引最后发布，中断时不得暴露不完整集合。
    workflow.store.put_product_object(
        f"COVERAGE::{coverage['run_id']}", run_id=coverage["run_id"],
        kind="coverage", body=index)


def _restore_product_coverage(workflow, sealed, run_id, inputs_digest, received_count, inputs):
    if sealed.get("run_id") != run_id or sealed.get("inputs_digest") != inputs_digest \
            or type(sealed.get("received_count")) is not int \
            or sealed["received_count"] != received_count:
        raise IntakeRejected("coverage输入或接收数量身份冲突")
    if sealed.get("schema_version") == "ag1.coverage.v1":
        return _check_product_coverage(workflow, sealed, inputs)
    index_fields = {
        "schema_version", "run_id", "inputs_digest", "received_count",
        "coverage_digest", "counts", "parts", "index_digest",
    }
    if sealed.get("schema_version") != "ag1.coverage.index.v1" \
            or set(sealed) != index_fields \
            or not isinstance(sealed["parts"], list) \
            or not isinstance(sealed["counts"], dict) \
            or set(sealed["counts"]) != set(_COVERAGE_COLLECTIONS) \
            or any(type(count) is not int or count < 0
                   for count in sealed["counts"].values()):
        raise IntakeRejected("coverage根索引合同非法")
    if sealed["index_digest"] != digest({
            key: value for key, value in sealed.items() if key != "index_digest"}) \
            or _coverage_json_size(sealed) >= _COVERAGE_PART_MAX_BYTES:
        raise IntakeRejected("coverage根索引摘要或大小不符")
    coverage = {
        "schema_version": "ag1.coverage.v1", "run_id": run_id,
        "inputs_digest": inputs_digest, "received_count": received_count,
        "entries": [], "segments": [], "unprocessed": [],
        "coverage_digest": sealed["coverage_digest"],
    }
    ref_fields = {
        "part_id", "collection", "part_index", "start", "count",
        "byte_length", "part_digest",
    }
    last_collection = -1
    for ordinal, ref in enumerate(sealed["parts"]):
        if not isinstance(ref, dict) or set(ref) != ref_fields \
                or ref["collection"] not in _COVERAGE_COLLECTIONS \
                or any(type(ref[key]) is not int
                       for key in ("part_index", "start", "count", "byte_length")):
            raise IntakeRejected("coverage分片引用合同非法")
        collection = ref["collection"]
        collection_index = _COVERAGE_COLLECTIONS.index(collection)
        expected_id = (f"COVERAGE-PART::{run_id}::"
                       f"{sealed['coverage_digest']}::{ordinal}")
        if ref["part_id"] != expected_id or ref["part_index"] != ordinal \
                or collection_index < last_collection \
                or ref["start"] != len(coverage[collection]) or ref["count"] <= 0 \
                or not 0 < ref["byte_length"] < _COVERAGE_PART_MAX_BYTES:
            raise IntakeRejected("coverage分片顺序、数量或身份不符")
        try:
            part = workflow.store.get_product_object(
                ref["part_id"], run_id=run_id, kind="coverage_part")
        except ValueError as exc:
            raise IntakeRejected(f"coverage分片持久化合同失效：{exc}") from exc
        expected = _coverage_part_metadata(
            coverage, collection, ordinal, ref["start"], ref["count"])
        if part is None:
            raise IntakeRejected("coverage分片缺失，禁止重新生成掩盖缺失")
        if set(part) != set(expected) | {"items", "part_digest"} \
                or any(part.get(key) != value for key, value in expected.items()) \
                or any(type(part[key]) is not int
                       for key in ("part_index", "start", "count")) \
                or not isinstance(part["items"], list) \
                or len(part["items"]) != ref["count"] \
                or any(not isinstance(item, dict) for item in part["items"]):
            raise IntakeRejected("coverage分片归属、范围或数量不符")
        if part["part_digest"] != ref["part_digest"] \
                or part["part_digest"] != digest({
                    key: value for key, value in part.items() if key != "part_digest"}) \
                or _coverage_json_size(part) != ref["byte_length"]:
            raise IntakeRejected("coverage分片hash或编码长度不符")
        coverage[collection].extend(part["items"])
        last_collection = collection_index
    if any(len(coverage[key]) != sealed["counts"][key] for key in _COVERAGE_COLLECTIONS):
        raise IntakeRejected("coverage集合完整数量不符")
    return _check_product_coverage(workflow, coverage, inputs)


def project_product_materials(workflow, run_id: str, inputs: list[dict], *,
                              require_existing: bool = False) -> dict:
    """复用既有来源和投影服务，只消费冻结blob，封存不可变coverage。"""
    fields = {
        "input_index", "origin_path", "original_filename", "blob_sha256",
        "byte_length", "status", "error",
    }
    if not isinstance(run_id, str) or not run_id.strip() \
            or not isinstance(inputs, list):
        raise IntakeRejected("材料run或输入身份非法")
    for index, item in enumerate(inputs):
        if not isinstance(item, dict) or set(item) != fields \
                or type(item["input_index"]) is not int or item["input_index"] != index \
                or not isinstance(item["origin_path"], str) \
                or not isinstance(item["original_filename"], str) \
                or item["status"] not in {"frozen", "failed", "rejected"}:
            raise IntakeRejected("冻结输入合同或序号非法")
        if item["status"] == "frozen":
            if type(item["byte_length"]) is not int or item["byte_length"] < 0 \
                    or not item["blob_sha256"] or item["error"] is not None:
                raise IntakeRejected("冻结输入缺少原件身份")
        elif item["blob_sha256"] is not None or not item["error"]:
            raise IntakeRejected("失败输入不能声明已冻结原件")
    inputs_digest = digest(inputs)
    object_id = f"COVERAGE::{run_id}"
    sealed = workflow.store.get_product_object(object_id, run_id=run_id, kind="coverage")
    if sealed is not None:
        return _restore_product_coverage(
            workflow, sealed, run_id, inputs_digest, len(inputs), inputs)
    if require_existing:
        raise IntakeRejected(
            "coverage缺失：公开恢复读取禁止重新生成材料覆盖")
    coverage = _build_product_coverage(workflow, run_id, inputs, persist=True)
    _check_product_coverage(workflow, coverage, inputs)
    _seal_product_coverage(workflow, coverage)
    return coverage


def _build_product_coverage(workflow, run_id, inputs, *, persist):
    """同一构建规则分别用于首次封存与只读重建，不写第二份事实源。"""
    coverage = {
        "schema_version": "ag1.coverage.v1", "run_id": run_id,
        "inputs_digest": digest(inputs), "received_count": len(inputs),
        "entries": [], "segments": [], "unprocessed": [],
    }
    checked_bytes, inspections = set(), {}

    def preserve_bytes(data):
        if persist:
            return workflow.blobs.put_bytes(data).sha256
        sha = sha256_hex(data)
        if sha not in checked_bytes:
            if workflow.blobs.read_bytes(sha) != data:
                raise IntakeRejected("coverage原件派生字节不符")
            checked_bytes.add(sha)
        return sha

    def add_entry(item, *, parent=None, member_index=None, member=None):
        material_id = f"MATERIAL::{run_id}::{item['input_index']}"
        locator = {"filename": item["original_filename"]}
        if parent is not None:
            material_id += f"::zip::{member_index}"
            locator.update(member_index=member_index, member=member)
        entry = {
            "material_id": material_id, "input_index": item["input_index"],
            "parent_id": parent, "locator": locator, "status": "pending",
            "blob_sha256": None, "source_id": None, "error": None,
        }
        coverage["entries"].append(entry)
        return entry

    def unprocessed(entry, status, error, locator=None):
        coverage["unprocessed"].append({
            "material_id": entry["material_id"], "source_id": entry["source_id"],
            "locator": entry["locator"] if locator is None else locator,
            "status": status, "error": error,
        })

    def stop_entry(entry, status, error):
        entry.update(status=status, error=error)
        unprocessed(entry, status, error)

    def project_file(entry, item, name, data):
        if not data:
            stop_entry(entry, "empty", "文件为空")
            return
        if Path(name).suffix.lower() == ".zip":
            stop_entry(entry, "unsupported", "嵌套ZIP不展开")
            return
        inspection_key = (entry["blob_sha256"], Path(name).suffix.lower())
        if inspection_key not in inspections:
            inspections[inspection_key] = inspect_attachment(name, data)
        inspection = inspections[inspection_key]
        if inspection.status != "saved":
            stop_entry(entry, inspection.status, inspection.error)
            return
        _product_source(workflow, run_id, entry, name, data, item["origin_path"],
                        media_type=inspection.media_type, persist=persist)
        records = (workflow.project_sources([entry["source_id"]]) if persist else
                   _read_original_projections(
                       workflow, entry["source_id"], entry["blob_sha256"], inspection))
        order = {
            digest({key: value for key, value in locator.items()
                    if key not in {"text", "text_sha256", "char_count"}}): index
            for index, locator in enumerate(inspection.projection.locators)
        }
        records.sort(key=lambda record: order.get(
            digest(record["locator"]),
            len(order) + record["locator"].get("unprocessed_index", 0)))
        segment_count = 0
        unprocessed_count = 0
        for record in records:
            location = record["locator"]
            if record["status"] != "projected" or not record["char_count"]:
                error = record["error"] or "空段落或空表格单元"
                # 旧投影合同以字符串保存PDF缺页，保留其明确页码。
                if error.startswith("page ") and ":" in error:
                    page = error.split(":", 1)[0][5:]
                    if page.isdecimal():
                        location = {"page": int(page)}
                unprocessed(entry, "unprocessed", error, location)
                unprocessed_count += 1
                continue
            raw = workflow.blobs.read_bytes(record["text_blob_sha256"])
            if sha256_hex(raw) != record["text_sha256"]:
                raise IntakeRejected("既有投影片段hash不符")
            text = raw.decode("utf-8")
            if len(text) != record["char_count"]:
                raise IntakeRejected("既有投影片段长度不符")
            byte_offset = location.get("start", 0)
            for offset in range(0, len(text), PRODUCT_MAX_SEGMENT_CHARS):
                chunk = text[offset:offset + PRODUCT_MAX_SEGMENT_CHARS]
                chunk_bytes = chunk.encode("utf-8")
                chunk_locator = dict(location)
                if len(text) > PRODUCT_MAX_SEGMENT_CHARS:
                    chunk_locator.update(char_start=offset, char_end=offset + len(chunk))
                    if "start" in location and "end" in location:
                        chunk_locator.update(start=byte_offset, end=byte_offset + len(chunk_bytes))
                byte_offset += len(chunk_bytes)
                chunk_sha = preserve_bytes(chunk_bytes)
                segment = {
                    "material_id": entry["material_id"], "source_id": entry["source_id"],
                    "locator": chunk_locator, "source_blob_sha256": entry["blob_sha256"],
                    "text_blob_sha256": chunk_sha, "text_sha256": chunk_sha,
                    "char_count": len(chunk), "tool": record["tool"],
                    "projection_id": record["projection_id"],
                }
                segment["segment_id"] = f"SEGMENT::{digest(segment)}"
                coverage["segments"].append(segment)
                segment_count += 1
        entry["status"] = ("partial" if segment_count else "unprocessed") \
            if unprocessed_count else "projected"
        if unprocessed_count:
            entry["error"] = f"{unprocessed_count}处未提取"

    for item in inputs:
        root = add_entry(item)
        if item["status"] != "frozen":
            stop_entry(root, item["status"], item["error"])
            continue
        data = workflow.blobs.read_bytes(item["blob_sha256"])
        if len(data) != item["byte_length"]:
            raise IntakeRejected("冻结原件长度身份不符")
        root["blob_sha256"] = item["blob_sha256"]
        if Path(item["original_filename"]).suffix.lower() != ".zip":
            project_file(root, item, item["original_filename"], data)
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError) as exc:
            stop_entry(root, "failed", f"ZIP读取失败：{type(exc).__name__}: {exc}")
            continue
        with archive:
            infos = archive.infolist()
            children = [
                add_entry(item, parent=root["material_id"], member_index=index,
                          member=_zip_member_display_name(info))
                for index, info in enumerate(infos)
            ]
            error = _product_zip_error(infos)
            if error:
                for entry in [root, *children]:
                    stop_entry(entry, "rejected", error)
                continue
            _product_source(workflow, run_id, root, item["original_filename"],
                            data, item["origin_path"], media_type="application/zip",
                            persist=persist)
            for info, entry in zip(infos, children):
                if info.is_dir():
                    entry["status"] = "directory"
                    continue
                try:
                    with archive.open(info) as handle:
                        member_data = handle.read(info.file_size + 1)
                    if len(member_data) != info.file_size:
                        raise IntakeRejected("ZIP成员长度与中央目录不符")
                except Exception as exc:  # 仅围住成员读取，含各解压库的错误类型。
                    stop_entry(entry, "failed",
                               f"ZIP成员读取失败：{type(exc).__name__}: {exc}")
                    continue
                entry["blob_sha256"] = preserve_bytes(member_data)
                project_file(entry, item,
                             f"{item['original_filename']}!/{entry['locator']['member']}",
                             member_data)
            root["status"] = "partial" if any(
                e["status"] not in {"directory", "projected"} for e in children
            ) else "expanded"
            if not children:
                stop_entry(root, "empty", "ZIP无成员")
    coverage["coverage_digest"] = product_coverage_digest(coverage)
    return coverage
