"""KTH 本地产品统一 CLI；不包含 Provider 或第二套业务逻辑。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import sys
import tempfile
from typing import Any

from .aggregate import build_offline_dimension_view, validate_offline_dimension_view
from .audit import render_trace, trace, trace_crl_dimension, trace_dimension_result
from .contracts import sha256_hex
from .review_queue import ReviewQueueRejected
from .store import BlobStore, CaseStore
from .workflow import LocalWorkflow, WorkflowRejected

TITLE = "【证据与判据核验，非正式评估报告】"
MAX_CLI_JSON_BYTES = 4 * 1024 * 1024
MAX_AUDIT_DIRECTORY_DEPTH = 24
MAX_AUDIT_TOTAL_ENTRIES = 16_384
MAX_AUDIT_JSON_FILES = 4096
MAX_AUDIT_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_AUDIT_TOTAL_BYTES = 64 * 1024 * 1024


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"错误：命令参数非法：{message}\n")


class CliRejected(ValueError):
    """CLI 文件、路由或输出边界非法。"""


def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                     allow_nan=False))


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CliRejected(f"输出不是规范JSON：{exc}") from exc


def _load_json_file(path: Path, *, label: str) -> Any:
    try:
        size = path.stat().st_size
        if size > MAX_CLI_JSON_BYTES:
            raise CliRejected(f"{label}超过字节上限{MAX_CLI_JSON_BYTES}：{path}")
        data = path.read_bytes()
        if len(data) != size:
            raise CliRejected(f"{label}读取期间发生变化：{path}")
        return json.loads(data.decode("utf-8"))
    except CliRejected:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliRejected(f"{label}读取或JSON解析失败：{path}：{exc}") from exc


def _require_case(case_dir: Path) -> None:
    if not (case_dir / "records.sqlite3").is_file():
        raise CliRejected(f"Case记录库不存在：{case_dir}")


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
    for source in sources:
        by_status[source["capture_status"]] = (
            by_status.get(source["capture_status"], 0) + 1)
    for status, count in sorted(by_status.items()):
        lines.append(f"  - {status}：{count} 条")
    no_pub_proof = sum(1 for source in sources if not source["published_at"])
    lines.append(f"  - 无发布时间证明：{no_pub_proof} 条（时间资格另行判断）")
    lines.append(f"  - 导入记录：{len(imports)} 条（附件/捕获/放弃传输分列）")
    lines.extend(("", "== 主张与资格 ==", f"  - 主张候选：{len(claims)} 条"))
    by_qstatus: dict[str, int] = {}
    for qualification in quals:
        by_qstatus[qualification["status"]] = (
            by_qstatus.get(qualification["status"], 0) + 1)
    if quals:
        for status, count in sorted(by_qstatus.items()):
            lines.append(f"  - 资格 {status}：{count} 条")
    else:
        lines.append("  - 资格：0 条（尚未审查）")
    lines.append(f"  - 缺口：{len(gaps)} 条")
    reviewed_sources = {claim["source_id"] for claim in claims}
    lines.append(f"  - 还有 {max(len(sources) - len(reviewed_sources), 0)} 个来源"
                 "未完成主张提取/资格审查")

    lines.append("")
    lines.append("== 判据结果（产品状态与原生处置分列）==")
    if not results:
        lines.append("  - 无（尚无判据消费）")
    for result in results:
        native = result["native_disposition"] or (
            "null（未调用原版或无对应状态）" +
            (f"；{result['native_note']}" if result["native_note"] else ""))
        lines.append(
            f"  - {result['criterion_id']}（{result['dimension']}）："
            f"产品={result['product_status']}，原生={native}")
        lines.append(f"    理由：{result['rationale']}")
        lines.append(f"    结果ID：{result['result_id']}（可用 trace 命令追溯原文）")

    census_path = case_dir / "audit" / "evidence-census.json"
    if census_path.exists():
        census = json.loads(census_path.read_text(encoding="utf-8"))
        summary = census.get("summary", {})
        l0 = summary.get("L0_原资产盘点", {})
        l1 = summary.get("L1_字节可用性", {})
        l2 = summary.get("L2_内容去重", {})
        lines.extend(("", "== 分层证据存量（来自 census 盘点）=="))
        lines.append(
            f"  - 清单捕获 {l0.get('清单捕获数')}：实读 {l0.get('实读数')}、"
            f"不可读 {l0.get('不可读数')}（保留在分母）")
        lines.append(
            f"  - 非空正文 {l1.get('非空正文')} 条 = "
            f"{l2.get('不同正文hash数')} 种正文 hash；"
            f"零字节 {l1.get('零字节正文')} 条")
        lines.append(f"  - 附件 {l0.get('附件数')}；放弃传输 "
                     f"{l0.get('放弃传输数')}（单列）")
    print("\n".join(lines))
    return 0


def _trace_source(workflow: LocalWorkflow, source_id: str) -> dict:
    source = workflow.store.fetch_one("sources", "source_id", source_id)
    if source is None:
        raise CliRejected(f"来源不存在：{source_id}")
    data = workflow.blobs.read_bytes(source["blob_sha256"])
    broken = []
    if sha256_hex(data) != source["blob_sha256"]:
        broken.append("来源blob摘要不一致")
    if len(data) != source["byte_length"]:
        broken.append("来源blob字节数不一致")
    return {"kind": "source", "object_id": source_id, "ok": not broken,
            "broken": broken, "source": source}


def _trace_projection(workflow: LocalWorkflow, projection_id: str) -> dict:
    projection = workflow.store.get_text_projection(projection_id)
    if projection is None:
        raise CliRejected(f"文本投影不存在：{projection_id}")
    source_report = _trace_source(workflow, projection["source_id"])
    broken = list(source_report["broken"])
    source = source_report["source"]
    if projection["source_blob_sha256"] != source["blob_sha256"]:
        broken.append("投影与来源blob身份不一致")
    text = None
    if projection.get("text_blob_sha256"):
        raw = workflow.blobs.read_bytes(projection["text_blob_sha256"])
        text = raw.decode("utf-8")
        if sha256_hex(raw) != projection["text_sha256"]:
            broken.append("投影文本摘要不一致")
        if len(text) != projection["char_count"]:
            broken.append("投影文本字符数不一致")
    elif projection.get("status") == "projected":
        broken.append("已完成投影缺少文本blob")
    return {
        "kind": "projection", "object_id": projection_id, "ok": not broken,
        "broken": broken, "source": source, "projection": projection,
        "text": text,
    }


def _trace_workflow_object(workflow: LocalWorkflow, object_id: str) -> dict:
    if object_id.startswith(("ATT::", "SRC::")):
        return _trace_source(workflow, object_id)
    if object_id.startswith("PROJ::"):
        return _trace_projection(workflow, object_id)
    if object_id.startswith("REVIEWREQ::"):
        request = workflow.reviews.get_request(object_id)
        projection = _trace_projection(workflow, request["projection_id"])
        broken = list(projection["broken"])
        if request["source_id"] != projection["source"]["source_id"] \
                or request["blob_sha256"] != projection["source"]["blob_sha256"] \
                or request["locator"] != projection["projection"]["locator"] \
                or request["quote_sha256"] != projection["projection"]["text_sha256"]:
            broken.append("复核请求与来源/投影引用闭包不一致")
        return {
            "kind": "review_request", "object_id": object_id,
            "ok": not broken, "broken": broken, "request": request,
            "source": projection["source"],
            "projection": projection["projection"],
        }
    if object_id.startswith("REVIEWRESP::"):
        response = workflow.reviews.get_response(object_id)
        request_report = _trace_workflow_object(workflow, response["request_id"])
        return {
            "kind": "review_response", "object_id": object_id,
            "ok": request_report["ok"], "broken": request_report["broken"],
            "response": response, "request": request_report["request"],
            "source": request_report["source"],
            "projection": request_report["projection"],
        }
    if object_id.startswith("JOB::"):
        job = workflow.status(object_id)
        return {"kind": "workflow_job", "object_id": object_id, "ok": True,
                "broken": [], "job": job}
    raise CliRejected(f"工作流对象ID类型不受支持：{object_id}")


def _find_audit_artifact(case_dir: Path, *, object_id: str,
                         id_field: str, label: str,
                         schema_prefix: str) -> tuple[Path, dict]:
    """只在Case audit树中按顶层精确ID定位一个受控JSON工件。"""
    _prefix, separator, digest = object_id.partition("::")
    if separator != "::" or len(digest) != 64 \
            or any(character not in "0123456789abcdef" for character in digest):
        raise CliRejected(f"{label} ID非法：{object_id}")
    audit_root = (case_dir / "audit").resolve()
    if not audit_root.is_dir():
        raise CliRejected(f"{label}不存在：{object_id}")
    matches = []
    total_entries = 0
    json_files = 0
    total_bytes = 0
    stack = [(audit_root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise CliRejected(f"Case audit目录读取失败：{directory}：{exc}") from exc
        child_directories = []
        try:
            for entry in entries:
                total_entries += 1
                if total_entries > MAX_AUDIT_TOTAL_ENTRIES:
                    raise CliRejected(
                        f"Case audit目录项超过上限{MAX_AUDIT_TOTAL_ENTRIES}")
                if entry.is_symlink():
                    raise CliRejected(f"Case audit禁止符号链接：{entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    child_depth = depth + 1
                    if child_depth > MAX_AUDIT_DIRECTORY_DEPTH:
                        raise CliRejected(
                            "Case audit目录深度超过上限"
                            f"{MAX_AUDIT_DIRECTORY_DEPTH}")
                    child_directories.append((Path(entry.path), child_depth))
                    continue
                if not entry.is_file(follow_symlinks=False) \
                        or not entry.name.lower().endswith(".json"):
                    continue
                json_files += 1
                if json_files > MAX_AUDIT_JSON_FILES:
                    raise CliRejected(
                        f"Case audit JSON文件超过上限{MAX_AUDIT_JSON_FILES}")
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise CliRejected(
                        f"Case audit工件stat失败：{entry.path}：{exc}") from exc
                total_bytes += size
                if total_bytes > MAX_AUDIT_TOTAL_BYTES:
                    raise CliRejected(
                        "Case audit JSON累计字节超过上限"
                        f"{MAX_AUDIT_TOTAL_BYTES}")
                if size > MAX_AUDIT_ARTIFACT_BYTES:
                    raise CliRejected(
                        "Case audit JSON单件字节超过上限"
                        f"{MAX_AUDIT_ARTIFACT_BYTES}")
                path = Path(entry.path)
                try:
                    data = path.read_bytes()
                    if len(data) != size:
                        raise CliRejected(f"Case audit工件读取期间大小变化：{path}")
                    value = json.loads(data.decode("utf-8"))
                except CliRejected:
                    raise
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and value.get(id_field) == object_id \
                        and isinstance(value.get("schema_version"), str) \
                        and value["schema_version"].startswith(schema_prefix):
                    matches.append((path.resolve(), value))
        finally:
            close = getattr(entries, "close", None)
            if callable(close):
                close()
        stack.extend(reversed(child_directories))
    if not matches:
        raise CliRejected(f"{label}不存在：{object_id}")
    if len(matches) != 1:
        paths = "、".join(str(path.relative_to(audit_root))
                         for path, _value in matches)
        raise CliRejected(f"{label}精确ID在audit中不唯一：{paths}")
    return matches[0]


def _trace_aggregation_artifact(case_dir: Path, object_id: str) -> dict:
    if object_id.startswith("AGGMAN::"):
        path, manifest = _find_audit_artifact(
            case_dir, object_id=object_id, id_field="manifest_id",
            label="aggregation manifest",
            schema_prefix="kth-hybrid.aggregation-manifest.")
        with CaseStore(case_dir / "records.sqlite3") as case:
            rebuilt_view = build_offline_dimension_view(
                case, BlobStore(case_dir / "blobs"), manifest)
        return {
            "kind": "aggregation_manifest", "object_id": object_id,
            "ok": True, "broken": [],
            "artifact_path": str(path.relative_to(case_dir.resolve())),
            "manifest": manifest,
            "rebuilt_view_id": rebuilt_view["view_id"],
        }
    path, view = _find_audit_artifact(
        case_dir, object_id=object_id, id_field="view_id", label="六维view",
        schema_prefix="kth-hybrid.offline-six-dimension-view.")
    validate_offline_dimension_view(view)
    _manifest_path, manifest = _find_audit_artifact(
        case_dir, object_id=view["manifest_id"], id_field="manifest_id",
        label="aggregation manifest",
        schema_prefix="kth-hybrid.aggregation-manifest.")
    with CaseStore(case_dir / "records.sqlite3") as case:
        rebuilt = build_offline_dimension_view(
            case, BlobStore(case_dir / "blobs"), manifest)
    if rebuilt != view:
        raise CliRejected("六维view与同Case manifest及精确六维结果重建不一致")
    return {
        "kind": "offline_dimension_view", "object_id": object_id,
        "manifest_id": view["manifest_id"], "ok": True, "broken": [],
        "artifact_path": str(path.relative_to(case_dir.resolve())),
        "view": view,
    }


def cmd_trace(case_dir: Path, result_id: str) -> int:
    if result_id.startswith(("AGGMAN::", "OFFLINE6::")):
        report = _trace_aggregation_artifact(case_dir, result_id)
        _print_json(report)
        return 0
    if result_id.startswith("CRLR2A::"):
        with CaseStore(case_dir / "records.sqlite3") as case:
            report = trace_crl_dimension(case, BlobStore(case_dir / "blobs"), result_id)
        _print_json({"kind": "crl_dimension", "object_id": result_id, **report})
        if not report["ok"]:
            print("错误：CRL维度追溯失败：" + "；".join(report["broken"]),
                  file=sys.stderr)
        return 0 if report["ok"] else 2
    if result_id.startswith("DIMR2::"):
        with CaseStore(case_dir / "records.sqlite3") as case:
            report = trace_dimension_result(
                case, BlobStore(case_dir / "blobs"), result_id)
        _print_json({"kind": "dimension", "object_id": result_id, **report})
        if not report["ok"]:
            print("错误：维度结果追溯失败：" + "；".join(report["broken"]),
                  file=sys.stderr)
        return 0 if report["ok"] else 2
    if result_id.startswith((
            "ATT::", "SRC::", "PROJ::", "REVIEWREQ::", "REVIEWRESP::",
            "JOB::")):
        with LocalWorkflow(case_dir) as workflow:
            report = _trace_workflow_object(workflow, result_id)
        _print_json(report)
        if not report["ok"]:
            print("错误：工作流对象追溯失败：" + "；".join(report["broken"]),
                  file=sys.stderr)
        return 0 if report["ok"] else 2
    blobs = BlobStore(case_dir / "blobs")
    with CaseStore(case_dir / "records.sqlite3") as case:
        report = trace(case, blobs, result_id, strict=False)
    print(render_trace(report))
    if not report.ok:
        print("错误：判据结果追溯失败：" + "；".join(report.broken),
              file=sys.stderr)
    return 0 if report.ok else 2


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value))


def _write_fsynced_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if path.read_bytes() != data:
        raise CliRejected(f"临时工件写后核验不一致：{path.name}")


def _atomic_write_file(path: Path | str, data: bytes) -> None:
    """在同父目录完整落盘并核验后，原子发布一个新文件。"""
    target = Path(path)
    if target.exists():
        raise CliRejected(f"输出文件已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=target.parent)
    os.close(descriptor)
    temp = Path(raw_temp)
    try:
        temp.unlink()
        _write_fsynced_file(temp, data)
        os.rename(temp, target)
    except Exception:
        if temp.exists():
            temp.unlink()
        elif target.is_file():
            target.unlink()
        raise


def _safe_package_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts \
            or any(part in {"", ".", ".."} for part in pure.parts):
        raise CliRejected(f"核验包相对路径非法：{relative}")
    target = root.joinpath(*pure.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise CliRejected(f"核验包相对路径越界：{relative}")
    return target


def _cleanup_temp_directory(temp: Path, parent: Path) -> None:
    resolved = temp.resolve()
    expected_parent = parent.resolve()
    if resolved.parent != expected_parent or not temp.name.startswith("."):
        raise CliRejected(f"拒绝清理边界外临时目录：{temp}")
    if temp.exists():
        shutil.rmtree(temp)


def _atomic_publish_directory(path: Path | str,
                              files: dict[str, bytes]) -> None:
    """在同父路径构建、fsync并逐字核验后，原子发布一个新目录。"""
    target = Path(path)
    if target.exists():
        raise CliRejected(f"输出目录已存在：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        for relative, data in sorted(files.items()):
            _write_fsynced_file(_safe_package_path(temp, relative), data)
        for relative, expected in sorted(files.items()):
            actual = _safe_package_path(temp, relative).read_bytes()
            if len(actual) != len(expected) \
                    or hashlib.sha256(actual).digest() != \
                    hashlib.sha256(expected).digest():
                raise CliRejected(f"核验包临时工件核验失败：{relative}")
        os.rename(temp, target)
    except Exception:
        if temp.exists():
            _cleanup_temp_directory(temp, target.parent)
        elif target.is_dir() and target.resolve().parent == target.parent.resolve():
            shutil.rmtree(target)
        raise


def _export_verification_package(workflow: LocalWorkflow, job_id: str,
                                 output_dir: Path) -> dict:
    if output_dir.exists():
        raise CliRejected(f"核验包输出目录必须尚不存在：{output_dir}")
    status = workflow.status(job_id)
    sources = []
    projections = []
    source_blobs: dict[str, bytes] = {}
    for frozen in status["sources"]:
        report = _trace_source(workflow, frozen["source_id"])
        if not report["ok"]:
            raise CliRejected("核验包来源核验失败：" + "；".join(report["broken"]))
        source = report["source"]
        relative_blob_path = f"blobs/{source['blob_sha256']}.bin"
        data = workflow.blobs.read_bytes(source["blob_sha256"])
        source_blobs[relative_blob_path] = data
        sources.append({**source, "export_blob_path": relative_blob_path})
    for frozen in status["projections"]:
        report = _trace_projection(workflow, frozen["projection_id"])
        if not report["ok"]:
            raise CliRejected("核验包投影核验失败：" + "；".join(report["broken"]))
        projections.append({**report["projection"], "text": report["text"]})
    requests = status["review_requests"]
    responses = []
    for request in requests:
        response = workflow.store.get_review_response_for_request(request["request_id"])
        if response is not None:
            responses.append(workflow.reviews.get_response(response["response_id"]))
    payloads = {
        "job.json": {key: value for key, value in status.items()
                     if key != "review_requests"},
        "status.json": status,
        "sources.json": sources,
        "projections.json": projections,
        "review-requests.json": requests,
        "review-responses.json": responses,
    }
    serialized = {filename: _json_bytes(value)
                  for filename, value in payloads.items()}
    serialized.update(source_blobs)
    files = []
    for filename in sorted(serialized):
        data = serialized[filename]
        files.append({"path": filename, "sha256": hashlib.sha256(data).hexdigest(),
                      "byte_length": len(data)})
    manifest = {
        "schema_version": "kth-local.verification-package.v1",
        "artifact_kind": "核验包", "not_a_formal_report": True,
        "job_id": job_id, "files": files,
    }
    manifest_bytes = _json_bytes(manifest)
    serialized["文件SHA256.json"] = manifest_bytes
    _atomic_publish_directory(output_dir, serialized)
    return {
        "artifact_kind": "核验包", "job_id": job_id,
        "output_dir": str(output_dir.resolve()),
        "manifest_sha256": sha256_hex(manifest_bytes),
        "file_count": len(files),
    }


def _add_case_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--case-dir", required=True, help="明确的Case目录")


def _build_parser() -> ChineseArgumentParser:
    parser = ChineseArgumentParser(prog="kth-local")
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=ChineseArgumentParser)
    inspect_parser = sub.add_parser("inspect", help="证据与判据核验视图")
    _add_case_dir(inspect_parser)
    trace_parser = sub.add_parser("trace", help="按精确ID追溯工件")
    _add_case_dir(trace_parser)
    trace_parser.add_argument("--result-id", required=True,
                              help="精确结果或工作流对象ID")

    case_parser = sub.add_parser("case", help="Case操作")
    case_sub = case_parser.add_subparsers(
        dest="case_command", required=True, parser_class=ChineseArgumentParser)
    case_create = case_sub.add_parser("create", help="创建或修订CaseBasis")
    _add_case_dir(case_create)
    case_create.add_argument("--subject-legal-name", required=True)
    case_create.add_argument("--subject-alias", action="append", default=[])
    case_create.add_argument("--evidence-cutoff", required=True)
    case_create.add_argument("--subject-source-basis", required=True)
    case_create.add_argument("--note")

    intake_parser = sub.add_parser("intake", help="附件导入")
    intake_sub = intake_parser.add_subparsers(
        dest="intake_command", required=True, parser_class=ChineseArgumentParser)
    intake_add = intake_sub.add_parser("add", help="保存明确列出的本地原件")
    _add_case_dir(intake_add)
    intake_add.add_argument("--file", action="append", required=True)

    project_parser = sub.add_parser("project", help="生成机械文本投影")
    _add_case_dir(project_parser)
    project_parser.add_argument("--source-id", action="append", required=True)

    job_parser = sub.add_parser("job", help="工作流job操作")
    job_sub = job_parser.add_subparsers(
        dest="job_command", required=True, parser_class=ChineseArgumentParser)
    job_create = job_sub.add_parser("create", help="从冻结JSON创建job")
    _add_case_dir(job_create)
    job_create.add_argument("--input", required=True)
    job_create.add_argument("--resume-failed-creation", action="store_true")

    for name, help_text in (("status", "读取指定job状态"),
                            ("run", "推进指定job的允许本地阶段"),
                            ("resume", "显式恢复指定failed job")):
        item = sub.add_parser(name, help=help_text)
        _add_case_dir(item)
        item.add_argument("--job-id", required=True)

    review_parser = sub.add_parser("review", help="受控专业复核队列")
    review_sub = review_parser.add_subparsers(
        dest="review_command", required=True, parser_class=ChineseArgumentParser)
    review_list = review_sub.add_parser("list", help="列出指定job请求")
    _add_case_dir(review_list)
    review_list.add_argument("--job-id", required=True)
    review_export = review_sub.add_parser("export-request", help="导出精确请求")
    _add_case_dir(review_export)
    review_export.add_argument("--request-id", required=True)
    review_export.add_argument("--output", required=True)
    review_import = review_sub.add_parser("import", help="导入受控复核返回")
    _add_case_dir(review_import)
    review_import.add_argument("--request-id", required=True)
    review_import.add_argument("--response-file", required=True)
    review_import.add_argument(
        "--source-mode", required=True,
        choices=("manual_import", "simulated", "runtime_provider"))
    review_import.add_argument("--allow-simulated", action="store_true")
    review_consume = review_sub.add_parser("consume", help="消费精确封存返回")
    _add_case_dir(review_consume)
    review_consume.add_argument("--response-id", required=True)
    review_consume.add_argument("--worker-id", required=True)

    export_parser = sub.add_parser("export", help="导出指定job核验包")
    _add_case_dir(export_parser)
    export_parser.add_argument("--job-id", required=True)
    export_parser.add_argument("--output-dir", required=True)
    return parser


def _dispatch(args: argparse.Namespace) -> int:
    case_dir = Path(args.case_dir)
    if args.command == "case" and args.case_command == "create":
        with LocalWorkflow(case_dir) as workflow:
            result = workflow.initialize_case(
                subject_legal_name=args.subject_legal_name,
                subject_aliases=args.subject_alias,
                evidence_cutoff=args.evidence_cutoff,
                subject_source_basis=args.subject_source_basis,
                note=args.note)
        _print_json(result)
        return 0

    _require_case(case_dir)
    if args.command == "inspect":
        return cmd_inspect(case_dir)
    if args.command == "trace":
        return cmd_trace(case_dir, args.result_id)

    with LocalWorkflow(case_dir) as workflow:
        if args.command == "intake":
            result = workflow.import_attachments([Path(item) for item in args.file])
        elif args.command == "project":
            result = workflow.project_sources(args.source_id)
        elif args.command == "job":
            payload = _load_json_file(Path(args.input), label="job输入")
            if not isinstance(payload, dict) or set(payload) != {
                    "assessment_unit", "profile_id", "method_versions", "review_specs"}:
                raise CliRejected("job输入字段必须精确包含assessment_unit、profile_id、"
                                  "method_versions和review_specs")
            result = workflow.create_job(
                **payload, resume_failed_creation=args.resume_failed_creation)
        elif args.command == "status":
            result = workflow.status(args.job_id)
        elif args.command == "run":
            result = workflow.status(args.job_id)
            state = result["state"]
            if state == "failed":
                raise CliRejected("job处于failed；必须使用resume并显式指定job-id")
            next_actions = {
                "awaiting_authorized_analysis": "等待已授权的专业复核返回",
                "response_sealed": "显式消费已封存的专业复核返回",
                "consumed": "专业返回已消费；等待既有确定性求值输入就绪",
            }
            result = {**result, "provider_calls": 0,
                      "next_action": next_actions.get(state, "检查job状态")}
        elif args.command == "resume":
            result = workflow.resume_failed_job(args.job_id)
        elif args.command == "review":
            if args.review_command == "list":
                result = workflow.status(args.job_id)["review_requests"]
            elif args.review_command == "export-request":
                request = workflow.reviews.get_request(args.request_id)
                output = Path(args.output)
                if output.exists():
                    raise CliRejected(f"复核请求输出文件已存在：{output}")
                request_bytes = _json_bytes(request)
                _atomic_write_file(output, request_bytes)
                result = {"request_id": args.request_id,
                          "output": str(output.resolve()),
                          "sha256": sha256_hex(request_bytes)}
            elif args.review_command == "import":
                response = _load_json_file(Path(args.response_file),
                                           label="review response")
                result = workflow.reviews.seal_response(
                    args.request_id, response, source_mode=args.source_mode,
                    allow_simulated=args.allow_simulated)
            else:
                result = workflow.reviews.consume_response(
                    args.response_id, worker_id=args.worker_id)
        elif args.command == "export":
            result = _export_verification_package(
                workflow, args.job_id, Path(args.output_dir))
        else:
            raise CliRejected(f"不支持的命令：{args.command}")
    _print_json(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (CliRejected, WorkflowRejected, ReviewQueueRejected, KeyError,
            ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
