"""中文 CLI：inspect（核验视图）与 trace（判据→封存原文反向追溯）。

输出明确为"证据与判据核验，非正式评估报告"；内部 hash 默认不展示，
工程引用只进审计输出。用法：

    python -m kth_hybrid.cli inspect --case-dir <case>
    python -m kth_hybrid.cli trace --case-dir <case> --result-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import render_trace, trace
from .store import BlobStore, CaseStore

TITLE = "【证据与判据核验，非正式评估报告】"


def _load_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def cmd_inspect(case_dir: Path) -> int:
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        sources = case.fetch_all("sources")
        claims = case.fetch_all("claims")
        quals = case.fetch_all("qualifications")
        gaps = case.fetch_all("gaps")
        results = case.fetch_all("criterion_results")
        imports = case.fetch_all("import_records")
    finally:
        case.close()

    lines = [TITLE, f"Case 目录：{case_dir}", ""]
    lines.append("== 来源（分母含失败采集，不隐藏）==")
    by_status: dict[str, int] = {}
    for s in sources:
        by_status[s["capture_status"]] = by_status.get(s["capture_status"], 0) + 1
    for status, count in sorted(by_status.items()):
        lines.append(f"  - {status}：{count} 条")
    no_pub_proof = sum(1 for s in sources if not s["published_at"])
    lines.append(f"  - 无发布时间证明：{no_pub_proof} 条（时间资格另行判断）")
    lines.append(f"  - 导入记录：{len(imports)} 条（附件/捕获/放弃传输分列）")

    lines.append("")
    lines.append("== 主张与资格 ==")
    lines.append(f"  - 主张候选：{len(claims)} 条")
    by_qstatus: dict[str, int] = {}
    for q in quals:
        by_qstatus[q["status"]] = by_qstatus.get(q["status"], 0) + 1
    if quals:
        for status, count in sorted(by_qstatus.items()):
            lines.append(f"  - 资格 {status}：{count} 条")
    else:
        lines.append("  - 资格：0 条（尚未审查）")
    lines.append(f"  - 缺口：{len(gaps)} 条")
    unreviewed_sources = max(len(sources) - len({c['source_id'] for c in claims}), 0)
    lines.append(f"  - 还有 {unreviewed_sources} 个来源未完成主张提取/资格审查")

    lines.append("")
    lines.append("== 判据结果（产品状态与原生处置分列）==")
    if not results:
        lines.append("  - 无（尚无判据消费）")
    for r in results:
        native = r["native_disposition"] or (
            "null（未调用原版或无对应状态）" + (
                f"；{r['native_note']}" if r["native_note"] else ""))
        lines.append(
            f"  - {r['criterion_id']}（{r['dimension']}）：产品={r['product_status']}，"
            f"原生={native}"
        )
        lines.append(f"    理由：{r['rationale']}")
        lines.append(f"    结果ID：{r['result_id']}（可用 trace 命令追溯原文）")

    census_path = case_dir / "audit" / "evidence-census.json"
    if census_path.exists():
        census = json.loads(census_path.read_text(encoding="utf-8"))
        summary = census.get("summary", {})
        l0 = summary.get("L0_原资产盘点", {})
        l1 = summary.get("L1_字节可用性", {})
        l2 = summary.get("L2_内容去重", {})
        lines.append("")
        lines.append("== 分层证据存量（来自 census 盘点）==")
        lines.append(
            f"  - 清单捕获 {l0.get('清单捕获数')}：实读 {l0.get('实读数')}、"
            f"不可读 {l0.get('不可读数')}（保留在分母）"
        )
        lines.append(
            f"  - 非空正文 {l1.get('非空正文')} 条 = {l2.get('不同正文hash数')} 种正文 hash；"
            f"零字节 {l1.get('零字节正文')} 条"
        )
        lines.append(f"  - 附件 {l0.get('附件数')}；放弃传输 {l0.get('放弃传输数')}（单列）")

    print("\n".join(lines))
    return 0


def cmd_trace(case_dir: Path, result_id: str) -> int:
    blobs = BlobStore(case_dir / "blobs")
    case = CaseStore(case_dir / "records.sqlite3")
    try:
        report = trace(case, blobs, result_id, strict=False)
    finally:
        case.close()
    print(render_trace(report))
    return 0 if report.ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kth_hybrid.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect", help="证据与判据核验视图")
    inspect_parser.add_argument("--case-dir", required=True)
    trace_parser = sub.add_parser("trace", help="判据结果反向追溯封存原文")
    trace_parser.add_argument("--case-dir", required=True)
    trace_parser.add_argument("--result-id", required=True)
    args = parser.parse_args(argv)
    case_dir = Path(args.case_dir)
    if not (case_dir / "records.sqlite3").exists():
        print(f"Case 记录库不存在：{case_dir}", file=sys.stderr)
        return 3
    if args.command == "inspect":
        return cmd_inspect(case_dir)
    return cmd_trace(case_dir, args.result_id)


if __name__ == "__main__":
    raise SystemExit(main())
