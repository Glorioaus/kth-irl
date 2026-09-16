"""受控运行时 Provider 派发层（试点范围）。

一次派发恰好对应一个既有队列请求，状态先落盘再行动：
planned → claimed → dispatch_recorded → succeeded / failed / outcome_unknown。
不自动重试；outcome_unknown 一律保守停止，禁止盲重发。

派发层的唯一职责是把已冻结的队列请求变成可核验的真实模型调用凭据：
授权、请求、响应、生产者与任务身份全部绑定进内容寻址封存工件；
消费入口（proposal_requests / review_queue）必须独立验证该链后才接受
runtime_provider 返回。凭据只从受控 env 文件按变量名读取，绝不进入
任何输出、日志、工件或提交。
"""

from __future__ import annotations

import copy
import json
import queue
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import sha256_hex
from .journal import Journal

PROVIDER_PROFILES: dict[str, dict[str, str]] = {
    # 取值来自基线已记录批准部署差异 model_gateway._PROFILES（仅非凭据配置），
    # 2026-09-16 起 glm 按 Owner 指示改走内部网关。
    "glm": {
        "endpoint": "https://aigateway.sunnyoptical.cn/zai-api/v1/chat/completions",
        "model": "glm-5.2",
        "api_key_env": "LLM_API_KEY",
    },
    "deepseek": {
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-pro",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
}
# 已封存工件内嵌授权的核验允许历史批准变体；新派发只用当前 PROVIDER_PROFILES。
_PROFILE_VARIANTS: dict[str, tuple[dict[str, str], ...]] = {
    "glm": (
        PROVIDER_PROFILES["glm"],
        {"endpoint": "https://api.z.ai/api/paas/v4/chat/completions",
         "model": "glm-5.2", "api_key_env": "ZAI_API_KEY"},
    ),
    "deepseek": (PROVIDER_PROFILES["deepseek"],),
}
AUTHORIZATION_SCHEMA = "kth-hybrid.provider-authorization.v1"
ARTIFACT_SCHEMA = "kth-hybrid.provider-dispatch.v1"
TASK_KEY_PREFIX = "provider-dispatch:"
PURPOSES = {"candidate_proposal", "professional_review"}
_ALLOWED_USE_OPTIONS = (
    "third_party_reported_fact", "company_self_statement", "source_discovery",
)


class ProviderGatewayRejected(RuntimeError):
    """派发或验证链不满足合同（确定性失败，可查明原因后另行受控处理）。"""


class ProviderDispatchUnknown(RuntimeError):
    """派发后无法确认外部结果；按既有纪律保守停止，禁止自动重发。"""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n"
    ).encode("utf-8")


def load_env_credential(env_path: Path | str, variable: str) -> str:
    """从受控 KEY=VALUE 文件只读加载指定变量；其余变量不解析、不返回。"""
    path = Path(env_path)
    if not path.is_file():
        raise ProviderGatewayRejected(f"受控env文件不存在：{path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, _, raw = item.partition("=")
        if key.strip() == variable:
            value = raw.strip().strip('"').strip("'")
            if not value:
                raise ProviderGatewayRejected(f"受控env变量为空：{variable}")
            return value
    raise ProviderGatewayRejected(f"受控env缺少变量：{variable}")


def _parse_window(auth: Mapping[str, Any], *, now: datetime | None = None) -> None:
    window = auth.get("window")
    if not isinstance(window, Mapping):
        raise ProviderGatewayRejected("授权缺少window")
    current = now or datetime.now(timezone.utc)
    started = datetime.fromisoformat(window["started"])
    expires = datetime.fromisoformat(window["expires"])
    if not (started <= current <= expires):
        raise ProviderGatewayRejected(
            f"授权窗口无效：{window['started']}~{window['expires']}")


def validate_authorization(auth: Mapping[str, Any], *,
                           now: datetime | None = None) -> None:
    """校验授权结构、窗口与上界；不读取凭据值。"""
    if not isinstance(auth, Mapping) \
            or auth.get("schema_version") != AUTHORIZATION_SCHEMA:
        raise ProviderGatewayRejected("授权schema非法")
    material = auth.get("material")
    if not isinstance(material, Mapping) \
            or not isinstance(material.get("original_sha256"), str) \
            or len(material["original_sha256"]) != 64:
        raise ProviderGatewayRejected("授权材料hash非法")
    purposes = auth.get("purposes")
    if not isinstance(purposes, list) or not purposes \
            or any(item not in PURPOSES for item in purposes):
        raise ProviderGatewayRejected("授权用途非法")
    providers = auth.get("providers")
    if not isinstance(providers, Mapping):
        raise ProviderGatewayRejected("授权providers非法")
    allowed_models: set[str] = set()
    for role in ("primary", "fallback"):
        item = providers.get(role)
        if not isinstance(item, Mapping):
            raise ProviderGatewayRejected(f"授权{role}缺失")
        provider_id = item.get("provider_id")
        variants = _PROFILE_VARIANTS.get(provider_id)
        entry = {"endpoint": item.get("endpoint"),
                 "model": item.get("model"),
                 "api_key_env": item.get("api_key_env")}
        if not variants or entry not in [dict(v) for v in variants]:
            raise ProviderGatewayRejected(f"授权{role}与受控profile不一致")
        allowed_models.add(item["model"])
    caps = auth.get("resource_caps")
    if not isinstance(caps, Mapping):
        raise ProviderGatewayRejected("授权资源上界非法")
    for key in ("max_real_requests", "max_cumulative_input_tokens",
                "max_cumulative_output_tokens"):
        value = caps.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ProviderGatewayRejected(f"授权资源上界{key}非法")
    _parse_window(auth, now=now)


def load_authorization(path: Path | str, *,
                       now: datetime | None = None) -> dict:
    auth = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_authorization(auth, now=now)
    return auth


def authorization_digest(auth: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes(auth))


def _wall_clock_transport(endpoint: str, headers: Mapping[str, str],
                          body: bytes, timeout_seconds: float) -> dict:
    """单次请求；墙钟上限（urllib 超时是空闲读超时，不是总时限）。"""
    result: "queue.Queue[dict]" = queue.Queue(maxsize=1)

    def invoke() -> None:
        request = urllib.request.Request(endpoint, data=body, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
                result.put({"status_code": response.status, "body": payload})
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:4000].decode("utf-8", "replace")
            result.put({"status_code": exc.code, "body": {"http_error": detail}})
        except Exception as exc:  # 连接/协议失败：交给墙钟外的统一映射
            result.put({"transport_error": f"{type(exc).__name__}: {exc}"})

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise TimeoutError(f"provider请求超过墙钟上限{timeout_seconds}s")
    outcome = result.get_nowait()
    if "transport_error" in outcome:
        raise ConnectionError(outcome["transport_error"])
    return outcome


class ProviderDispatcher:
    """把一个已冻结队列请求派发给受控 Provider 并封存调用凭据。"""

    def __init__(self, workflow, *, authorization_path: Path | str,
                 env_path: Path | str,
                 transport: Callable[..., dict] | None = None,
                 timeout_seconds: float = 300.0,
                 now: datetime | None = None) -> None:
        self.workflow = workflow
        self.authorization_path = Path(authorization_path)
        self.env_path = Path(env_path)
        self.authorization = load_authorization(
            self.authorization_path, now=now)
        self.transport = transport or _wall_clock_transport
        self.timeout_seconds = timeout_seconds

    # ---- 资源上界核算（从 Journal 与封存工件推导，不另记账） ----

    def usage(self) -> dict:
        conn = self.workflow.journal._conn
        rows = conn.execute(
            "SELECT a.outcome, a.detail, t.output_ref, t.external_actions "
            "FROM task_attempts a JOIN tasks t ON a.task_key=t.task_key "
            "WHERE a.task_key LIKE ?",
            (TASK_KEY_PREFIX + "%",)).fetchall()
        requests = 0
        input_tokens = 0
        output_tokens = 0
        seen_refs: set[str] = set()
        for outcome, detail, output_ref, external_actions in rows:
            if outcome in ("dispatch_recorded", "succeeded",
                           "outcome_unknown") \
                    or (outcome == "failed" and external_actions > 0):
                requests += 1
            if output_ref:
                seen_refs.add(output_ref)
            if detail and "failure_blob=" in str(detail):
                seen_refs.add(str(detail).split("failure_blob=", 1)[1][:64])
        for blob_sha in sorted(seen_refs):
            try:
                raw = self.workflow.blobs.read_bytes(blob_sha)
            except (KeyError, OSError):
                continue
            artifact = json.loads(raw.decode("utf-8"))
            usage = artifact.get("usage") \
                or artifact.get("response", {}).get("body", {}).get("usage")
            if isinstance(usage, Mapping):
                input_tokens += usage.get("prompt_tokens") or 0
                output_tokens += usage.get("completion_tokens") or 0
        return {"requests": requests, "input_tokens": input_tokens,
                "output_tokens": output_tokens}

    def _check_ready(self, *, purpose: str, provider_id: str,
                     material_blob_sha256: str) -> dict:
        if purpose not in self.authorization["purposes"]:
            raise ProviderGatewayRejected(f"授权不覆盖用途：{purpose}")
        profile = PROVIDER_PROFILES[provider_id]
        authorized = any(
            item.get("provider_id") == provider_id
            for item in self.authorization["providers"].values())
        if not authorized:
            raise ProviderGatewayRejected(f"授权不覆盖provider：{provider_id}")
        if material_blob_sha256 != \
                self.authorization["material"]["original_sha256"]:
            raise ProviderGatewayRejected(
                "队列请求材料不在授权外发范围（hash不符）")
        caps = self.authorization["resource_caps"]
        usage = self.usage()
        if usage["requests"] >= caps["max_real_requests"]:
            raise ProviderGatewayRejected("真实请求次数达到授权上界")
        if usage["input_tokens"] >= caps["max_cumulative_input_tokens"] \
                or usage["output_tokens"] >= caps["max_cumulative_output_tokens"]:
            raise ProviderGatewayRejected("累计token达到授权上界")
        return profile

    # ---- 派发 ----

    def dispatch(self, *, request: Mapping[str, Any], purpose: str,
                 messages: list[dict], output_schema: Mapping[str, Any],
                 provider_id: str = "glm",
                 max_output_tokens: int = 4096,
                 material_blob_sha256: str | None = None,
                 task_suffix: str = "",
                 thinking_enabled: bool = True) -> dict:
        queue_request_id = request["request_id"]
        task_key = f"{TASK_KEY_PREFIX}{queue_request_id}{task_suffix}"
        profile = self._check_ready(
            purpose=purpose, provider_id=provider_id,
            material_blob_sha256=material_blob_sha256
            or _request_material_blob(request, purpose))
        api_key = load_env_credential(self.env_path, profile["api_key_env"])

        payload: dict[str, Any] = {
            "model": profile["model"],
            "messages": [dict(item) for item in messages],
            "max_tokens": max_output_tokens,
            "stream": False,
            "thinking": {
                "type": "enabled" if thinking_enabled else "disabled"},
        }
        payload["temperature"] = 0
        schema_contract = {
            "instruction": (
                "Return exactly one JSON object and no prose. Do not call, "
                "name, or simulate any tool. The JSON must satisfy the exact "
                "output_schema and contain every required key. Every field "
                "declared as an array must remain a JSON array even when it "
                "contains only one item or no items; 即使只有一项也必须使用数组，"
                "不得改成字符串、对象或 null。"
            ),
            "required_output_keys": sorted(output_schema["required"]),
            "output_schema": copy.deepcopy(output_schema),
        }
        payload["messages"] = [
            {"role": "system",
             "content": json.dumps(schema_contract, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))},
            *payload["messages"],
        ]
        payload["response_format"] = {"type": "json_object"}
        request_bytes = canonical_json_bytes(payload)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        journal: Journal = self.workflow.journal
        journal.ensure_task(task_key, request["request_input_digest"])
        claim = journal.claim(task_key, "provider-gateway",
                              request["request_input_digest"])
        journal.record_dispatch(claim)
        try:
            outcome = self.transport(
                profile["endpoint"], headers, request_bytes,
                self.timeout_seconds)
        except (TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            journal.record_failure(claim, f"{type(exc).__name__}",
                                   outcome_unknown=True)
            raise ProviderDispatchUnknown(
                f"派发结果未知，已按outcome_unknown封账，禁止自动重发："
                f"{type(exc).__name__}") from exc
        try:
            artifact = self._seal_success(
                claim=claim, request=request, purpose=purpose,
                provider_id=provider_id, profile=profile, payload=payload,
                outcome=outcome)
        except ProviderGatewayRejected as exc:
            # 确定性失败同样封存原始响应，供审计诊断；不产生新请求。
            failure = {
                "schema_version": "kth-hybrid.provider-dispatch-failure.v1",
                "queue": {
                    "request_id": request["request_id"],
                    "request_input_digest": request["request_input_digest"],
                    "purpose": purpose,
                    "source_mode": "runtime_provider",
                },
                "authorization_digest":
                    authorization_digest(self.authorization),
                "provider": {
                    "provider_id": provider_id,
                    "model": profile["model"],
                    "endpoint": profile["endpoint"],
                },
                "request_payload": copy.deepcopy(payload),
                "response": copy.deepcopy(outcome),
                "error": str(exc),
                "sealed_at": datetime.now(timezone.utc).isoformat(),
            }
            blob = self.workflow.blobs.put_bytes(
                canonical_json_bytes(failure))
            journal.record_failure(
                claim, f"{exc}；failure_blob={blob.sha256}")
            raise
        return artifact

    def _seal_success(self, *, claim, request, purpose, provider_id,
                      profile, payload, outcome) -> dict:
        if "transport_error" in outcome:
            raise ProviderGatewayRejected(
                f"provider传输失败：{outcome['transport_error']}")
        body = outcome.get("body")
        status_code = outcome.get("status_code")
        if status_code != 200:
            detail = ""
            if isinstance(body, Mapping):
                raw_detail = body.get("http_error") or json.dumps(
                    body, ensure_ascii=False)[:200]
                detail = str(raw_detail)[:200]
            raise ProviderGatewayRejected(
                f"provider返回HTTP {status_code}：{detail}")
        choices = body.get("choices") if isinstance(body, Mapping) else None
        if not isinstance(choices, list) or len(choices) != 1 \
                or not isinstance(choices[0], Mapping):
            raise ProviderGatewayRejected("provider响应choices非法")
        finish_state = choices[0].get("finish_reason")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderGatewayRejected("provider响应content为空")
        usage_raw = body.get("usage")
        if not isinstance(usage_raw, Mapping):
            raise ProviderGatewayRejected("provider响应usage缺失")
        usage = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage_raw.get(key)
            if not isinstance(value, int) or value < 0:
                raise ProviderGatewayRejected(f"provider响应usage.{key}非法")
            usage[key] = value
        try:
            model_output = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderGatewayRejected(
                f"provider输出不是合法JSON：{exc}") from exc
        if not isinstance(model_output, dict):
            raise ProviderGatewayRejected("provider输出必须是JSON对象")

        caps = self.authorization["resource_caps"]
        used = self.usage()
        if used["requests"] + 1 > caps["max_real_requests"] \
                or used["input_tokens"] + usage["prompt_tokens"] \
                > caps["max_cumulative_input_tokens"] \
                or used["output_tokens"] + usage["completion_tokens"] \
                > caps["max_cumulative_output_tokens"]:
            raise ProviderGatewayRejected("本次派发将超出授权资源上界")

        artifact_body = {
            "schema_version": ARTIFACT_SCHEMA,
            "queue": {
                "request_id": request["request_id"],
                "request_input_digest": request["request_input_digest"],
                "purpose": purpose,
                "source_mode": "runtime_provider",
            },
            "authorization": copy.deepcopy(self.authorization),
            "authorization_digest": authorization_digest(self.authorization),
            "provider": {
                "provider_id": provider_id,
                "model": profile["model"],
                "endpoint": profile["endpoint"],
            },
            "request_payload": copy.deepcopy(payload),
            "transport": {
                "timeout_seconds": self.timeout_seconds,
                "attempt_task_key": claim.task_key,
            },
            "response": {
                "status_code": status_code,
                "provider_request_id": body.get("id"),
                "model": body.get("model"),
                "finish_state": finish_state,
                "raw": copy.deepcopy(body),
            },
            "usage": usage,
            "model_output": model_output,
            "sealed_at": datetime.now(timezone.utc).isoformat(),
        }
        digest = sha256_hex(canonical_json_bytes(artifact_body))
        artifact = {**artifact_body, "artifact_id": f"PROVDISP::{digest}"}
        blob = self.workflow.blobs.put_bytes(canonical_json_bytes(artifact))
        self.workflow.journal.commit(claim, output_ref=blob.sha256)
        return {**artifact, "artifact_blob_sha256": blob.sha256}


def _request_material_blob(request: Mapping[str, Any],
                           purpose: str) -> str:
    blob = request.get("blob_sha256")
    if isinstance(blob, str) and len(blob) == 64:
        return blob
    source_id = request.get("source_id") \
        or (request.get("authorization") or {}).get("source_id")
    if isinstance(source_id, str) and source_id.startswith("ATT::"):
        return source_id[len("ATT::"):]
    raise ProviderGatewayRejected("无法从队列请求解析材料blob身份")


# ---- 消费入口验证链 ----

def _load_artifact(workflow, provider_dispatch: Mapping[str, Any] | None) -> dict:
    if not isinstance(provider_dispatch, Mapping) \
            or not isinstance(provider_dispatch.get("artifact_blob_sha256"), str):
        raise ProviderGatewayRejected(
            "runtime_provider必须提供已封存派发工件引用")
    blob_sha = provider_dispatch["artifact_blob_sha256"]
    try:
        raw = workflow.blobs.read_bytes(blob_sha)
    except (KeyError, OSError) as exc:
        raise ProviderGatewayRejected(f"派发工件不存在：{blob_sha}") from exc
    if sha256_hex(raw) != blob_sha:
        raise ProviderGatewayRejected("派发工件blob hash不一致")
    artifact = json.loads(raw.decode("utf-8"))
    if artifact.get("schema_version") != ARTIFACT_SCHEMA:
        raise ProviderGatewayRejected("派发工件schema非法")
    return artifact


def verify_runtime_response(queue, request: Mapping[str, Any],
                            response: Mapping[str, Any],
                            provider_dispatch: Mapping[str, Any] | None, *,
                            purpose: str,
                            now: datetime | None = None) -> dict:
    """消费入口的独立验证链：授权、请求、响应、生产者与任务身份。"""
    artifact = _load_artifact(queue, provider_dispatch)
    binding = artifact.get("queue")
    if binding.get("request_id") != request["request_id"] \
            or binding.get("request_input_digest") \
            != request["request_input_digest"] \
            or binding.get("purpose") != purpose \
            or binding.get("source_mode") != "runtime_provider":
        raise ProviderGatewayRejected("派发工件与队列请求身份不一致")
    journal: Journal = queue.journal
    task_key = artifact["transport"]["attempt_task_key"]
    state = journal.task_state(task_key)
    if state is None or state.get("state") != "succeeded" \
            or state.get("output_ref") != \
            provider_dispatch["artifact_blob_sha256"]:
        raise ProviderGatewayRejected(
            "派发任务未按恰好一次合同封账（state/output_ref不符）")
    auth = artifact.get("authorization")
    validate_authorization(auth, now=now)
    if artifact.get("authorization_digest") != authorization_digest(auth):
        raise ProviderGatewayRejected("派发工件授权摘要不一致")
    if purpose not in auth["purposes"]:
        raise ProviderGatewayRejected("授权不覆盖该用途")
    provider = artifact.get("provider", {})
    if not any(item.get("provider_id") == provider.get("provider_id")
               for item in auth["providers"].values()):
        raise ProviderGatewayRejected("授权不覆盖该provider")
    if _request_material_blob(request, purpose) \
            != auth["material"]["original_sha256"]:
        raise ProviderGatewayRejected("请求材料不在授权外发范围")
    if response != derive_response_envelope(artifact, request, purpose=purpose):
        raise ProviderGatewayRejected(
            "返回正文与封存模型输出不一致（不得手工改写）")
    return artifact


def derive_response_envelope(artifact: Mapping[str, Any],
                             request: Mapping[str, Any], *,
                             purpose: str) -> dict:
    """从封存模型输出确定性推导队列返回正文；不含任何手工输入。"""
    if purpose == "candidate_proposal":
        from .proposal_requests import candidate_claim_id
        candidates = []
        for item in artifact["model_output"]["candidates"]:
            enriched = {
                "quote": item["quote"],
                "quote_sha256": request["quote_sha256"],
                "locator": copy.deepcopy(item["locator"]),
                "interpretation": item["interpretation"],
                "subject_scope": item["subject_scope"],
                "dimension_id": item["dimension_id"],
                "criterion_id": item["criterion_id"],
                "mapping": copy.deepcopy(item["mapping"]),
            }
            enriched["claim_id"] = candidate_claim_id(
                source_id=request["source_id"], candidate=enriched)
            candidates.append(enriched)
        return {
            "schema_version": "proposal_response.v1",
            "request_id": request["request_id"],
            "request_input_digest": request["request_input_digest"],
            "producer": {
                "producer_id": artifact["artifact_id"],
                "producer_kind": "runtime_provider",
            },
            "output_schema": request["output_schema"],
            "candidates": candidates,
        }
    if purpose == "professional_review":
        from .review_queue import (
            REQUEST_SCHEMA_V2, RESPONSE_SCHEMA_V2, RESPONSE_SCHEMA,
            _CITATION_FIELDS,
        )
        expected_schema = (
            RESPONSE_SCHEMA_V2
            if request.get("schema_version") == REQUEST_SCHEMA_V2
            else RESPONSE_SCHEMA)
        citation_source = (request.get("authorization")
                           if request.get("schema_version") == REQUEST_SCHEMA_V2
                           else request)
        output = artifact["model_output"]
        return {
            "schema_version": expected_schema,
            "request_id": request["request_id"],
            "request_input_digest": request["request_input_digest"],
            "producer": {
                "producer_id": artifact["artifact_id"],
                "producer_kind": "runtime_provider",
            },
            "output_schema": request["output_schema"],
            "decision": output["decision"],
            "evidence_class": output["evidence_class"],
            "findings": copy.deepcopy(output["findings"]),
            "citations": [
                {key: copy.deepcopy(citation_source[key])
                 for key in _CITATION_FIELDS},
            ],
        }
    raise ProviderGatewayRejected(f"未知用途：{purpose}")


# ---- 受控提示构造（不携带任何人工预填答案或期望等级） ----

_PROPOSAL_SYSTEM = (
    "你是KTH评估流程中的受控证据候选提出器。你只能依据给定的精确投影片段"
    "提出Claim候选：不得引用片段之外的文字，不得猜测，不得输出等级、成熟度、"
    "处置、投资或任何决定字段。片段不足以支持任何判据时返回空candidates。"
    "quote字段必须与给定quote逐字节一致；criterion_id必须取自给定判据清单；"
    "evidence_class必须取自该判据的eligible_evidence_classes；"
    "requested_use必须取自给定选项。"
)

_REVIEW_SYSTEM = (
    "你是KTH评估流程中的受控专业复核器。你只依据给定的精确证据片段与判据"
    "canonical文本判断该证据是否支持该判据：足以支持选supports，不足以支持"
    "选does_not_support。不得输出等级、成熟度、处置、投资或任何决定字段，"
    "不得引入片段之外的假设。findings用简短中文客观描述证据与判据的对应关系。"
)


def _request_subject(request: Mapping[str, Any]) -> str:
    bindings = request.get("evaluation_input_proof_bindings")
    if not isinstance(bindings, Mapping):
        bindings = (request.get("authorization") or {}).get(
            "evaluation_input_proof_bindings")
    try:
        subject = bindings["assessment_unit"]["subject"]["value"]
    except (KeyError, TypeError):
        raise ProviderGatewayRejected("队列请求缺少评估主体绑定")
    if not isinstance(subject, str) or not subject.strip():
        raise ProviderGatewayRejected("队列请求评估主体为空")
    return subject


def build_proposal_messages(request: Mapping[str, Any], catalog: Mapping[str, Any],
                            *, dimension_id: str = "TRL",
                            max_candidates: int = 3) -> tuple[list[dict], dict]:
    criteria = [
        {"criterion_id": row["criterion_id"], "level": row["level"],
         "text": row["text"],
         "eligible_evidence_classes": row["eligible_evidence_classes"]}
        for row in catalog["dimensions"][dimension_id]["registry"]["criteria"]
        if row.get("applicable")
    ]
    user = {
        "purpose": request["purpose"],
        "subject": _request_subject(request),
        "quote": request["quote"],
        "locator": copy.deepcopy(request["locator"]),
        "dimension_id": dimension_id,
        "criteria": criteria,
        "instructions": {
            "max_candidates": max_candidates,
            "candidate_fields": ["quote", "locator", "interpretation",
                                 "subject_scope", "dimension_id",
                                 "criterion_id", "mapping"],
            "mapping_fields": ["criterion_id", "evidence_class",
                               "requested_use"],
            "requested_use_options": list(_ALLOWED_USE_OPTIONS),
            "subject_scope_rule": "subject_scope必须等于给定subject",
            "locator_rule": "locator必须与给定locator完全一致",
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string"},
                        "locator": {"type": "object"},
                        "interpretation": {"type": "string"},
                        "subject_scope": {"type": "string"},
                        "dimension_id": {"type": "string"},
                        "criterion_id": {"type": "string"},
                        "mapping": {
                            "type": "object",
                            "properties": {
                                "criterion_id": {"type": "string"},
                                "evidence_class": {"type": "string"},
                                "requested_use": {"type": "string"},
                            },
                            "required": ["criterion_id", "evidence_class",
                                         "requested_use"],
                        },
                    },
                    "required": ["quote", "locator", "interpretation",
                                 "subject_scope", "dimension_id",
                                 "criterion_id", "mapping"],
                },
            },
        },
        "required": ["candidates"],
    }
    messages = [
        {"role": "system", "content": _PROPOSAL_SYSTEM},
        {"role": "user", "content": json.dumps(
            user, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))},
    ]
    return messages, output_schema


def build_review_messages(request: Mapping[str, Any], catalog: Mapping[str, Any],
                          ) -> tuple[list[dict], dict]:
    authorization = request.get("authorization") or {}
    criterion = authorization.get("canonical_criterion") or {}
    if not criterion:
        raise ProviderGatewayRejected("复核请求缺少canonical判据")
    user = {
        "purpose": request.get("purpose"),
        "subject": _request_subject(request),
        "criterion": {
            "criterion_id": criterion.get("criterion_id"),
            "dimension": criterion.get("dimension"),
            "level": criterion.get("level"),
            "text": criterion.get("text"),
            "eligible_evidence_classes": criterion.get(
                "eligible_evidence_classes"),
        },
        "claim": {
            "excerpt_text": (authorization.get("qualification_input_view")
                             or {}).get("claim", {}).get("excerpt_text"),
            "interpretation": (authorization.get("qualification_input_view")
                               or {}).get("claim", {}).get("interpretation"),
            "evidence_class": authorization.get("evidence_class"),
            "requested_use": authorization.get("requested_use"),
        },
        "quote": authorization.get("quote"),
        "instructions": {
            "decision_options": ["supports", "does_not_support"],
            "evidence_class_rule":
                "evidence_class必须取自该判据的eligible_evidence_classes",
            "findings_keys_hint": [
                "environment_kind", "test_method", "measured_results",
                "requirements_thresholds", "project_specific",
                "configuration_id", "system_boundary",
            ],
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "evidence_class": {"type": "string"},
            "findings": {"type": "object"},
        },
        "required": ["decision", "evidence_class", "findings"],
    }
    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": json.dumps(
            user, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))},
    ]
    return messages, output_schema
