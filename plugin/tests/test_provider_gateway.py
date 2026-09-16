"""受控 Provider 派发层：离线合同测试（fake transport，零网络零凭证）。

真实链路只在显式授权窗口内由 CLI 触发；本文件只验证派发恰好一次合同、
封存工件验证链与双消费门的防伪行为。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from kth_hybrid.catalog import build_catalog_from_wheel
from kth_hybrid.provider_gateway import (
    ARTIFACT_SCHEMA,
    ProviderDispatcher,
    ProviderGatewayRejected,
    derive_response_envelope,
)
from kth_hybrid.workflow import LocalWorkflow

SUBJECT = "武汉微玖光电科技有限公司"
UNIT = {
    "scope_id": "UNIT-WEIJU-E3",
    "subject_scope": SUBJECT,
    "unit_kind": "current_case_company_level",
    "unit_label": "微玖E3公司级评估单元",
    "scope_id_ref": {"kind": "field_reference", "path": "case:units.json#/scope_id"},
    "subject_ref": {"kind": "field_reference", "path": "case:units.json#/subject"},
    "unit_kind_ref": {"kind": "field_reference", "path": "case:units.json#/kind"},
    "unit_label_ref": {"kind": "field_reference", "path": "case:units.json#/label"},
}
FINANCING = {
    "financing_entity_id": "FIN-WEIJU-E3",
    "subject_scope": SUBJECT,
    "assessment_unit_refs": [UNIT["scope_id"]],
    "entity_ref": {"kind": "field_reference", "path": "case:financing.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:financing.json#/subject"},
    "assessment_units_ref": {"kind": "field_reference",
                             "path": "case:financing.json#/units"},
}
FRL = {
    "external_financing_planned": False,
    "financing_entity_id": FINANCING["financing_entity_id"],
    "subject_scope": SUBJECT,
    "external_financing_planned_ref": {
        "kind": "field_reference", "path": "case:frl.json#/planned"},
    "financing_entity_ref": {
        "kind": "field_reference", "path": "case:frl.json#/entity"},
    "subject_ref": {"kind": "field_reference", "path": "case:frl.json#/subject"},
}


def _put_provenance(workflow: LocalWorkflow) -> None:
    documents = {
        "identity.json": {"subject": SUBJECT},
        "units.json": {"scope_id": UNIT["scope_id"], "subject": SUBJECT,
                       "kind": UNIT["unit_kind"], "label": UNIT["unit_label"]},
        "financing.json": {"entity": FINANCING["financing_entity_id"],
                           "subject": SUBJECT,
                           "units": FINANCING["assessment_unit_refs"]},
        "frl.json": {"planned": False, "entity": FINANCING["financing_entity_id"],
                     "subject": SUBJECT},
    }
    for name, body in documents.items():
        blob = workflow.blobs.put_bytes(json.dumps(body, ensure_ascii=False).encode())
        workflow.store.add_import_record("case_provenance", f"session:{name}",
                                         blob.sha256)


def _evaluation_inputs() -> dict:
    from kth_hybrid.aggregation_profiles import (
        CURRENT_AGGREGATION_PROFILE_ID,
        get_aggregation_profile,
    )
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    return {
        "assessment_unit": copy.deepcopy(UNIT),
        "financing_entity": copy.deepcopy(FINANCING),
        "frl_applicability": copy.deepcopy(FRL),
        "profile": {"profile_id": profile["profile_id"],
                    "profile_digest": profile["profile_digest"]},
        "method_versions": {
            "contract_schema": "kth-local.evaluation-method-contract.v1",
            "candidate_proposal": "kth-local.candidate-proposal.v1",
            "qualification": "kth-hybrid.qualification.v4",
            "professional_review": "kth-local.professional-review.v2",
            "catalog_sha256": profile["catalog_sha256"],
            "profile_id": profile["profile_id"],
            "profile_digest": profile["profile_digest"],
            **{
                f"{dimension.lower()}_rule_version": values["rule_version"]
                for dimension, values in profile["dimensions"].items()
            },
            **{
                f"{dimension.lower()}_result_schema_version":
                    values["result_schema_version"]
                for dimension, values in profile["dimensions"].items()
            },
        },
    }


def _write_auth(tmp_path, *, material_sha256: str, **overrides) -> object:
    now = datetime.now(timezone.utc)
    auth = {
        "schema_version": "kth-hybrid.provider-authorization.v1",
        "material": {"original_sha256": material_sha256},
        "purposes": ["candidate_proposal", "professional_review"],
        "providers": {
            "primary": {"provider_id": "glm", "model": "glm-5.2",
                        "endpoint": "https://aigateway.sunnyoptical.cn/zai-api/v1/chat/completions",
                        "api_key_env": "LLM_API_KEY"},
            "fallback": {"provider_id": "deepseek", "model": "deepseek-v4-pro",
                         "endpoint": "https://api.deepseek.com/chat/completions",
                         "api_key_env": "DEEPSEEK_API_KEY"},
        },
        "resource_caps": {"max_real_requests": 6,
                          "max_cumulative_input_tokens": 60000,
                          "max_cumulative_output_tokens": 12000},
        "window": {"started": (now - timedelta(minutes=1)).isoformat(),
                   "expires": (now + timedelta(hours=4)).isoformat()},
    }
    auth.update(overrides)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
    return path


def _fake_transport(calls: list):
    def transport(endpoint, headers, body, timeout_seconds):
        calls.append({"endpoint": endpoint, "body": json.loads(body)})
        payload = json.loads(body)
        user = next(item for item in payload["messages"]
                    if item["role"] == "user")
        content = json.loads(user["content"])
        if "criteria" in content:
            model_output = {"candidates": [{
                "quote": content["quote"],
                "locator": copy.deepcopy(content["locator"]),
                "interpretation": "已完成实验室组件集成测试。",
                "subject_scope": content["subject"],
                "dimension_id": content["dimension_id"],
                "criterion_id": "TRL4-C1",
                "mapping": {"criterion_id": "TRL4-C1",
                            "evidence_class": "test_record",
                            "requested_use": "third_party_reported_fact"},
            }]}
        else:
            model_output = {
                "decision": "supports",
                "evidence_class": "test_record",
                "findings": {"environment_kind": "laboratory",
                             "test_method": "受控组件集成测试",
                             "measured_results": "组件共同产生预期结果"},
            }
        return {
            "status_code": 200,
            "body": {
                "id": "chatcmpl-fake-0001",
                "model": payload["model"],
                "choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps(
                                 model_output, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 512, "completion_tokens": 128,
                          "total_tokens": 640},
            },
        }
    return transport


def _candidate_job(workflow: LocalWorkflow, tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "qualified-runtime.docx"
    document = docx.Document()
    document.add_paragraph(
        f"{SUBJECT}已完成实验室组件集成测试，组件共同产生预期结果。")
    document.save(path)
    imported = workflow.import_attachments([path])[0]
    projection = next(item for item in workflow.project_sources(
        [imported["source_id"]]) if item["status"] == "projected")
    with workflow.store._conn:
        workflow.store._conn.execute(
            "UPDATE sources SET source_family='news-media', "
            "retrieved_at='2026-09-08T00:00:00Z', "
            "capture_status='raw_capture_validated' WHERE source_id=?",
            (imported["source_id"],),
        )
    return workflow.create_candidate_job(
        evaluation_inputs=_evaluation_inputs(),
        proposal_specs=[{
            "source_id": imported["source_id"],
            "blob_sha256": imported["blob_sha256"],
            "projection_id": projection["projection_id"],
            "locator": projection["locator"],
            "quote": projection["text"],
            "purpose": "提出待资格核验的TRL候选",
            "output_schema": "proposal_response.v1",
        }],
        catalog=build_catalog_from_wheel(),
    )


@pytest.fixture()
def workflow_fixture(tmp_path):
    value = LocalWorkflow(tmp_path / "case")
    _put_provenance(value)
    value.initialize_case(
        subject_legal_name=SUBJECT,
        subject_aliases=[],
        evidence_cutoff="2026-09-09T00:00:00Z",
        subject_source_basis=json.dumps({
            "kind": "field_reference", "path": "case:identity.json#/subject",
            "status": "claimed",
        }, ensure_ascii=False),
    )
    job = _candidate_job(value, tmp_path)
    yield value, job
    value.close()


def _make_dispatcher(workflow, tmp_path, job, *, calls=None):
    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key\nOTHER=1\n",
                        encoding="utf-8")
    auth_path = _write_auth(tmp_path, material_sha256=job["sources"][0]
                            ["blob_sha256"])
    return ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=_fake_transport(calls if calls is not None else []))


def test_runtime_provider_full_chain_offline(workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    calls: list = []
    dispatcher = _make_dispatcher(workflow, tmp_path, job, calls=calls)

    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    from kth_hybrid.provider_gateway import build_proposal_messages
    messages, schema = build_proposal_messages(
        proposal, build_catalog_from_wheel())
    artifact = dispatcher.dispatch(
        request=proposal, purpose="candidate_proposal", messages=messages,
        output_schema=schema)
    assert artifact["schema_version"] == ARTIFACT_SCHEMA
    assert artifact["response"]["provider_request_id"] == "chatcmpl-fake-0001"
    assert artifact["usage"]["total_tokens"] == 640
    usage_snapshot = dispatcher.usage()
    assert (usage_snapshot["requests"], usage_snapshot["input_tokens"],
            usage_snapshot["output_tokens"]) == (1, 512, 128)

    envelope = derive_response_envelope(artifact, proposal,
                                        purpose="candidate_proposal")
    sealed = workflow.proposals.seal_response(
        proposal["request_id"], envelope, source_mode="runtime_provider",
        provider_dispatch={"artifact_blob_sha256":
                           artifact["artifact_blob_sha256"]})
    consumed = workflow.proposals.consume_response(
        sealed["response_id"], worker_id="pilot-runtime",
        catalog=build_catalog_from_wheel())
    statuses = consumed["materialization"]["candidate_statuses"]
    assert statuses and all(item["status"] == "qualified"
                            for item in statuses)

    review_request = workflow.reviews.get_request(
        consumed["materialization"]["review_request_ids"][0])
    from kth_hybrid.provider_gateway import build_review_messages
    messages, schema = build_review_messages(
        review_request, build_catalog_from_wheel())
    review_artifact = dispatcher.dispatch(
        request=review_request, purpose="professional_review",
        messages=messages, output_schema=schema)
    review_envelope = derive_response_envelope(
        review_artifact, review_request, purpose="professional_review")
    sealed_review = workflow.reviews.seal_response(
        review_request["request_id"], review_envelope,
        source_mode="runtime_provider",
        provider_dispatch={"artifact_blob_sha256":
                           review_artifact["artifact_blob_sha256"]})
    workflow.reviews.consume_response(
        sealed_review["response_id"], worker_id="pilot-runtime-review")

    status = workflow.run_job(job["job_id"])
    assert status["state"] == "completed"
    trl = next(row for row in status["dimension_outputs"]
               if row["dimension_id"] == "TRL")
    assert trl["result_id"].startswith("DIMR2::TRL::")
    assert status["artifacts"]["source_modes"] == ["runtime_provider"]
    assert len(calls) == 2
    assert calls[0]["body"]["model"] == "glm-5.2"


def test_runtime_provider_requires_dispatch_proof(workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    dispatcher = _make_dispatcher(workflow, tmp_path, job)
    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    from kth_hybrid.provider_gateway import build_proposal_messages
    messages, schema = build_proposal_messages(
        proposal, build_catalog_from_wheel())
    artifact = dispatcher.dispatch(
        request=proposal, purpose="candidate_proposal", messages=messages,
        output_schema=schema)
    envelope = derive_response_envelope(artifact, proposal,
                                        purpose="candidate_proposal")
    from kth_hybrid.proposal_requests import ProposalQueueRejected
    with pytest.raises(ProposalQueueRejected, match="受控派发验证链"):
        workflow.proposals.seal_response(
            proposal["request_id"], envelope, source_mode="runtime_provider")


def test_runtime_provider_rejects_hand_edited_envelope(
        workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    dispatcher = _make_dispatcher(workflow, tmp_path, job)
    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    from kth_hybrid.provider_gateway import build_proposal_messages
    messages, schema = build_proposal_messages(
        proposal, build_catalog_from_wheel())
    artifact = dispatcher.dispatch(
        request=proposal, purpose="candidate_proposal", messages=messages,
        output_schema=schema)
    envelope = derive_response_envelope(artifact, proposal,
                                        purpose="candidate_proposal")
    tampered = copy.deepcopy(envelope)
    tampered["candidates"][0]["interpretation"] = "手工改写的解释。"
    from kth_hybrid.proposal_requests import ProposalQueueRejected
    with pytest.raises(ProposalQueueRejected, match="封存模型输出不一致"):
        workflow.proposals.seal_response(
            proposal["request_id"], tampered, source_mode="runtime_provider",
            provider_dispatch={"artifact_blob_sha256":
                               artifact["artifact_blob_sha256"]})


def test_dispatch_rejects_material_out_of_scope(workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    auth_path = _write_auth(
        tmp_path, material_sha256="0" * 64)
    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key\n", encoding="utf-8")
    dispatcher = ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=_fake_transport([]))
    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    from kth_hybrid.provider_gateway import build_proposal_messages
    messages, schema = build_proposal_messages(
        proposal, build_catalog_from_wheel())
    with pytest.raises(ProviderGatewayRejected, match="授权外发范围"):
        dispatcher.dispatch(request=proposal, purpose="candidate_proposal",
                            messages=messages, output_schema=schema)


def test_dispatch_rejects_expired_authorization(workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    now = datetime.now(timezone.utc)
    auth_path = _write_auth(
        tmp_path,
        material_sha256=job["sources"][0]["blob_sha256"],
        window={"started": (now - timedelta(hours=5)).isoformat(),
                "expires": (now - timedelta(hours=1)).isoformat()})
    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key\n", encoding="utf-8")
    proposal = workflow.proposals.get_request(job["proposal_request_ids"][0])
    from kth_hybrid.provider_gateway import build_proposal_messages
    messages, schema = build_proposal_messages(
        proposal, build_catalog_from_wheel())
    with pytest.raises(ProviderGatewayRejected, match="授权窗口无效"):
        ProviderDispatcher(
            workflow, authorization_path=auth_path, env_path=env_path,
            transport=_fake_transport([]))


def test_legacy_zai_authorization_variant_still_validates(tmp_path):
    """已封存工件内嵌的旧glm授权（api.z.ai）必须保持可复验。"""
    from kth_hybrid.provider_gateway import load_authorization
    now = datetime.now(timezone.utc)
    auth = {
        "schema_version": "kth-hybrid.provider-authorization.v1",
        "material": {"original_sha256": "a" * 64},
        "purposes": ["candidate_proposal"],
        "providers": {
            "primary": {"provider_id": "glm", "model": "glm-5.2",
                        "endpoint": "https://api.z.ai/api/paas/v4/chat/completions",
                        "api_key_env": "ZAI_API_KEY"},
            "fallback": {"provider_id": "deepseek", "model": "deepseek-v4-pro",
                         "endpoint": "https://api.deepseek.com/chat/completions",
                         "api_key_env": "DEEPSEEK_API_KEY"},
        },
        "resource_caps": {"max_real_requests": 6,
                          "max_cumulative_input_tokens": 60000,
                          "max_cumulative_output_tokens": 12000},
        "window": {"started": (now - timedelta(minutes=1)).isoformat(),
                   "expires": (now + timedelta(hours=1)).isoformat()},
    }
    path = tmp_path / "legacy-auth.json"
    path.write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")
    assert load_authorization(path)["providers"]["primary"]["model"] == "glm-5.2"


# ---- 用量未知、派发前预留与结算（零transport验证） ----

def _proposal_request(workflow, job):
    return workflow.proposals.get_request(job["proposal_request_ids"][0])


def _messages_schema(request):
    from kth_hybrid.provider_gateway import build_proposal_messages
    return build_proposal_messages(request, build_catalog_from_wheel())


def test_unknown_legacy_usage_blocks_dispatch(workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    calls: list = []
    dispatcher = _make_dispatcher(workflow, tmp_path, job, calls=calls)
    request = _proposal_request(workflow, job)
    # 前账本时代：已派发但无任何usage留存的失败mission。
    workflow.journal.ensure_task("provider-dispatch:LEGACY-UNKNOWN", "x" * 64)
    legacy_claim = workflow.journal.claim(
        "provider-dispatch:LEGACY-UNKNOWN", "legacy", "x" * 64)
    workflow.journal.record_dispatch(legacy_claim)
    workflow.journal.record_failure(legacy_claim, "HTTP 401")
    usage = dispatcher.usage()
    assert usage["requests"] == 1 and usage["unknown_attempts"] == 1
    messages, schema = _messages_schema(request)
    with pytest.raises(ProviderGatewayRejected, match="剩余额度不可证明"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema)
    assert calls == []


def test_insufficient_quota_rejects_before_transport(workflow_fixture,
                                                     tmp_path):
    workflow, job = workflow_fixture
    calls: list = []
    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key\n", encoding="utf-8")
    auth_path = _write_auth(
        tmp_path, material_sha256=job["sources"][0]["blob_sha256"],
        resource_caps={"max_real_requests": 6,
                       "max_cumulative_input_tokens": 60000,
                       "max_cumulative_output_tokens": 100})
    dispatcher = ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=_fake_transport(calls))
    request = _proposal_request(workflow, job)
    messages, schema = _messages_schema(request)
    with pytest.raises(ProviderGatewayRejected, match="额度预留被拒"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema,
                            max_output_tokens=4096)
    assert calls == []


def test_failed_mission_counts_and_occupies_conservatively(
        workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    calls: list = []

    def failing(endpoint, headers, body, timeout_seconds):
        calls.append({"endpoint": endpoint})
        return {"status_code": 401, "body": {"http_error": "unauthorized"}}

    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key", encoding="utf-8")
    auth_path = _write_auth(
        tmp_path, material_sha256=job["sources"][0]["blob_sha256"],
        resource_caps={"max_real_requests": 6,
                       "max_cumulative_input_tokens": 60000,
                       "max_cumulative_output_tokens": 5000})
    dispatcher = ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=failing)
    request = _proposal_request(workflow, job)
    messages, schema = _messages_schema(request)
    with pytest.raises(ProviderGatewayRejected, match="HTTP 401"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema)
    usage = dispatcher.usage()
    assert usage["requests"] == 1 and usage["unknown_attempts"] == 1
    ledger = workflow.store.fetch_provider_usage_ledger()
    assert ledger and ledger[0]["status"] == "failed"         and ledger[0]["usage_status"] == "unknown"
    # 失败mission的未知用量按其请求预留上界占用：紧上限下第二次
    # 派发在预留事务内被拒，且不再产生transport调用。
    with pytest.raises(ProviderGatewayRejected, match="额度预留被拒"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema,
                            task_suffix="#again")
    assert len(calls) == 1


def test_missing_usage_seals_unknown_and_blocks(workflow_fixture,
                                                tmp_path):
    workflow, job = workflow_fixture
    calls: list = []

    def no_usage(endpoint, headers, body, timeout_seconds):
        calls.append({"endpoint": endpoint})
        payload = json.loads(body)
        content = {"candidates": [{
            "quote": json.loads(next(m for m in payload["messages"]
                                    if m["role"] == "user")["content"])["quote"],
            "locator": {}, "interpretation": "x", "subject_scope": "s",
            "dimension_id": "TRL", "criterion_id": "TRL4-C1",
            "mapping": {"criterion_id": "TRL4-C1",
                        "evidence_class": "test_record",
                        "requested_use": "third_party_reported_fact"}}]}
        return {"status_code": 200, "body": {
            "id": "chatcmpl-nousage", "model": payload["model"],
            "choices": [{"finish_reason": "stop", "message": {"content":
                json.dumps(content, ensure_ascii=False)}}]}}

    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key", encoding="utf-8")
    auth_path = _write_auth(
        tmp_path, material_sha256=job["sources"][0]["blob_sha256"],
        resource_caps={"max_real_requests": 6,
                       "max_cumulative_input_tokens": 60000,
                       "max_cumulative_output_tokens": 120})
    dispatcher = ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=no_usage)
    request = _proposal_request(workflow, job)
    messages, schema = _messages_schema(request)
    artifact = dispatcher.dispatch(
        request=request, purpose="candidate_proposal", messages=messages,
        output_schema=schema, max_output_tokens=50)
    assert artifact["usage"] is None
    assert artifact["usage_reconciliation"]["status"] == "unknown"
    # 账本内未知不按0处理：按该次请求预留上界保守占用，紧上限下
    # 第二次派发必须在预留事务内被拒且零transport。
    with pytest.raises(ProviderGatewayRejected, match="额度预留被拒"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema,
                            max_output_tokens=100, task_suffix="#next")
    assert len(calls) == 1


def test_outstanding_reservation_is_shared_balance(workflow_fixture,
                                                   tmp_path):
    from kth_hybrid.store import BudgetRejected
    workflow, job = workflow_fixture
    calls: list = []
    dispatcher = _make_dispatcher(workflow, tmp_path, job, calls=calls)
    request = _proposal_request(workflow, job)
    # 存储层：同一预算范围的第二次预留必须计入第一次的未结算占用。
    workflow.store.reserve_provider_usage(
        budget_scope="scope-shared", task_key="provider-dispatch:OTHER",
        purpose="candidate_proposal", provider_id="glm",
        input_reserved=60, output_reserved=60, historic_usage=[],
        request_cap=1, input_cap=100, output_cap=100)
    with pytest.raises(BudgetRejected, match="请求次数2超过上界1"):
        workflow.store.reserve_provider_usage(
            budget_scope="scope-shared", task_key="provider-dispatch:SECOND",
            purpose="candidate_proposal", provider_id="glm",
            input_reserved=10, output_reserved=10, historic_usage=[],
            request_cap=1, input_cap=100, output_cap=100)
    # 派发层：同一预算范围内的未结算占用参与事务内实况计算，零transport。
    scope = dispatcher._budget_scope()
    workflow.store.reserve_provider_usage(
        budget_scope=scope, task_key="provider-dispatch:OUTSTANDING",
        purpose="candidate_proposal", provider_id="glm",
        input_reserved=50000, output_reserved=11000, historic_usage=[],
        request_cap=6, input_cap=60000, output_cap=12000)
    messages, schema = _messages_schema(request)
    with pytest.raises(ProviderGatewayRejected, match="额度预留被拒"):
        dispatcher.dispatch(request=request, purpose="candidate_proposal",
                            messages=messages, output_schema=schema)
    assert calls == []


def test_prior_consumption_reconciliation_allows_dispatch(
        workflow_fixture, tmp_path):
    workflow, job = workflow_fixture
    calls: list = []
    env_path = tmp_path / "gateway-env-fake"
    env_path.write_text("LLM_API_KEY=fake-offline-key" + chr(10), encoding="utf-8")
    auth_path = _write_auth(
        tmp_path, material_sha256=job["sources"][0]["blob_sha256"],
        prior_consumption={
            "declared_by": "测试：Owner批准的逐mission对账",
            "missions": [
                {"task_key": "provider-dispatch:LEGACY-1",
                 "usage_status": "known", "input_actual": 1980,
                 "output_actual": 6621},
                {"task_key": "provider-dispatch:LEGACY-2",
                 "usage_status": "unknown", "input_bound": 2500,
                 "output_bound": 1000,
                 "evidence_source": "Owner批准：按该次请求上界折算"},
            ]})
    dispatcher = ProviderDispatcher(
        workflow, authorization_path=auth_path, env_path=env_path,
        transport=_fake_transport(calls))
    request = _proposal_request(workflow, job)
    messages, schema = _messages_schema(request)
    artifact = dispatcher.dispatch(
        request=request, purpose="candidate_proposal", messages=messages,
        output_schema=schema)
    historic, historic_error = dispatcher._historic_usage_items()
    assert historic_error is None and len(historic) == 2
    ledger = workflow.store.fetch_provider_usage_ledger()
    assert ledger[0]["status"] == "succeeded"         and ledger[0]["usage_status"] == "known"
    # 覆盖表只列旧mission：新出现的本地未知mission仍必须阻断。
    workflow.journal.ensure_task("provider-dispatch:NEW-UNKNOWN", "z" * 64)
    nc = workflow.journal.claim("provider-dispatch:NEW-UNKNOWN", "x",
                                "z" * 64)
    workflow.journal.record_dispatch(nc)
    workflow.journal.record_failure(nc, "HTTP 500")
    _items2, error2 = dispatcher._historic_usage_items()
    assert error2 and "未被逐项对账覆盖" in error2
