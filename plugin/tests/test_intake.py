"""T04：导入层——附件身份核验、zip 安全、空502、依赖缺失、文本抽取。

合成 fixture 全部标记 ``synthetic=true``；真实素材测试见
``test_real_capture_readonly.py``（只读、路径经环境变量显式给定）。
"""

from __future__ import annotations

import json
import zipfile

import pytest

from kth_hybrid.contracts import sha256_hex
from kth_hybrid.intake import (
    IntakeRejected,
    import_attachment,
    import_capture,
    import_zip_attachment,
    extract_docx_paragraphs,
    extract_pdf_pages,
)
from kth_hybrid.store import BlobStore, CaseStore


@pytest.fixture()
def stack(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    case = CaseStore(tmp_path / "records.sqlite3")
    yield blobs, case
    case.close()


def _synthetic_capture(root, name, *, body: bytes, status="raw_capture_validated",
                       failure=None, response_status=200, include_deps=True):
    d = root / name
    (d / "frozen-capture").mkdir(parents=True)
    (d / "frozen-capture" / "raw-body.bin").write_bytes(body)
    (d / "frozen-capture" / "transport.json").write_text("{}", encoding="utf-8")
    receipt = {
        "schema_version": "synthetic",
        "synthetic": True,
        "capture_status": status,
        "failure_code": failure,
        "response_status": response_status,
        "raw_body_sha256": sha256_hex(body),
        "raw_body_size": len(body),
        "retrieved_at": "2026-09-01T00:00:00Z",
        "fixed_clock": "2026-09-01T00:00:00Z",
        "as_of_cut": "2026-08-27T03:02:29Z",
        "final_url": "https://example.test/synthetic",
        "provider_id": "synthetic-provider",
        "evidence_eligible": False,
        "qualification_candidate_refs": [],
    }
    (d / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False), encoding="utf-8"
    )
    if include_deps:
        for dep in ("request.json", "transport-attempt.json", "live-provider-result.json"):
            (d / dep).write_text('{"synthetic": true}', encoding="utf-8")
    return d


def test_attachment_hash_mismatch_rejected(stack, tmp_path):
    blobs, case = stack
    att = tmp_path / "att.bin"
    att.write_bytes(b"real-bytes")
    with pytest.raises(IntakeRejected, match="身份不符"):
        import_attachment(att, blobs, case, declared_sha256="0" * 64)


def test_attachment_import_creates_source_with_family(stack, tmp_path):
    blobs, case = stack
    att = tmp_path / "att.bin"
    att.write_bytes(b"attachment-content")
    outcome = import_attachment(
        att, blobs, case, declared_sha256=sha256_hex(b"attachment-content"),
        declared_size=len(b"attachment-content"),
        original_filename="合成附件.bin", media_type="application/pdf",
    )
    source = case.fetch_one("sources", "source_id", outcome.source_id)
    assert source["capture_status"] == "attachment"
    assert source["source_family"] == "owner_attachment"
    assert blobs.read_bytes(source["blob_sha256"]) == b"attachment-content"


def test_zip_path_escape_rejected(stack, tmp_path):
    blobs, case = stack
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escape.txt", "payload")
    with pytest.raises(IntakeRejected, match="路径逃逸"):
        import_zip_attachment(evil, blobs, case)


def test_zip_duplicate_names_rejected(stack, tmp_path):
    blobs, case = stack
    dup = tmp_path / "dup.zip"
    with zipfile.ZipFile(dup, "w") as archive:
        archive.writestr("same.txt", "a")
        archive.writestr("same.txt", "b")
    with pytest.raises(IntakeRejected, match="重名"):
        import_zip_attachment(dup, blobs, case)


def test_zip_compression_bomb_rejected(stack, tmp_path):
    blobs, case = stack
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.txt", b"\x00" * (4 * 1024 * 1024))  # 高度可压缩
    with pytest.raises(IntakeRejected, match="压缩炸弹"):
        import_zip_attachment(bomb, blobs, case)


def test_zip_members_imported_with_hashes(stack, tmp_path):
    blobs, case = stack
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("dir/a.txt", "成员A内容")
        archive.writestr("dir/b.txt", "成员B内容")
    outcome = import_zip_attachment(good, blobs, case)
    sources = case.fetch_all("sources")
    members = [s for s in sources if s["capture_status"] == "attachment_zip_member"]
    assert len(members) == 2
    assert all("good.zip!/" in s["locator"] for s in members)
    assert blobs.read_bytes(members[0]["blob_sha256"]).decode("utf-8").startswith("成员")


def test_empty_502_capture_imported_as_failure_not_evidence(stack, tmp_path):
    blobs, case = stack
    cap = _synthetic_capture(tmp_path, "CAP-502", body=b"", status="blocked",
                             failure="http_status", response_status=502)
    result = import_capture(cap, blobs, case)
    assert result.nonempty is False
    source = case.fetch_one("sources", "source_id", result.source_id)
    assert "blocked" in source["capture_status"]
    assert source["capture_status"].endswith("/empty_body")
    # 空502正文存在但永不为 met 通道：字节为空这一事实可被读取
    assert blobs.read_bytes(source["blob_sha256"]) == b""


def test_missing_dependency_recorded_not_silent(stack, tmp_path):
    cap = _synthetic_capture(tmp_path, "CAP-BROKEN", body=b"x", include_deps=False)
    result = import_capture(cap, None, None)
    # 正文与 receipt 可读（可读性=True），但 metadata 依赖不完整单独记录，
    # 不静默当作完整，也不归类为损坏
    assert result.readable is True
    assert result.missing_dependencies == [
        "request.json", "transport-attempt.json", "live-provider-result.json"]


def test_missing_receipt_makes_capture_unreadable(tmp_path):
    cap = _synthetic_capture(tmp_path, "CAP-NORECEIPT", body=b"x")
    (cap / "receipt.json").unlink()
    result = import_capture(cap, None, None)
    assert result.readable is False
    assert "receipt.json" in result.error


def test_pdf_extraction_records_tool_and_pages(stack, tmp_path):
    pytest.importorskip("PyPDF2")
    # 构造最小单页 PDF（合成，空白页无文本层：抽取应标记 unprocessed 而不是假装空白）
    import io

    from PyPDF2 import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    projection = extract_pdf_pages(buffer.getvalue())
    assert projection.tool.startswith("PyPDF2")
    assert projection.locator_kind == "pdf_page"
    assert any("无文本层" in u for u in projection.unprocessed)


def test_docx_extraction_locator_and_hash(tmp_path):
    docx_mod = pytest.importorskip("docx")
    import io

    document = docx_mod.Document()
    document.add_paragraph("合成访谈第一段：受控测试内容。")
    document.add_paragraph("合成访谈第二段。")
    buffer = io.BytesIO()
    document.save(buffer)
    projection = extract_docx_paragraphs(buffer.getvalue())
    assert projection.tool.startswith("python-docx")
    assert len(projection.locators) == 2
    assert projection.locators[0]["paragraph"] == 1
    assert projection.locators[0]["text"].startswith("合成访谈第一段")
    assert projection.locators[0]["text_sha256"] == sha256_hex(
        "合成访谈第一段：受控测试内容。".encode("utf-8")
    )
