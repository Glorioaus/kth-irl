"""NIGHT限定整改：独立验收P1的当前仓库反例。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from kth_hybrid.aggregate import (
    build_offline_dimension_view,
    freeze_aggregation_manifest,
    validate_dimension_index,
)
from kth_hybrid.audit import trace_dimension_result, validate_dimension_payload
from kth_hybrid.catalog import APPROVED_WHEEL_SHA256, build_catalog_from_wheel
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.kernels.brl import RULE_REQUIREMENTS as BRL_REQUIREMENTS
from kth_hybrid.kernels.brl import evaluate_brl_dimension
from kth_hybrid.kernels.iprl import RULE_REQUIREMENTS as IPRL_REQUIREMENTS
from kth_hybrid.kernels.iprl import evaluate_iprl_dimension
from kth_hybrid.kernels.tmrl import RULE_REQUIREMENTS as TMRL_REQUIREMENTS
from kth_hybrid.kernels.tmrl import evaluate_tmrl_dimension
from kth_hybrid.kernels.trl import RULE_REQUIREMENTS as TRL_REQUIREMENTS
from kth_hybrid.kernels.trl import evaluate_trl_dimension
from kth_hybrid.roles import (
    confirm_role_candidate,
    role_candidate_digest,
    validate_role_attempt,
)
from kth_hybrid.store import BlobStore, CaseStore
from kth_hybrid import runner


CATALOG = build_catalog_from_wheel()
SCOPE = "Company-A"
UNIT = {"scope_id": "UNIT-A", "subject_scope": SCOPE,
        "unit_kind": "material_unit", "unit_label": "Unit-A"}
BASE_CASE = os.environ.get("KTH_NIGHT_FIX_BASE_CASE")


def _criterion(dimension, criterion_id):
    return next(row for row in CATALOG["dimensions"][dimension]["registry"]["criteria"]
                if row["criterion_id"] == criterion_id)


def _row(result, criterion_id):
    return next(row for row in result["criteria"]
                if row["criterion_id"] == criterion_id)


def test_brl6_rejects_soft_refundable_unfulfilled_pilot():
    criterion = _criterion("BRL", "BRL6-BM")
    review = {
        "review_id": "BRL-SOFT", "criterion_id": "BRL6-BM",
        "claim_id": "C-BRL", "decision": "supports",
        "evidence_class": "pilot_or_test_sale",
        "findings": {BRL_REQUIREMENTS["BRL6-BM"]: True,
                     "transaction_commitment": "soft_interest",
                     "refundability_visible": True, "fulfilled": False},
        "scope_id": "UNIT-A", "reviewer": "audit",
        "review_basis": "软意向、可退款、未履约", "support_scope": "BRL6-BM"}
    result = evaluate_brl_dimension(
        CATALOG["dimensions"]["BRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _row(result, "BRL6-BM")["native_disposition"] != "met"


def test_brl8_metrics_need_actual_values_targets_period_and_denominator():
    criterion = _criterion("BRL", "BRL8-BM")
    vague = {"review_id": "BRL-METRICS", "criterion_id": "BRL8-BM",
             "claim_id": "C-M", "decision": "supports",
             "evidence_class": "operating_metrics",
             "findings": {BRL_REQUIREMENTS["BRL8-BM"]: True,
                          "operating_period": "2026-Q2",
                          "metric_denominator": "orders"},
             "scope_id": "UNIT-A", "reviewer": "audit",
             "review_basis": "无实际值/目标", "support_scope": "BRL8-BM"}
    result = evaluate_brl_dimension(
        CATALOG["dimensions"]["BRL"]["registry"]["criteria"], [vague],
        scope=SCOPE, assessment_unit=UNIT)
    assert _row(result, "BRL8-BM")["native_disposition"] != "met"


def test_trl3_r_and_d_record_does_not_require_test_measurements():
    criterion = _criterion("TRL", "TRL3-C2")
    review = {"review_id": "TRL-RD", "criterion_id": "TRL3-C2",
              "claim_id": "C-TRL", "decision": "supports",
              "evidence_class": "r_and_d_record",
              "findings": {TRL_REQUIREMENTS["TRL3-C2"]: True,
                           "project_specific": True, "configuration_id": "CFG-A",
                           "system_boundary": "System-A", "activity_status": "active"},
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "项目主动研发记录", "support_scope": "TRL3-C2"}
    result = evaluate_trl_dimension(
        CATALOG["dimensions"]["TRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _row(result, "TRL3-C2")["native_disposition"] == "met"
    assert _row(result, "TRL3-C1")["native_disposition"] != "met"


@pytest.mark.parametrize("criterion_id,evidence_class,findings", [
    ("IPRL5-C2", "formal_application",
     {"record_status": "project_assertion"}),
    ("IPRL5-C3", "executed_assignment",
     {"record_status": "draft_unsigned"}),
    ("IPRL6-C3", "fto_assessment",
     {"record_status": "self_analysis", "professional_scope": False,
      "product_configuration": "CFG", "jurisdiction": "CN", "as_of": "2026-08-27"}),
    ("IPRL9-C2", "maintained_right",
     {"record_status": "unknown", "jurisdictions": ["CN"],
      "project_right_binding": True, "rightsholder": SCOPE}),
])
def test_iprl_record_strength_rejects_weak_records(
        criterion_id, evidence_class, findings):
    criterion = _criterion("IPRL", criterion_id)
    base = {IPRL_REQUIREMENTS[criterion_id]: True, "ip_specific": True,
            "asset_id": "IP-A"}
    base.update(findings)
    review = {"review_id": "IP-WEAK", "criterion_id": criterion_id,
              "claim_id": "C-IP", "decision": "supports",
              "evidence_class": evidence_class, "findings": base,
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "弱记录", "support_scope": criterion_id}
    result = evaluate_iprl_dimension(
        CATALOG["dimensions"]["IPRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _row(result, criterion_id)["native_disposition"] != "met"


def test_tmrl_unsigned_ownership_agreement_is_not_met():
    criterion = _criterion("TMRL", "TMRL5-C3")
    review = {"review_id": "TM-DRAFT", "criterion_id": "TMRL5-C3",
              "claim_id": "C-TM", "decision": "supports",
              "evidence_class": "executed_ownership_agreement",
              "findings": {TMRL_REQUIREMENTS["TMRL5-C3"]: True,
                           "team_specific": True, "subject_ids": ["P1"],
                           "current_period": "2026-Q3", "work_evidence_refs": ["W1"],
                           "agreement_status": "draft_unsigned"},
              "scope_id": "UNIT-A", "reviewer": "audit",
              "review_basis": "未签草案", "support_scope": "TMRL5-C3"}
    result = evaluate_tmrl_dimension(
        CATALOG["dimensions"]["TMRL"]["registry"]["criteria"], [review],
        scope=SCOPE, assessment_unit=UNIT)
    assert _row(result, "TMRL5-C3")["native_disposition"] != "met"


def _license():
    body = {"dimension_id": "BRL", "result_id": "BRL-R",
            "claim_id": "CLAIM-1", "quote_sha256": "a" * 64,
            "evidence_class": "business_concept", "subject_scope": SCOPE,
            "scope_id": "UNIT-A", "qualification_view_digest": "b" * 64,
            "allowed_uses": ["maturity_assessment"],
            "support_scope": "仅支持BRL1-BM"}
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {"license_id": f"EVIDUSE::{digest}", **body}


def _view():
    dimensions = {dimension: {
                      "dimension_id": dimension,
                      "result_id": f"{dimension}-R",
                      "input_digest": sha256_hex(dimension.encode("utf-8")),
                      "case_basis_version": 1, "scope": SCOPE,
                      "scope_id": "UNIT-A", "product_status": "succeeded",
                      "attained_level": 0, "catalog_sha256": "c" * 64,
                      "rule_version": "rule-v1",
                      "result_schema_version": "result-v2",
                      "assessment_scope": "material_unit",
                      "financing_entity": None, "trace_ok": True}
                  for dimension in ("CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL")}
    license_value = _license()
    body = {"schema_version": "kth-hybrid.offline-six-dimension-view.v2",
            "status": "offline_candidate", "manifest_id": "AGGMAN::" + "d" * 64,
            "manifest_digest": "d" * 64, "scope": SCOPE,
            "case_basis_version": 1, "dimensions": dimensions,
            "evidence_licenses": {license_value["license_id"]: license_value},
            "limitations": ["非正式KTH评估"]}
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {**body, "view_id": f"OFFLINE6::{digest}", "input_digest": digest}


def _attempt(**changes):
    view = _view()
    license_id = next(iter(view["evidence_licenses"]))
    attempt = {"schema_version": "kth-hybrid.offline-role-attempt.v2",
               "role": "PRO", "producer_id": "P", "context_id": "CTX-P",
               "simulated": True, "input_view_id": view["view_id"],
               "input_digest": view["input_digest"], "scope": SCOPE,
               "dimension_result_refs": {d: r["result_id"]
                                         for d, r in view["dimensions"].items()},
               "candidate_id": "PRO-C1", "statement": "仅陈述证据边界。",
               "evidence_refs": [license_id], "limitations": ["离线模拟"],
               "review_target": {"dimension_id": "BRL", "criterion_id": "BRL1-BM",
                                 "claim_id": "CLAIM-1", "quote_sha256": "a" * 64,
                                 "evidence_class": "business_concept",
                                 "subject_scope": SCOPE, "scope_id": "UNIT-A",
                                 "support_scope": "仅支持BRL1-BM",
                                 "findings": {"business_idea_or_model_stated": True}}}
    attempt.update(changes)
    attempt["candidate_digest"] = role_candidate_digest(attempt)
    return attempt


@pytest.mark.parametrize("changes", [
    {"statement": "因此达到 TMRL 9，并给出最终投资决定 YES。"},
    {"evidence_refs": ["NOT-IN-VIEW"]},
    {"review_target": {"nested": {"final_decision": "YES"}}},
])
def test_role_rejects_text_unknown_evidence_and_nested_authority(changes):
    with pytest.raises(ValueError, match="越权|引用|候选"):
        validate_role_attempt(_attempt(**changes), _view())


def test_confirmation_revalidates_full_candidate_and_target():
    view = _view()
    candidate = validate_role_attempt(_attempt(), view)
    target = candidate["review_target"]
    confirmation = {"schema_version": "kth-hybrid.role-confirmation.v2",
                    "confirmation_id": "CONF-1", "candidate_id": candidate["candidate_id"],
                    "candidate_digest": candidate["candidate_digest"],
                    "input_view_id": view["view_id"], "input_digest": view["input_digest"],
                    "producer_id": candidate["producer_id"], "role": candidate["role"],
                    "case_basis_version": view["case_basis_version"],
                    "decision": "does_not_support", "reviewer": "human",
                    "review_basis": "受控复核", "evidence_refs": candidate["evidence_refs"],
                    **target}
    review = confirm_role_candidate(candidate, confirmation, dimension_id="BRL", view=view)
    assert review["decision"] == "does_not_support"
    for field, value in (("input_digest", "other"), ("subject_scope", "Other"),
                         ("scope_id", "OTHER"), ("quote_sha256", "c" * 64)):
        with pytest.raises(ValueError):
            confirm_role_candidate(candidate, {**confirmation, field: value},
                                   dimension_id="BRL", view=view)
    with pytest.raises(ValueError, match="候选|越权"):
        confirm_role_candidate({"candidate_id": "PRO-C1", "final_decision": "YES"},
                               confirmation, dimension_id="BRL", view=view)


def test_validate_dimension_index_rejects_mixed_versions():
    rows = [{"dimension_id": dimension, "scope": SCOPE, "case_basis_version": 1,
             "result_id": f"{dimension}-R", "input_digest": f"{dimension}-D",
             "product_status": "insufficient", "attained_level": 0,
             "trace_ok": True,
             "rule_version": "method-v2" if dimension == "TRL" else "method-v1"}
            for dimension in ("CRL", "BRL", "TRL", "IPRL", "TMRL", "FRL")]
    with pytest.raises(ValueError, match="方法|版本"):
        validate_dimension_index(rows, scope=SCOPE, case_basis_version=1)


@pytest.fixture
def case_copy(tmp_path):
    if not BASE_CASE:
        pytest.skip("未设置KTH_NIGHT_FIX_BASE_CASE")
    source = Path(BASE_CASE)
    root = tmp_path / "case"
    root.mkdir()
    shutil.copy2(source / "records.sqlite3", root / "records.sqlite3")
    shutil.copytree(source / "blobs", root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        basis = case.get_case_basis()
    finally:
        case.close()
    scope = basis["subject_legal_name"]
    unit = {"scope_id": "UNIT-WEIJU-COMPANY-CURRENT-CASE",
            "subject_scope": scope, "unit_kind": "current_case_company_level",
            "unit_label": "微玖当前Case公司级候选单元",
            "scope_id_ref": {"kind": "field_reference",
                             "path": "case:assessment-units.json#/units/0/scope_id"},
            "subject_ref": {"kind": "field_reference",
                            "path": "case:assessment-units.json#/units/0/subject"},
            "unit_kind_ref": {"kind": "field_reference",
                              "path": "case:assessment-units.json#/units/0/kind"},
            "unit_label_ref": {"kind": "field_reference",
                               "path": "case:assessment-units.json#/units/0/label"}}
    entity = {"financing_entity_id": "FIN-WEIJU-COMPANY",
              "subject_scope": scope,
              "assessment_unit_refs": ["UNIT-WEIJU-COMPANY-CURRENT-CASE"],
              "entity_ref": {"kind": "field_reference",
                             "path": "case:frl-scope.json#/entity_id"},
              "subject_ref": {"kind": "field_reference",
                              "path": "case:frl-scope.json#/subject"},
              "assessment_units_ref": {"kind": "field_reference",
                                       "path": "case:frl-scope.json#/units"}}
    runner.run_frl_dimension_slice(
        root, catalog=CATALOG, case_basis=basis, scope=scope,
        financing_entity=entity)
    for function in (runner.run_brl_dimension_slice, runner.run_trl_dimension_slice,
                     runner.run_iprl_dimension_slice, runner.run_tmrl_dimension_slice):
        function(root, catalog=CATALOG, case_basis=basis, scope=scope,
                 assessment_unit=unit)
    return root


def _latest_brl(case):
    return dict(case._conn.execute(
        "SELECT * FROM dimension_results WHERE dimension_id='BRL' "
        "ORDER BY created_at DESC,result_id DESC LIMIT 1").fetchone())


def test_trace_binds_every_database_result_field(case_copy):
    case = CaseStore(case_copy / "records.sqlite3")
    blobs = BlobStore(case_copy / "blobs")
    try:
        row = _latest_brl(case)
        assert trace_dimension_result(case, blobs, row["result_id"])["ok"]
        for field, value in (("scope_id", "FORGED"), ("scope", "Other"),
                             ("product_status", "succeeded"),
                             ("case_basis_version", 999)):
            original = row[field]
            with case._conn:
                case._conn.execute(
                    f"UPDATE dimension_results SET {field}=? WHERE result_id=?",
                    (value, row["result_id"]))
            assert not trace_dimension_result(case, blobs, row["result_id"])["ok"]
            with case._conn:
                case._conn.execute(
                    f"UPDATE dimension_results SET {field}=? WHERE result_id=?",
                    (original, row["result_id"]))
    finally:
        case.close()


def test_trace_recomputes_dimension_and_rejects_unknown_version(case_copy):
    case = CaseStore(case_copy / "records.sqlite3")
    blobs = BlobStore(case_copy / "blobs")
    try:
        row = _latest_brl(case)
        payload = json.loads(blobs.read_bytes(row["result_blob_sha256"]).decode())
        forged = copy.deepcopy(payload)
        forged["dimension"].update(attained_level=9, first_unmet_level=None,
                                   product_status="succeeded")
        for item in forged["dimension"]["criteria"]:
            item.update(native_disposition="met", product_status="succeeded")
        ref = blobs.put_bytes(json.dumps(
            forged, ensure_ascii=False, sort_keys=True).encode())
        with case._conn:
            case._conn.execute(
                "UPDATE dimension_results SET result_blob_sha256=?,product_status=? "
                "WHERE result_id=?", (ref.sha256, "succeeded", row["result_id"]))
        assert not trace_dimension_result(case, blobs, row["result_id"])["ok"]

        unknown = copy.deepcopy(payload)
        unknown["frozen_inputs"]["rule_version"] = "unknown-rule-v99"
        unknown["dimension"]["rule_version"] = "unknown-rule-v99"
        digest = sha256_hex(json.dumps(
            unknown["frozen_inputs"], ensure_ascii=False, sort_keys=True).encode())
        unknown["input_digest"] = digest
        unknown["result_id"] = f"DIMR2::BRL::{digest}"
        broken = validate_dimension_payload(case, blobs, unknown)
        assert any("版本" in item or "方法" in item for item in broken)
    finally:
        case.close()


def test_explicit_manifest_freezes_exact_results_units_and_versions(case_copy):
    case = CaseStore(case_copy / "records.sqlite3")
    blobs = BlobStore(case_copy / "blobs")
    try:
        result_ids = {"CRL": case._conn.execute(
            "SELECT result_id FROM crl_dimension_results ORDER BY created_at DESC,result_id DESC LIMIT 1").fetchone()[0]}
        for dimension in ("BRL", "TRL", "IPRL", "TMRL", "FRL"):
            result_ids[dimension] = case._conn.execute(
                "SELECT result_id FROM dimension_results WHERE dimension_id=? "
                "ORDER BY created_at DESC,result_id DESC LIMIT 1", (dimension,)).fetchone()[0]
        manifest = freeze_aggregation_manifest(case, blobs, result_ids)
        view = build_offline_dimension_view(case, blobs, manifest)
        assert {d: row["result_id"] for d, row in view["dimensions"].items()} == result_ids
        assert all(row.get("rule_version") for row in manifest["dimensions"].values())
        assert manifest["dimensions"]["BRL"]["assessment_scope"]["scope_id"] \
            == manifest["dimensions"]["BRL"]["scope_id"]
        assert manifest["dimensions"]["FRL"]["financing_entity"][
            "assessment_unit_refs"]
        forged = copy.deepcopy(manifest)
        forged["dimensions"]["BRL"]["scope_id"] = "FORGED"
        with pytest.raises(ValueError, match="单元|scope|manifest"):
            build_offline_dimension_view(case, blobs, forged)
        second_scope = {
            "unit": {"scope_id": "UNIT-SECOND", "subject": manifest["scope"],
                     "kind": "material_business_unit", "label": "Second-Unit"}}
        second_ref = blobs.put_bytes(json.dumps(
            second_scope, ensure_ascii=False, sort_keys=True).encode())
        case.add_import_record(
            "case_provenance", "case:assessment-unit-second.json",
            second_ref.sha256, "限定修复manifest版本配对测试")
        basis = case.get_case_basis()
        second_result = runner.run_brl_dimension_slice(
            case_copy, catalog=CATALOG, case_basis=basis,
            scope=manifest["scope"], assessment_unit={
                "scope_id": "UNIT-SECOND", "subject_scope": manifest["scope"],
                "unit_kind": "material_business_unit", "unit_label": "Second-Unit",
                "scope_id_ref": {"kind": "field_reference",
                                 "path": "case:assessment-unit-second.json#/unit/scope_id"},
                "subject_ref": {"kind": "field_reference",
                                "path": "case:assessment-unit-second.json#/unit/subject"},
                "unit_kind_ref": {"kind": "field_reference",
                                  "path": "case:assessment-unit-second.json#/unit/kind"},
                "unit_label_ref": {"kind": "field_reference",
                                   "path": "case:assessment-unit-second.json#/unit/label"}})
        assert second_result["result_id"] != result_ids["BRL"]
        replay = build_offline_dimension_view(case, blobs, manifest)
        assert replay["dimensions"]["BRL"]["result_id"] == result_ids["BRL"]
    finally:
        case.close()
