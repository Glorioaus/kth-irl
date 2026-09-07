"""T03：原件仓与记录库——同内容去重、身份重核、目录逃逸、截断、失败保留。"""

from __future__ import annotations

import sqlite3

import pytest

from kth_hybrid.contracts import BlobRef, sha256_hex
from kth_hybrid.store import (
    BlobStore,
    CaseStore,
    StoreIntegrityError,
    find_uncommitted_blobs,
)


@pytest.fixture()
def blobs(tmp_path):
    return BlobStore(tmp_path / "blobstore")


@pytest.fixture()
def case(tmp_path):
    store = CaseStore(tmp_path / "case" / "records.sqlite3")
    yield store
    store.close()


def test_same_bytes_have_one_content_identity(tmp_path):
    store = BlobStore(tmp_path / "objects")
    a = store.put_bytes(b"source-text")
    b = store.put_bytes(b"source-text")
    assert a.sha256 == b.sha256
    assert a.byte_length == 11
    assert store.read_bytes(a) == b"source-text"


def test_different_bytes_have_different_identity(blobs):
    a = blobs.put_bytes(b"alpha")
    b = blobs.put_bytes(b"beta")
    assert a.sha256 != b.sha256
    assert blobs.read_bytes(a) == b"alpha"


def test_read_bytes_reverifies_tampered_object(blobs):
    ref = blobs.put_bytes(b"immutable-original")
    target = blobs.root / "objects" / ref.sha256[:2] / ref.sha256
    # 直接改写对象文件（模拟拥有文件系统权限的篡改者）
    target.write_bytes(b"tampered!!!!!!")
    with pytest.raises(StoreIntegrityError, match="复核失败"):
        blobs.read_bytes(ref)


def test_truncated_object_fails_visibility(blobs):
    ref = blobs.put_bytes(b"0123456789abcdef")
    target = blobs.root / "objects" / ref.sha256[:2] / ref.sha256
    target.write_bytes(target.read_bytes()[:4])
    with pytest.raises(StoreIntegrityError):
        blobs.read_bytes(ref)


def test_path_traversal_ref_is_rejected(blobs):
    with pytest.raises(StoreIntegrityError, match="非法对象 id"):
        blobs.read_bytes(r"..\..\escape")


def test_read_range_validates_bounds(blobs):
    ref = blobs.put_bytes(b"0123456789")
    assert blobs.read_range(ref, 2, 5) == b"234"
    with pytest.raises(StoreIntegrityError, match="越界"):
        blobs.read_range(ref, 8, 12)
    with pytest.raises(StoreIntegrityError, match="越界"):
        blobs.read_range(ref, 5, 5)


def test_write_failure_does_not_produce_silent_success(blobs, tmp_path, monkeypatch):
    original_replace = __import__("os").replace

    def failing_replace(src, dst, *args, **kwargs):
        raise PermissionError(5, "拒绝访问", str(dst))

    monkeypatch.setattr(__import__("os"), "replace", failing_replace)
    with pytest.raises(PermissionError):
        blobs.put_bytes(b"would-be-orphan")
    monkeypatch.setattr(__import__("os"), "replace", original_replace)
    assert blobs.list_objects() == set()  # 失败不留半成品对象
    # 重试同一内容成功，身份一致
    ref = blobs.put_bytes(b"would-be-orphan")
    assert blobs.has(ref.sha256)


def test_blob_between_file_and_db_commit_is_orphan_not_success(blobs, case):
    # 场景：原件已落盘，进程在 DB 提交前崩溃 → 孤立工件必须可识别且保留
    ref = blobs.put_bytes(b"real-capture-bytes")
    orphans = find_uncommitted_blobs(blobs, case)
    assert orphans == {ref.sha256}
    # 恢复：重新导入同一内容（同身份去重），补齐 DB 引用
    import_id = case.add_import_record("capture", "old-session-path", ref.sha256)
    case.add_source("src-1", ref.sha256, len(b"real-capture-bytes"), import_id=import_id)
    assert find_uncommitted_blobs(blobs, case) == set()
    assert blobs.read_bytes(ref.sha256) == b"real-capture-bytes"


def test_duplicate_source_id_conflict_is_rejected(blobs, case):
    ref = blobs.put_bytes(b"x")
    case.add_source("src-dup", ref.sha256, 1)
    with pytest.raises(sqlite3.IntegrityError):
        case.add_source("src-dup", ref.sha256, 1)


def test_stage_lifecycle_and_run_binding(tmp_path):
    store = CaseStore(tmp_path / "stages.sqlite3")
    try:
        run_id = store.new_run(input_digest="digest-1")
        store.init_stages(run_id)
        assert store.stage_state("intake") == ("pending", run_id)
        store.set_stage("intake", "succeeded", run_id)
        assert store.stage_state("intake") == ("succeeded", run_id)
        with pytest.raises(KeyError):
            store.stage_state("nonexistent-stage")
    finally:
        store.close()
