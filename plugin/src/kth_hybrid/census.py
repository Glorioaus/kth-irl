"""全量分层证据盘点（census）。

分母纪律：80 条清单捕获逐条有盘点行；不可读/缺失**保留在分母**；
两附件、两个 abandonment、bookkeeping 单列，不与证据条数混同。

层次（v3 计划 T04 验收口径）：
- L0 原资产盘点：每条是否实读或明确不可读
- L1 字节可用性：本次真正读到的非空正文、metadata 是否闭合
- L2 内容去重：不同正文 hash 数、同 hash 组（独立性未审=未知）
- L3 主张资格：已审 Claim 计数（R1 样本阶段由 T05/T06 填写）
- L4 判据可用性：可支持 criterion 的主张（R1 样本阶段由 T06 填写）
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .intake import (
    AbandonmentImport,
    CaptureImport,
    import_abandonment,
    import_attachment,
    import_capture,
    import_zip_attachment,
)
from .store import BlobStore, CaseStore

# 源码检出时的默认清单；独立安装的历史采集导入应显式传入manifest。
MANIFEST_DEFAULT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "provenance" / "manifest.v1.json"
)

# 已知审计锚点（2026-09-07 架构裁决 W1/W4：区间哈希定位抽查）
ANCHOR_COMPANY_REPORT = {
    "capture": "RESEARCH-ACTION-0c72d7707275f5623f78fcf4dfbf299751c723daba234ac277855825ff6328c2",
    "body_sha256": "718e402e82d5fb1c77550f4e4057b0f39fbb8ab9c49e515227e35523e0ff1b1c",
    "interval": (33267, 33542),
}
ANCHOR_TECH_REVIEW = {
    "capture": "RESEARCH-ACTION-e5a8defc0b3dbfc607c5367cbb18e2714d6ff83699bae40cedd2691f55b51db6",
    "body_sha256": "a42bda71a2363f6ec2cbcd6aac050f28f6f1b606e4d0a27fb73adffbed1047b7",
    "interval": (209967, 210317),
}


@dataclass
class CensusResult:
    session_root: str
    manifest_path: str
    captures: list[CaptureImport] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    abandonments: list[AbandonmentImport] = field(default_factory=list)
    bookkeeping: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # ---- 分层统计 ----

    def layer0_assets(self) -> dict:
        return {
            "清单捕获数": len(self.captures),
            "实读数": sum(1 for c in self.captures if c.readable),
            "不可读数": sum(1 for c in self.captures if not c.readable),
            "不可读明细": [
                {"dir": c.dir_name, "reason": c.error}
                for c in self.captures if not c.readable
            ],
            "附件数": len(self.attachments),
            "放弃传输数": len(self.abandonments),
            "簿记目录": len(self.bookkeeping),
        }

    def layer1_bytes(self) -> dict:
        readable = [c for c in self.captures if c.readable]
        nonempty = [c for c in readable if c.nonempty]
        return {
            "实读捕获": len(readable),
            "非空正文": len(nonempty),
            "零字节正文": sum(1 for c in readable if not c.nonempty),
            "哈希与receipt声明一致": sum(
                1 for c in readable if c.hash_matches_declaration
            ),
            "声明不一致明细": [
                {"dir": c.dir_name, "declared": c.declared_raw_sha256,
                 "actual": c.raw_body_sha256}
                for c in readable if not c.hash_matches_declaration
            ],
            "receipt标记raw_capture_validated": sum(
                1 for c in readable if c.capture_status == "raw_capture_validated"
            ),
            "receipt标记blocked(502)": sum(
                1 for c in readable if c.capture_status == "blocked"
            ),
            "receipt标记evidence_eligible=false": sum(
                1 for c in readable if c.evidence_eligible is False
            ),
            "依赖文件齐全": sum(1 for c in readable if not c.missing_dependencies),
        }

    def layer2_dedup(self) -> dict:
        readable = [c for c in self.captures if c.readable and c.nonempty]
        groups: dict[str, list[str]] = defaultdict(list)
        for c in readable:
            groups[c.raw_body_sha256].append(c.dir_name)
        return {
            "非空正文数": len(readable),
            "不同正文hash数": len(groups),
            "同hash组数(组内>1)": sum(1 for v in groups.values() if len(v) > 1),
            "最大同hash组": max((len(v) for v in groups.values()), default=0),
            "来源独立性": "未知（同hash仅表示字节相同；转载/同源关系未审查）",
        }

    def layer3_claims(self, case: CaseStore | None = None) -> dict:
        """P2 计量单位分离：Claim 数、已审 Claim 数、各资格状态、待提取来源
        分别计数；不以 claims 行数冒充已审数。"""
        claims_total = 0
        audited = 0
        by_status: dict[str, int] = {}
        qualified_distinct_claims = 0
        pending_sources = 0
        if case is not None:
            claims_total = len(case.fetch_all("claims"))
            quals = case.fetch_all("qualifications")
            audited_claims = set()
            qualified_claims = set()
            for q in quals:
                audited_claims.add(q["claim_id"])
                by_status[q["status"]] = by_status.get(q["status"], 0) + 1
                if q["status"] == "qualified":
                    qualified_claims.add(q["claim_id"])
            audited = len(audited_claims)
            qualified_distinct_claims = len(qualified_claims)
            claimed_sources = {c["source_id"] for c in case.fetch_all("claims")}
            pending_sources = sum(
                1 for s in case.fetch_all("sources")
                if s["source_id"] not in claimed_sources
            )
        return {
            "已登记资格候选引用(旧session, 仅候选)": sum(
                c.qualification_candidate_count for c in self.captures if c.readable
            ),
            "Claim总数": claims_total,
            "已完成资格审查的Claim": audited,
            "资格按状态": by_status,
            "已资格主张(distinct qualified claims)": qualified_distinct_claims,
            "未完成主张提取的来源(采集实例)": pending_sources,
            "说明": "资格通过不自动等于 criterion met；N/A 或重复消费不增加已资格"
                    "主张计数（distinct claim 口径）",
        }

    def layer4_criteria(self, case: CaseStore | None = None) -> dict:
        """P2 计量单位分离：criterion 结果与可用关系分别计数。"""
        results_by_status: dict[str, int] = {}
        usable_relations = 0
        if case is not None:
            for r in case.fetch_all("criterion_results"):
                results_by_status[r["product_status"]] = \
                    results_by_status.get(r["product_status"], 0) + 1
                if r["product_status"] == "succeeded":
                    # 可用关系 = succeeded 结果实际引用的 distinct 合格主张数
                    usable_relations += len(_load_list(r["qual_refs"]))
        return {
            "criterion结果按产品状态": results_by_status,
            "可用关系(合格主张→判据消费)": usable_relations,
            "说明": "一条 Claim 可被多个 criterion 消费、N/A 可能没有 Claim；"
                    "两者均不增加'已资格主张'计数",
        }

    def summary(self, case: CaseStore | None = None) -> dict:
        return {
            "schema_version": "kth-rebuild.evidence-census.v1",
            "session_root": self.session_root,
            "manifest_path": self.manifest_path,
            "L0_原资产盘点": self.layer0_assets(),
            "L1_字节可用性": self.layer1_bytes(),
            "L2_内容去重": self.layer2_dedup(),
            "L3_主张资格": self.layer3_claims(case),
            "L4_判据可用性": self.layer4_criteria(case),
            "errors": self.errors,
        }

    def rows(self) -> list[dict]:
        return [asdict(c) for c in self.captures]


def _load_list(value: str | None) -> list:
    try:
        parsed = json.loads(value) if value else []
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def load_manifest_entries(manifest_path: Path = MANIFEST_DEFAULT) -> list[dict]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return manifest["entries"]["golden_case_captures"]


def run_census(session_root: Path | str, *, manifest_path: Path = MANIFEST_DEFAULT,
               blobs: BlobStore | None = None, case: CaseStore | None = None,
               do_import: bool = False) -> CensusResult:
    """扫描封存 session：80 条清单捕获逐条盘点（可选导入），附件/abandonment 单列。"""
    session_root = Path(session_root)
    result = CensusResult(session_root=str(session_root),
                          manifest_path=str(manifest_path))
    manifest_entries = load_manifest_entries(manifest_path)

    research = session_root / "research"
    manifest_declared = {e["dir"]: e for e in manifest_entries}
    for entry in manifest_entries:
        capture_dir = research / entry["dir"]
        imported = import_capture(
            capture_dir, blobs if do_import else None, case if do_import else None,
            session_root=session_root,
        )
        # 与 M0 清单声明对照：清单值 vs 本次实测值不一致必须显式记录
        if imported.readable and (
            entry["raw_body_sha256"] != imported.raw_body_sha256
            or entry["raw_body_bytes"] != imported.raw_body_size
        ):
            result.errors.append(
                f"manifest_mismatch：{entry['dir']} 清单声明 "
                f"{entry['raw_body_sha256']}/{entry['raw_body_bytes']}，"
                f"实测 {imported.raw_body_sha256}/{imported.raw_body_size}"
            )
        result.captures.append(imported)

    # 额外发现：research/ 下不在清单中的目录（abandonment 等），必须显示，不静默并入
    on_disk = {p.name for p in research.iterdir() if p.is_dir()} if research.exists() else set()
    extra = sorted(on_disk - set(manifest_declared) - {"inventory-history"})
    for name in extra:
        dir_path = research / name
        if (dir_path / "abandonment.json").exists():
            result.abandonments.append(
                import_abandonment(dir_path, blobs if do_import else None,
                                   case if do_import else None)
            )
        else:
            result.errors.append(f"清单外目录未分类：{name}")

    bookkeeping = research / "inventory-history"
    if bookkeeping.exists():
        result.bookkeeping = sorted(p.name for p in bookkeeping.iterdir())

    # 附件（显式路径，M0 清单钉定）
    attachments_root = session_root / "inputs" / "attachments"
    inventory_path = session_root / "attachment-inventory.json"
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        for att in inventory.get("attachments", []):
            att_path = session_root / att["stored_path"]
            is_zip = (
                str(att.get("original_filename", "")).lower().endswith(".zip")
                or att.get("mime_type") in
                ("application/zip", "application/x-zip-compressed")
            )
            if do_import and blobs is not None and case is not None:
                if is_zip:
                    outcome = import_zip_attachment(
                        att_path, blobs, case,
                        declared_sha256=att.get("sha256"),
                        original_filename=att.get("original_filename"),
                    )
                else:
                    outcome = import_attachment(
                        att_path, blobs, case,
                        declared_sha256=att.get("sha256"),
                        declared_size=att.get("size"),
                        original_filename=att.get("original_filename"),
                        media_type=att.get("mime_type"),
                    )
                record = {"attachment_id": att.get("attachment_id"),
                          "is_zip": is_zip, "outcome": asdict(outcome)}
            else:
                digest = sha256_of(att_path)
                record = {"attachment_id": att.get("attachment_id"),
                          "is_zip": is_zip,
                          "declared_sha256": att.get("sha256"),
                          "actual_sha256": digest,
                          "matches": digest == att.get("sha256")}
            result.attachments.append(record)
    return result


def sha256_of(path: Path) -> str | None:
    from .contracts import sha256_hex

    try:
        return sha256_hex(path.read_bytes())
    except OSError:
        return None


def verify_anchor(session_root: Path | str, anchor: dict) -> dict:
    """重验已知审计锚点：正文 hash + 区间可定位。"""
    session_root = Path(session_root)
    body_path = session_root / "research" / anchor["capture"] / "frozen-capture" / "raw-body.bin"
    try:
        data = body_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    from .contracts import sha256_hex

    digest = sha256_hex(data)
    start, end = anchor["interval"]
    in_range = 0 <= start < end <= len(data)
    return {
        "ok": digest == anchor["body_sha256"] and in_range,
        "body_sha256_matches": digest == anchor["body_sha256"],
        "interval_in_range": in_range,
        "excerpt_sha256": sha256_hex(data[start:end]) if in_range else None,
    }


def render_chinese(summary: dict, rows: list[dict]) -> str:
    l0, l1, l2, l3, l4 = (summary["L0_原资产盘点"], summary["L1_字节可用性"],
                          summary["L2_内容去重"], summary["L3_主张资格"],
                          summary["L4_判据可用性"])
    lines = [
        "# 微玖 Case 证据存量（分层盘点）",
        "",
        "> 证据与判据核验记录，非正式评估报告。分母为 M0 清单 80 条捕获；"
        "不可读/未审项不从分母消失。",
        "",
        "## L0 原资产盘点",
        f"- 清单捕获 {l0['清单捕获数']} 条：实读 {l0['实读数']}，不可读 {l0['不可读数']}",
        f"- 附件 {l0['附件数']} 件（单列）；显式放弃传输 {l0['放弃传输数']} 条（单列）；"
        f"簿记目录 {l0['簿记目录']} 项",
        "## L1 字节可用性",
        f"- 本次实读非空正文 {l1['非空正文']} 条；零字节 {l1['零字节正文']} 条",
        f"- 哈希与 receipt 声明一致 {l1['哈希与receipt声明一致']} 条"
        f"（不一致 {len(l1['声明不一致明细'])} 条）",
        f"- receipt 状态：raw_capture_validated {l1['receipt标记raw_capture_validated']}、"
        f"blocked(502) {l1['receipt标记blocked(502)']}",
        f"- 可读 receipt 全部 evidence_eligible=false："
        f"{l1['receipt标记evidence_eligible=false']}/{l1['实读捕获']}",
        "## L2 内容去重",
        f"- 非空正文 {l2['非空正文数']} 条对应 {l2['不同正文hash数']} 种正文 hash；"
        f"同 hash 组 {l2['同hash组数(组内>1)']} 组（最大组 {l2['最大同hash组']} 条）",
        f"- 来源独立性：{l2['来源独立性']}",
        "## L3 主张资格（R1.1 计量分离）",
        f"- 旧 session 登记的资格候选引用 {l3['已登记资格候选引用(旧session, 仅候选)']} 条"
        "（仅候选，不继承权威）",
        f"- Claim 总数 {l3['Claim总数']}；已完成资格审查 {l3['已完成资格审查的Claim']}；"
        f"资格按状态 {json.dumps(l3['资格按状态'], ensure_ascii=False)}",
        f"- 已资格主张（distinct qualified claims）"
        f"{l3['已资格主张(distinct qualified claims)']}；"
        f"未完成主张提取的来源（采集实例）"
        f"{l3['未完成主张提取的来源(采集实例)']}",
        f"- {l3['说明']}",
        "## L4 判据可用性（R1.1 计量分离）",
        f"- criterion 结果按产品状态 "
        f"{json.dumps(l4['criterion结果按产品状态'], ensure_ascii=False)}",
        f"- 可用关系（合格主张→判据消费）{l4['可用关系(合格主张→判据消费)']}",
        f"- {l4['说明']}",
        "",
        "## 逐条明细",
        "| 捕获目录 | 实读 | 非空 | 正文hash(前12) | 状态 | 与声明一致 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['dir_name'][:44]}… | {'是' if row['readable'] else '否'} "
            f"| {'是' if row['nonempty'] else '否'} "
            f"| {(row['raw_body_sha256'] or '-')[:12]} "
            f"| {row['capture_status'] or row['error'] or '-'} "
            f"| {'是' if row['hash_matches_declaration'] else '否'} |"
        )
    if summary.get("errors"):
        lines.append("")
        lines.append("## 盘点错误")
        lines.extend(f"- {e}" for e in summary["errors"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="全量分层证据盘点（可导入新 Case）")
    parser.add_argument("--session-root", required=True)
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--manifest", default=str(MANIFEST_DEFAULT))
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="导入到新 Case（否则只读盘点）")
    args = parser.parse_args(argv)

    case_dir = Path(args.case_dir)
    blobs = case = None
    if args.do_import:
        blobs = BlobStore(case_dir / "blobs")
        case = CaseStore(case_dir / "records.sqlite3")
    try:
        result = run_census(args.session_root, manifest_path=Path(args.manifest),
                            blobs=blobs, case=case, do_import=args.do_import)
        summary = result.summary(case=case)
    finally:
        if case is not None:
            case.close()

    audit_dir = case_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "evidence-census.json").write_text(
        json.dumps({"summary": summary, "rows": result.rows(),
                    "attachments": result.attachments,
                    "abandonments": [asdict(a) for a in result.abandonments],
                    "bookkeeping": result.bookkeeping},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (audit_dir / "证据存量.md").write_text(
        render_chinese(summary, result.rows()), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
