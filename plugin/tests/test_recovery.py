"""T03：恢复——认领竞争、fencing、注入故障、真实进程终止、并发。"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kth_hybrid.journal import CommitRejected, Journal
from kth_hybrid.store import BlobStore, CaseStore, find_uncommitted_blobs

PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"


def test_two_workers_cannot_both_claim(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        first = journal.claim("import:capture-1", "worker-A", "input-hash-1")
        with pytest.raises(CommitRejected, match="不可认领"):
            journal.claim("import:capture-1", "worker-B", "input-hash-1")
        # 被抢占语境：worker-B 拿不到有效 token，无法提交
        fake = type(first)("import:capture-1", "worker-B", first.token + 999,
                           "input-hash-1", first.attempt_no + 1)
        with pytest.raises(CommitRejected, match="fencing"):
            journal.commit(fake, "result-ref")
        journal.commit(first, "result-ref-A")
        assert journal.task_state("import:capture-1")["state"] == "succeeded"
    finally:
        journal.close()


def test_expired_worker_cannot_commit_after_reclaim(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        first = journal.claim("task-1", "worker-A", "input-1")
        journal.record_failure(first, "模拟失败", outcome_unknown=False)
        # 失败后重试是新建 attempt：旧 token 失效
        second = journal.claim("task-1", "worker-B", "input-1")
        assert second.token > first.token
        with pytest.raises(CommitRejected, match="fencing"):
            journal.commit(first, "late-result")
        journal.commit(second, "fresh-result")
        attempts = journal.attempts("task-1")
        assert [a["outcome"] for a in attempts] == ["failed", "succeeded"]
        assert len(attempts) == 2  # 失败 attempt 保留
    finally:
        journal.close()


def test_input_identity_mismatch_rejects_claim_and_commit(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        journal.ensure_task("task-x", "input-actual")
        with pytest.raises(CommitRejected, match="输入身份不符"):
            journal.claim("task-x", "worker-A", "input-forged")
        claim = journal.claim("task-x", "worker-A", "input-actual")
        forged = type(claim)("task-x", "worker-A", claim.token, "input-forged",
                             claim.attempt_no)
        with pytest.raises(CommitRejected, match="输入身份不符"):
            journal.commit(forged, "result")
    finally:
        journal.close()


def test_dispatch_then_crash_is_outcome_unknown_no_blind_redispatch(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    try:
        claim = journal.claim("ext:mission-1", "worker-A", "input-1")
        journal.record_dispatch(claim)  # 外部动作已实际派发并落账
        # 派发后、持久结果前崩溃 → 保守 outcome_unknown
        journal.record_failure(claim, "崩溃于响应落盘前", outcome_unknown=True)
        state = journal.task_state("ext:mission-1")
        assert state["state"] == "outcome_unknown"
        assert state["external_actions"] == 1  # 计数：恰好一次派发记录
        # outcome_unknown 不允许自动重认领（需显式新任务/已验证幂等语义）
        with pytest.raises(CommitRejected, match="不可认领"):
            journal.claim("ext:mission-1", "worker-B", "input-1")
    finally:
        journal.close()


def _child_script(tmp_path: Path) -> Path:
    script = tmp_path / "crash_child.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, r"{PLUGIN_SRC}")
        import os
        from kth_hybrid.store import BlobStore, CaseStore

        work = sys.argv[1]
        phase = sys.argv[2]
        blobs = BlobStore(os.path.join(work, "objects"))
        case = CaseStore(os.path.join(work, "records.sqlite3"))
        run_id = case.new_run(input_digest="digest-child")
        case.init_stages(run_id)
        case.set_stage("intake", "running", run_id)
        # 原件先落盘
        ref = blobs.put_bytes(b"capture-from-child")
        case.add_import_record("capture", "child-origin", ref.sha256)
        if phase == "before_db":
            os._exit(9)  # 硬终止：DB 未提交 source
        case.add_source("src-child", ref.sha256, len(b"capture-from-child"),
                        import_id=1)
        case.set_stage("intake", "succeeded", run_id)
        if phase == "after_db":
            os._exit(9)  # 硬终止：提交后、进程正常收尾前
        print("completed")
    """), encoding="utf-8")
    return script


def _run_child(tmp_path: Path, phase: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(_child_script(tmp_path)), str(tmp_path / "work"), phase],
        capture_output=True, text=True, timeout=120,
    )


def test_real_process_kill_before_db_commit_leaves_orphan(tmp_path):
    result = _run_child(tmp_path, "before_db")
    assert result.returncode == 9  # 真实进程终止，不是可被清理的异常
    blobs = BlobStore(tmp_path / "work" / "objects")
    case = CaseStore(tmp_path / "work" / "records.sqlite3")
    try:
        stage = case.stage_state("intake")
        assert stage[0] == "running"  # 阶段未虚报完成
        orphans = find_uncommitted_blobs(blobs, case)
        assert len(orphans) == 1  # 孤立工件保留可识别
        # 恢复：同内容重导入不产生第二身份，补齐引用后无孤立
        ref_orphan = next(iter(orphans))
        data = blobs.read_bytes(ref_orphan)
        ref2 = blobs.put_bytes(data)
        assert ref2.sha256 == ref_orphan
        import_id = case.add_import_record("capture-recovery", "child-origin", ref2.sha256)
        case.add_source("src-child", ref2.sha256, len(data), import_id=import_id)
        case.set_stage("intake", "succeeded")
        assert find_uncommitted_blobs(blobs, case) == set()
    finally:
        case.close()


def test_real_process_kill_after_db_commit_preserves_transaction(tmp_path):
    result = _run_child(tmp_path, "after_db")
    assert result.returncode == 9
    blobs = BlobStore(tmp_path / "work" / "objects")
    case = CaseStore(tmp_path / "work" / "records.sqlite3")
    try:
        # WAL 恢复：已提交事务完整可见
        source = case.fetch_one("sources", "source_id", "src-child")
        assert source is not None and source["byte_length"] == len(b"capture-from-child")
        assert case.stage_state("intake")[0] == "succeeded"
        assert find_uncommitted_blobs(blobs, case) == set()
        assert blobs.read_bytes(source["blob_sha256"]) == b"capture-from-child"
    finally:
        case.close()


def test_concurrent_claims_through_two_connections(tmp_path):
    # 两个独立连接（≈两个进程）并发认领同一任务：恰好一个成功
    journal_a = Journal(tmp_path / "journal.sqlite3")
    journal_b = Journal(tmp_path / "journal.sqlite3")
    try:
        journal_a.ensure_task("shared-task", "input-1")
        outcomes = []

        def try_claim(j, worker):
            try:
                claim = j.claim("shared-task", worker, "input-1")
                outcomes.append((worker, "claimed", claim.token))
            except CommitRejected as exc:
                outcomes.append((worker, "rejected", str(exc)))

        import threading

        t1 = threading.Thread(target=try_claim, args=(journal_a, "A"))
        t2 = threading.Thread(target=try_claim, args=(journal_b, "B"))
        t1.start(); t2.start(); t1.join(); t2.join()
        claimed = [o for o in outcomes if o[1] == "claimed"]
        assert len(claimed) == 1
        assert len(outcomes) == 2
    finally:
        journal_a.close()
        journal_b.close()
