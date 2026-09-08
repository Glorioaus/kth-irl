"""T04：全量分层盘点——分母纪律、不可读保留、层次分离、去重。"""

from __future__ import annotations

import json
from pathlib import Path

from kth_hybrid.census import render_chinese, run_census
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore


def _build_synthetic_session(root: Path) -> Path:
    """合成 mini-session：3 条 200 非空（其中 2 条同正文）、1 条 502 空、
    1 条不可读（权限拒绝模拟=目录缺失依赖）、2 条 abandonment、簿记目录。"""
    session = root / "session"
    research = session / "research"
    research.mkdir(parents=True)

    def capture(name, body, status="raw_capture_validated", response=200, failure=None,
                deps=True):
        d = research / name
        (d / "frozen-capture").mkdir(parents=True)
        (d / "frozen-capture" / "raw-body.bin").write_bytes(body)
        (d / "frozen-capture" / "transport.json").write_text("{}", encoding="utf-8")
        receipt = {
            "synthetic": True, "capture_status": status, "failure_code": failure,
            "response_status": response, "raw_body_sha256": sha256_hex(body),
            "raw_body_size": len(body), "retrieved_at": "2026-09-01T00:00:00Z",
            "fixed_clock": "2026-09-01T00:00:00Z", "as_of_cut": "2026-08-27T03:02:29Z",
            "final_url": "https://example.test/" + name,
            "provider_id": "synthetic", "evidence_eligible": False,
            "qualification_candidate_refs": [],
        }
        (d / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        if deps:
            for dep in ("request.json", "transport-attempt.json",
                        "live-provider-result.json"):
                (d / dep).write_text("{}", encoding="utf-8")
        return d

    capture("CAP-A", b"same-body")
    capture("CAP-B", b"same-body")          # 与 A 同正文（同hash组）
    capture("CAP-C", b"other-body")
    capture("CAP-502", b"", status="blocked", response=502, failure="http_status")
    broken = capture("CAP-BROKEN", b"x", deps=False)
    # 删除 receipt 模拟不可读（依赖缺失）
    (broken / "receipt.json").unlink()

    for i, name in enumerate(("ABAND-1", "ABAND-2")):
        d = research / name
        d.mkdir()
        (d / "abandonment.json").write_text('{"synthetic": true}', encoding="utf-8")
        (d / "request.json").write_text("{}", encoding="utf-8")
        (d / "transport-attempt.json").write_text("{}", encoding="utf-8")
        (d / "transport-failure.json").write_text("{}", encoding="utf-8")
    (research / "inventory-history").mkdir()
    (research / "inventory-history" / "bookkeeping.json").write_text("{}", encoding="utf-8")

    attachments = session / "inputs" / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "0000.bin").write_bytes(b"synthetic-attachment")
    (session / "attachment-inventory.json").write_text(json.dumps({
        "attachment_count": 1,
        "attachments": [{
            "attachment_id": "ATTACHMENT-SYNTH",
            "copied_at": "2026-09-08T00:00:00Z", "mime_type": "application/octet-stream",
            "original_filename": "合成附件.bin",
            "sha256": sha256_hex(b"synthetic-attachment"),
            "size": 20, "stored_path": "inputs/attachments/0000.bin",
        }],
    }), encoding="utf-8")

    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "entries": {"golden_case_captures": [
            {"dir": "CAP-A", "raw_body_sha256": sha256_hex(b"same-body"),
             "raw_body_bytes": 9},
            {"dir": "CAP-B", "raw_body_sha256": sha256_hex(b"same-body"),
             "raw_body_bytes": 9},
            {"dir": "CAP-C", "raw_body_sha256": sha256_hex(b"other-body"),
             "raw_body_bytes": 10},
            {"dir": "CAP-502", "raw_body_sha256": sha256_hex(b""),
             "raw_body_bytes": 0},
            {"dir": "CAP-BROKEN", "raw_body_sha256": sha256_hex(b"x"),
             "raw_body_bytes": 1},
        ]},
    }), encoding="utf-8")
    return session


def test_census_layers_keep_denominator(tmp_path):
    session = _build_synthetic_session(tmp_path)
    result = run_census(session, manifest_path=tmp_path / "manifest.json")
    l0 = result.layer0_assets()
    assert l0["清单捕获数"] == 5
    assert l0["实读数"] == 4
    assert l0["不可读数"] == 1  # 不可读保留在分母
    assert l0["不可读明细"][0]["dir"] == "CAP-BROKEN"
    assert l0["附件数"] == 1
    assert l0["放弃传输数"] == 2
    assert l0["簿记目录"] == 1


def test_census_byte_and_dedup_layers(tmp_path):
    session = _build_synthetic_session(tmp_path)
    result = run_census(session, manifest_path=tmp_path / "manifest.json")
    l1 = result.layer1_bytes()
    assert l1["非空正文"] == 3
    assert l1["零字节正文"] == 1
    assert l1["receipt标记blocked(502)"] == 1
    assert l1["receipt标记evidence_eligible=false"] == 4
    l2 = result.layer2_dedup()
    assert l2["非空正文数"] == 3
    assert l2["不同正文hash数"] == 2
    assert l2["同hash组数(组内>1)"] == 1
    assert l2["最大同hash组"] == 2


def test_census_import_writes_sources_and_artifacts(tmp_path):
    session = _build_synthetic_session(tmp_path)
    case_dir = tmp_path / "case"
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        result = run_census(session, manifest_path=tmp_path / "manifest.json",
                            blobs=blobs, case=case, do_import=True)
        sources = case.fetch_all("sources")
        capture_sources = [s for s in sources if s["source_id"].startswith("CAP::")]
        # 5 条清单：CAP-BROKEN 不可读不导入；v2 按采集实例登记——CAP-A/B 同正文
        # 也是两个独立 Source（共享同一 blob），共 4 个
        assert len(capture_sources) == 4
        assert len({s["source_id"] for s in capture_sources}) == 4
        assert len({s["blob_sha256"] for s in capture_sources}) == 3  # blob去重
        empty = [s for s in capture_sources if s["byte_length"] == 0]
        assert len(empty) == 1 and empty[0]["capture_status"].endswith("/empty_body")
        att_sources = [s for s in sources if s["source_id"].startswith("ATT::")]
        assert len(att_sources) == 1
        imports = case.fetch_all("import_records")
        kinds = {r["kind"] for r in imports}
        assert kinds == {"historical_capture", "attachment", "abandoned_transport"}
        assert len([r for r in imports if r["kind"] == "abandoned_transport"]) == 2
        # 导入身份与盘点一致
        assert result.attachments[0]["outcome"]["blob_sha256"] == \
            sha256_hex(b"synthetic-attachment")
    finally:
        case.close()


def test_manifest_mismatch_is_recorded(tmp_path):
    session = _build_synthetic_session(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"]["golden_case_captures"][0]["raw_body_sha256"] = "f" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = run_census(session, manifest_path=tmp_path / "manifest.json")
    assert any("manifest_mismatch" in e for e in result.errors)


def test_render_chinese_mentions_all_layers(tmp_path):
    session = _build_synthetic_session(tmp_path)
    result = run_census(session, manifest_path=tmp_path / "manifest.json")
    text = render_chinese(result.summary(), result.rows())
    for marker in ("L0 原资产盘点", "L1 字节可用性", "L2 内容去重", "L3 主张资格",
                   "L4 判据可用性", "非正式评估报告"):
        assert marker in text
    assert "CAP-502" in text and "CAP-BROKEN" in text  # 失败/不可读可见
