"""Codex任务合同；执行观察、来源及候选正文不互相授予权威。"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone

from .contracts import sha256_hex

CONTRACT_VERSION = "ag1.m1.v4"
CAPABILITIES = {"file_read", "code_execution", "isolated_contexts", "execution_records"}
RESULT_FIELDS = {
    "schema_version", "task_id", "run_id", "input_digest", "role", "context_id",
    "source_mode", "token", "started_at", "ended_at",
    "execution_status", "output", "usage",
    "case_id", "dispatch_id",
}
BINDING_FIELDS = {
    "task_id", "run_id", "input_digest", "role", "context_id",
    "source_mode", "token", "started_at",
    "case_id", "dispatch_id",
}


class ProductRejected(ValueError):
    """产品合同拒绝；不转化为业务NO或证据不足。"""


def canonical(value) -> bytes:
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 24:
            raise ProductRejected("json_depth: JSON层级超过24")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ProductRejected("json_keys: 键必须为字符串")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ProductRejected("json_type: 不支持的JSON类型")
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProductRejected("json_value: 非规范JSON") from exc
    if len(data) > 4 * 1024 * 1024:
        raise ProductRejected("json_size: JSON超过4MiB")
    return data


def digest(value) -> str:
    return sha256_hex(canonical(value))


def fields(value, required: set[str], *, label: str) -> None:
    if not isinstance(value, dict) or set(value) != required:
        raise ProductRejected(f"fields:{label}: 字段集合不匹配")


def nonempty(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductRejected(f"text:{label}: 需要非空字符串")
    return value


def reject_final_fields(value) -> None:
    """只检查语义对象的键；原文引文字符串不被当作结构化权限声明。"""
    forbidden = {
        "finaldecision", "maturitylevel", "finalevidence", "evidencequalification",
        "verified", "nativedisposition", "nativelevel", "totalscore", "decisionvalue",
    }
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = "".join(
                    char for char in unicodedata.normalize("NFKC", key).casefold()
                    if char.isalnum())
                if normalized in forbidden:
                    raise ProductRejected(f"forbidden: 候选不得填写最终字段{key}")
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)


def timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("时区缺失")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProductRejected("timestamp: 需要带时区的ISO时间") from exc


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_host(host: dict) -> list[str]:
    fields(host, {"name", "version", "session_id", "capabilities"}, label="host")
    if host["name"] != "codex":
        raise ProductRejected("host: 首版仅支持codex")
    nonempty(host["version"], "host.version")
    nonempty(host["session_id"], "host.session_id")
    fields(host["capabilities"], CAPABILITIES, label="capabilities")
    if any(type(value) is not bool for value in host["capabilities"].values()):
        raise ProductRejected("capabilities: 能力必须为布尔值")
    return [f"missing_capability:{name}" for name in sorted(CAPABILITIES)
            if not host["capabilities"][name]]


def validate_result(task: dict, ticket: dict, envelope: dict) -> dict:
    canonical(envelope)
    fields(envelope, RESULT_FIELDS, label="host-result")
    if envelope["schema_version"] != "ag1.host-result.v1":
        raise ProductRejected("contract: 宿主返回版本不支持")
    if any(envelope[key] != ticket[key] for key in BINDING_FIELDS) \
            or type(envelope["token"]) is not int \
            or any(envelope[key] != task[key]
                   for key in ("task_id", "run_id", "input_digest", "role")):
        raise ProductRejected("binding: 宿主返回与任务/派发票据不一致")
    if timestamp(envelope["ended_at"]) < timestamp(envelope["started_at"]):
        raise ProductRejected("timestamp: 结束早于开始")
    if not isinstance(envelope["execution_status"], str) \
            or envelope["execution_status"] not in {"succeeded", "failed", "cancelled"}:
        raise ProductRejected("execution_status: 不支持的执行终态")
    if envelope["execution_status"] != "succeeded" and envelope["output"] is not None:
        raise ProductRejected("failed_output: 执行失败/取消不能携带业务结果")
    if envelope["execution_status"] == "succeeded" \
            and not isinstance(envelope["output"], dict):
        raise ProductRejected("output: 成功返回须包含候选对象")
    reject_final_fields(envelope["output"])
    fields(envelope["usage"], {"input_tokens", "output_tokens"}, label="usage")
    if any(value is not None and (type(value) is not int or value < 0)
           for value in envelope["usage"].values()):
        raise ProductRejected("usage: 用量必须为非负整数或未知null")
    return json.loads(canonical(envelope))
