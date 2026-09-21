"""Agent产品协调器，所有持久化复用既有Store、BlobStore及Journal。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .agent_host import (
    CONTRACT_VERSION, ProductRejected, canonical, digest, fields,
    nonempty, now, reject_final_fields, timestamp, validate_host, validate_result,
)
from .journal import Claim, CommitRejected
from .workflow import LocalWorkflow

MATERIAL_CONTRACT_VERSION = "ag1.m2.v2"
LEGACY_MATERIAL_CONTRACT_VERSION = "ag1.m2.v1"


class ProductSession:
    def __init__(self, case_dir: Path | str):
        self.workflow = LocalWorkflow(case_dir)
        self.store = self.workflow.store
        self.blobs = self.workflow.blobs
        self.journal = self.workflow.journal
        self.case_id = self.store.product_case_id()

    def _put(self, object_id: str, run_id: str, kind: str, body: dict) -> dict:
        canonical(body)
        return self.store.put_product_object(
            object_id, run_id=run_id, kind=kind, body=body)

    def _get(self, object_id: str, kind: str, run_id: str | None = None) -> dict:
        body = self.store.get_product_object(object_id, kind=kind, run_id=run_id)
        if body is None:
            raise ProductRejected(f"not_found:{kind}: 对象不存在")
        return body

    def _run(self, run_id: str) -> dict:
        run = self._get(run_id, "submission", run_id)
        product_contract = run.get("product_contract")
        if (
                run.get("run_id") != run_id
                or run.get("contract_version") != CONTRACT_VERSION
                or run.get("case_id") != self.case_id
                or product_contract not in {
                    MATERIAL_CONTRACT_VERSION, LEGACY_MATERIAL_CONTRACT_VERSION}
                or "RUN::" + digest({
                    "case_id": self.case_id,
                    "request": run["request"],
                    "inputs": run.get("inputs"),
                    "contract_version": CONTRACT_VERSION,
                    "product_contract": product_contract,
                }) != run_id
        ):
            raise ProductRejected("integrity: 提交身份/合同不一致")
        if product_contract not in {
                MATERIAL_CONTRACT_VERSION, LEGACY_MATERIAL_CONTRACT_VERSION}:
            raise ProductRejected("legacy_contract: 旧入口没有冻结材料，不能冒充M2")
        return run

    def prepare_submission(self, request: dict) -> dict:
        self.store.assert_product_task_inventory()
        request = json.loads(canonical(request))
        if not isinstance(request, dict):
            raise ProductRejected("fields:submission: 需要JSON对象")
        required = {"schema_version", "question", "attachments", "host", "mode"}
        fields(request, required | ({"revision"} if "revision" in request else set()),
               label="submission")
        if request["schema_version"] != "ag1.submission.v1" \
                or not isinstance(request["mode"], str) \
                or request["mode"] not in {"offline", "host"}:
            raise ProductRejected("contract: 提交版本或模式非法")
        nonempty(request["question"], "question")
        paths = request["attachments"]
        if not isinstance(paths, list) or not 1 <= len(paths) <= 256 \
                or any(not isinstance(path, str) or not Path(path).is_absolute()
                       for path in paths):
            raise ProductRejected("attachments: 需要1至256个显式绝对路径")
        blockers = validate_host(request["host"])
        if request["mode"] != "offline":
            blockers.append("authorization_required:G2")
        if "revision" in request:
            fields(request["revision"], {"parent_run_id", "reason"}, label="revision")
            self._run(request["revision"]["parent_run_id"])
            nonempty(request["revision"]["reason"], "revision.reason")
        from .intake import freeze_product_inputs

        inputs = [] if blockers else freeze_product_inputs(paths, self.blobs)
        run_id = "RUN::" + digest({
            "case_id": self.case_id, "request": request, "inputs": inputs,
            "contract_version": CONTRACT_VERSION,
            "product_contract": MATERIAL_CONTRACT_VERSION})
        run = self._put(run_id, run_id, "submission", {
            "run_id": run_id, "contract_version": CONTRACT_VERSION,
            "case_id": self.case_id, "product_contract": MATERIAL_CONTRACT_VERSION,
            "request": request, "blockers": blockers, "inputs": inputs,
        })
        if not blockers:
            self.materials(run_id, create=True)
        return run

    def materials(self, run_id: str, *, create: bool = False) -> dict:
        from .intake import product_coverage_digest, project_product_materials

        run = self._run(run_id)
        if run["blockers"]:
            raise ProductRejected("blocked: 无材料处理能力或授权")
        for item in run["inputs"]:
            if item["blob_sha256"] is not None:
                data = self.blobs.read_bytes(item["blob_sha256"])
                if len(data) != item["byte_length"]:
                    raise ProductRejected("integrity: 冻结输入长度不符")
        existing = project_product_materials(
            self.workflow, run_id, run["inputs"],
            require_existing=not create,
        )
        expected = product_coverage_digest({
            key: value for key, value in existing.items() if key != "coverage_digest"})
        if existing["run_id"] != run_id or expected != existing["coverage_digest"] \
                or existing["received_count"] != len(run["inputs"]):
            raise ProductRejected("integrity: 材料覆盖身份不一致")
        for segment in existing["segments"]:
            text = self.blobs.read_bytes(segment["text_blob_sha256"])
            from .contracts import sha256_hex

            if sha256_hex(text) != segment["text_sha256"] \
                    or len(text.decode("utf-8")) != segment["char_count"]:
                raise ProductRejected("integrity: 派生片段身份不一致")
        return existing

    def discovery_batches(self, run_id: str) -> list[dict]:
        coverage = self.materials(run_id)
        batches, items, size = [], [], 0
        for segment in coverage["segments"]:
            text = self.blobs.read_bytes(segment["text_blob_sha256"]).decode("utf-8")
            for start in range(0, max(1, len(text)), 32768):
                chunk = text[start:start + 32768]
                if items and size + len(chunk) > 32768:
                    batches.append({"items": items})
                    items, size = [], 0
                items.append({
                    "segment_id": segment["segment_id"],
                    "start": start, "end": start + len(chunk), "text": chunk,
                })
                size += len(chunk)
        if items:
            batches.append({"items": items})
        return [{
            "batch_id": f"{run_id}::discovery::{index}",
            "coverage_digest": coverage["coverage_digest"], **batch,
        } for index, batch in enumerate(batches)]

    def _segment_text(self, coverage: dict, segment_id: str) -> str:
        segment = next((item for item in coverage["segments"]
                        if item["segment_id"] == segment_id), None)
        if segment is None:
            raise ProductRejected("citation: 片段不属于当前材料")
        return self.blobs.read_bytes(segment["text_blob_sha256"]).decode("utf-8")

    def propose_scope(self, run_id: str, *, task_id: str) -> dict:
        from .unit_scope import validate_candidate

        self.store.assert_product_task_inventory()
        task = self._task(task_id)
        if task["run_id"] != run_id or task["role"] != "scope_discovery":
            raise ProductRejected("binding: 发现任务不属于本次范围")
        envelope = self._sealed(task)
        if envelope is None or envelope["execution_status"] != "succeeded":
            raise ProductRejected("scope_candidate: 需要成功封存的发现返回")
        coverage = self.materials(run_id)
        output = validate_candidate(
            envelope["output"], coverage,
            lambda segment_id: self._segment_text(coverage, segment_id))
        candidate = {
            **output, "run_id": run_id, "task_id": task_id,
            "response_digest": digest(envelope), "source_mode": envelope["source_mode"],
        }
        candidate["candidate_digest"] = digest(candidate)
        self.advance(run_id)
        return self._put("SCOPE-CANDIDATE::" + run_id, run_id, "scope_candidate", candidate)

    def _candidate(self, run_id: str) -> dict:
        candidate = self._get("SCOPE-CANDIDATE::" + run_id, "scope_candidate", run_id)
        task = self._task(candidate["task_id"])
        envelope = self._sealed(task)
        if candidate["run_id"] != run_id or task["run_id"] != run_id \
                or envelope is None or digest(envelope) != candidate["response_digest"] \
                or digest({key: value for key, value in candidate.items()
                           if key != "candidate_digest"}) != candidate["candidate_digest"]:
            raise ProductRejected("integrity: 范围候选失去上游返回")
        return candidate

    def confirm_scope(self, run_id: str, confirmation: dict) -> dict:
        from .unit_scope import validate_confirmation

        self.store.assert_product_task_inventory()
        candidate = self._candidate(run_id)
        coverage = self.materials(run_id)
        frozen = validate_confirmation(
            confirmation, candidate, coverage,
            lambda segment_id: self._segment_text(coverage, segment_id))
        body = {**frozen, "run_id": run_id, "source_mode": "simulated",
                "coverage_digest": coverage["coverage_digest"]}
        body["scope_digest"] = digest(body)
        existing = self.store.get_product_object(
            "SCOPE::" + run_id, run_id=run_id, kind="scope")
        if existing is not None and existing != body:
            raise ProductRejected("new_run: 材料/范围/时点改变须创建新run")
        return self._put("SCOPE::" + run_id, run_id, "scope", body)

    def scope(self, run_id: str) -> dict:
        from .unit_scope import validate_confirmation

        self.store.assert_product_task_inventory()
        self._run(run_id)
        scope = self._get("SCOPE::" + run_id, "scope", run_id)
        candidate = self._candidate(run_id)
        coverage = self.materials(run_id)
        fields_to_remove = {"run_id", "source_mode", "coverage_digest", "scope_digest",
                            "changes"}
        checked = validate_confirmation(
            {key: value for key, value in scope.items() if key not in fields_to_remove},
            candidate, coverage,
            lambda segment_id: self._segment_text(coverage, segment_id))
        if checked["changes"] != scope["changes"] \
                or scope["coverage_digest"] != coverage["coverage_digest"] \
                or digest({key: value for key, value in scope.items()
                           if key != "scope_digest"}) != scope["scope_digest"]:
            raise ProductRejected("integrity: 冻结范围身份不一致")
        return scope

    def prepare_task(self, run_id: str, *, role: str, payload: dict) -> dict:
        self.store.assert_product_task_inventory()
        run = self._run(run_id)
        if run["blockers"]:
            raise ProductRejected("blocked: 必需能力或授权缺失")
        if role != "scope_discovery":
            raise ProductRejected("scope_unconfirmed: 未确认范围不得创建专业任务")
        if not isinstance(payload, dict):
            raise ProductRejected("payload: 任务输入必须为对象")
        canonical(payload)
        reject_final_fields(payload)
        if set(payload) - {"instruction"}:
            raise ProductRejected("fields:scope_discovery: 输入仅接受instruction")
        if "instruction" in payload:
            nonempty(payload["instruction"], "instruction")
        coverage = self.materials(run_id)
        body = {
            "contract_version": CONTRACT_VERSION, "run_id": run_id,
            "case_id": self.case_id,
            "role": role, "payload": payload,
            "allowed_materials": [
                {"material_id": item["material_id"], "blob_sha256": item["blob_sha256"]}
                for item in coverage["entries"] if item["blob_sha256"] is not None],
            "host_session_id": run["request"]["host"]["session_id"],
            "coverage_digest": coverage["coverage_digest"],
        }
        input_digest = digest(body)
        task_id = "TASK::" + input_digest
        task = self._put(task_id, run_id, "task", {
            **body, "input_digest": input_digest, "task_id": task_id,
        })
        self.journal.ensure_task(task_id, input_digest)
        return task

    def _task_snapshot(self, run_id: str | None = None) -> dict:
        """先核验整个Case任务/执行关系，再按run提供本次调用快照。"""
        with self.store.product_read_view():
            records = self.store.read_product_task_records()
            self.store.assert_product_task_inventory(records)
            objects = {item["object_id"]: item for item in records["objects"]}
            journal = {
                item["task_key"]: item for item in records["journal_tasks"]
            }
            attempts = {}
            for item in records["attempts"]:
                attempts.setdefault(item["task_key"], []).append(item)
            tasks = []
            for object_id, item in objects.items():
                if item["kind"] != "task":
                    continue
                task = item["body"]
                run = objects.get(task["run_id"])
                if run is None or run["kind"] != "submission":
                    raise ProductRejected("integrity: 产品任务父run不存在")
                run_body = run["body"]
                expected_run = "RUN::" + digest({
                    "case_id": self.case_id,
                    "request": run_body["request"],
                    "inputs": run_body["inputs"],
                    "contract_version": CONTRACT_VERSION,
                    "product_contract": run_body["product_contract"],
                })
                if run_body["run_id"] != expected_run \
                        or object_id != task["task_id"] \
                        or task["case_id"] != self.case_id \
                        or task["contract_version"] != CONTRACT_VERSION:
                    raise ProductRejected("integrity: 任务父身份无法重建")
                journal_row = journal.get(object_id)
                tasks.append({
                    "task_id": object_id,
                    "body": task,
                    "run": run_body,
                    "journal": journal_row,
                    "attempts": attempts.get(object_id, []),
                })
            tasks.sort(key=lambda item: item["task_id"])
            if run_id is not None:
                tasks = [item for item in tasks
                         if item["body"]["run_id"] == run_id]
            return {"tasks": tasks, "objects": objects}

    def _task(self, task_id: str) -> dict:
        snapshot = self._task_snapshot()
        item = next((item for item in snapshot["tasks"]
                     if item["task_id"] == task_id), None)
        if item is None:
            raise ProductRejected("not_found: 产品任务不存在")
        task = item["body"]
        run = self._run(task["run_id"])
        coverage = self.materials(task["run_id"])
        body = {key: value for key, value in task.items()
                if key not in {"task_id", "input_digest"}}
        if task_id != "TASK::" + digest(body) \
                or task["input_digest"] != digest(body) \
                or task["case_id"] != run["case_id"] \
                or task["contract_version"] != CONTRACT_VERSION \
                or task["allowed_materials"] != [
                    {"material_id": item["material_id"], "blob_sha256": item["blob_sha256"]}
                    for item in coverage["entries"] if item["blob_sha256"] is not None] \
                or task["host_session_id"] != run["request"]["host"]["session_id"] \
                or task["coverage_digest"] != coverage["coverage_digest"]:
            raise ProductRejected("integrity: 任务输入摘要不一致")
        reject_final_fields(task["payload"])
        return task

    def pending_tasks(self, run_id: str) -> list[dict]:
        run = self._run(run_id)
        if run["blockers"]:
            return []
        self.materials(run_id)
        snapshot = self._task_snapshot(run_id)
        return [item["body"] for item in snapshot["tasks"]
                if item["journal"] is not None
                and item["journal"]["state"] == "planned"]

    def begin_task(self, task_id: str, *, context_id: str, source_mode: str) -> dict:
        self.store.assert_product_task_inventory()
        task = self._task(task_id)
        run = self._run(task["run_id"])
        if run["request"]["mode"] != "offline" or source_mode != "simulated":
            raise ProductRejected("authorization: 无G2许可，禁止真实或冒充来源派发")
        nonempty(context_id, "context_id")
        if len(context_id.encode("utf-8")) > 512:
            raise ProductRejected("context_id: 上下文标识不得超过512字节")
        if self.journal.task_state(task_id)["state"] != "planned":
            raise ProductRejected("already_dispatched: 不自动重派既有任务")
        try:
            claim = self.journal.claim(task_id, context_id, task["input_digest"])
        except CommitRejected as exc:
            raise ProductRejected("already_dispatched: 任务已被认领") from exc
        ticket = {
            **{key: task[key] for key in ("task_id", "run_id", "input_digest", "role")},
            "context_id": context_id, "source_mode": source_mode,
            "token": claim.token, "attempt_no": claim.attempt_no,
            "started_at": now(), "contract_version": CONTRACT_VERSION,
            "independence_status": "simulated",
            "case_id": self.case_id, "dispatch_id": str(uuid.uuid4()),
        }
        self._put("DISPATCH::" + task_id, task["run_id"], "dispatch", ticket)
        self.journal.record_dispatch(claim)
        return ticket

    @staticmethod
    def _claim(ticket: dict) -> Claim:
        return Claim(ticket["task_id"], ticket["context_id"], ticket["token"],
                     ticket["input_digest"], ticket["attempt_no"])

    def _ticket(self, task: dict) -> dict:
        ticket = self._get("DISPATCH::" + task["task_id"], "dispatch", task["run_id"])
        fields(ticket, {
            "task_id", "run_id", "input_digest", "role", "context_id", "source_mode",
            "token", "attempt_no", "started_at", "contract_version",
            "independence_status", "case_id", "dispatch_id",
        }, label="dispatch")
        state = self.journal.task_state(task["task_id"])
        if any(ticket[key] != task[key] for key in (
                "task_id", "run_id", "input_digest", "role", "case_id",
                "contract_version")) \
                or ticket["source_mode"] != "simulated" \
                or ticket["independence_status"] != "simulated" \
                or type(ticket["token"]) is not int or ticket["token"] <= 0 \
                or type(ticket["attempt_no"]) is not int or ticket["attempt_no"] <= 0 \
                or state["current_token"] != ticket["token"] \
                or state["current_worker"] != ticket["context_id"] \
                or state["input_id"] != ticket["input_digest"]:
            raise ProductRejected("integrity: 派发票据与任务/台账合同不一致")
        nonempty(ticket["context_id"], "context_id")
        if len(ticket["context_id"].encode("utf-8")) > 512:
            raise ProductRejected("integrity: 派发上下文标识超限")
        try:
            uuid.UUID(ticket["dispatch_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ProductRejected("integrity: 非法派发实例标识") from exc
        timestamp(ticket["started_at"])
        attempts = self.journal.attempts(task["task_id"])
        if not any(item["attempt_no"] == ticket["attempt_no"]
                   and item["token"] == ticket["token"]
                   and item["worker_id"] == ticket["context_id"]
                   and item["input_id"] == ticket["input_digest"]
                   and item["outcome"] == state["state"] for item in attempts):
            raise ProductRejected("integrity: 派发票据缺少对应attempt")
        return ticket

    def _sealed(self, task: dict) -> dict | None:
        ref = self.store.get_product_object(
            "RESPONSE::" + task["task_id"], run_id=task["run_id"], kind="response")
        if ref is None:
            state = self.journal.task_state(task["task_id"])
            if state["state"] in {"succeeded", "failed"} \
                    and state["external_actions"] > 0:
                raise ProductRejected(
                    "integrity: Journal终态缺少必需封存返回")
            return None
        fields(ref, {"task_id", "blob_sha256", "result_digest"}, label="response")
        ticket = self._ticket(task)
        envelope = json.loads(self.blobs.read_bytes(ref["blob_sha256"]))
        validate_result(task, ticket, envelope)
        self._validate_output(task, envelope)
        if digest(envelope) != ref["result_digest"] \
                or ref["task_id"] != task["task_id"] \
                or ticket["case_id"] != task["case_id"]:
            raise ProductRejected("integrity: 封存返回摘要不一致")
        state = self.journal.task_state(task["task_id"])
        if state["input_id"] != task["input_digest"] \
                or state["current_token"] != ticket["token"] \
                or state["current_worker"] != ticket["context_id"] \
                or state["external_actions"] != 1 \
                or state["state"] not in {
                    "dispatch_recorded", "outcome_unknown", "succeeded", "failed"} \
                or (state["state"] == "failed"
                    and envelope["execution_status"] == "succeeded") \
                or (state["state"] == "succeeded"
                    and (state["output_ref"] != ref["blob_sha256"]
                         or envelope["execution_status"] != "succeeded")):
            raise ProductRejected("integrity: 封存返回与派发台账不一致")
        return envelope

    def _validate_output(self, task: dict, envelope: dict) -> None:
        if envelope["execution_status"] != "succeeded":
            return
        output = envelope["output"]
        if task["role"] == "scope_discovery":
            if "units" in output:
                from .unit_scope import validate_candidate

                coverage = self.materials(task["run_id"])
                validate_candidate(
                    output, coverage,
                    lambda segment_id: self._segment_text(coverage, segment_id))
            else:
                if set(output) - {"candidates", "unknowns"} \
                        or output.get("candidates") != [] \
                        or not isinstance(output.get("unknowns", []), list) \
                        or any(not isinstance(value, str)
                               for value in output.get("unknowns", [])):
                    raise ProductRejected(
                        "fields:scope_discovery: 需完整单元合同或明确空候选")

    def submit_host_result(self, task_id: str, envelope: dict) -> dict:
        self.store.assert_product_task_inventory()
        task = self._task(task_id)
        ticket = self._ticket(task)
        envelope = validate_result(task, ticket, envelope)
        self._validate_output(task, envelope)
        existing = self._sealed(task)
        if existing is not None:
            if existing != envelope:
                raise ProductRejected("immutable: 封存返回不可替换")
            return existing
        state = self.journal.task_state(task_id)
        if state["state"] not in {"dispatch_recorded", "outcome_unknown"} \
                or state["current_token"] != ticket["token"] \
                or state["current_worker"] != ticket["context_id"] \
                or state["input_id"] != ticket["input_digest"]:
            raise ProductRejected("binding: 无可接收该返回的派发")
        blob = self.blobs.put_bytes(canonical(envelope))
        self._put("RESPONSE::" + task_id, task["run_id"], "response", {
            "task_id": task_id, "blob_sha256": blob.sha256,
            "result_digest": digest(envelope),
        })
        # 封存优先：本地中断后可只读消费此返回，绝不因此重新派发。
        try:
            if state["state"] == "outcome_unknown":
                # 接收原票据的迟到返回，不翻转未知attempt，不产生新派发。
                pass
            elif envelope["execution_status"] == "succeeded":
                self.journal.commit(self._claim(ticket), blob.sha256)
            else:
                self.journal.record_failure(
                    self._claim(ticket), envelope["execution_status"])
        except CommitRejected as exc:
            if self._sealed(task) != envelope:
                raise ProductRejected("binding: 封存后台账冲突") from exc
        return envelope

    def advance(self, run_id: str) -> dict:
        run = self._run(run_id)
        if not run["blockers"]:
            self.materials(run_id)
        snapshot = self._task_snapshot(run_id)
        for item in snapshot["tasks"]:
            task = item["body"]
            envelope = self._sealed(task)
            if envelope is not None:
                self._put("CONSUMED::" + task["task_id"], run_id, "consumption", {
                    "task_id": task["task_id"], "result_digest": digest(envelope),
                    "execution_status": envelope["execution_status"],
                })
        return self.status(run_id)

    def status(self, run_id: str) -> dict:
        from .intake import IntakeRejected

        run = self._run(run_id)
        snapshot = self._task_snapshot(run_id)
        material_blocker = None
        if not run["blockers"]:
            try:
                self.materials(run_id)
            except (ProductRejected, IntakeRejected) as exc:
                if str(exc).startswith("legacy_extraction_contract:"):
                    material_blocker = str(exc)
                else:
                    raise
        tasks = []
        for item in snapshot["tasks"]:
            task = item["body"]
            state = item["journal"]
            if state is None:
                state = {
                    "state": "registration_incomplete",
                    "external_actions": 0,
                }
            if state["external_actions"] or self.store.get_product_object(
                    "DISPATCH::" + task["task_id"], run_id=run_id, kind="dispatch"):
                self._ticket(task)
            envelope = self._sealed(task)
            consumed = self.store.get_product_object(
                "CONSUMED::" + task["task_id"], run_id=run_id, kind="consumption")
            if consumed is not None and (envelope is None or consumed != {
                    "task_id": task["task_id"],
                    "result_digest": digest(envelope),
                    "execution_status": envelope["execution_status"],
            }):
                raise ProductRejected("integrity: 消费记录失去封存返回")
            tasks.append({
                "task_id": task["task_id"], "role": task["role"],
                "state": ("consumed" if consumed is not None else
                          "response_sealed" if envelope else state["state"]),
                "journal_state": state["state"],
                "execution_status": (
                    envelope["execution_status"] if envelope
                    else "failed" if state["state"] == "failed" else None
                ),
                "source_mode": envelope["source_mode"] if envelope else None,
                "professional_result": None,
            })
        assessment_status = "awaiting_scope_confirmation"
        if self.store.get_product_object("SCOPE::" + run_id, run_id=run_id, kind="scope"):
            self.scope(run_id)
            assessment_status = "awaiting_research"
        if any(task["journal_state"] == "failed"
               or task["execution_status"] in {"failed", "cancelled"}
               for task in tasks):
            assessment_status = "execution_failed"
        if material_blocker is not None:
            assessment_status = "blocked"
            run["blockers"] = [*run["blockers"], material_blocker]
        if run["blockers"]:
            assessment_status = "blocked"
        return {
            "run_id": run_id, "contract_version": CONTRACT_VERSION,
            "assessment_status": assessment_status, "blockers": run["blockers"],
            "tasks": tasks, "evidence_layer": "E" if run["request"]["mode"] == "offline"
            else "unverified", "professional_acceptance": "not_evaluated",
        }

    def close(self) -> None:
        self.workflow.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
