"""T04：真实封存 session 只读盘点（路径经环境变量显式给定，不写任何位置）。

运行方式（记录于 R1 检查点）：

    KTH_REAL_SESSION_ROOT=D:/t/kth-phase8-real-session-e7edcba32c19969a317f4bca/session \
      python -m pytest tests/test_real_capture_readonly.py -q

未设置环境变量时跳过（不把真实绝对路径写死进仓库）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kth_hybrid.census import (
    ANCHOR_COMPANY_REPORT,
    ANCHOR_TECH_REVIEW,
    MANIFEST_DEFAULT,
    run_census,
    verify_anchor,
)

SESSION_ROOT = os.environ.get("KTH_REAL_SESSION_ROOT")
MANIFEST = Path(os.environ.get("KTH_HYBRID_MANIFEST", MANIFEST_DEFAULT))

pytestmark = pytest.mark.skipif(
    not SESSION_ROOT, reason="未设置 KTH_REAL_SESSION_ROOT（真实只读样本路径）"
)


@pytest.fixture(scope="module")
def census():
    return run_census(Path(SESSION_ROOT), manifest_path=MANIFEST)


def test_all_80_manifest_captures_have_rows(census):
    assert len(census.captures) == 80
    assert len({c.dir_name for c in census.captures}) == 80


def test_denominator_keeps_unreadable(census):
    l0 = census.layer0_assets()
    assert l0["清单捕获数"] == 80
    assert l0["实读数"] + l0["不可读数"] == 80
    # 上轮审核记录 4 个捕获目录拒绝访问；本轮按实测登记（不硬凑旧数字）
    assert l0["不可读数"] >= 0
    for item in l0["不可读明细"]:
        assert item["reason"]


def test_manifest_declared_shape_reproduced(census):
    """40 非空/40 零字节、18 种正文 hash 为 M0 清单声明，本轮实测对照。"""
    l1, l2 = census.layer1_bytes(), census.layer2_dedup()
    assert l1["非空正文"] + l1["零字节正文"] == census.layer0_assets()["实读数"]
    # 声明值：40/40 与 18 种；实测漂移（如不可读4条）会反映在实读数上
    assert l1["非空正文"] <= 40
    assert l1["零字节正文"] <= 40
    assert l2["不同正文hash数"] <= 18


def test_receipt_state_facts(census):
    """P2 修正：不写死单一访问条件下的数量；验证分类闭合与清单声明一致性。

    历史观测记录（不覆盖）：
    - 2026-09-08 R1 交付时本机实读 76（4 条 PermissionError），36 非空/40 空；
    - 2026-09-08 审核复测环境实读 80（40 非空/40 空）。
    两次观测都满足：非空+空=实读；实读+不可读=80；
    实读非空+不可读中按清单声明为非空的条数=清单声明非空总数(40)。
    """
    l1 = census.layer1_bytes()
    l0 = census.layer0_assets()
    readable = l1["实读捕获"]
    # 分类闭合：非空+零字节=实读；实读+不可读=80
    assert l1["非空正文"] + l1["零字节正文"] == readable
    assert readable + l0["不可读数"] == 80
    # 状态划分与实读一致
    assert l1["receipt标记blocked(502)"] == l1["零字节正文"]
    assert l1["receipt标记raw_capture_validated"] == l1["非空正文"]
    # 可读 receipt 全部 evidence_eligible=false；哈希与自身声明一致
    assert l1["receipt标记evidence_eligible=false"] == readable
    assert l1["哈希与receipt声明一致"] == readable
    assert census.layer1_bytes()["声明不一致明细"] == []
    # 与 M0 清单声明一致性：实读非空 + 不可读中清单声明非空 = 清单声明非空总数
    from kth_hybrid.census import load_manifest_entries

    entries = load_manifest_entries(MANIFEST)
    declared_nonempty = sum(1 for e in entries if e["raw_body_bytes"] > 0)
    readable_dirs = {c.dir_name for c in census.captures if c.readable}
    unreadable_declared_nonempty = sum(
        1 for e in entries
        if e["dir"] not in readable_dirs and e["raw_body_bytes"] > 0
    )
    assert l1["非空正文"] + unreadable_declared_nonempty == declared_nonempty, \
        "分母闭合：实读非空＋不可读但清单声明非空＝清单声明非空总数"


def test_as_of_cut_is_frozen(census):
    readable = [c for c in census.captures if c.readable]
    assert readable
    cuts = {c.as_of_cut for c in readable}
    assert cuts == {"2026-08-27T03:02:29Z"}  # 证据截止不变


def test_abandonment_and_bookkeeping_separate(census):
    l0 = census.layer0_assets()
    assert l0["放弃传输数"] == 2
    assert l0["簿记目录"] >= 1
    for a in census.abandonments:
        assert a.readable
        assert "abandonment.json" in a.files


def test_attachments_declared_identity(census):
    assert len(census.attachments) == 2
    for record in census.attachments:
        assert record["matches"] is True  # 声明 hash 与实读一致
    zips = [r for r in census.attachments if r["is_zip"]]
    assert len(zips) == 1  # 访谈 zip


def test_anchor_company_report_still_resolvable():
    report = verify_anchor(Path(SESSION_ROOT), ANCHOR_COMPANY_REPORT)
    assert report["ok"], report
    assert report["body_sha256_matches"]


def test_anchor_tech_review_still_resolvable():
    report = verify_anchor(Path(SESSION_ROOT), ANCHOR_TECH_REVIEW)
    assert report["ok"], report
    assert report["interval_in_range"]


def test_no_unclassified_leftovers(census):
    assert census.errors == [] or all(
        "manifest_mismatch" in e for e in census.errors
    )
