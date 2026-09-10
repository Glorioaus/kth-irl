"""本地产品闭环：命名 aggregation profile 与历史重放边界。"""

from __future__ import annotations

import copy
import json

import pytest

from kth_hybrid import runner
from kth_hybrid.aggregate import (
    EXPECTED_DIMENSIONS,
    LEGACY_MANIFEST_SCHEMA,
    MANIFEST_SCHEMA,
    build_offline_dimension_view,
    freeze_aggregation_manifest,
    validate_dimension_index,
)
from kth_hybrid.aggregation_profiles import (
    CURRENT_AGGREGATION_PROFILE_ID,
    get_aggregation_profile,
)
from kth_hybrid.contracts import sha256_hex
from kth_hybrid.store import BlobStore, CaseStore
from test_night_brl_runner import SUBJECT, UNIT_ID, _seed, _unit_input


def _manifest_identity(body):
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {**body, "manifest_id": f"AGGMAN::{digest}",
            "manifest_digest": digest}


def _result_ids(case):
    ids = {
        "CRL": case._conn.execute(
            "SELECT result_id FROM crl_dimension_results "
            "ORDER BY created_at DESC,result_id DESC LIMIT 1"
        ).fetchone()[0]
    }
    for dimension in ("BRL", "TRL", "IPRL", "TMRL", "FRL"):
        ids[dimension] = case._conn.execute(
            "SELECT result_id FROM dimension_results WHERE dimension_id=? "
            "ORDER BY created_at DESC,result_id DESC LIMIT 1",
            (dimension,),
        ).fetchone()[0]
    return ids


def _synthetic_index_rows():
    rows = []
    for dimension in sorted(EXPECTED_DIMENSIONS):
        row = {
            "dimension_id": dimension,
            "result_id": f"{dimension}-RESULT",
            "input_digest": f"{dimension}-INPUT",
            "case_basis_version": 1,
            "scope": SUBJECT,
            "scope_id": None,
            "product_status": "insufficient",
            "attained_level": 0,
            "catalog_sha256": "c" * 64,
            "rule_version": "method-v1",
            "result_schema_version": "result-v1",
            "assessment_scope": None,
            "financing_entity": None,
            "trace_ok": True,
        }
        if dimension in {"BRL", "TRL", "IPRL", "TMRL"}:
            row["scope_id"] = UNIT_ID
            row["assessment_scope"] = {
                "scope_id": UNIT_ID,
                "subject_scope": SUBJECT,
            }
        elif dimension == "FRL":
            row["scope_id"] = "FIN-A"
            row["financing_entity"] = {
                "financing_entity_id": "FIN-A",
                "subject_scope": SUBJECT,
                "assessment_unit_refs": [UNIT_ID],
            }
        rows.append(row)
    return rows


@pytest.fixture(scope="module")
def profile_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("aggregation-profile")
    basis, catalog, _revision = _seed(root)
    unit = _unit_input()
    blobs = BlobStore(root / "blobs")
    case = CaseStore(root / "records.sqlite3")
    try:
        scope_blob = blobs.put_bytes(json.dumps({
            "entity_id": "FIN-A",
            "subject": SUBJECT,
            "units": [unit["scope_id"]],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        case.add_import_record(
            "case_provenance", "case:frl-scope.json", scope_blob.sha256)
    finally:
        case.close()
    entity = {
        "financing_entity_id": "FIN-A",
        "subject_scope": SUBJECT,
        "assessment_unit_refs": [unit["scope_id"]],
        "entity_ref": {"kind": "field_reference",
                       "path": "case:frl-scope.json#/entity_id"},
        "subject_ref": {"kind": "field_reference",
                        "path": "case:frl-scope.json#/subject"},
        "assessment_units_ref": {"kind": "field_reference",
                                 "path": "case:frl-scope.json#/units"},
    }
    results = {
        "CRL": runner.run_crl_dimension_slice(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT),
        "FRL": runner.run_frl_dimension_slice(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT,
            financing_entity=entity),
    }
    for dimension, run in (
            ("BRL", runner.run_brl_dimension_slice),
            ("TRL", runner.run_trl_dimension_slice),
            ("IPRL", runner.run_iprl_dimension_slice),
            ("TMRL", runner.run_tmrl_dimension_slice)):
        results[dimension] = run(
            root, catalog=catalog, case_basis=basis, scope=SUBJECT,
            assessment_unit=unit)
    return root, basis, catalog, results


def test_current_profile_is_content_addressed_and_registers_only_approved_combo():
    profile = get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID)
    assert profile["profile_id"] == CURRENT_AGGREGATION_PROFILE_ID
    assert profile["catalog_sha256"] == \
        "2c49050858555ebb063a7b82177ee43e7caf7d2b93e2319998d7d0b047a471fe"
    assert profile["dimensions"] == {
        "CRL": {"rule_version": "kth-hybrid.crl.r2a.v1",
                "result_schema_version": "kth-hybrid.r2a-crl-dimension.v3"},
        "BRL": {"rule_version": "kth-hybrid.brl.night.v3",
                "result_schema_version": "kth-hybrid.dimension-result.v2"},
        "TRL": {"rule_version": "kth-hybrid.trl.night.v4",
                "result_schema_version": "kth-hybrid.dimension-result.v2"},
        "IPRL": {"rule_version": "kth-hybrid.iprl.night.v2",
                 "result_schema_version": "kth-hybrid.dimension-result.v2"},
        "TMRL": {"rule_version": "kth-hybrid.tmrl.night.v3",
                 "result_schema_version": "kth-hybrid.dimension-result.v2"},
        "FRL": {"rule_version": "kth-hybrid.frl.r2b.v1",
                "result_schema_version": "kth-hybrid.dimension-result.v2"},
    }
    body = {key: value for key, value in profile.items()
            if key not in {"profile_id", "profile_digest"}}
    digest = sha256_hex(json.dumps(
        body, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    assert profile["profile_digest"] == digest
    assert profile["profile_id"] == f"AGGPROF::{digest}"


@pytest.mark.parametrize("rows", [
    None,
    42,
    "not-rows",
    {},
    [None],
    [["not-a-row"]],
    [{"dimension_id": []}],
    [{"dimension_id": "UNKNOWN"}],
])
def test_validate_dimension_index_rejects_malformed_rows_with_value_error(rows):
    with pytest.raises(ValueError):
        validate_dimension_index(
            rows, scope=SUBJECT, case_basis_version=1)


@pytest.mark.parametrize("expected_rule_versions", [
    sorted(EXPECTED_DIMENSIONS),
    tuple(sorted(EXPECTED_DIMENSIONS)),
])
def test_validate_dimension_index_rejects_non_dict_expected_versions(
        expected_rule_versions):
    with pytest.raises(ValueError, match="版本|字典|集合"):
        validate_dimension_index(
            _synthetic_index_rows(), scope=SUBJECT, case_basis_version=1,
            expected_rule_versions=expected_rule_versions)


def test_validate_dimension_index_accepts_iterable_rows_and_exact_version_map():
    expected = {dimension: "method-v1" for dimension in EXPECTED_DIMENSIONS}
    indexed = validate_dimension_index(
        (row for row in _synthetic_index_rows()),
        scope=SUBJECT, case_basis_version=1,
        expected_rule_versions=expected)
    assert set(indexed) == EXPECTED_DIMENSIONS


@pytest.mark.parametrize("dimension,field,value,match", [
    ("BRL", "rule_version", [], "rule_version|字符串"),
    ("BRL", "assessment_scope", [], "assessment_scope|字典"),
    ("BRL", "assessment_scope", ["not-a-dict"], "assessment_scope|字典"),
    ("FRL", "financing_entity", [], "financing_entity|字典"),
    ("FRL", "financing_entity", ["not-a-dict"], "financing_entity|字典"),
])
def test_validate_dimension_index_rejects_nested_json_type_pollution(
        dimension, field, value, match):
    rows = _synthetic_index_rows()
    target = next(row for row in rows if row["dimension_id"] == dimension)
    target[field] = value
    with pytest.raises(ValueError, match=match):
        validate_dimension_index(rows, scope=SUBJECT, case_basis_version=1)


@pytest.mark.parametrize("scope_id", [[], {}, "", "   "])
def test_validate_dimension_index_rejects_invalid_shared_scope_id(scope_id):
    rows = _synthetic_index_rows()
    target = next(row for row in rows if row["dimension_id"] == "BRL")
    target["scope_id"] = copy.deepcopy(scope_id)
    target["assessment_scope"]["scope_id"] = copy.deepcopy(scope_id)
    with pytest.raises(ValueError, match="scope_id|字符串"):
        validate_dimension_index(rows, scope=SUBJECT, case_basis_version=1)


@pytest.mark.parametrize("scope_id", [[], {}, "", "   "])
def test_validate_dimension_index_rejects_invalid_frl_scope_id(scope_id):
    rows = _synthetic_index_rows()
    target = next(row for row in rows if row["dimension_id"] == "FRL")
    target["scope_id"] = copy.deepcopy(scope_id)
    target["financing_entity"]["financing_entity_id"] = copy.deepcopy(scope_id)
    with pytest.raises(ValueError, match="scope_id|字符串"):
        validate_dimension_index(rows, scope=SUBJECT, case_basis_version=1)


@pytest.mark.parametrize("scope_id", [[], {}, "", "   "])
def test_validate_dimension_index_requires_null_crl_scope_id(scope_id):
    rows = _synthetic_index_rows()
    target = next(row for row in rows if row["dimension_id"] == "CRL")
    target["scope_id"] = scope_id
    with pytest.raises(ValueError, match="CRL|scope_id|null"):
        validate_dimension_index(rows, scope=SUBJECT, case_basis_version=1)


def test_freeze_requires_registered_profile_id_and_six_exact_result_ids(
        profile_case):
    root, _basis, _catalog, results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    ids = {dimension: result["result_id"]
           for dimension, result in results.items()}
    try:
        manifest = freeze_aggregation_manifest(
            case, blobs, ids, profile_id=CURRENT_AGGREGATION_PROFILE_ID)
        view = build_offline_dimension_view(case, blobs, manifest)
        with pytest.raises(ValueError, match="profile|登记"):
            freeze_aggregation_manifest(
                case, blobs, ids, profile_id="AGGPROF::" + "0" * 64)
        with pytest.raises((TypeError, ValueError), match="profile|字符串|登记"):
            freeze_aggregation_manifest(
                case, blobs, ids,
                profile_id=get_aggregation_profile(CURRENT_AGGREGATION_PROFILE_ID))
        with pytest.raises(ValueError, match="六维|result_id|缺失"):
            freeze_aggregation_manifest(
                case, blobs, {key: value for key, value in ids.items()
                              if key != "FRL"},
                profile_id=CURRENT_AGGREGATION_PROFILE_ID)
    finally:
        case.close()
    assert manifest["schema_version"] == MANIFEST_SCHEMA
    assert manifest["profile_id"] == CURRENT_AGGREGATION_PROFILE_ID
    assert manifest["profile_digest"] == get_aggregation_profile(
        CURRENT_AGGREGATION_PROFILE_ID)["profile_digest"]
    assert {dimension: row["result_id"]
            for dimension, row in manifest["dimensions"].items()} == ids
    assert {dimension: row["result_id"]
            for dimension, row in view["dimensions"].items()} == ids


@pytest.mark.parametrize("field,value,match", [
    ("scope", "Other-Company", "scope"),
    ("case_basis_version", 999, "CaseBasis"),
    ("catalog_sha256", "0" * 64, "catalog"),
    ("rule_version", "unknown-rule-v99", "方法|版本|profile"),
    ("result_schema_version", "unknown-result-v99", "schema|profile"),
])
def test_profile_validation_rejects_mixed_scope_basis_catalog_rule_or_schema(
        profile_case, field, value, match):
    root, _basis, _catalog, _results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        ids = _result_ids(case)
        manifest = freeze_aggregation_manifest(
            case, blobs, ids, profile_id=CURRENT_AGGREGATION_PROFILE_ID)
    finally:
        case.close()
    rows = copy.deepcopy(list(manifest["dimensions"].values()))
    rows[0][field] = value
    with pytest.raises(ValueError, match=match):
        validate_dimension_index(
            rows, scope=manifest["scope"],
            case_basis_version=manifest["case_basis_version"],
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)


@pytest.mark.parametrize("change", [
    {"dimension": "TRL", "field": "scope_id", "value": "UNIT-B"},
    {"dimension": "TRL", "field": "assessment_scope",
     "value": {"scope_id": "UNIT-A", "subject_scope": SUBJECT,
               "unit_kind": "other", "unit_label": "Other"}},
])
def test_profile_rejects_cross_dimension_assessment_unit_drift(
        profile_case, change):
    root, _basis, _catalog, _results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        manifest = freeze_aggregation_manifest(
            case, blobs, _result_ids(case),
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)
    finally:
        case.close()
    rows = copy.deepcopy(manifest["dimensions"])
    rows[change["dimension"]][change["field"]] = change["value"]
    with pytest.raises(ValueError, match="共享|评估单元|scope_id"):
        validate_dimension_index(
            list(rows.values()), scope=manifest["scope"],
            case_basis_version=manifest["case_basis_version"],
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)


@pytest.mark.parametrize("assessment_unit_refs", [
    ["UNIT-B"],
    [UNIT_ID, "UNIT-EXTRA"],
])
def test_profile_requires_frl_to_reference_exactly_the_shared_assessment_unit(
        profile_case, assessment_unit_refs):
    root, _basis, _catalog, _results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        manifest = freeze_aggregation_manifest(
            case, blobs, _result_ids(case),
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)
    finally:
        case.close()
    rows = copy.deepcopy(manifest["dimensions"])
    rows["FRL"]["financing_entity"]["assessment_unit_refs"] = \
        assessment_unit_refs
    with pytest.raises(ValueError, match="FRL|共享|评估单元"):
        validate_dimension_index(
            list(rows.values()), scope=manifest["scope"],
            case_basis_version=manifest["case_basis_version"],
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)


def test_frozen_manifest_ignores_new_latest_result(profile_case):
    root, basis, catalog, _results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        ids = _result_ids(case)
        manifest = freeze_aggregation_manifest(
            case, blobs, ids, profile_id=CURRENT_AGGREGATION_PROFILE_ID)
    finally:
        case.close()
    unit = _unit_input()
    unit["unit_label"] = "Unit-A-New-Identity"
    newer = runner.run_brl_dimension_slice(
        root, catalog=catalog, case_basis=basis, scope=SUBJECT,
        assessment_unit=unit)
    assert newer["result_id"] != ids["BRL"]
    case = CaseStore(root / "records.sqlite3")
    try:
        replay = build_offline_dimension_view(case, blobs, manifest)
    finally:
        case.close()
    assert replay["dimensions"]["BRL"]["result_id"] == ids["BRL"]


def test_v1_manifest_replays_read_only_but_new_freeze_only_emits_v2(profile_case):
    root, _basis, _catalog, results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        current = freeze_aggregation_manifest(
            case, blobs, {dimension: result["result_id"]
                          for dimension, result in results.items()},
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)
        legacy_body = {
            "schema_version": LEGACY_MANIFEST_SCHEMA,
            "case_basis_version": current["case_basis_version"],
            "scope": current["scope"],
            "expected_rule_versions": {
                dimension: row["rule_version"]
                for dimension, row in current["dimensions"].items()
            },
            "dimensions": current["dimensions"],
        }
        legacy = _manifest_identity(legacy_body)
        legacy_view = build_offline_dimension_view(case, blobs, legacy)
    finally:
        case.close()
    assert current["schema_version"] == MANIFEST_SCHEMA
    assert current["schema_version"] != LEGACY_MANIFEST_SCHEMA
    assert "profile_id" not in legacy
    assert {dimension: row["result_id"]
            for dimension, row in legacy_view["dimensions"].items()} == {
                dimension: row["result_id"]
                for dimension, row in current["dimensions"].items()}


@pytest.mark.parametrize("extra_field", [
    {"profile_id": CURRENT_AGGREGATION_PROFILE_ID},
    {"unknown_field": "not-allowed"},
])
def test_v1_manifest_rejects_extra_fields_even_when_resealed(
        profile_case, extra_field):
    root, _basis, _catalog, results = profile_case
    case = CaseStore(root / "records.sqlite3")
    blobs = BlobStore(root / "blobs")
    try:
        current = freeze_aggregation_manifest(
            case, blobs, {dimension: result["result_id"]
                          for dimension, result in results.items()},
            profile_id=CURRENT_AGGREGATION_PROFILE_ID)
        legacy_body = {
            "schema_version": LEGACY_MANIFEST_SCHEMA,
            "case_basis_version": current["case_basis_version"],
            "scope": current["scope"],
            "expected_rule_versions": {
                dimension: row["rule_version"]
                for dimension, row in current["dimensions"].items()
            },
            "dimensions": current["dimensions"],
            **extra_field,
        }
        resealed = _manifest_identity(legacy_body)
        with pytest.raises(ValueError, match="v1|字段|manifest"):
            build_offline_dimension_view(case, blobs, resealed)
    finally:
        case.close()
